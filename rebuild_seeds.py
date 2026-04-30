#!/usr/bin/env python3
"""
rebuild_seeds.py
================
Fetches Nifty 50 PE from 2006 → today.

    cd yield_gap_fixed
    python rebuild_seeds.py

Sources (in order):
  1. NSE indicesHistory API  — daily PE/PB/DivYield, uses curl_cffi Chrome
                               impersonation to bypass NSE's TLS checks.
                               Returns EOD_PE field. Works 2001–today.
  2. NSE daily archive CSV   — per-day fallback using curl_cffi Chrome.
                               Reliable for 2011–today.
  3. Seed CSV                — already covers 2017–2022 from manual downloads.
"""
from __future__ import annotations

import io, json, sys, time, warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

try:
    import pandas as pd
    import requests
except ImportError as e:
    sys.exit(f"pip install pandas requests")

try:
    from curl_cffi import requests as cfr
    _HAS_CURL = True
except ImportError:
    _HAS_CURL = False
    print("⚠️  curl_cffi not installed — NSE API may be blocked.")
    print("   Install: pip install curl_cffi")

ROOT     = Path(__file__).resolve().parent
SEED_DIR = ROOT / "data" / "seed"
LIVE_DIR = ROOT / "data" / "live"
SEED_DIR.mkdir(parents=True, exist_ok=True)
LIVE_DIR.mkdir(parents=True, exist_ok=True)


# ── helpers ───────────────────────────────────────────────────────────────────

def _save(data: dict, path: Path) -> None:
    rows = sorted(data.items())
    pd.DataFrame({
        "date": [str(d)[:10] for d, _ in rows],
        "pe":   [round(v, 2) for _, v in rows],
    }).to_csv(path, index=False)


def _load(path: Path) -> dict[date, float]:
    out: dict[date, float] = {}
    if not path.exists() or path.stat().st_size < 10:
        return out
    try:
        df = pd.read_csv(path, dtype={"date": str})
        for _, row in df.iterrows():
            try:
                d  = pd.to_datetime(row["date"]).date()
                pe = float(row["pe"])
                if 5 < pe < 200:
                    out[d] = pe
            except Exception:
                pass
    except Exception:
        pass
    return out


# ── 1. Bond yield check ───────────────────────────────────────────────────────

def check_bond() -> bool:
    p = SEED_DIR / "india_10y_bond_yield_seed.csv"
    if p.exists() and p.stat().st_size > 1000:
        df = pd.read_csv(p, dtype={"date": str})
        print(f"[1/3] ✅ Bond yield seed: {len(df)} rows  ({df['date'].iloc[0]} → {df['date'].iloc[-1]})")
        return True
    print("[1/3] ❌ Bond yield seed missing")
    return False


# ── 2. NSE indicesHistory API (correct PE endpoint) ──────────────────────────

def _nse_session_cffi():
    """Return a curl_cffi Session pre-loaded with NSE cookies."""
    sess = cfr.Session()
    try:
        sess.get("https://www.nseindia.com", impersonate="chrome", timeout=15)
        time.sleep(1.2)
        sess.get("https://www.nseindia.com/market-data/live-equity-market",
                 impersonate="chrome", timeout=10)
        time.sleep(0.8)
    except Exception as e:
        print(f"   Warning: NSE cookie prefetch: {e}")
    return sess


def _fetch_nse_chunk(sess, from_str: str, to_str: str) -> dict[date, float]:
    """
    One call to NSE indicesHistory API.
    from_str / to_str: 'DD-MM-YYYY'
    Returns {date: pe} from EOD_PE field.
    """
    url = (
        "https://www.nseindia.com/api/historical/indicesHistory"
        f"?indexType=NIFTY%2050&from={from_str}&to={to_str}"
    )
    results: dict[date, float] = {}
    try:
        r = sess.get(url, impersonate="chrome", timeout=20)
        if r.status_code != 200:
            return results
        data = r.json()
        records = data.get("data", {}).get("indexCloseOnlineRecords", []) or []
        for rec in records:
            try:
                d_str  = rec.get("EOD_TIMESTAMP", "")
                pe_raw = rec.get("EOD_PE", "")
                if d_str and pe_raw:
                    d  = pd.to_datetime(str(d_str), dayfirst=True, errors="coerce")
                    pe = float(str(pe_raw).replace(",", ""))
                    if pd.notna(d) and 5 < pe < 200:
                        results[d.date()] = pe
            except Exception:
                pass
    except Exception:
        pass
    return results


def fetch_pe_nse_api(missing: list[date]) -> dict[date, float]:
    if not _HAS_CURL:
        print("   Skipping NSE API (curl_cffi not available)")
        return {}

    print("  [1] NSE indicesHistory API (curl_cffi Chrome) ...")
    sess = _nse_session_cffi()
    new: dict[date, float] = {}

    # Process in 60-day chunks, newest first (NSE more likely to serve recent data)
    dates_sorted = sorted(missing, reverse=True)
    chunk_ends   = []
    i = 0
    while i < len(dates_sorted):
        chunk_end   = dates_sorted[i]
        chunk_start = max(dates_sorted[i:i+60][-1], chunk_end - timedelta(days=60))
        chunk_ends.append((chunk_start, chunk_end))
        # advance past all dates within this chunk
        while i < len(dates_sorted) and dates_sorted[i] >= chunk_start:
            i += 1

    for chunk_start, chunk_end in chunk_ends:
        from_str = chunk_start.strftime("%d-%m-%Y")
        to_str   = chunk_end.strftime("%d-%m-%Y")
        chunk    = _fetch_nse_chunk(sess, from_str, to_str)
        if chunk:
            new.update(chunk)
            print(f"   {from_str} → {to_str}: {len(chunk)} rows ✅")
        else:
            print(f"   {from_str} → {to_str}: 0 rows")
        time.sleep(0.6)

    print(f"   NSE API total: {len(new)} rows")
    return new


# ── 3. NSE daily archive (per-day, curl_cffi) ─────────────────────────────────

def _one_archive(d: date) -> tuple[date, float] | None:
    url = (
        f"https://archives.nseindia.com/content/indices/"
        f"ind_close_all_{d.strftime('%d%m%Y')}.csv"
    )
    hdrs = {"Referer": "https://archives.nseindia.com/"}

    def _parse(text: str) -> float | None:
        if "P/E" not in text:
            return None
        try:
            df  = pd.read_csv(io.StringIO(text))
            row = df[df["Index Name"].astype(str).str.strip() == "Nifty 50"]
            if row.empty:
                return None
            pe = float(str(row["P/E"].iloc[0]).replace(",", ""))
            return pe if 5 < pe < 200 else None
        except Exception:
            return None

    # Try curl_cffi first (Chrome TLS fingerprint — bypasses NSE blocks)
    if _HAS_CURL:
        try:
            r = cfr.get(url, headers=hdrs, impersonate="chrome", timeout=10)
            if r.status_code == 200:
                pe = _parse(r.text)
                if pe is not None:
                    return (d, pe)
        except Exception:
            pass

    # Fall back to plain requests
    try:
        r = requests.get(url, headers=hdrs, timeout=8, verify=False)
        if r.status_code == 200:
            pe = _parse(r.text)
            if pe is not None:
                return (d, pe)
    except Exception:
        pass

    return None


def fetch_pe_archives(missing: list[date]) -> dict[date, float]:
    # Archives reliable from 2011; no point trying older dates
    targets = [d for d in missing if d.year >= 2011]
    if not targets:
        return {}

    print(f"  [2] NSE daily archives ({len(targets)} days, 6 workers) ...")
    new: dict[date, float] = {}

    # Process in chunks of 200 with a small pause between chunks to avoid rate-limits
    chunk_size = 200
    for start_i in range(0, len(targets), chunk_size):
        batch = targets[start_i: start_i + chunk_size]
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_one_archive, d): d for d in batch}
            for f in as_completed(futs):
                res = f.result()
                if res:
                    new[res[0]] = res[1]
        pct = min(start_i + chunk_size, len(targets))
        print(f"   {pct}/{len(targets)} checked, {len(new)} found so far")
        if start_i + chunk_size < len(targets):
            time.sleep(2)  # polite pause between batches

    print(f"   Archives total: {len(new)} rows")
    return new


# ── Main ──────────────────────────────────────────────────────────────────────

def rebuild_pe() -> bool:
    pe_path = LIVE_DIR / "nifty_pe_history.csv"

    # Load existing data (live CSV + seed CSV)
    existing = _load(pe_path)
    seed_path = SEED_DIR / "nifty_pe_seed.csv"
    seed = _load(seed_path)
    # seed is base; live values take priority
    combined = {**seed, **existing}

    if combined:
        dd = sorted(combined)
        print(f"[2/3] Have {len(combined)} PE rows  ({dd[0]} → {dd[-1]})")
    else:
        print("[2/3] No existing PE data")

    # What's missing?
    target_start = date(2006, 1, 1)
    target_end   = date.today() - timedelta(days=1)
    all_weekdays = [
        target_start + timedelta(days=i)
        for i in range((target_end - target_start).days + 1)
        if (target_start + timedelta(days=i)).weekday() < 5
    ]
    missing = sorted(d for d in all_weekdays if d not in combined)
    print(f"  Missing: {len(missing)} trading days")

    if not missing:
        _save(combined, pe_path)
        print("  ✅ Already complete!")
        return True

    import urllib3; urllib3.disable_warnings()

    # Source 1: NSE indicesHistory API via curl_cffi
    new1 = fetch_pe_nse_api(missing)
    combined.update(new1)

    # Source 2: NSE archive per-day for what's still missing (2011+)
    still = sorted(d for d in missing if d not in combined)
    if still:
        new2 = fetch_pe_archives(still)
        combined.update(new2)

    # Save
    _save(combined, pe_path)
    dd = sorted(combined)
    final_missing = [d for d in all_weekdays if d not in combined]
    pct = 100 * (1 - len(final_missing) / len(all_weekdays))
    print(f"\n[3/3] ✅ Saved {len(combined)} rows  ({dd[0]} → {dd[-1]})")
    print(f"      Coverage: {pct:.1f}%  ({len(final_missing)} weekdays still missing)")

    if final_missing:
        gaps_by_year: dict[int, int] = {}
        for d in final_missing:
            gaps_by_year[d.year] = gaps_by_year.get(d.year, 0) + 1
        print("      Gaps by year:", dict(sorted(gaps_by_year.items())))
        if any(y < 2011 for y in gaps_by_year):
            print()
            print("  ℹ️  Pre-2011 gaps: NSE archives don't go that far.")
            print("     Download 2006-2010 PE files from NSE website and drop")
            print("     them in data/seed/ as nifty_pe_seed_2006_2010.csv")
            print("     Format: date,pe  (YYYY-MM-DD, float)")

    return True


if __name__ == "__main__":
    import urllib3; urllib3.disable_warnings()
    print("=" * 60)
    print("Yield Gap Dashboard — PE Data Rebuild")
    print("=" * 60)
    ok1 = check_bond()
    ok2 = rebuild_pe()
    print("=" * 60)
    if ok1 and ok2:
        print("✅ Done!  Run: streamlit run app.py")

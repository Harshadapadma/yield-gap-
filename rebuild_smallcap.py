"""
rebuild_smallcap.py
─────────────────────────────────────────────────────────────────────────────
One-shot multi-source rebuild for Nifty Smallcap 100 (saved to
data/live/indices/IDX_CNXSC.csv).

Tries every source we know about until one returns enough data:

    1. yfinance ^CNXSMALL  (Yahoo's live alias for Nifty Smallcap 100)
    2. yfinance ^CNXSC     (older alias, often empty now)
    3. niftyindices.com    (NSE official; full history back to 2003)
    4. NSE daily archive   (per-day CSV scrape; recent history only)

Run:
    cd <project root>
    python rebuild_smallcap.py
"""

import sys
import time
import warnings
import json
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd

OUT_PATH = ROOT / "data" / "live" / "indices" / "IDX_CNXSC.csv"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

TARGET_START = "2000-01-01"      # request from 2000; Nifty Smallcap 100 base
                                  # date is 2004 so earlier years return empty
TODAY = date.today()
CHUNK_DAYS = 365                  # niftyindices.com chokes on >1yr ranges,
                                  # so fetch in 365-day windows


def _save(s: pd.Series) -> None:
    s = s.dropna().sort_index()
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")]
    df = s.reset_index()
    df.columns = ["date", "close"]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df.to_csv(OUT_PATH, index=False)
    print(f"  ✅ wrote {len(df):,} rows to {OUT_PATH}")


# ─────────────────────────────────────────────────────────────────────────────
# Source 1: yfinance via ^CNXSMALL (Yahoo's current live alias)
# ─────────────────────────────────────────────────────────────────────────────

def src_stooq() -> pd.Series:
    """stooq.com — free, no auth, no rate limits, no captcha.

    Tries several possible symbols since stooq's Indian-index naming isn't
    well documented.  Whichever returns a non-empty CSV wins.
    """
    print("\n[A] stooq.com (multiple symbol guesses)…")
    try:
        import requests
    except ImportError:
        print("  failed: requests not installed"); return pd.Series(dtype=float)

    candidates = [
        "^cnxsmall",      # most likely
        "^cnxsc",
        "^nsmcap",
        "^cnx_small",
        "^nsmcap100",
        "cnxsmall.in",
        "^cnxsmall.in",
    ]
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept":     "text/csv, text/plain, */*",
    }
    for sym in candidates:
        url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 200 and len(r.text) > 200 and "Date" in r.text[:50]:
                # Parse CSV: Date,Open,High,Low,Close,Volume
                import io
                df = pd.read_csv(io.StringIO(r.text))
                if "Date" in df.columns and "Close" in df.columns and len(df) > 100:
                    df["Date"] = pd.to_datetime(df["Date"])
                    s = df.set_index("Date")["Close"].dropna().sort_index()
                    print(f"  ✅ {sym}: {len(s):,} rows ({s.index[0].date()} → {s.index[-1].date()})")
                    return s
                else:
                    print(f"  {sym}: parsed but too short ({len(df)} rows)")
            else:
                print(f"  {sym}: HTTP {r.status_code}, body[:60]={r.text[:60]!r}")
        except Exception as exc:
            print(f"  {sym}: {type(exc).__name__}: {exc}")
    return pd.Series(dtype=float)


def src_yfinance_cnxsmall() -> pd.Series:
    print("\n[1/4] yfinance ^CNXSMALL (explicit start)…")
    try:
        import yfinance as yf
        # ^CNXSMALL doesn't accept period='max'.  Use explicit start date.
        h = yf.Ticker("^CNXSMALL").history(
            start=TARGET_START,
            end=str(TODAY + timedelta(days=1)),
            auto_adjust=True,
        )
        if h is not None and not h.empty and "Close" in h.columns:
            s = h["Close"].dropna()
            print(f"  got {len(s):,} rows ({s.index[0].date()} → {s.index[-1].date()})")
            return s
    except Exception as exc:
        print(f"  failed: {type(exc).__name__}: {exc}")
    return pd.Series(dtype=float)


# ─────────────────────────────────────────────────────────────────────────────
# Source 2: yfinance via ^CNXSC (older alias)
# ─────────────────────────────────────────────────────────────────────────────

def src_yfinance_cnxsc() -> pd.Series:
    """yfinance ^CNXSC fetched in YEAR-BY-YEAR chunks.

    Single call with start="2000-01-01" returns only 1 row because yfinance's
    internal period= fallback rejects huge ranges for some Indian-index tickers.
    Year-by-year chunking sidesteps that and gets the full available history.
    """
    print("\n[2/4] yfinance ^CNXSC (year-by-year chunks)…")
    try:
        import yfinance as yf
    except Exception as exc:
        print(f"  failed: {exc}"); return pd.Series(dtype=float)

    chunks = []
    start_year = int(TARGET_START.split("-")[0])
    end_year   = TODAY.year
    for year in range(start_year, end_year + 1):
        chunk_start = f"{year}-01-01"
        chunk_end   = f"{year}-12-31" if year < end_year else str(TODAY + timedelta(days=1))
        try:
            h = yf.Ticker("^CNXSC").history(
                start=chunk_start, end=chunk_end, auto_adjust=True,
            )
            if h is not None and not h.empty and "Close" in h.columns:
                s = h["Close"].dropna()
                if not s.empty:
                    chunks.append(s)
                    print(f"  {year}: {len(s):,} rows")
        except Exception as exc:
            pass
        time.sleep(0.05)

    if not chunks:
        print("  no chunks returned data")
        return pd.Series(dtype=float)
    combined = pd.concat(chunks).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    print(f"  total: {len(combined):,} rows ({combined.index[0].date()} → {combined.index[-1].date()})")
    return combined


def src_nse_hist_api() -> pd.Series:
    """NSE's /api/historical/indices endpoint — different from daily archives.

    URL: https://www.nseindia.com/api/historical/indices?indexType=NIFTY%20SMALLCAP%20100&from=DD-MM-YYYY&to=DD-MM-YYYY

    Returns CH_CLOSING_VALUE in actual index points.  90-day chunks with
    cookies primed.  Often works when daily archives + niftyindices fail.
    """
    print("\n[2b/4] NSE hist API /api/historical/indices…")
    try:
        import requests, urllib.parse
    except ImportError:
        print("  failed: requests not installed"); return pd.Series(dtype=float)

    sess = requests.Session()
    sess.headers.update({
        "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":      "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":     "https://www.nseindia.com/reports-indices-historical-index-data",
    })
    try:
        sess.get("https://www.nseindia.com", timeout=10)
        time.sleep(0.6)
        sess.get("https://www.nseindia.com/reports-indices-historical-index-data", timeout=10)
        time.sleep(0.6)
    except Exception as exc:
        print(f"  cookie prime failed: {exc}")

    idx_enc = urllib.parse.quote("NIFTY SMALLCAP 100")
    out = {}
    chunk_start = pd.Timestamp(TARGET_START).date()
    end_dt = TODAY
    chunk_count = 0
    while chunk_start <= end_dt:
        chunk_end = min(chunk_start + timedelta(days=89), end_dt)
        from_str = chunk_start.strftime("%d-%m-%Y")
        to_str   = chunk_end.strftime("%d-%m-%Y")
        url = (f"https://www.nseindia.com/api/historical/indices"
               f"?indexType={idx_enc}&from={from_str}&to={to_str}")
        try:
            r = sess.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                rows = data.get("data", [])
                for row in rows:
                    try:
                        dt_raw = (row.get("CH_TIMESTAMP") or row.get("TIMESTAMP")
                                  or row.get("HistoricalDate") or row.get("date", ""))
                        v_raw  = (row.get("CH_CLOSING_VALUE") or row.get("CLOSING_INDEX_VAL")
                                  or row.get("close") or row.get("Close", ""))
                        ts  = pd.Timestamp(str(dt_raw).strip()).normalize()
                        val = float(str(v_raw).replace(",", ""))
                        if val > 100:
                            out[ts] = val
                    except Exception:
                        pass
                if rows:
                    if chunk_count % 4 == 0:
                        print(f"  {chunk_start} → {chunk_end}: {len(rows)} rows  (running total: {len(out):,})")
            else:
                if chunk_count % 8 == 0:
                    print(f"  {chunk_start}: HTTP {r.status_code}")
        except Exception as exc:
            if chunk_count % 8 == 0:
                print(f"  {chunk_start}: {type(exc).__name__}")
        chunk_start = chunk_end + timedelta(days=1)
        chunk_count += 1
        # Refresh cookies every 20 chunks to avoid expiry
        if chunk_count % 20 == 0:
            try:
                sess.get("https://www.nseindia.com", timeout=10)
                time.sleep(0.5)
            except Exception:
                pass
        time.sleep(0.3)

    if not out:
        return pd.Series(dtype=float)
    s = pd.Series(out).sort_index()
    print(f"  total: {len(s):,} rows  ({s.index[0].date()} → {s.index[-1].date()})")
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Source 3: niftyindices.com getHistoricalData (NSE official)
# ─────────────────────────────────────────────────────────────────────────────

def src_niftyindices() -> pd.Series:
    print("\n[3/4] niftyindices.com 'NIFTY SMALLCAP 100' (full history)…")
    try:
        import requests
    except ImportError:
        print("  failed: requests not installed (`pip install requests`)")
        return pd.Series(dtype=float)

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
        "Accept":           "application/json, text/javascript, */*; q=0.01",
        "Accept-Language":  "en-US,en;q=0.9",
        "Content-Type":     "application/json; charset=UTF-8",
        "Referer":          "https://www.niftyindices.com/reports/historical-data",
        "Origin":           "https://www.niftyindices.com",
        "X-Requested-With": "XMLHttpRequest",
    })

    # ── Prime cookies by visiting the homepage and the historical-data page.
    # niftyindices.com requires session cookies + an ASP.NET _VIEWSTATE which
    # only get set after this round-trip — without it every API call returns
    # HTTP 500.
    print("  priming session cookies…")
    try:
        sess.get("https://www.niftyindices.com/", timeout=15)
        time.sleep(0.5)
        sess.get("https://www.niftyindices.com/reports/historical-data", timeout=15)
        time.sleep(0.5)
    except Exception as exc:
        print(f"  cookie prime failed: {exc}")

    all_records = []
    chunk_start = pd.Timestamp(TARGET_START)
    end_ts      = pd.Timestamp(TODAY)
    chunks_done = 0
    chunks_with_data = 0
    while chunk_start <= end_ts:
        # 365-day windows — niftyindices.com refuses larger ranges
        chunk_end = min(chunk_start + pd.Timedelta(days=CHUNK_DAYS - 1), end_ts)
        payload = {
            "name":      "NIFTY SMALLCAP 100",
            "startDate": chunk_start.strftime("%d-%b-%Y"),
            "endDate":   chunk_end.strftime("%d-%b-%Y"),
        }
        try:
            r = sess.post(
                "https://www.niftyindices.com/Backpage.aspx/getHistoricalData",
                json=payload, timeout=20,
            )
            if r.status_code == 200:
                outer = r.json()
                raw_str = outer.get("d", "[]")
                rows = json.loads(raw_str) if isinstance(raw_str, str) else raw_str
                if isinstance(rows, list):
                    all_records.extend(rows)
                    if rows:
                        chunks_with_data += 1
                    # Print every 5 chunks to keep output compact
                    if chunks_done % 5 == 0 or rows:
                        print(f"  chunk {chunk_start.date()} → {chunk_end.date()}: "
                              f"{len(rows)} rows")
            else:
                print(f"  chunk {chunk_start.date()}: HTTP {r.status_code}")
        except Exception as exc:
            print(f"  chunk {chunk_start.date()}: {type(exc).__name__}: {exc}")
        chunk_start = chunk_end + pd.Timedelta(days=1)
        chunks_done += 1
        time.sleep(0.3)
    print(f"  total chunks: {chunks_done}, chunks with data: {chunks_with_data}, "
          f"total records: {len(all_records):,}")

    if not all_records:
        return pd.Series(dtype=float)

    out = {}
    for row in all_records:
        try:
            dt_raw  = row.get("TIMESTAMP") or row.get("HistoricalDate") or row.get("date", "")
            val_raw = (row.get("CLOSING_INDEX_VAL") or row.get("Close")
                       or row.get("close") or row.get("closingIndexVal", ""))
            ts  = pd.Timestamp(str(dt_raw).strip()).normalize()
            val = float(str(val_raw).replace(",", ""))
            if val > 0:
                out[ts] = val
        except Exception:
            pass

    if not out:
        return pd.Series(dtype=float)
    s = pd.Series(out).sort_index()
    print(f"  parsed {len(s):,} valid rows ({s.index[0].date()} → {s.index[-1].date()})")
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Source 4: NSE daily archive (per-day CSV scrape)
# ─────────────────────────────────────────────────────────────────────────────

def src_nse_archives(start_dt: date, end_dt: date) -> pd.Series:
    """Scrape NSE daily archive CSVs (one HTTP request per trading day).

    Slow for long ranges (~5500 requests for 22 years) but reliable: NSE
    archives are public, no auth, no captcha.  We cap with parallel workers
    to keep wall-time reasonable.
    """
    print(f"\n[4/4] NSE daily archive {start_dt} → {end_dt}…")
    try:
        import requests
        import concurrent.futures
    except ImportError:
        print("  failed: requests / concurrent not installed")
        return pd.Series(dtype=float)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer":    "https://archives.nseindia.com/",
    }

    # Build list of weekdays in range
    weekdays = []
    current = start_dt
    while current <= end_dt:
        if current.weekday() < 5:
            weekdays.append(current)
        current += timedelta(days=1)

    total = len(weekdays)
    print(f"  scraping {total:,} weekdays in parallel (this takes a while)…")

    out: dict = {}
    sess = requests.Session()
    sess.headers.update(headers)

    # Try curl_cffi for browser TLS-fingerprint impersonation — often bypasses
    # Cloudflare/firewall 403s that block plain `requests`.
    # IMPORTANT: do NOT use curl_cffi.Session() at all — it has thread-safety
    # issues that cause concurrent requests to silently return garbage.
    # Use the module-level cfr.get() instead, which creates a fresh handle
    # per call.  Slightly slower per request but bulletproof for parallelism.
    use_cffi = False
    try:
        from curl_cffi import requests as cfr
        # Single warm-up request to confirm the library works
        _test = cfr.get("https://archives.nseindia.com/",
                        impersonate="chrome120", timeout=10)
        if _test.status_code in (200, 403, 404):   # any response = library works
            use_cffi = True
            print(f"  using curl_cffi (one-shot) — chrome120 — warmup status {_test.status_code}")
    except Exception as exc:
        print(f"  curl_cffi unavailable ({exc}) — using plain requests")

    def _fetch_day(d: date, debug: bool = False):
        url = (f"https://archives.nseindia.com/content/indices/"
               f"ind_close_all_{d.strftime('%d%m%Y')}.csv")
        for attempt in range(3):
            try:
                if use_cffi:
                    # Module-level call — no session = no thread-safety issues
                    r = cfr.get(url, impersonate="chrome120", timeout=15,
                                headers={"Referer": "https://archives.nseindia.com/"})
                else:
                    r = sess.get(url, timeout=15)
                if r.status_code == 200 and len(r.text) > 200:
                    import io, csv
                    reader = csv.DictReader(io.StringIO(r.text))
                    for row in reader:
                        name_col  = row.get("Index Name") or row.get("IndexName") or row.get("Name") or ""
                        close_col = (row.get("Closing Index Value")
                                     or row.get("ClosingIndexValue")
                                     or row.get("Close") or "")
                        if name_col.strip().upper() == "NIFTY SMALLCAP 100" and close_col:
                            try:
                                val = float(str(close_col).replace(",", ""))
                                if val > 100:
                                    return d, val
                            except ValueError:
                                pass
                    if debug:
                        rows = list(csv.DictReader(io.StringIO(r.text)))
                        names = [r.get("Index Name", "") for r in rows[:10]]
                        print(f"  DEBUG {d}: csv had {len(rows)} rows, sample names: {names[:5]}")
                    return d, None
                elif r.status_code in (429, 503):
                    time.sleep(2.0 * (attempt + 1))
                    continue
                elif r.status_code == 403:
                    if debug:
                        print(f"  DEBUG {d}: HTTP 403 — IP-blocked.  Use VPN or wait 24h.")
                    return d, None
                elif r.status_code == 404:
                    return d, None
                else:
                    if debug:
                        print(f"  DEBUG {d}: HTTP {r.status_code}, body[:80]={r.text[:80]!r}")
                    return d, None
            except Exception as exc:
                if debug:
                    print(f"  DEBUG {d}: {type(exc).__name__}: {exc}")
                time.sleep(1.0)
                continue
        return d, None

    # Probe one recent weekday — if 403 (IP-blocked) on probe, ABORT.
    print("  probing one recent date for diagnostic…")
    probe_blocked = False
    probe_value   = None
    for probe_back in range(1, 8):
        probe_d = TODAY - timedelta(days=probe_back)
        if probe_d.weekday() < 5:
            url = (f"https://archives.nseindia.com/content/indices/"
                   f"ind_close_all_{probe_d.strftime('%d%m%Y')}.csv")
            try:
                if use_cffi:
                    rp = cfr.get(url, impersonate="chrome120", timeout=15,
                                 headers={"Referer": "https://archives.nseindia.com/"})
                else:
                    rp = sess.get(url, timeout=15)
                if rp.status_code == 403:
                    probe_blocked = True
                    print(f"  probe {probe_d}: HTTP 403 Access Denied")
                    break
                _, v = _fetch_day(probe_d, debug=True)
                if v is not None:
                    probe_value = v
                    print(f"  probe {probe_d}: value={v}")
                    break
            except Exception as exc:
                print(f"  probe {probe_d}: {type(exc).__name__}: {exc}")
                continue

    if probe_blocked:
        print()
        print("  ❌ Your IP is BLOCKED by NSE (HTTP 403 Access Denied).")
        print("     The block was likely triggered by earlier parallel requests.")
        print("     NSE typically lifts these blocks in 24–72 hours.")
        print()
        print("  Options:")
        print("    1. Wait 24–72 hours, retry")
        print("    2. Switch IP via VPN / phone hotspot, retry")
        print("    3. Run script from a different machine / network")
        print()
        print("  Aborting scrape — no point hitting NSE 5800 more times.")
        return pd.Series(dtype=float)

    if probe_value is None:
        print("  ❌ Probe failed for all attempted dates.  Aborting scrape.")
        return pd.Series(dtype=float)

    done = 0
    # 3 workers: fast enough overall but gentle enough to avoid retriggering
    # the IP block.  Each request is a fresh curl_cffi handle (no session
    # sharing) so thread safety is no longer a concern.
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        futs = [pool.submit(_fetch_day, d) for d in weekdays]
        for fut in concurrent.futures.as_completed(futs):
            d, val = fut.result()
            if val is not None:
                out[pd.Timestamp(d)] = val
            done += 1
            if done % 100 == 0 or done == total:
                pct = done / total * 100
                hit_pct = (len(out) / done * 100) if done else 0
                print(f"    {done:,}/{total:,} ({pct:5.1f}%)  hits: {len(out):,} ({hit_pct:.0f}%)")
                # Safety brake: if we've crawled 250 dates and got NO hits,
                # something is structurally wrong (IP blocked again, name
                # mismatch, etc.).  Abort instead of grinding through 5800.
                if done >= 250 and len(out) == 0:
                    print(f"\n    ❌ 250 requests, 0 hits — aborting.")
                    print(f"    Either IP got re-blocked or the index name changed.")
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

    if not out:
        return pd.Series(dtype=float)
    s = pd.Series(out).sort_index()
    print(f"  scraped {len(s):,} daily archive entries "
          f"({s.index[0].date()} → {s.index[-1].date()})")
    return s


# ─────────────────────────────────────────────────────────────────────────────
# Main: try each source, accept the first one with a healthy series
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 64)
    print("  Rebuild Nifty Smallcap 100 (IDX_CNXSC.csv)")
    print()
    print("  Source priority:")
    print("    1. NSE daily archives (only working source as of May 2026)")
    print("    2. yfinance ^CNXSC year-by-year  (fallback for tail)")
    print("    3. NSE hist API                  (often 503 — fallback)")
    print("    4. niftyindices.com              (often 500 — fallback)")
    print()
    print(f"  Target start: {TARGET_START}  (Smallcap 100 base date is 2004-04-01;")
    print(f"  earlier years return empty.  Full scrape ≈ 5–10 min.)")
    print("=" * 64)

    candidates: list[tuple[str, pd.Series]] = []

    # ── FIRST: try stooq.com (instant, no rate limit). If it has Smallcap,
    # we're done in 5 seconds and can skip the slow NSE archive crawl entirely.
    s_stooq = src_stooq()
    if len(s_stooq) > 1000:
        candidates.append(("stooq.com", s_stooq))

    # ── SECOND: NSE daily archive scrape (proven working, slow ~5–10 min).
    # Only run if stooq didn't give us long history.
    if not candidates or max(len(c[1]) for c in candidates) < 1000:
        archive_start = max(date(2004, 4, 1), date.fromisoformat(TARGET_START))
        s4 = src_nse_archives(archive_start, TODAY)
        if len(s4) > 30:
            candidates.append(("NSE daily archives", s4))

    # ── FALLBACKS: only if neither of the above gave long history
    if not candidates or max(len(c[1]) for c in candidates) < 1000:
        s2 = src_yfinance_cnxsc()
        if len(s2) > 0:
            candidates.append(("yfinance ^CNXSC chunked", s2))

    if not candidates or max(len(c[1]) for c in candidates) < 1000:
        s_nse_api = src_nse_hist_api()
        if len(s_nse_api) > 100:
            candidates.append(("NSE hist API", s_nse_api))

    if not candidates or max(len(c[1]) for c in candidates) < 1000:
        s3 = src_niftyindices()
        if len(s3) > 100:
            candidates.append(("niftyindices.com", s3))

    if not candidates:
        print("\n❌ ALL SOURCES FAILED. Likely network issue or NSE/Yahoo block.")
        print("   Retry in 10 min, or check internet connectivity.")
        return 1

    # ── Build the merged series: start with the longest-history candidate,
    # then splice in any newer rows from other candidates.  This gives us
    # full history (from niftyindices) + the freshest possible tail (from
    # yfinance, which usually publishes today's close earlier).
    candidates.sort(key=lambda c: len(c[1]), reverse=True)
    name, best = candidates[0]
    print(f"\n✅ Base source: {name}  ({len(best):,} rows, "
          f"{best.index[0].date()} → {best.index[-1].date()})")

    for src_name, src_series in candidates[1:]:
        if src_series.empty:
            continue
        new_rows = src_series[src_series.index > best.index[-1]]
        if not new_rows.empty:
            print(f"  + splicing {len(new_rows):,} newer rows from {src_name}")
            best = pd.concat([best, new_rows]).sort_index()
            best = best[~best.index.duplicated(keep="last")]

    print(f"\nFinal merged series: {len(best):,} rows  "
          f"{best.index[0].date()} → {best.index[-1].date()}")

    _save(best)
    print()
    print("Now reload the Return Spread page in the browser. Smallcap should work.")
    print("(No Streamlit restart needed — the file is read fresh each query.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

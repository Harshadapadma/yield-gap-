"""
fill_gap_2016_2018.py  —  Fill 2016-2018 data gap in Midcap 100 and Smallcap 100.

Uses NSE historical indices API (indicesHistory endpoint) with proper session
management and monthly chunking to get correct PRICE RETURN data.

Run from project root:
    python fill_gap_2016_2018.py

This ONLY fills the known gap (Apr 2016 → Apr 2018).
It does NOT overwrite existing data — only adds missing dates.
"""

import io, sys, time, random, warnings
warnings.filterwarnings("ignore")
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import requests

try:
    from curl_cffi import requests as cf_requests
    _HAS_CF = True
except ImportError:
    _HAS_CF = False
    print("⚠  curl_cffi not found — using plain requests (lower success rate)")

IDX_DIR = ROOT / "data" / "live" / "indices"

HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/live-equity-market",
    "X-Requested-With": "XMLHttpRequest",
}

# Canonical NSE name → CSV filename ticker
TARGETS = {
    "Nifty Midcap 100":   "IDX_NSMIDCP",
    "Nifty Smallcap 100": "IDX_CNXSC",
}

# NSE indicesHistory API name mapping
NSE_API_NAMES = {
    "Nifty Midcap 100":   "NIFTY MIDCAP 100",
    "Nifty Smallcap 100": "NIFTY SMALLCAP 100",
}

GAP_START = date(2016, 4, 1)
GAP_END   = date(2018, 3, 31)


# ── Helpers ───────────────────────────────────────────────────────────────────

def csv_path(ticker: str) -> Path:
    return IDX_DIR / f"{ticker}.csv"


def load_series(ticker: str) -> pd.Series:
    p = csv_path(ticker)
    if not p.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(p, parse_dates=["date"])
    s = df.dropna(subset=["date","close"]).set_index("date")["close"].sort_index()
    s.index = pd.to_datetime(s.index).normalize()
    return s[~s.index.duplicated(keep="last")]


def save_series(s: pd.Series, ticker: str):
    if s.empty:
        return
    df = s.reset_index()
    df.columns = ["date", "close"]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    df.to_csv(csv_path(ticker), index=False)
    print(f"    💾 Saved {len(df)} rows to {csv_path(ticker).name}")


def merge_series(base: pd.Series, new: pd.Series) -> pd.Series:
    """Merge two series — new data does NOT overwrite existing."""
    if base.empty:
        return new
    if new.empty:
        return base
    # Only add dates not already in base
    to_add = new[~new.index.isin(base.index)]
    if to_add.empty:
        return base
    combined = pd.concat([base, to_add])
    return combined[~combined.index.duplicated(keep="last")].sort_index()


def month_chunks(start: date, end: date):
    """Yield (chunk_start, chunk_end) pairs — one per calendar month."""
    cur = start
    while cur <= end:
        # End of this month
        if cur.month == 12:
            month_end = date(cur.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        chunk_end = min(month_end, end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


# ── NSE Website API ───────────────────────────────────────────────────────────

class NSESession:
    """Manage NSE website session with cookies."""

    def __init__(self):
        if _HAS_CF:
            self._sess = cf_requests.Session()
        else:
            self._sess = requests.Session()
            self._sess.headers.update(HDRS)
        self._ready = False

    def setup(self) -> bool:
        """Visit NSE home page to acquire cookies."""
        print("  Setting up NSE session (visiting home page) ...")
        try:
            if _HAS_CF:
                r = self._sess.get(
                    "https://www.nseindia.com",
                    headers=HDRS,
                    impersonate="chrome",
                    timeout=20,
                )
            else:
                r = self._sess.get("https://www.nseindia.com", timeout=20)
            if r.status_code == 200:
                print(f"    ✅ Session established (cookies: {len(self._sess.cookies)} set)")
                self._ready = True
                time.sleep(2)
                # Also visit the market data page for additional cookies
                try:
                    if _HAS_CF:
                        self._sess.get(
                            "https://www.nseindia.com/market-data/live-equity-market",
                            headers=HDRS,
                            impersonate="chrome",
                            timeout=15,
                        )
                    else:
                        self._sess.get(
                            "https://www.nseindia.com/market-data/live-equity-market",
                            timeout=15,
                        )
                    time.sleep(1)
                except Exception:
                    pass
                return True
            else:
                print(f"    ❌ Home page returned {r.status_code}")
                return False
        except Exception as e:
            print(f"    ❌ Session setup failed: {e}")
            return False

    def get_indices_history(
        self,
        index_name: str,
        from_date: date,
        to_date: date,
    ) -> pd.Series:
        """
        Fetch historical index data via NSE indicesHistory API.
        Returns empty Series on failure.
        """
        if not self._ready:
            return pd.Series(dtype=float)

        url = (
            "https://www.nseindia.com/api/historical/indicesHistory"
            f"?indexType={index_name.replace(' ', '%20')}"
            f"&from={from_date.strftime('%d-%m-%Y')}"
            f"&to={to_date.strftime('%d-%m-%Y')}"
        )

        for attempt in range(3):
            try:
                if _HAS_CF:
                    r = self._sess.get(
                        url,
                        headers={**HDRS, "Referer": "https://www.nseindia.com/"},
                        impersonate="chrome",
                        timeout=20,
                    )
                else:
                    r = self._sess.get(url, timeout=20)

                if r.status_code == 200:
                    data = r.json()
                    # Response structure: {"data": {"indexCloseOnlineRecords": [...], ...}}
                    records = []
                    if isinstance(data, dict):
                        inner = data.get("data", {})
                        if isinstance(inner, dict):
                            records = inner.get("indexCloseOnlineRecords", [])
                        elif isinstance(inner, list):
                            records = inner

                    if not records:
                        # Sometimes the API returns "data" as a list directly
                        if isinstance(data.get("data"), list):
                            records = data["data"]

                    if records:
                        result = {}
                        for rec in records:
                            try:
                                # Try different field names
                                ts_str = (
                                    rec.get("EOD_TIMESTAMP")
                                    or rec.get("eodTimestamp")
                                    or rec.get("TIMESTAMP")
                                    or rec.get("date")
                                    or ""
                                )
                                val = (
                                    rec.get("EOD_CLOSE_INDEX_VAL")
                                    or rec.get("eodCloseIndexVal")
                                    or rec.get("CLOSE_INDEX_VAL")
                                    or rec.get("close")
                                    or 0
                                )
                                if ts_str and float(val) > 0:
                                    ts = pd.Timestamp(str(ts_str).strip()).normalize()
                                    result[ts] = float(str(val).replace(",", ""))
                            except Exception:
                                pass
                        if result:
                            return pd.Series(result).sort_index()

                    # Empty records — might be a session issue, re-establish
                    if r.status_code == 200 and not records:
                        if attempt == 0:
                            print(f"      Empty response for {from_date}→{to_date}, refreshing session ...")
                            self.setup()
                            time.sleep(3)
                        continue

                elif r.status_code in (401, 403):
                    print(f"      Auth error {r.status_code}, re-establishing session ...")
                    self.setup()
                    time.sleep(5)

                elif r.status_code == 429:
                    wait = 30 * (attempt + 1)
                    print(f"      Rate limited (429), waiting {wait}s ...")
                    time.sleep(wait)

                else:
                    print(f"      HTTP {r.status_code} for {from_date}→{to_date}")

            except Exception as e:
                print(f"      Error attempt {attempt+1}: {e}")
                time.sleep(5)

        return pd.Series(dtype=float)


# ── NSE Archives fallback ─────────────────────────────────────────────────────

def fetch_archive_day(d: date) -> dict:
    """Fetch one day from NSE archives. Returns {} on failure."""
    url = (
        f"https://archives.nseindia.com/content/indices/"
        f"ind_close_all_{d.strftime('%d%m%Y')}.csv"
    )
    hdrs = {
        "User-Agent": HDRS["User-Agent"],
        "Referer": "https://archives.nseindia.com/",
    }

    for attempt in range(2):
        try:
            if _HAS_CF and attempt == 0:
                sess = cf_requests.Session()
                r = sess.get(url, impersonate="chrome", headers=hdrs, timeout=15)
            else:
                r = requests.get(url, headers=hdrs, timeout=12)

            if r.status_code == 200 and "Index" in r.text:
                df = pd.read_csv(io.StringIO(r.text))
                df.columns = [c.strip() for c in df.columns]
                close_col = next(
                    (c for c in ["Closing Index Value", "ClosingIndexValue"] if c in df.columns),
                    None,
                )
                name_col = next(
                    (c for c in ["Index Name", "IndexName"] if c in df.columns),
                    None,
                )
                if close_col and name_col:
                    result = {}
                    for _, row in df.iterrows():
                        nm = str(row[name_col]).strip()
                        if nm in ("Nifty Midcap 100", "Nifty Smallcap 100",
                                  "CNX Midcap", "S&P CNX Midcap",
                                  "CNX Smallcap", "S&P CNX Smallcap 100"):
                            canonical = (
                                "Nifty Midcap 100"
                                if "midcap" in nm.lower()
                                else "Nifty Smallcap 100"
                            )
                            try:
                                v = float(str(row[close_col]).replace(",", ""))
                                if v > 0:
                                    result[canonical] = v
                            except Exception:
                                pass
                    if result:
                        return result
        except Exception:
            if attempt == 0:
                time.sleep(1)

    return {}


def fill_from_archives(
    missing_dates: list[date],
    name: str,
    delay: float = 3.0,
    max_per_batch: int = 50,
) -> pd.Series:
    """
    Fetch specific dates from NSE archives with slow pacing.
    Uses larger delays to avoid rate limiting.
    """
    print(f"\n  📦 Archive fallback for {name}: {len(missing_dates)} dates, delay={delay}s")
    results = {}
    ok = 0

    # Randomize order slightly to avoid detection patterns
    batch = list(missing_dates)
    random.shuffle(batch)

    for i, d in enumerate(batch, 1):
        day_data = fetch_archive_day(d)
        if name in day_data:
            results[pd.Timestamp(d)] = day_data[name]
            ok += 1

        if i % 20 == 0:
            pct = 100 * ok // i
            print(f"    {i}/{len(batch)} ({pct}% ok) ...")

        # Slow pacing with jitter to avoid rate limiting
        jitter = random.uniform(0, delay * 0.5)
        time.sleep(delay + jitter)

    print(f"  Archive: got {ok}/{len(missing_dates)} days ({100*ok//max(len(missing_dates),1)}%)")
    return pd.Series(results).sort_index() if results else pd.Series(dtype=float)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("=" * 65)
    print("  Gap Fill: Nifty Midcap 100 & Smallcap 100 — 2016-04 to 2018-03")
    print("=" * 65)

    # ── Step 0: Check what's already filled ────────────────────────────────
    print("\n[0] Checking existing data ...")
    gap_start_ts = pd.Timestamp(GAP_START)
    gap_end_ts   = pd.Timestamp(GAP_END)

    status = {}
    for name, ticker in TARGETS.items():
        s = load_series(ticker)
        in_gap = s[(s.index >= gap_start_ts) & (s.index <= gap_end_ts)]
        all_gap_days = len(pd.bdate_range(gap_start_ts, gap_end_ts))
        status[name] = {
            "series":   s,
            "ticker":   ticker,
            "in_gap":   in_gap,
            "missing":  all_gap_days - len(in_gap),
            "total_gap_days": all_gap_days,
        }
        print(f"  {name}: {len(in_gap)}/{all_gap_days} gap days filled "
              f"({100*len(in_gap)//all_gap_days}%)")

    all_filled = all(v["missing"] == 0 for v in status.values())
    if all_filled:
        print("\n✅ Both indices already have complete 2016-2018 data!")
        return

    # ── Step 1: NSE indicesHistory API (monthly chunks) ────────────────────
    print("\n[1] Trying NSE indicesHistory API (monthly chunks) ...")
    nse = NSESession()
    session_ok = nse.setup()

    api_results = {name: {} for name in TARGETS}

    if session_ok:
        for name in TARGETS:
            api_name = NSE_API_NAMES[name]
            print(f"\n  Fetching {name} ({api_name}) ...")
            month_count = 0

            for chunk_start, chunk_end in month_chunks(GAP_START, GAP_END):
                chunk_series = nse.get_indices_history(api_name, chunk_start, chunk_end)

                if not chunk_series.empty:
                    for ts, val in chunk_series.items():
                        api_results[name][ts] = val
                    month_count += 1
                    print(f"    ✅ {chunk_start} → {chunk_end}: {len(chunk_series)} rows")
                else:
                    print(f"    ❌ {chunk_start} → {chunk_end}: no data")

                # Polite delay between API calls
                time.sleep(random.uniform(1.5, 3.0))

            total = len(api_results[name])
            print(f"  {name}: got {total} rows from API ({month_count}/24 months)")
    else:
        print("  ❌ NSE session failed — skipping API (will use archives)")

    # ── Step 2: Merge API results into CSVs ────────────────────────────────
    print("\n[2] Merging API results ...")
    remaining_dates = {name: [] for name in TARGETS}

    for name, ticker in TARGETS.items():
        api_s = pd.Series(api_results[name]).sort_index()
        api_s = api_s[api_s > 0]

        if not api_s.empty:
            existing = status[name]["series"]
            merged   = merge_series(existing, api_s)
            save_series(merged, ticker)
            status[name]["series"] = merged
            print(f"  ✅ {name}: +{len(api_s)} rows from API")

        # Find still-missing dates in the gap
        updated = load_series(ticker)
        in_gap = updated[(updated.index >= gap_start_ts) & (updated.index <= gap_end_ts)]
        all_gap_bdays = pd.bdate_range(gap_start_ts, gap_end_ts)
        missing = [d.date() for d in all_gap_bdays if d not in in_gap.index]
        remaining_dates[name] = missing
        print(f"  {name}: {len(missing)} dates still missing after API step")

    # ── Step 3: NSE Archives fallback for remaining dates ─────────────────
    # Find union of still-missing dates across both indices
    all_missing = sorted(set(
        d for dates in remaining_dates.values() for d in dates
    ))

    if all_missing:
        print(f"\n[3] NSE Archives fallback: {len(all_missing)} dates still missing")
        print("    (This will take a while due to slow pacing to avoid rate limits)")
        print(f"    Estimated time: {len(all_missing) * 3.5 / 60:.0f} minutes")

        arc_data = {name: {} for name in TARGETS}
        ok = 0
        for i, d in enumerate(all_missing, 1):
            day_data = fetch_archive_day(d)
            if day_data:
                ok += 1
                for name in TARGETS:
                    if name in day_data:
                        arc_data[name][pd.Timestamp(d)] = day_data[name]

            if i % 30 == 0 or i == len(all_missing):
                print(f"    {i}/{len(all_missing)} ({100*i//len(all_missing)}%)  "
                      f"successful fetches: {ok}")

            delay = random.uniform(2.5, 5.0)  # 2.5–5s between requests
            time.sleep(delay)

        # Merge archive results
        print("\n  Merging archive results ...")
        for name, ticker in TARGETS.items():
            arc_s = pd.Series(arc_data[name]).sort_index()
            arc_s = arc_s[arc_s > 0]
            if not arc_s.empty:
                existing = load_series(ticker)
                merged = merge_series(existing, arc_s)
                save_series(merged, ticker)
                print(f"  ✅ {name}: +{len(arc_s)} rows from archives")
            else:
                print(f"  ⚠  {name}: archives returned 0 rows for remaining dates")
    else:
        print("\n[3] No dates remaining — archives step skipped ✅")

    # ── Final summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 65)
    print("FINAL STATUS")
    print("=" * 65)
    for name, ticker in TARGETS.items():
        s = load_series(ticker)
        in_gap = s[(s.index >= gap_start_ts) & (s.index <= gap_end_ts)]
        all_bdays = len(pd.bdate_range(gap_start_ts, gap_end_ts))
        pct = 100 * len(in_gap) // all_bdays
        status_icon = "✅" if pct > 90 else ("⚠️" if pct > 50 else "❌")
        print(f"\n{status_icon} {name}")
        print(f"   Gap days filled: {len(in_gap)}/{all_bdays} ({pct}%)")
        if not in_gap.empty:
            print(f"   First gap value: {in_gap.index[0].date()} = {in_gap.iloc[0]:.2f}")
            print(f"   Last gap value:  {in_gap.index[-1].date()} = {in_gap.iloc[-1]:.2f}")
        print(f"   Full series: {s.index[0].date()} → {s.index[-1].date()} ({len(s)} rows)")

    print("\nDone. If gap is still not filled:")
    print("  1. Check if niftyindices.com is back up: python refetch_all.py")
    print("  2. Try again with larger archive delay: edit delay=5.0 in fill_from_archives()")


if __name__ == "__main__":
    main()

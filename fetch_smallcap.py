"""
fetch_smallcap.py — Fetch Nifty Smallcap 100 data for IDX_CNXSC.csv

Sources tried in order:
  1. niftyindices.com  (fastest, has full history from 2004)
  2. NSE daily archives year-by-year (reliable, starts 2011)

Run:
    python fetch_smallcap.py
"""
import io, sys, time, json, warnings
warnings.filterwarnings("ignore")
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import requests
import pandas as pd

try:
    from curl_cffi import requests as cfr
    _CF = cfr.Session()
    HAS_CF = True
except ImportError:
    _CF = None
    HAS_CF = False

IDX_DIR = ROOT / "data" / "live" / "indices"
OUT_FILE = IDX_DIR / "IDX_CNXSC.csv"
TODAY    = date.today()

HDRS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def save(s: pd.Series):
    df = s.reset_index()
    df.columns = ["date", "close"]
    df["date"]  = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    df = df.drop_duplicates("date", keep="last").sort_values("date")
    df.to_csv(OUT_FILE, index=False)
    print(f"  Saved {len(df)} rows to {OUT_FILE.name}")


def merge(base: pd.Series, new: pd.Series) -> pd.Series:
    if base.empty: return new
    if new.empty:  return base
    c = pd.concat([base, new])
    return c[~c.index.duplicated(keep="last")].sort_index()


# ── Source 1: niftyindices.com ─────────────────────────────────────────────────
def fetch_niftyindices(start="2004-01-01", end=str(TODAY)) -> pd.Series:
    url  = "https://www.niftyindices.com/Backpage.aspx/getHistoricalData"
    hdrs = {**HDRS, "Content-Type": "application/json",
            "Referer": "https://www.niftyindices.com/",
            "Origin":  "https://www.niftyindices.com"}

    start_dt = pd.Timestamp(start)
    end_dt   = pd.Timestamp(end)
    rows: list = []
    chunk = start_dt

    print("  Fetching from niftyindices.com in 2-year chunks ...")
    while chunk <= end_dt:
        chunk_end = min(chunk + pd.DateOffset(years=2) - pd.DateOffset(days=1), end_dt)
        payload   = {"name": "NIFTY SMALLCAP 100",
                     "startDate": chunk.strftime("%d-%b-%Y"),
                     "endDate":   chunk_end.strftime("%d-%b-%Y")}
        for attempt in range(4):
            try:
                r = requests.post(url, json=payload, headers=hdrs, timeout=60)
                if r.status_code == 200:
                    chunk_rows = json.loads(r.json().get("d", "[]"))
                    rows.extend(chunk_rows)
                    print(f"    {chunk.date()} → {chunk_end.date()}: {len(chunk_rows)} rows")
                    break
            except Exception as e:
                wait = 15 * (attempt + 1)
                print(f"    Attempt {attempt+1}/4 failed ({e}) — waiting {wait}s ...")
                time.sleep(wait)
        else:
            print(f"    ⚠  Chunk {chunk.date()} → {chunk_end.date()} gave up")
        chunk = chunk_end + pd.DateOffset(days=1)
        time.sleep(1.5)

    result = {}
    for row in rows:
        try:
            ts = pd.Timestamp(str(row.get("TIMESTAMP", "")).strip()).normalize()
            v  = float(str(row.get("CLOSING_INDEX_VAL", "")).replace(",", ""))
            if v > 0:
                result[ts] = v
        except Exception:
            pass
    s = pd.Series(result).sort_index() if result else pd.Series(dtype=float)
    print(f"  niftyindices.com total: {len(s)} rows")
    return s


# ── Source 2: NSE archives (day by day, year by year) ─────────────────────────
def _fetch_archive_day(d: date) -> float | None:
    url  = f"https://archives.nseindia.com/content/indices/ind_close_all_{d.strftime('%d%m%Y')}.csv"
    hdrs_arc = {**HDRS, "Referer": "https://archives.nseindia.com/"}

    if HAS_CF:
        try:
            r = _CF.get(url, impersonate="chrome", headers=hdrs_arc, timeout=12)
            if r.status_code == 200 and "Index" in r.text:
                return _parse_smallcap(r.text)
        except Exception:
            pass

    try:
        r = requests.get(url, headers=hdrs_arc, timeout=10)
        if r.status_code == 200 and "Index" in r.text:
            return _parse_smallcap(r.text)
    except Exception:
        pass
    return None


def _parse_smallcap(text: str) -> float | None:
    try:
        df = pd.read_csv(io.StringIO(text))
        df.columns = [c.strip() for c in df.columns]
        name_col  = next((c for c in ["Index Name", "IndexName"] if c in df.columns), None)
        close_col = next((c for c in ["Closing Index Value", "ClosingIndexValue"] if c in df.columns), None)
        if not name_col or not close_col:
            return None
        row = df[df[name_col].str.contains("Smallcap 100", case=False, na=False)]
        if row.empty:
            row = df[df[name_col].str.contains("SMALLCAP 100", case=False, na=False)]
        if row.empty:
            return None
        v = float(str(row.iloc[0][close_col]).replace(",", ""))
        return v if v > 0 else None
    except Exception:
        return None


def weekdays(start: date, end: date) -> list[date]:
    return [start + timedelta(i) for i in range((end - start).days + 1)
            if (start + timedelta(i)).weekday() < 5]


def fetch_archives_yearly(from_year: int = 2011) -> pd.Series:
    print(f"  Fetching from NSE archives year by year ({from_year} → {TODAY.year}) ...")
    all_data: dict = {}

    for year in range(from_year, TODAY.year + 1):
        y_start = date(year, 1, 1)
        y_end   = min(date(year, 12, 31), TODAY)
        days    = weekdays(y_start, y_end)
        ok = 0

        for d in days:
            val = _fetch_archive_day(d)
            if val is not None:
                all_data[pd.Timestamp(d)] = val
                ok += 1
            time.sleep(0.25)

        print(f"    {year}: {ok}/{len(days)} trading days fetched")

    s = pd.Series(all_data).sort_index() if all_data else pd.Series(dtype=float)
    print(f"  NSE archives total: {len(s)} rows")
    return s


# ── Main ───────────────────────────────────────────────────────────────────────
print("=" * 60)
print("  Fetching Nifty Smallcap 100 → IDX_CNXSC.csv")
print("=" * 60)

result = pd.Series(dtype=float)

# Load any existing data
if OUT_FILE.exists():
    try:
        existing = pd.read_csv(OUT_FILE, index_col=0, parse_dates=True).iloc[:,0].dropna()
        existing.index = pd.to_datetime(existing.index).normalize()
        print(f"  Existing data: {len(existing)} rows ({existing.index[0].date()} → {existing.index[-1].date()})")
        result = existing
    except Exception:
        pass

# Step 1: Try niftyindices.com
print("\n[1/2] Trying niftyindices.com ...")
ni_data = fetch_niftyindices("2004-01-01", str(TODAY))
if not ni_data.empty:
    result = merge(result, ni_data)
    save(result)
    print(f"  ✅  niftyindices.com success! Total: {len(result)} rows "
          f"({result.index[0].date()} → {result.index[-1].date()})")
else:
    print("  ❌  niftyindices.com returned no data")

    # Step 2: NSE archives
    print("\n[2/2] Falling back to NSE daily archives (year by year) ...")
    # Figure out what years we still need
    have_from = result.index[0].year if not result.empty else 2011
    have_to   = result.index[-1].year if not result.empty else 2010
    fetch_from_year = max(2011, have_to)   # archives start ~2011

    arc_data = fetch_archives_yearly(2011)
    if not arc_data.empty:
        result = merge(result, arc_data)
        save(result)
        print(f"  ✅  NSE archives done. Total: {len(result)} rows "
              f"({result.index[0].date()} → {result.index[-1].date()})")
    else:
        print("  ❌  NSE archives also returned no data")

print(f"\n{'='*60}")
if not result.empty:
    print(f"  DONE: {len(result)} rows  "
          f"({result.index[0].date()} → {result.index[-1].date()})")
else:
    print("  FAILED: no data fetched")
print(f"{'='*60}")

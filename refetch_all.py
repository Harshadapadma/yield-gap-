"""
refetch_all.py  —  Fresh fetch of ALL data from the best available sources.

    cd <project root>
    python refetch_all.py

What this does:
  1. Yahoo Finance (primary)  → all NSE indices where YF gives PRICE RETURN
  2. NSE Archives             → Midcap 100, Smallcap 100 (YF gives TRI for these)
  3. niftyindices.com         → pre-2013 backfill for Midcap/Smallcap/Next50/Nifty500
  4. Bond yield               → kept from existing seeds (2006→Apr-2026) + live CSV
  5. Data validation          → removes TRI bleed, bad spikes, obvious outliers

Run time: ~3 minutes (yfinance batch + sequential NSE archive fetch)
"""

import io, sys, time, warnings, json
warnings.filterwarnings("ignore")
from pathlib import Path
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import requests
import pandas as pd
import yfinance as yf
try:
    from curl_cffi import requests as cf_requests
    _CF_SESSION = cf_requests.Session()
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False
    _CF_SESSION = None

IDX_DIR = ROOT / "data" / "live" / "indices"
IDX_DIR.mkdir(parents=True, exist_ok=True)

TODAY        = date.today()
TARGET_START = date(2006, 1, 1)

HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ══════════════════════════════════════════════════════════════════════════════
# INSTRUMENT DEFINITIONS
# ══════════════════════════════════════════════════════════════════════════════

# Yahoo Finance gives correct PRICE RETURN for these NSE indices
YF_NSE_INDICES = {
    "Nifty 50":            "^NSEI",
    "Nifty Bank":          "^NSEBANK",
    "Nifty IT":            "^CNXIT",
    "Nifty Pharma":        "^CNXPHARMA",
    "Nifty Auto":          "^CNXAUTO",
    "Nifty FMCG":          "^CNXFMCG",
    "Nifty Metal":         "^CNXMETAL",
    "Nifty Energy":        "^CNXENERGY",
    "Nifty Realty":        "^CNXREALTY",
}

# Yahoo Finance gives TOTAL RETURN INDEX for these — use NSE Archives instead
ARCHIVE_ONLY_INDICES = {
    "Nifty Midcap 100":   "^NSMIDCP",    # YF=TRI → use archives
    "Nifty Smallcap 100": "^CNXSC",      # YF=TRI or broken
    "Nifty Next 50":      "NIFTYNEXT50",
    "Nifty 500":          "NIFTY500",
}

# All 13 NSE index canonical names (used in archive CSVs)
ALL_NSE = {**YF_NSE_INDICES, **ARCHIVE_ONLY_INDICES}

# niftyindices.com API names (pre-2013 backfill source)
NI_NAMES = {
    "Nifty 50":            "NIFTY 50",
    "Nifty Bank":          "NIFTY BANK",
    "Nifty IT":            "NIFTY IT",
    "Nifty Pharma":        "NIFTY PHARMA",
    "Nifty Auto":          "NIFTY AUTO",
    "Nifty FMCG":          "NIFTY FMCG",
    "Nifty Metal":         "NIFTY METAL",
    "Nifty Energy":        "NIFTY ENERGY",
    "Nifty Realty":        "NIFTY REALTY",
    "Nifty Midcap 100":    "NIFTY MIDCAP 100",
    "Nifty Smallcap 100":  "NIFTY SMALLCAP 100",
    "Nifty Next 50":       "NIFTY NEXT 50",
    "Nifty 500":           "NIFTY 500",
}

# Sensex + global instruments via Yahoo Finance
GLOBAL_YF = [
    ("Sensex",                "^BSESN",       "2006-01-01"),
    ("Gold BeES (Nippon)",    "GOLDBEES.NS",  "2007-04-01"),
    ("Nifty BeES (Nippon)",   "NIFTYBEES.NS", "2006-01-01"),
    ("Junior BeES (NNext50)", "JUNIORBEES.NS","2006-01-01"),
    ("Bank BeES (Nippon)",    "BANKBEES.NS",  "2010-01-01"),
    ("USD/INR",               "USDINR=X",     "2006-01-01"),
    ("Gold (USD-Futures)",    "GC=F",         "2006-01-01"),
    ("Crude Oil (WTI)",       "CL=F",         "2006-01-01"),
    ("S&P 500",               "^GSPC",        "2006-01-01"),
    ("NASDAQ 100",            "^NDX",         "2006-01-01"),
    ("US 10Y Yield",          "^TNX",         "2006-01-01"),
]

# Note: No absolute sanity ranges — they would destroy valid HISTORICAL data.
# (e.g., Nifty 50 was ~4,000 in 2008 but is ~24,000 today.
#  A range of [14,000, 30,000] would wipe out all pre-2020 Nifty 50 data.)
# TRI vs PR filtering is handled in index_store.py via _NO_YF_FALLBACK.

# Old "CNX/S&P CNX" archive names → canonical names (pre-Sep 2015 archives)
CNX_ALIASES = {
    "s&pcnxnifty":       "Nifty 50",     "cnxnifty":          "Nifty 50",
    "s&pcnxbank":        "Nifty Bank",   "cnxbank":           "Nifty Bank",
    "cnxit":             "Nifty IT",     "s&pcnxit":          "Nifty IT",
    "cnxpharma":         "Nifty Pharma", "s&pcnxpharma":      "Nifty Pharma",
    "cnxauto":           "Nifty Auto",   "s&pcnxauto":        "Nifty Auto",
    "cnxfmcg":           "Nifty FMCG",   "s&pcnxfmcg":        "Nifty FMCG",
    "cnxmetal":          "Nifty Metal",  "s&pcnxmetal":       "Nifty Metal",
    "cnxenergy":         "Nifty Energy", "s&pcnxenergy":      "Nifty Energy",
    "cnxrealty":         "Nifty Realty", "s&pcnxrealty":      "Nifty Realty",
    "cnxmidcap":         "Nifty Midcap 100",    "cnxmidcap100":      "Nifty Midcap 100",
    "s&pcnxmidcap":      "Nifty Midcap 100",    "s&pcnxmidcap100":   "Nifty Midcap 100",
    "cnxsmallcap":       "Nifty Smallcap 100",  "cnxsmallcap100":    "Nifty Smallcap 100",
    "s&pcnxsmallcap":    "Nifty Smallcap 100",  "s&pcnxsmallcap100": "Nifty Smallcap 100",
    "niftyjunior":       "Nifty Next 50",        "niftynext50":       "Nifty Next 50",
    "cnxniftyjunior":    "Nifty Next 50",        "s&pcnxniftyjunior": "Nifty Next 50",
    "s&pcnx500":         "Nifty 500",            "cnx500":            "Nifty 500",
    "nifty500":          "Nifty 500",
}


# ══════════════════════════════════════════════════════════════════════════════
# FILE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def safe_fname(t):
    return t.replace("/","_").replace("^","IDX_").replace("=","_").replace(" ","_")

def csv_path(ticker):
    return IDX_DIR / f"{safe_fname(ticker)}.csv"

def load_csv(ticker: str) -> pd.Series:
    p = csv_path(ticker)
    if not p.exists() or p.stat().st_size < 10:
        return pd.Series(dtype=float)
    try:
        df = pd.read_csv(p, parse_dates=["date"])
        df = df.dropna(subset=["date","close"])
        s  = df.set_index("date")["close"].sort_index()
        s.index = pd.to_datetime(s.index).normalize()
        return s[~s.index.duplicated(keep="last")]
    except Exception:
        return pd.Series(dtype=float)

def save_csv(s: pd.Series, ticker: str):
    if s is None or (isinstance(s, pd.Series) and s.empty):
        return
    df = s.reset_index()
    df.columns = ["date","close"]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["close"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    df.to_csv(csv_path(ticker), index=False)

def merge(base: pd.Series, new: pd.Series) -> pd.Series:
    if base is None or base.empty:
        return new.sort_index() if new is not None else pd.Series(dtype=float)
    if new is None or new.empty:
        return base
    c = pd.concat([base, new])
    return c[~c.index.duplicated(keep="last")].sort_index()

def weekdays(start: date, end: date) -> list:
    return [start + timedelta(i) for i in range((end-start).days+1)
            if (start + timedelta(i)).weekday() < 5]


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 1 — Yahoo Finance
# ══════════════════════════════════════════════════════════════════════════════

def fetch_yf(ticker: str, start: str = "2006-01-01") -> pd.Series:
    def clean(x) -> pd.Series:
        if isinstance(x, pd.DataFrame): x = x.iloc[:,0]
        s = x.dropna().squeeze()
        if not isinstance(s, pd.Series): s = pd.Series([s])
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        return s[~s.index.duplicated(keep="last")]

    # Try history() first — returns the most data
    for period in ["25y","20y","15y","10y","5y"]:
        try:
            h = yf.Ticker(ticker).history(period=period, auto_adjust=True)
            if not h.empty and "Close" in h.columns:
                s = clean(h["Close"])
                if len(s) > 30:
                    return s
        except Exception:
            pass

    # Fallback: download() with explicit start date
    try:
        df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
        if not df.empty and "Close" in df.columns:
            return clean(df["Close"])
    except Exception:
        pass

    return pd.Series(dtype=float)


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 2 — NSE Archives (sequential, 1 req / 0.3s to stay under rate limit)
# ══════════════════════════════════════════════════════════════════════════════

def probe_archive_start() -> date:
    """Return earliest date NSE archives has data."""
    for yr in [2011, 2012, 2013, 2014, 2015]:
        d = date(yr, 1, 3 if yr == 2011 else 2)
        url = f"https://archives.nseindia.com/content/indices/ind_close_all_{d.strftime('%d%m%Y')}.csv"
        try:
            r = requests.get(url, headers={**HDRS,"Referer":"https://archives.nseindia.com/"}, timeout=10)
            if r.status_code == 200 and "Closing Index Value" in r.text:
                return date(yr, 1, 2)
        except Exception:
            pass
        time.sleep(0.3)
    return date(2013, 1, 2)

def parse_archive_day(text: str) -> dict:
    try:
        df = pd.read_csv(io.StringIO(text))
        df.columns = [c.strip() for c in df.columns]
        close_col = next((c for c in ["Closing Index Value","ClosingIndexValue","Close"] if c in df.columns), None)
        name_col  = next((c for c in ["Index Name","IndexName","Name"] if c in df.columns), None)
        if not close_col or not name_col:
            return {}
        result = {}
        for _, row in df.iterrows():
            nm  = str(row[name_col]).strip()
            key = nm.lower().replace(" ","").replace("-","").replace("(","").replace(")","")
            # Match to canonical name
            matched = next((c for c in ALL_NSE if nm.lower().replace(" ","") == c.lower().replace(" ","")), None)
            if matched is None:
                matched = CNX_ALIASES.get(key)
            if matched:
                try:
                    v = float(str(row[close_col]).replace(",",""))
                    if v > 0:
                        result[matched] = v
                except Exception:
                    pass
        return result
    except Exception:
        return {}

def _fetch_archive_day(d: date) -> tuple[date, dict]:
    """
    Fetch one day's NSE archive CSV.
    Tries curl_cffi (Chrome impersonation) first — much lower block rate.
    Falls back to plain requests if curl_cffi unavailable.
    Returns (date, {canonical_name: value}) or (date, {}) on failure.
    """
    url = f"https://archives.nseindia.com/content/indices/ind_close_all_{d.strftime('%d%m%Y')}.csv"
    hdrs = {**HDRS, "Referer": "https://archives.nseindia.com/"}

    # Try curl_cffi Chrome impersonation first
    if _HAS_CURL_CFFI:
        try:
            r = _CF_SESSION.get(url, impersonate="chrome", headers=hdrs, timeout=15)
            if r.status_code == 200 and "Index" in r.text:
                return d, parse_archive_day(r.text)
        except Exception:
            pass

    # Fallback: plain requests
    try:
        r = requests.get(url, headers=hdrs, timeout=12)
        if r.status_code == 200 and "Index" in r.text:
            return d, parse_archive_day(r.text)
    except Exception:
        pass

    return d, {}


def fetch_archive_range(start: date, end: date,
                        names: set = None,
                        delay: float = 0.3) -> dict[str, dict]:
    """
    Fetch NSE archive CSVs for date range.
    Returns {canonical_name: {date: value}}.
    Uses curl_cffi Chrome impersonation to minimise rate-limit blocks.
    """
    days = weekdays(start, end)
    data: dict[str, dict] = {n: {} for n in (names or ALL_NSE.keys())}
    total = len(days)

    mode = "curl_cffi+Chrome" if _HAS_CURL_CFFI else "plain requests"
    print(f"  NSE Archives: fetching {total} trading days "
          f"({total*delay/60:.0f}m{int((total*delay)%60)}s est., mode={mode}) ...")

    ok = blocked = 0
    for i, d in enumerate(days, 1):
        _, parsed = _fetch_archive_day(d)
        if parsed:
            for nm, v in parsed.items():
                if nm in data:
                    data[nm][pd.Timestamp(d)] = v
            ok += 1
        else:
            blocked += 1

        if i % 200 == 0 or i == total:
            eta = (total - i) * delay
            print(f"    {i}/{total} ({100*i//total}%)  ok={ok}  blocked={blocked}  "
                  f"ETA={eta:.0f}s", flush=True)
        time.sleep(delay)

    return data


def fetch_archive_dates_targeted(dates: list[date], names: set,
                                 delay: float = 0.5,
                                 max_retries: int = 3) -> dict[str, dict]:
    """
    Fetch specific dates from NSE archives with retry logic.
    Uses longer delay and multiple retries — for filling known gaps.
    """
    data: dict[str, dict] = {n: {} for n in names}
    remaining = list(dates)

    for attempt in range(max_retries):
        if not remaining:
            break
        failed_this_round = []
        print(f"  Targeted fetch attempt {attempt+1}/{max_retries}: "
              f"{len(remaining)} dates, delay={delay}s ...")
        for d in remaining:
            _, parsed = _fetch_archive_day(d)
            if parsed:
                for nm, v in parsed.items():
                    if nm in data:
                        data[nm][pd.Timestamp(d)] = v
            else:
                failed_this_round.append(d)
            time.sleep(delay)
        remaining = failed_this_round
        delay = min(delay * 1.5, 2.0)   # back off on retries

    if remaining:
        print(f"  ⚠  Still missing after {max_retries} attempts: {len(remaining)} dates")
    return data


# ══════════════════════════════════════════════════════════════════════════════
# SOURCE 3 — niftyindices.com (pre-archive backfill, 2006→archive_start)
# ══════════════════════════════════════════════════════════════════════════════

_NI_HDRS = {**HDRS,
            "Content-Type": "application/json",
            "Referer": "https://www.niftyindices.com/",
            "Origin":  "https://www.niftyindices.com"}
_NI_URL  = "https://www.niftyindices.com/Backpage.aspx/getHistoricalData"

def test_niftyindices() -> tuple[bool, str]:
    """
    Test connectivity to niftyindices.com with a generous timeout.
    Returns (ok, error_message).
    A ReadTimeout means the site is up but slow — treat as retryable, not fatal.
    """
    for attempt, to in enumerate([20, 40, 60]):
        try:
            r = requests.post(
                _NI_URL,
                json={"name":"NIFTY 50","startDate":"01-Jan-2025","endDate":"31-Jan-2025"},
                headers=_NI_HDRS, timeout=to,
            )
            if r.status_code == 200:
                rows = json.loads(r.json().get("d","[]"))
                return (True, "") if rows else (False, "0 rows returned")
            if r.status_code in (500, 502, 503, 504):
                if attempt < 2:
                    print(f"  niftyindices HTTP {r.status_code} — retrying (attempt {attempt+2}/3) ...")
                    time.sleep(15)
                    continue
            return False, f"HTTP {r.status_code}"
        except requests.exceptions.ReadTimeout:
            if attempt < 2:
                print(f"  niftyindices timed out ({to}s) — retrying with longer timeout ...")
                time.sleep(10)
                continue
            return False, f"Read timed out after {to}s — site is slow, try again later"
        except requests.exceptions.ConnectionError:
            return False, "connection error — site unreachable"
        except Exception as e:
            return False, str(e)[:80]
    return False, "all retries exhausted"


def fetch_niftyindices(name: str, start: str, end: str) -> pd.Series:
    """
    Fetch full historical data from niftyindices.com in 2-year chunks.
    Uses generous timeouts (60s) and retries on both HTTP errors and timeouts.
    """
    ni_name   = NI_NAMES.get(name, name.upper())
    start_dt  = pd.Timestamp(start)
    end_dt    = pd.Timestamp(end)
    all_rows: list = []
    chunk = start_dt

    while chunk <= end_dt:
        chunk_end = min(chunk + pd.DateOffset(years=2) - pd.DateOffset(days=1), end_dt)
        payload = {"name": ni_name,
                   "startDate": chunk.strftime("%d-%b-%Y"),
                   "endDate":   chunk_end.strftime("%d-%b-%Y")}
        fetched = False
        for attempt in range(6):                     # up to 6 attempts per chunk
            to = 60 if attempt < 3 else 90           # escalate timeout on later retries
            try:
                r = requests.post(_NI_URL, json=payload,
                                  headers=_NI_HDRS, timeout=to)
                if r.status_code == 200:
                    rows = json.loads(r.json().get("d","[]"))
                    if isinstance(rows, list):
                        all_rows.extend(rows)
                    fetched = True
                    break
                elif r.status_code in (500, 502, 503, 504) and attempt < 5:
                    wait = 20 * (attempt + 1)
                    print(f"    HTTP {r.status_code} chunk {chunk.date()}→{chunk_end.date()} "
                          f"— waiting {wait}s ...")
                    time.sleep(wait)
                else:
                    break
            except requests.exceptions.ReadTimeout:
                wait = 15 * (attempt + 1)
                print(f"    Timeout ({to}s) chunk {chunk.date()}→{chunk_end.date()} "
                      f"— waiting {wait}s (attempt {attempt+1}/6) ...")
                time.sleep(wait)
            except Exception as e:
                if attempt < 5:
                    time.sleep(10)

        if not fetched:
            print(f"    ⚠  Chunk {chunk.date()}→{chunk_end.date()} failed after 6 attempts")

        chunk = chunk_end + pd.DateOffset(days=1)
        time.sleep(1.0)   # polite pause between chunks

    results = {}
    for row in all_rows:
        try:
            ts = pd.Timestamp(str(row.get("TIMESTAMP","")).strip()).normalize()
            v  = float(str(row.get("CLOSING_INDEX_VAL","")).replace(",",""))
            if v > 0:
                results[ts] = v
        except Exception:
            pass
    return pd.Series(results).sort_index() if results else pd.Series(dtype=float)


# ══════════════════════════════════════════════════════════════════════════════
# NSE WEBSITE API  (indicesHistory endpoint — monthly chunks)
# Returns correct PRICE RETURN data; better success rate than archives for
# historical dates (2016-2018) that the archive CDN often rate-limits.
# ══════════════════════════════════════════════════════════════════════════════

# Map canonical names → NSE API indexType param
_NSE_API_INDEX_NAMES = {
    "Nifty Midcap 100":   "NIFTY MIDCAP 100",
    "Nifty Smallcap 100": "NIFTY SMALLCAP 100",
    "Nifty Next 50":      "NIFTY NEXT 50",
    "Nifty 500":          "NIFTY 500",
}

_NSE_SESSION: object = None   # lazy init

def _get_nse_session():
    """Lazy-init a session with NSE website cookies."""
    global _NSE_SESSION
    if _NSE_SESSION is not None:
        return _NSE_SESSION
    try:
        if _HAS_CURL_CFFI:
            import curl_cffi.requests as _cfr
            sess = _cfr.Session()
            sess.get("https://www.nseindia.com", impersonate="chrome",
                     headers=HDRS, timeout=20)
            time.sleep(2)
            sess.get("https://www.nseindia.com/market-data/live-equity-market",
                     impersonate="chrome", headers=HDRS, timeout=15)
            time.sleep(1)
        else:
            sess = requests.Session()
            sess.headers.update(HDRS)
            sess.get("https://www.nseindia.com", timeout=20)
            time.sleep(2)
        _NSE_SESSION = sess
    except Exception:
        _NSE_SESSION = None
    return _NSE_SESSION


def _fetch_nse_api_month(name: str, start: date, end: date) -> pd.Series:
    """
    Fetch one month of historical index data from NSE indicesHistory API.
    Returns empty Series on failure.
    """
    sess = _get_nse_session()
    if sess is None:
        return pd.Series(dtype=float)

    api_name = _NSE_API_INDEX_NAMES.get(name, name.upper())
    url = (
        "https://www.nseindia.com/api/historical/indicesHistory"
        f"?indexType={requests.utils.quote(api_name)}"
        f"&from={start.strftime('%d-%m-%Y')}"
        f"&to={end.strftime('%d-%m-%Y')}"
    )
    api_hdrs = {**HDRS,
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.nseindia.com/"}

    for attempt in range(3):
        try:
            if _HAS_CURL_CFFI:
                r = sess.get(url, impersonate="chrome", headers=api_hdrs, timeout=20)
            else:
                r = sess.get(url, headers=api_hdrs, timeout=20)

            if r.status_code == 200:
                data = r.json()
                inner = data.get("data", {})
                records = []
                if isinstance(inner, dict):
                    records = inner.get("indexCloseOnlineRecords", [])
                elif isinstance(inner, list):
                    records = inner

                result = {}
                for rec in records:
                    try:
                        ts_str = (rec.get("EOD_TIMESTAMP") or rec.get("TIMESTAMP")
                                  or rec.get("eodTimestamp") or "")
                        val    = (rec.get("EOD_CLOSE_INDEX_VAL") or
                                  rec.get("CLOSE_INDEX_VAL") or
                                  rec.get("eodCloseIndexVal") or 0)
                        v = float(str(val).replace(",", ""))
                        if ts_str and v > 0:
                            ts = pd.Timestamp(str(ts_str).strip()).normalize()
                            result[ts] = v
                    except Exception:
                        pass
                if result:
                    return pd.Series(result).sort_index()

                # Empty — re-establish session on first attempt
                if attempt == 0:
                    global _NSE_SESSION
                    _NSE_SESSION = None
                    time.sleep(3)
                    _get_nse_session()

            elif r.status_code == 429:
                time.sleep(30 * (attempt + 1))
            elif r.status_code in (401, 403):
                _NSE_SESSION = None
                time.sleep(5)
                _get_nse_session()

        except Exception:
            time.sleep(5)
        time.sleep(2)

    return pd.Series(dtype=float)


def fetch_gap_via_nse_api(name: str, gap_start: date, gap_end: date) -> pd.Series:
    """
    Use the NSE indicesHistory API with monthly chunks to fill a data gap.
    This is the preferred method over NSE archives for historical dates.
    """
    if name not in _NSE_API_INDEX_NAMES:
        return pd.Series(dtype=float)

    results = {}
    cur = gap_start
    months_ok = 0

    while cur <= gap_end:
        # End of this month
        if cur.month == 12:
            month_end = date(cur.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(cur.year, cur.month + 1, 1) - timedelta(days=1)
        chunk_end = min(month_end, gap_end)

        s = _fetch_nse_api_month(name, cur, chunk_end)
        if not s.empty:
            for ts, v in s.items():
                results[ts] = v
            months_ok += 1

        time.sleep(1.5 + (0.5 * (1 - months_ok/max(1,months_ok+1))))  # adaptive delay
        cur = chunk_end + timedelta(days=1)

    total_months = sum(1 for _ in
        [(m) for m in range(12*(gap_end.year - gap_start.year) +
                            gap_end.month - gap_start.month + 1)])
    print(f"      NSE API: {months_ok}/{total_months} months, {len(results)} total rows")
    return pd.Series(results).sort_index() if results else pd.Series(dtype=float)


# ══════════════════════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def fill_internal_gaps(csv_ticker: str, yf_ticker: str | None, name: str,
                       min_gap_days: int = 10) -> None:
    """
    Find trading-day gaps >min_gap_days in an existing CSV and fill them.

    Priority:
      1. NSE indicesHistory API (monthly chunks) — correct PR data, no CDN blocking
      2. NSE Archives — fallback if API returns nothing
      3. If both fail, leave gap empty (no wrong TRI data written)

    yf_ticker is accepted for API compatibility but not used (YF gives TRI for
    these indices which is incorrect).
    """
    s = load_csv(csv_ticker)
    if s.empty or len(s) < 2:
        return

    # Build full expected business-day calendar between first and last known date
    full_idx = pd.bdate_range(s.index[0], s.index[-1])
    missing  = full_idx[~full_idx.isin(s.index)]
    if missing.empty:
        return

    # Group consecutive missing dates into gap blocks
    gaps: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    g_start = missing[0]
    g_prev  = missing[0]
    for d in missing[1:]:
        if (d - g_prev).days > 5:
            gaps.append((g_start, g_prev))
            g_start = d
        g_prev = d
    gaps.append((g_start, g_prev))

    long_gaps = [(gs, ge) for gs, ge in gaps
                 if len(pd.bdate_range(gs, ge)) >= min_gap_days]
    if not long_gaps:
        return

    print(f"    {name}: {len(long_gaps)} internal gap(s) detected")

    for gs, ge in long_gaps:
        gap_dates = [d.date() for d in pd.bdate_range(gs, ge)]
        n_days = len(gap_dates)
        print(f"      Gap {gs.date()}→{ge.date()} ({n_days} trading days)")

        # --- Primary: NSE indicesHistory API ---
        print(f"      → Trying NSE indicesHistory API ...")
        api_series = fetch_gap_via_nse_api(name, gs.date(), ge.date())
        api_series = api_series[api_series > 0] if not api_series.empty else api_series

        if not api_series.empty:
            s = merge(s, api_series)
            pct = 100 * len(api_series) // n_days
            print(f"      ✅  Filled {len(api_series)}/{n_days} days ({pct}%) via NSE API")
            if pct >= 90:
                continue   # gap mostly filled, no need for archive fallback

        # --- Fallback: NSE Archives (targeted, with retry) ---
        # Only attempt dates still missing after API step
        still_missing = [d for d in gap_dates
                         if pd.Timestamp(d) not in s.index]
        if still_missing:
            print(f"      → Archive fallback for {len(still_missing)} remaining dates ...")
            arc = fetch_archive_dates_targeted(
                still_missing, names={name}, delay=0.5, max_retries=3
            )
            arc_series = pd.Series(arc.get(name, {})).sort_index()
            arc_series = arc_series[arc_series > 0]

            if not arc_series.empty:
                s = merge(s, arc_series)
                pct = 100 * len(arc_series) // len(still_missing)
                print(f"      ✅  Archive filled {len(arc_series)}/{len(still_missing)} days ({pct}%)")
            else:
                print(f"      ⚠  Archives returned 0 rows — gap left empty "
                      f"(no wrong data written)")

    save_csv(s, csv_ticker)


def validate_series(s: pd.Series, name: str) -> pd.Series:
    """
    Remove only clearly impossible values. Preserves ALL historical data.

    Rules:
      1. Remove zero / negative values (price can never be ≤ 0)
      2. For ARCHIVE-ONLY indices (Midcap, Smallcap, Next50, Nifty500):
         NSE archives always give Price Return — trust them completely.
         No absolute range check so 2013 values (~8,000) and 2026 values (~60,000) both survive.
      3. For YF-sourced indices: trust YF for the tickers in YF_NSE_INDICES
         (YF gives correct PR for those). No filtering.

    The _scale_ok check in index_store.py handles TRI bleed during live incremental
    fetches. refetch_all.py should NOT destroy valid historical data.
    """
    if s.empty:
        return s

    # Rule 1: remove zero/negative
    bad = s <= 0
    if bad.any():
        print(f"    Removed {bad.sum()} zero/negative values for {name}")
        s = s[~bad]

    return s


# ══════════════════════════════════════════════════════════════════════════════
# BOND YIELD  (keep existing seeds + live CSV; add latest from Stooq/FRED)
# ══════════════════════════════════════════════════════════════════════════════

def _fetch_bond_stooq() -> pd.Series:
    """Try stooq.com for India 10Y bond yield (daily)."""
    for sym in ["10inbnd.b", "10yiny.b", "ind10y.b"]:
        url = f"https://stooq.com/q/d/l/?s={sym}&d1=20060101&d2={TODAY.strftime('%Y%m%d')}&i=d"
        try:
            r = requests.get(url, headers=HDRS, timeout=15)
            if r.status_code == 200 and "Close" in r.text:
                df = pd.read_csv(io.StringIO(r.text), parse_dates=["Date"])
                df = df.dropna(subset=["Close"])
                s = df.set_index("Date")["Close"]
                s.index = pd.to_datetime(s.index).normalize()
                s = s[~s.index.duplicated(keep="last")].sort_index()
                valid = s[(s >= 4) & (s <= 15)]
                if len(valid) > 200:
                    return valid
        except Exception:
            pass
    return pd.Series(dtype=float)

def update_bond_yield():
    """Refresh bond_daily_live.csv with any new values from Stooq."""
    live_path = ROOT / "data" / "live" / "bond_daily_live.csv"
    live = pd.Series(dtype=float)
    if live_path.exists():
        try:
            df = pd.read_csv(live_path, index_col=0, parse_dates=True)
            live = df["yield"].dropna()
        except Exception:
            pass

    fresh = _fetch_bond_stooq()
    if fresh.empty:
        print("  Bond yield: Stooq unavailable — keeping existing live CSV")
        return

    # Only add rows newer than what we have
    if not live.empty:
        fresh = fresh[fresh.index > live.index[-1]]

    if fresh.empty:
        print(f"  Bond yield: already up to date ({live.index[-1].date()})")
        return

    updated = pd.concat([live, fresh]).sort_index()
    updated = updated[~updated.index.duplicated(keep="last")]
    df_out = updated.reset_index()
    df_out.columns = ["date","yield"]
    df_out["date"] = pd.to_datetime(df_out["date"]).dt.strftime("%Y-%m-%d")
    df_out.to_csv(live_path, index=False)
    print(f"  Bond yield: +{len(fresh)} new rows → last={updated.index[-1].date()} ({updated.iloc[-1]:.3f}%)")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    sep = "=" * 70
    print(sep)
    print("  Data Refetch — Yahoo Finance primary, NSE Archives for Midcap/etc.")
    print(f"  Target: {TARGET_START} → {TODAY}")
    print(sep)

    # ── Step 1: Yahoo Finance NSE indices ──────────────────────────────────
    print("\n[1/4] Fetching NSE indices via Yahoo Finance ...")

    # Only re-fetch tickers that are missing today's data (incremental)
    today_ts = pd.Timestamp(TODAY)
    stale = {name: ticker for name, ticker in YF_NSE_INDICES.items()
             if (ex := load_csv(ticker)).empty
             or ex.index[-1] < today_ts - pd.Timedelta(days=5)}  # >5 days stale

    if not stale:
        print("  All YF indices already up to date ✓")
        yf_results = {name: load_csv(ticker) for name, ticker in YF_NSE_INDICES.items()}
    else:
        print(f"  Fetching {len(stale)} stale tickers (parallel) ...")
        yf_results = {name: load_csv(ticker) for name, ticker in YF_NSE_INDICES.items()}

        def _fetch_one_yf(name_ticker):
            name, ticker = name_ticker
            s = fetch_yf(ticker)
            return name, ticker, s

        with ThreadPoolExecutor(max_workers=6) as ex_pool:
            futures = {ex_pool.submit(_fetch_one_yf, nt): nt for nt in stale.items()}
            for f in as_completed(futures):
                name, ticker, s = f.result()
                if not s.empty:
                    s = validate_series(s, name)
                    existing = load_csv(ticker)
                    merged   = merge(existing, s)
                    save_csv(merged, ticker)
                    span = f"{merged.index[0].date()} → {merged.index[-1].date()}"
                    print(f"  ✅  {name:22s}  {ticker:15s}  {span}  ({len(merged)} rows)")
                    yf_results[name] = merged
                else:
                    print(f"  ❌  {name:22s}  {ticker:15s}  no data from Yahoo Finance")

    # ── Step 2: NSE Archives for Midcap / Smallcap / Next50 / Nifty500 ───
    print("\n[2/4] NSE Archives → Midcap 100, Smallcap 100, Next 50, Nifty 500 ...")
    arc_start = probe_archive_start()
    print(f"  Archives available from: {arc_start}")

    # ── 2a: Forward fill (incremental update to today) ────────────────────
    # Only fetch dates newer than what we already have — fast incremental update.
    latest_existing: dict[str, date] = {}
    for name, ticker in ARCHIVE_ONLY_INDICES.items():
        existing = load_csv(ticker)
        if existing.empty:
            latest_existing[name] = arc_start - timedelta(days=1)
        else:
            latest_existing[name] = existing.index[-1].date()

    min_last   = min(latest_existing.values())
    fetch_from = max(arc_start, min_last - timedelta(days=7))
    fetch_to   = TODAY

    days_to_fetch = len(weekdays(fetch_from, fetch_to))
    if days_to_fetch == 0:
        print(f"  All indices up to date ({fetch_to})")
    else:
        print(f"  Fetching {days_to_fetch} trading days ({fetch_from} → {fetch_to}) ...")
        arc_data = fetch_archive_range(
            start=fetch_from,
            end=fetch_to,
            names=set(ARCHIVE_ONLY_INDICES.keys()),
        )
        for name, ticker in ARCHIVE_ONLY_INDICES.items():
            new_data = arc_data.get(name, {})
            if new_data:
                new_s = pd.Series(new_data).sort_index()
                new_s = validate_series(new_s, name)
                existing = load_csv(ticker)
                merged = merge(existing, new_s)
                save_csv(merged, ticker)
                span = f"{merged.index[0].date()} → {merged.index[-1].date()}"
                print(f"  ✅  {name:22s}  {span}  ({len(merged)} rows)")
            else:
                print(f"  ⚠️  {name:22s}  0 new rows (may already be up to date)")

    # ── 2b: Backward fill (archive-only indices that don't reach arc_start) ─
    # Indices like Next50 / Nifty500 currently start 2013; archives go to 2011.
    # Fetch arc_start → each index's current earliest date so archives fill the gap.
    backward_needed = {
        name: ticker for name, ticker in ARCHIVE_ONLY_INDICES.items()
        if (ex := load_csv(ticker)) is not None
        and not ex.empty
        and ex.index[0].date() > arc_start + timedelta(days=30)
    }
    if backward_needed:
        print(f"\n  Backward fill: {len(backward_needed)} indices need data before "
              f"{list(backward_needed.keys())[0]}'s current start ...")
        # Find the overall earliest start we need to reach
        earliest_starts = {name: load_csv(ticker).index[0].date()
                           for name, ticker in backward_needed.items()}
        back_to = arc_start
        back_end = max(earliest_starts.values())  # fetch up to the latest "early start"

        days_back = len(weekdays(back_to, back_end))
        print(f"  Fetching {days_back} trading days ({back_to} → {back_end}) ...")
        arc_back = fetch_archive_range(
            start=back_to,
            end=back_end,
            names=set(backward_needed.keys()),
        )
        for name, ticker in backward_needed.items():
            new_data = arc_back.get(name, {})
            if new_data:
                new_s = pd.Series(new_data).sort_index()
                new_s = validate_series(new_s, name)
                existing = load_csv(ticker)
                merged = merge(new_s, existing)   # new_s fills early gap
                save_csv(merged, ticker)
                span = f"{merged.index[0].date()} → {merged.index[-1].date()}"
                print(f"  ✅  {name:22s}  {span}  ({len(merged)} rows)")
            else:
                print(f"  ⚠️  {name:22s}  0 rows from backward fill")

    # ── Step 2b: Fill internal gaps using NSE indicesHistory API ─────────
    # NSE archive fetches can be rate-blocked, leaving multi-month holes.
    # Use the NSE website indicesHistory API (monthly chunks) to fill gaps.
    # This returns correct PRICE RETURN data (not TRI).
    print("\n  Checking for internal gaps in archive-only indices ...")
    # Check ALL archive-only indices for gaps — API works for all of them
    _GAP_INDICES = {
        "Nifty Midcap 100":   "^NSMIDCP",
        "Nifty Smallcap 100": "^CNXSC",
        "Nifty Next 50":      "NIFTYNEXT50",
        "Nifty 500":          "NIFTY500",
    }
    for name, csv_ticker in _GAP_INDICES.items():
        fill_internal_gaps(csv_ticker, None, name, min_gap_days=10)

    # ── Step 3: niftyindices.com backfill ────────────────────────────────
    # niftyindices.com has data from index inception back to 2000+.
    # For each index whose earliest data is after TARGET_START, fetch the
    # FULL gap: 2006-01-01 → (current earliest date in CSV).
    # This fills both:
    #   (a) pre-archive era (2006 → 2011) for YF indices
    #   (b) 2011-2013 gap for archive-only indices (Next50, Nifty500, Smallcap)
    print(f"\n[3/4] niftyindices.com backfill (2006-01-01 → current earliest) ...")
    ni_ok, ni_err = test_niftyindices()
    print(f"  niftyindices.com: {'OK ✅' if ni_ok else f'UNAVAILABLE ❌  ({ni_err})'}")

    if ni_ok:
        for name, ticker in ALL_NSE.items():
            existing = load_csv(ticker)
            earliest = existing.index[0].date() if not existing.empty else TODAY
            if earliest <= TARGET_START:
                print(f"  {name}: already starts {earliest} ✓ skip")
                continue
            # Fetch from 2006-01-01 right up to the day before earliest known data
            # (slight overlap is fine — merge keeps existing data for overlapping dates)
            fetch_end = min(earliest + timedelta(days=30), TODAY)
            print(f"  {name}: backfilling {TARGET_START} → {fetch_end} "
                  f"(currently starts {earliest}) ...")
            ni_data = fetch_niftyindices(name, "2006-01-01", fetch_end.strftime("%Y-%m-%d"))
            if not ni_data.empty:
                ni_data = validate_series(ni_data, name)
                # Existing archive/YF data takes precedence for any overlap
                merged = merge(ni_data, existing)
                save_csv(merged, ticker)
                print(f"    ✅  +{len(ni_data)} rows  "
                      f"new start: {merged.index[0].date()}  "
                      f"({len(merged)} total rows)")
            else:
                print(f"    ❌  no data returned")
    else:
        print("  Skipping niftyindices backfill (will retry next run when site is up)")

        # ── Fallback A: Yahoo Finance direct (PR) for YF-compatible indices ──
        # These tickers return correct Price Return; just save what Step 1 already got.
        print("\n  [Fallback A] Yahoo Finance (PR) — Nifty 50, Bank, IT, Pharma, etc.")
        for name, ticker in YF_NSE_INDICES.items():
            existing = load_csv(ticker)
            yf_data  = yf_results.get(name, pd.Series(dtype=float))
            if not yf_data.empty and not existing.empty:
                if yf_data.index[0].date() < existing.index[0].date():
                    merged = merge(yf_data, existing)
                    save_csv(merged, ticker)
                    print(f"    {name}: {merged.index[0].date()} → {merged.index[-1].date()}  ({len(merged)} rows)")
                else:
                    print(f"    {name}: already at {existing.index[0].date()} ✓")
            else:
                print(f"    {name}: no YF data to add")

        # ── Fallback B: ratio-splice for archive-only TRI tickers ────────────
        # niftyindices is down, but YF has TRI data for ^NSMIDCP / ^CNXSC from 2007.
        # We scale the YF TRI series to match the official NSE PR level at arc_start,
        # giving approximate (but shape-correct) pre-archive values.
        #
        # NOTE: Returns computed wholly within the pre-2013 TRI period are slightly
        # elevated (~dividend yield ≈ 1-2%/yr) compared to true PR returns.
        # Once niftyindices.com is reachable, run refetch_all.py again to replace
        # with exact PR data.
        _TRI_YF_TICKERS = {
            "Nifty Midcap 100":   "^NSMIDCP",
            "Nifty Smallcap 100": "^CNXSC",
        }
        print("\n  [Fallback B] Ratio-splice: YF TRI → scaled PR for Midcap/Smallcap ...")
        for name, yf_ticker in _TRI_YF_TICKERS.items():
            csv_ticker = ARCHIVE_ONLY_INDICES[name]
            existing   = load_csv(csv_ticker)
            if existing.empty:
                print(f"    {name}: no archive data to splice onto — skip")
                continue
            arc_start_ts = existing.index[0]
            # Only do this if we're missing pre-archive data
            if existing.index[0].date() <= date(2007, 10, 1):
                print(f"    {name}: already starts {existing.index[0].date()} ✓")
                continue

            print(f"    {name}: fetching YF TRI ({yf_ticker}) for pre-archive period ...")
            tri_full = fetch_yf(yf_ticker, start="2006-01-01")
            if tri_full.empty:
                print(f"    {name}: YF returned no data")
                continue

            # Get the TRI value at (or just after) the archive start date
            tri_at_join_idx = tri_full.index[tri_full.index >= arc_start_ts]
            if tri_at_join_idx.empty:
                print(f"    {name}: YF data doesn't overlap with archive")
                continue
            tri_at_join = float(tri_full.loc[tri_at_join_idx[0]])
            pr_at_join  = float(existing.iloc[0])
            scale       = pr_at_join / tri_at_join

            pre_archive = tri_full[tri_full.index < arc_start_ts]
            if pre_archive.empty:
                print(f"    {name}: no YF data before archive start")
                continue

            scaled = (pre_archive * scale).round(2)
            scaled = validate_series(scaled, name)

            merged = merge(scaled, existing)    # archive PR data wins for overlapping dates
            save_csv(merged, csv_ticker)
            print(f"    ✅  {name}: {merged.index[0].date()} → {merged.index[-1].date()}  "
                  f"({len(merged)} rows, scale={scale:.4f}, "
                  f"pre-archive rows={len(scaled)} [~TRI-adjusted])")

    # ── Step 4: Global instruments (Sensex, Gold, Crude, etc.) ───────────
    print("\n[4/4] Fetching global instruments via Yahoo Finance ...")

    # Only re-fetch stale global tickers (>5 days behind today)
    stale_global = [(name, ticker, start) for name, ticker, start in GLOBAL_YF
                    if (ex2 := load_csv(ticker)).empty
                    or ex2.index[-1] < today_ts - pd.Timedelta(days=5)]

    if not stale_global:
        print("  All global instruments already up to date ✓")
    else:
        print(f"  Fetching {len(stale_global)} stale tickers (parallel) ...")

        def _fetch_global(item):
            name, ticker, start = item
            s = fetch_yf(ticker, start)
            return name, ticker, s

        with ThreadPoolExecutor(max_workers=6) as ex_g:
            futures = {ex_g.submit(_fetch_global, item): item for item in stale_global}
            for f in as_completed(futures):
                name, ticker, s = f.result()
                if not s.empty:
                    s_clean = s.dropna()
                    existing = load_csv(ticker)
                    merged   = merge(existing, s_clean)
                    save_csv(merged, ticker)
                    span = f"{merged.index[0].date()} → {merged.index[-1].date()}"
                    print(f"  ✅  {name:24s}  {span}  ({len(merged)} rows)")
                else:
                    print(f"  ❌  {name:24s}  no data from Yahoo Finance")

    # ── Step 5: Bond yield update ─────────────────────────────────────────
    print("\n[5/5] Updating bond yield ...")
    update_bond_yield()

    # ── Final status ──────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("  FINAL STATUS")
    print(f"{sep}")
    print("  NSE Indices:")
    for name, ticker in ALL_NSE.items():
        s = load_csv(ticker)
        if s.empty:
            print(f"  ❌  {name:30s}  no data")
        else:
            start_ok = s.index[0].date() <= date(2007, 9, 1)
            flag = "OK  " if start_ok else "WARN"
            note = "" if start_ok else f"  (starts {s.index[0].date()})"
            print(f"  {flag}  {name:30s}  {s.index[0].date()} → {s.index[-1].date()}  {len(s)} rows{note}")

    print("\n  Other instruments:")
    for name, ticker, _ in GLOBAL_YF:
        s = load_csv(ticker)
        if not s.empty:
            print(f"  OK    {name:28s}  {s.index[0].date()} → {s.index[-1].date()}  {len(s)} rows")
        else:
            print(f"  ❌    {name}")

    print(f"\n  Done. Restart your Streamlit app.\n{sep}")


if __name__ == "__main__":
    main()

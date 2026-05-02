"""
rebuild_clean.py — Wipe and rebuild ALL data from Yahoo Finance, year by year.

Run:
    python rebuild_clean.py

What it does:
  1. Deletes all existing CSV files in data/live/indices/ (except NIFTY500_SEED.csv)
  2. Deletes corrupted cache/nifty_close*.csv files
  3. Fetches every ticker from Yahoo Finance year by year (2000 → today)
  4. For archive-only NSE indices (Midcap/Smallcap/Next50/Nifty500),
     falls back to NSE archives + niftyindices.com (same as refetch_all.py)
  5. Saves clean data — NO spike correction applied

Estimated time: 5-15 minutes depending on network and rate limits.
"""

import sys, time, warnings
warnings.filterwarnings("ignore")
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd
import yfinance as yf

IDX_DIR   = ROOT / "data" / "live" / "indices"
CACHE_DIR = ROOT / "cache"
TODAY     = date.today()
START_YEAR = 2000

# ── Ticker definitions ─────────────────────────────────────────────────────────
# (display_name, yf_ticker, csv_filename_stem)
YF_TICKERS = [
    # NSE indices — Yahoo Finance gives correct PRICE RETURN
    ("Nifty 50",            "^NSEI",              "IDX_NSEI"),
    ("Sensex",              "^BSESN",             "IDX_BSESN"),
    ("Nifty Bank",          "^NSEBANK",           "IDX_NSEBANK"),
    ("Nifty IT",            "^CNXIT",             "IDX_CNXIT"),
    ("Nifty Pharma",        "^CNXPHARMA",         "IDX_CNXPHARMA"),
    ("Nifty Auto",          "^CNXAUTO",           "IDX_CNXAUTO"),
    ("Nifty FMCG",          "^CNXFMCG",           "IDX_CNXFMCG"),
    ("Nifty Metal",         "^CNXMETAL",          "IDX_CNXMETAL"),
    ("Nifty Energy",        "^CNXENERGY",         "IDX_CNXENERGY"),
    ("Nifty Realty",        "^CNXREALTY",         "IDX_CNXREALTY"),
    ("Nifty Midcap 100",    "NIFTY_MIDCAP_100.NS","NIFTY_MIDCAP_100.NS"),
    ("Nifty Smallcap 100",  "^CNXSC",             "IDX_CNXSC"),
    ("Nifty 500",           "^CRSLDX",            "NIFTY500"),
    # Global
    ("S&P 500",             "^GSPC",              "IDX_GSPC"),
    ("NASDAQ 100",          "^NDX",               "IDX_NDX"),
    ("US 10Y Yield",        "^TNX",               "IDX_TNX"),
    ("Gold (USD Futures)",  "GC=F",               "GC_F"),
    ("Crude Oil (WTI)",     "CL=F",               "CL_F"),
    ("USD/INR",             "USDINR=X",           "USDINR_X"),
    # ETFs
    ("Gold BeES",           "GOLDBEES.NS",        "GOLDBEES.NS"),
    ("Nifty BeES",          "NIFTYBEES.NS",       "NIFTYBEES.NS"),
    ("Junior BeES",         "JUNIORBEES.NS",      "JUNIORBEES.NS"),
    ("Bank BeES",           "BANKBEES.NS",        "BANKBEES.NS"),
    # Stocks
    ("HDFC Bank",           "HDFCBANK.NS",        "HDFCBANK.NS"),
    ("Reliance",            "RELIANCE.NS",        "RELIANCE.NS"),
    ("TCS",                 "TCS.NS",             "TCS.NS"),
    ("SBI",                 "SBIN.NS",            "SBIN.NS"),
    ("Sun Pharma",          "SUNPHARMA.NS",       "SUNPHARMA.NS"),
    ("Adani Ent.",          "ADANIENT.NS",        "ADANIENT.NS"),
    ("Power Grid",          "POWERGRID.NS",       "POWERGRID.NS"),
    ("20 Microns",          "20MICRONS.NS",       "20MICRONS.NS"),
    ("Asahi Songwon",       "ASAHISONG.NS",       "ASAHISONG.NS"),
    ("Rel. Infra.",         "RPOWER.NS",          "RPOWER.NS"),
    ("RIIL",                "RIIL.NS",            "RIIL.NS"),
    ("Rel. Chem.",          "RELCHEMQ.NS",        "RELCHEMQ.NS"),
]

SKIP_DELETE = {"NIFTY500_SEED.csv", "CL_F 2.csv"}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _clean(df_or_series) -> pd.Series:
    if isinstance(df_or_series, pd.DataFrame):
        col = "Close" if "Close" in df_or_series.columns else df_or_series.columns[0]
        s = df_or_series[col].dropna().squeeze()
    else:
        s = df_or_series.dropna().squeeze()
    if not isinstance(s, pd.Series):
        s = pd.Series([s])
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    return s[s > 0]


def fetch_yf_yearly(ticker: str, name: str) -> pd.Series:
    """Fetch ticker year by year from 2000 to today, return merged Series."""
    pieces: list[pd.Series] = []
    current_year = TODAY.year

    print(f"    Fetching {name} ({ticker}) year by year ...", flush=True)

    for year in range(START_YEAR, current_year + 1):
        start = f"{year}-01-01"
        end   = f"{year}-12-31" if year < current_year else str(TODAY)
        for attempt in range(3):
            try:
                df = yf.download(
                    ticker, start=start, end=end,
                    progress=False, auto_adjust=True,
                )
                if not df.empty:
                    s = _clean(df)
                    if not s.empty:
                        pieces.append(s)
                break
            except Exception as e:
                if attempt < 2:
                    time.sleep(2 + attempt * 3)
                else:
                    pass  # give up on this year
        time.sleep(0.4)  # polite delay between requests

    if not pieces:
        return pd.Series(dtype=float)

    merged = pd.concat(pieces)
    merged = merged[~merged.index.duplicated(keep="last")].sort_index()
    return merged


def save_csv(s: pd.Series, stem: str):
    path = IDX_DIR / f"{stem}.csv"
    df = s.reset_index()
    df.columns = ["date", "close"]
    df["date"]  = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    df.to_csv(path, index=False)


# ── Step 1: Delete old data ────────────────────────────────────────────────────

print("=" * 60)
print("  CLEAN REBUILD — Wiping and rebuilding all data")
print("=" * 60)

print("\n[1/3] Deleting old index/stock CSV files ...")
deleted = 0
for f in IDX_DIR.glob("*.csv"):
    if f.name in SKIP_DELETE:
        print(f"  KEEP  {f.name}")
        continue
    f.unlink()
    deleted += 1
print(f"  Deleted {deleted} files.")

# Delete corrupted cache files
print("\n  Deleting corrupted cache files ...")
for pattern in ["nifty_close*.csv"]:
    for f in CACHE_DIR.glob(pattern):
        f.unlink()
        print(f"  Deleted cache/{f.name}")


# ── Step 2: Year-by-year Yahoo Finance rebuild ────────────────────────────────

print(f"\n[2/3] Fetching all tickers from Yahoo Finance (year by year, {START_YEAR}→{TODAY}) ...")
print(f"  {len(YF_TICKERS)} tickers total. Estimated time: 5-10 min.\n")

ok_count = 0
fail_count = 0

for i, (name, ticker, stem) in enumerate(YF_TICKERS, 1):
    print(f"  [{i:02d}/{len(YF_TICKERS)}] {name} ...", flush=True)
    s = fetch_yf_yearly(ticker, name)
    if s.empty:
        print(f"          ❌  No data returned")
        fail_count += 1
    else:
        save_csv(s, stem)
        print(f"          ✅  {s.index[0].date()} → {s.index[-1].date()}  ({len(s)} rows)")
        ok_count += 1
    print()


# ── Step 3: niftyindices.com backfill for pre-2013 NSE data ──────────────────
# For indices where Yahoo Finance may not have data back to 2000, try niftyindices.com
print("\n[3/3] Trying niftyindices.com backfill for any index starting after 2007 ...")

NI_TARGETS = {
    "Nifty 50":            ("IDX_NSEI",             "NIFTY 50"),
    "Nifty Bank":          ("IDX_NSEBANK",           "NIFTY BANK"),
    "Nifty IT":            ("IDX_CNXIT",             "NIFTY IT"),
    "Nifty Pharma":        ("IDX_CNXPHARMA",         "NIFTY PHARMA"),
    "Nifty Auto":          ("IDX_CNXAUTO",           "NIFTY AUTO"),
    "Nifty FMCG":          ("IDX_CNXFMCG",           "NIFTY FMCG"),
    "Nifty Metal":         ("IDX_CNXMETAL",           "NIFTY METAL"),
    "Nifty Energy":        ("IDX_CNXENERGY",          "NIFTY ENERGY"),
    "Nifty Realty":        ("IDX_CNXREALTY",          "NIFTY REALTY"),
    "Nifty Midcap 100":    ("NIFTY_MIDCAP_100.NS",    "NIFTY MIDCAP 100"),
    "Nifty Smallcap 100":  ("IDX_CNXSC",              "NIFTY SMALLCAP 100"),
    "Nifty 500":           ("NIFTY500",               "NIFTY 500"),
}

import json, requests as req

_NI_HDRS = {
    "User-Agent": "Mozilla/5.0",
    "Content-Type": "application/json",
    "Referer": "https://www.niftyindices.com/",
    "Origin":  "https://www.niftyindices.com",
}
_NI_URL = "https://www.niftyindices.com/Backpage.aspx/getHistoricalData"

def _test_ni() -> bool:
    try:
        r = req.post(_NI_URL,
                     json={"name":"NIFTY 50","startDate":"01-Jan-2025","endDate":"31-Jan-2025"},
                     headers=_NI_HDRS, timeout=20)
        return r.status_code == 200 and bool(json.loads(r.json().get("d","[]")))
    except Exception:
        return False

def _fetch_ni(ni_name: str, start_str: str, end_str: str) -> pd.Series:
    """Fetch from niftyindices.com in 2-year chunks."""
    start_dt = pd.Timestamp(start_str)
    end_dt   = pd.Timestamp(end_str)
    rows = []
    chunk = start_dt
    while chunk <= end_dt:
        chunk_end = min(chunk + pd.DateOffset(years=2) - pd.DateOffset(days=1), end_dt)
        payload = {"name": ni_name,
                   "startDate": chunk.strftime("%d-%b-%Y"),
                   "endDate":   chunk_end.strftime("%d-%b-%Y")}
        for attempt in range(4):
            try:
                r = req.post(_NI_URL, json=payload, headers=_NI_HDRS, timeout=60)
                if r.status_code == 200:
                    rows.extend(json.loads(r.json().get("d", "[]")))
                    break
            except Exception:
                pass
            time.sleep(10 * (attempt + 1))
        chunk = chunk_end + pd.DateOffset(days=1)
        time.sleep(1)

    result = {}
    for row in rows:
        try:
            ts = pd.Timestamp(str(row.get("TIMESTAMP","")).strip()).normalize()
            v  = float(str(row.get("CLOSING_INDEX_VAL","")).replace(",",""))
            if v > 0:
                result[ts] = v
        except Exception:
            pass
    return pd.Series(result).sort_index() if result else pd.Series(dtype=float)

ni_ok = _test_ni()
print(f"  niftyindices.com: {'reachable ✅' if ni_ok else 'unreachable ❌  (skipping backfill)'}")

if ni_ok:
    for display_name, (stem, ni_name) in NI_TARGETS.items():
        path = IDX_DIR / f"{stem}.csv"
        if not path.exists():
            print(f"  {display_name}: CSV not found, skip")
            continue
        try:
            existing = pd.read_csv(path, index_col=0, parse_dates=True).iloc[:,0].dropna()
            existing.index = pd.to_datetime(existing.index).normalize()
            if existing.empty:
                continue
            earliest = existing.index[0].date()
            if earliest <= date(2000, 12, 31):
                print(f"  {display_name}: starts {earliest} ✓ no backfill needed")
                continue

            fill_end = (existing.index[0] + pd.Timedelta(days=30)).date()
            print(f"  {display_name}: backfilling 2000-01-01 → {fill_end} (currently starts {earliest}) ...")
            ni_data = _fetch_ni(ni_name, "2000-01-01", str(fill_end))
            if not ni_data.empty:
                ni_data = ni_data[ni_data > 0]
                merged = pd.concat([ni_data, existing])
                merged = merged[~merged.index.duplicated(keep="last")].sort_index()
                save_csv(merged, stem)
                print(f"    ✅  +{len(ni_data)} rows  new start: {merged.index[0].date()}  ({len(merged)} total)")
            else:
                print(f"    ❌  no data returned from niftyindices.com")
        except Exception as e:
            print(f"    ❌  error: {e}")


# ── Final status ───────────────────────────────────────────────────────────────

print(f"\n{'='*60}")
print("  FINAL STATUS")
print(f"{'='*60}")
for _, _, stem in YF_TICKERS:
    path = IDX_DIR / f"{stem}.csv"
    if path.exists():
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            s  = df.iloc[:,0].dropna()
            print(f"  ✅  {stem:30s}  {s.index[0].date()} → {s.index[-1].date()}  ({len(s)} rows)")
        except Exception:
            print(f"  ❌  {stem:30s}  (read error)")
    else:
        print(f"  ❌  {stem:30s}  (no file)")

print(f"\n  Successful: {ok_count}/{len(YF_TICKERS)}   Failed: {fail_count}")
print(f"\n  Done. Restart your Streamlit app.\n{'='*60}")

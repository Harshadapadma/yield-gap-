"""
rebuild_smallcap_nselib.py
─────────────────────────────────────────────────────────────────────────────
Use the `nselib` Python library (https://github.com/RuchiTanmay/nselib —
158 stars, actively maintained) to fetch full Nifty Smallcap 100 history.

`nselib.capital_market.index_data()` hits NSE's `/api/historical/indicesHistory`
endpoint, which is DIFFERENT from:
  - openchart (uses charting.nseindia.com)
  - archive scrape (uses archives.nseindia.com)
  - niftyindices.com (different domain entirely)

So it has a separate rate-limit + bot-protection policy.  When the others fail,
this often works.

Run:
    pip install nselib
    python rebuild_smallcap_nselib.py
"""

import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd

OUT_PATH = ROOT / "data" / "live" / "indices" / "IDX_CNXSC.csv"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

print("=" * 64)
print("  Rebuild Nifty Smallcap 100 via nselib")
print("=" * 64)

# ── Auto-install nselib ────────────────────────────────────────────────────
try:
    from nselib import capital_market
except ImportError:
    print("\nnselib not installed.  Installing now…")
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "nselib"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"❌ pip install failed:\n{result.stderr}")
        sys.exit(1)
    print("✅ nselib installed.")
    from nselib import capital_market


# ── Fetch in 1-year chunks (nselib's API has a year-range limit) ───────────
TODAY = date.today()
TARGET_START = date(2004, 4, 1)   # Nifty Smallcap 100 base date

# nselib accepts dates as 'dd-mm-YYYY' strings
def _fmt(d: date) -> str:
    return d.strftime("%d-%m-%Y")

# Try several name variants — for pre-2016 data we ALSO need to try the
# OLD name "CNX Smallcap" / "CNX Smallcap 100" because NSE renamed many
# indices in April 2016 (CNX → Nifty).
NAME_CANDIDATES_NEW = [   # post-April-2016 naming
    "NIFTY SMALLCAP 100",
    "NIFTY SMLCAP 100",
    "Nifty Smallcap 100",
    "NIFTY SMALL CAP 100",
]
NAME_CANDIDATES_OLD = [   # pre-April-2016 naming (CNX prefix)
    "CNX SMALLCAP",
    "CNX Smallcap",
    "CNX SMALLCAP 100",
    "CNX NIFTY SMALLCAP",
    "S&P CNX SMALLCAP",
]

def _probe(name: str, start_d: date, end_d: date):
    """Return (rows, df) for a symbol name + date range, or (0, None)."""
    try:
        df = capital_market.index_data(
            index=name,
            from_date=_fmt(start_d),
            to_date=_fmt(end_d),
        )
        if df is not None and not df.empty:
            return len(df), df
    except Exception:
        pass
    return 0, None

target_index_new = None
target_index_old = None

# Probe 1: recent dates → find the post-2016 name
print("\nProbing post-2016 name (against last 30 days)…")
probe_start = TODAY - timedelta(days=30)
for cand in NAME_CANDIDATES_NEW:
    n, _ = _probe(cand, probe_start, TODAY)
    if n > 0:
        print(f"  ✅ '{cand}' returned {n} rows")
        target_index_new = cand
        break
    print(f"  ❌ '{cand}'")

# Probe 2: 2010 dates → find the pre-2016 name
print("\nProbing pre-2016 name (against year 2010)…")
probe_old_start = date(2010, 1, 1)
probe_old_end   = date(2010, 3, 31)
for cand in NAME_CANDIDATES_OLD:
    n, _ = _probe(cand, probe_old_start, probe_old_end)
    if n > 0:
        print(f"  ✅ '{cand}' returned {n} rows in Q1 2010")
        target_index_old = cand
        break
    print(f"  ❌ '{cand}'")

if not target_index_new and not target_index_old:
    print("\n❌ No symbol name worked.")
    sys.exit(1)

print(f"\nUsing names — new: {target_index_new!r}   old: {target_index_old!r}")


# ── Full-history fetch in 90-day chunks ────────────────────────────────────
# nselib's index_data() is pagination-capped at ~70 rows per request.
# 90-day chunks → ~70 rows × 4 chunks/year = ~280 rows/year (full coverage).
# For chunks BEFORE April 2016 we use the OLD index name "CNX SMALLCAP"
# (that's what NSE called it before they renamed to "Nifty Smallcap 100").
import time
CHUNK_DAYS = 90
RENAME_DATE = date(2016, 4, 1)   # NSE renamed CNX → Nifty on this date

def _name_for_chunk(chunk_end: date) -> str | None:
    if chunk_end < RENAME_DATE:
        return target_index_old
    return target_index_new

all_chunks = []
chunk_start = TARGET_START
chunk_n = 0
empty_streak = 0
while chunk_start <= TODAY:
    chunk_end = min(chunk_start + timedelta(days=CHUNK_DAYS - 1), TODAY)
    chunk_n += 1

    use_name = _name_for_chunk(chunk_end)
    if not use_name:
        # No working name for this date range — skip
        chunk_start = chunk_end + timedelta(days=1)
        continue

    # Retry once on empty result — could be transient rate-limit
    df = None
    last_err = None
    for attempt in range(2):
        try:
            df = capital_market.index_data(
                index=use_name,
                from_date=_fmt(chunk_start),
                to_date=_fmt(chunk_end),
            )
            if df is not None and not df.empty:
                break
        except Exception as exc:
            last_err = exc
            df = None
        if attempt == 0:
            time.sleep(1.0)

    if df is not None and not df.empty:
        all_chunks.append(df)
        empty_streak = 0
        if chunk_n % 5 == 0 or chunk_n <= 3:
            print(f"  [{chunk_n:3d}] {chunk_start} → {chunk_end} [{use_name[:18]}]: {len(df):,} rows  (running: {sum(len(c) for c in all_chunks):,})")
    else:
        empty_streak += 1
        if chunk_n <= 5 or chunk_n % 10 == 0:
            tag = "ERR" if last_err else "empty"
            print(f"  [{chunk_n:3d}] {chunk_start} → {chunk_end} [{use_name[:18]}]: {tag}")
        # If 8 consecutive empties AND we've already passed 2010,
        # the OLD endpoint isn't returning data either — give up on pre-2016.
        if empty_streak >= 8 and chunk_start.year < 2016:
            print(f"  → 8 consecutive empty chunks at year {chunk_start.year}, skipping to April 2016")
            chunk_start = date(2016, 4, 1)
            empty_streak = 0
            continue

    chunk_start = chunk_end + timedelta(days=1)
    time.sleep(0.3)

print(f"\nTotal raw rows fetched: {sum(len(c) for c in all_chunks):,} from {len(all_chunks)} chunks")


# ── Merge ──────────────────────────────────────────────────────────────────
if not all_chunks:
    print("\n❌ nselib returned no data for any chunk.")
    print("   Either NSE is blocking your IP for ALL endpoints, or the symbol name is wrong.")
    sys.exit(1)

combined = pd.concat(all_chunks).reset_index(drop=True)

# nselib returns columns like: HistoricalDate, OPEN, HIGH, LOW, CLOSE
# (column names vary by version — find the close + date columns flexibly)
date_col = next((c for c in combined.columns
                 if 'date' in c.lower() or 'timestamp' in c.lower()), None)
close_col = next((c for c in combined.columns
                  if c.upper() == 'CLOSE' or 'close' in c.lower()), None)

if not date_col or not close_col:
    print(f"❌ couldn't find date/close columns.  available: {list(combined.columns)}")
    sys.exit(1)

print(f"\nUsing columns: date={date_col!r}, close={close_col!r}")

# Build clean Series
combined[date_col]  = pd.to_datetime(combined[date_col], errors='coerce')
combined[close_col] = pd.to_numeric(
    combined[close_col].astype(str).str.replace(",", ""),
    errors='coerce',
)
combined = combined.dropna(subset=[date_col, close_col])
s = combined.set_index(date_col)[close_col].sort_index()
s = s[~s.index.duplicated(keep='last')]
s.index = s.index.tz_localize(None).normalize()

# Sanity filter — pre-2016 CNX Smallcap had different base, so wider band
s = s[(s >= 100) & (s <= 25000)]

# Merge with whatever's already in the CSV (so we keep the 2,501 rows
# from the previous run even if the old-name fetch returned nothing).
if OUT_PATH.exists() and OUT_PATH.stat().st_size > 32:
    try:
        existing = pd.read_csv(OUT_PATH, parse_dates=['date']).set_index('date')['close']
        existing.index = pd.to_datetime(existing.index).tz_localize(None).normalize()
        before_n = len(s)
        s = pd.concat([existing, s])
        s = s[~s.index.duplicated(keep='last')].sort_index()
        print(f"\n  merged with existing CSV: {before_n} new + {len(existing)} existing → {len(s)} unique")
    except Exception as exc:
        print(f"  could not merge with existing: {exc}")

print(f"\n✅ Final merged series: {len(s):,} rows  "
      f"({s.index[0].date()} → {s.index[-1].date()})")


# ── Write to CSV ───────────────────────────────────────────────────────────
df_out = s.reset_index()
df_out.columns = ['date', 'close']
df_out['date'] = pd.to_datetime(df_out['date']).dt.strftime('%Y-%m-%d')
df_out.to_csv(OUT_PATH, index=False)
print(f"✅ wrote {len(df_out):,} rows to {OUT_PATH}")

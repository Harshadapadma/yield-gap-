"""
rebuild_smallcap_openchart.py
─────────────────────────────────────────────────────────────────────────────
Fetch Nifty Smallcap 100 full history (2004 → today) via the `openchart`
Python library, which hits NSE's charting platform API.

WHY THIS WORKS WHEN OTHER SOURCES DON'T:
- Yahoo dropped ^CNXSC and ^CNXSMALL (delisted)
- niftyindices.com API returns 500 to non-browser clients
- NSE archives currently 403 the user's IP
- stooq paywalled
- `openchart` uses NSE's charting endpoint (charting.nseindia.com) which is
  a DIFFERENT API than the archive CSVs — separate rate-limit + bot-protection
  policy, often accessible when the others aren't.

Installation:
    pip install openchart

Run:
    python rebuild_smallcap_openchart.py
"""

import sys
import warnings
from datetime import datetime, date, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd

OUT_PATH = ROOT / "data" / "live" / "indices" / "IDX_CNXSC.csv"
OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

print("=" * 64)
print("  Rebuild Nifty Smallcap 100 via openchart (NSE charting API)")
print("=" * 64)

# ── Install / import openchart ─────────────────────────────────────────────
try:
    from openchart import NSEData
except ImportError:
    print("\nopenchart not installed.  Installing now…")
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet", "openchart"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"❌ pip install failed:\n{result.stderr}")
        sys.exit(1)
    print("✅ openchart installed.")
    from openchart import NSEData


# ── Find the right symbol ──────────────────────────────────────────────────
nse = NSEData()
print("\nSearching NSE charting catalogue for 'SMALLCAP'…")
try:
    candidates = nse.search('SMALLCAP', 'IDX')
    print(candidates.to_string(index=False))
except Exception as exc:
    print(f"⚠️  search failed: {exc}")
    candidates = pd.DataFrame()

# Pick the Smallcap 100 row.
# NSE's charting catalogue uses "NIFTY SMLCAP 100" (abbreviated SMLCAP) —
# matching on description ("NIFTY SMALLCAP 100") is more reliable than symbol.
target_symbol = None
if not candidates.empty:
    for _, row in candidates.iterrows():
        sym  = str(row.get('symbol', '')).upper()
        desc = str(row.get('description', '')).upper()
        if ('SMLCAP 100' in sym
            or 'SMALLCAP 100' in sym
            or 'SMALLCAP 100' in desc
            or 'SMLCAP 100' in desc):
            target_symbol = row['symbol']
            print(f"  matched row: symbol={row['symbol']!r}  desc={row['description']!r}  scripcode={row['scripcode']!r}")
            break
    # Hard-coded scripcode fallback — Nifty Smallcap 100 = 26019 always
    if target_symbol is None:
        match = candidates[candidates['scripcode'].astype(str) == '26019']
        if not match.empty:
            target_symbol = match.iloc[0]['symbol']

if not target_symbol:
    target_symbol = 'NIFTY SMLCAP 100'   # NSE's actual symbol string

print(f"\nUsing symbol: {target_symbol!r}")


# ── Fetch full history in 5-year chunks ────────────────────────────────────
TODAY = date.today()
TARGET_START = date(2004, 4, 1)    # Nifty Smallcap 100 base date
CHUNK_YEARS = 5                     # NSE charting endpoint comfortable with this

all_chunks = []
chunk_start = TARGET_START
while chunk_start <= TODAY:
    chunk_end = min(date(chunk_start.year + CHUNK_YEARS, chunk_start.month,
                         chunk_start.day), TODAY)
    print(f"\n  fetching {chunk_start} → {chunk_end}…")
    try:
        df = nse.historical(
            target_symbol, 'IDX',
            datetime.combine(chunk_start, datetime.min.time()),
            datetime.combine(chunk_end, datetime.min.time()),
            '1d',
        )
        if df is not None and not df.empty:
            print(f"    got {len(df):,} rows")
            all_chunks.append(df)
        else:
            print(f"    empty result")
    except Exception as exc:
        print(f"    error: {type(exc).__name__}: {exc}")
    chunk_start = chunk_end + timedelta(days=1)


# ── Merge chunks ───────────────────────────────────────────────────────────
if not all_chunks:
    print("\n❌ openchart returned no data for any chunk.")
    print("   The NSE charting API may be down or blocking your IP.")
    print("   Try again from a phone hotspot / VPN.")
    sys.exit(1)

combined = pd.concat(all_chunks).sort_index()
combined = combined[~combined.index.duplicated(keep="last")]

# openchart returns a DataFrame with columns: Open, High, Low, Close, Volume
close_col = next((c for c in combined.columns if 'close' in c.lower()), None)
if not close_col:
    print(f"❌ no 'Close' column.  columns = {list(combined.columns)}")
    sys.exit(1)

s = combined[close_col].dropna()
s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
s = s[~s.index.duplicated(keep="last")].sort_index()

# Sanity: filter out impossible values
s = s[(s >= 1500) & (s <= 25000)]

print(f"\n✅ Final merged series: {len(s):,} rows  "
      f"({s.index[0].date()} → {s.index[-1].date()})")


# ── Write to CSV ───────────────────────────────────────────────────────────
df_out = s.reset_index()
df_out.columns = ['date', 'close']
df_out['date'] = pd.to_datetime(df_out['date']).dt.strftime('%Y-%m-%d')
df_out.to_csv(OUT_PATH, index=False)
print(f"✅ wrote {len(df_out):,} rows to {OUT_PATH}")
print(f"\nReload the Return Spread page in the browser. "
      f"Smallcap should now show full history.")

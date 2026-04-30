"""
scripts/update_live_data.py
────────────────────────────
Run by GitHub Actions every weekday after market close.
Fetches today's bond yield + Nifty 50 PE and appends them to the
data/live/ CSV files so Streamlit Cloud always has up-to-date data.
"""

import sys, os
from pathlib import Path
from datetime import date

# Make sure imports resolve from project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

TODAY = date.today()
print(f"\n{'='*55}")
print(f"  Daily data update — {TODAY}")
print(f"{'='*55}\n")

# ── 1. Bond Yield ─────────────────────────────────────────────
print("[ Bond Yield ]")
try:
    from data.fetcher import fetch_bond_yield
    series, status = fetch_bond_yield()
    print(f"  {status['message']}")
    latest = series.index[-1].date()
    val    = float(series.iloc[-1])
    print(f"  Latest: {latest} = {val:.3f}%")
    if latest < TODAY:
        print(f"  ⚠️  Could not get today's value (got {latest})")
    else:
        print(f"  ✅ Today's value saved")
except Exception as exc:
    print(f"  ❌ Failed: {exc}")

print()

# ── 2. Nifty 50 PE ───────────────────────────────────────────
print("[ Nifty 50 PE ]")
try:
    from data.fetcher import fetch_pe_history
    series, status = fetch_pe_history()
    print(f"  {status['message']}")
    # Filter out sentinel -1 values
    valid  = series[series > 0]
    latest = valid.index[-1].date()
    val    = float(valid.iloc[-1])
    print(f"  Latest valid: {latest} = {val:.2f}")
    if latest < TODAY:
        print(f"  ⚠️  Could not get today's PE (got {latest}) — NSE may not have published yet")
    else:
        print(f"  ✅ Today's PE saved")
except Exception as exc:
    print(f"  ❌ Failed: {exc}")

print()

# ── 3. Nifty 50 price (via yfinance — always works) ──────────
print("[ Nifty 50 Price ]")
try:
    from data.fetcher import fetch_nifty
    series, status = fetch_nifty(period="5d")
    print(f"  {status['message']}")
except Exception as exc:
    print(f"  ❌ Failed: {exc}")

print()
print("Done.")

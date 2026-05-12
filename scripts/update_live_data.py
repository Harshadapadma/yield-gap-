"""
scripts/update_live_data.py
Run by GitHub Actions daily to refresh bond yield, Nifty PE, and Nifty close.
Writes to:
  data/live/bond_daily_live.csv
  data/live/nifty_pe_history.csv
  cache/nifty_close.csv
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.fetcher import fetch_bond_yield, fetch_nifty, fetch_pe_history

print("\n=== Daily Live Data Update ===\n")

print("[1/3] Bond yield (India 10Y G-sec)...")
try:
    bond, status = fetch_bond_yield(start_date="2000-01-01")
    msg = status.get("message", str(status)) if isinstance(status, dict) else str(status)
    print(f"  OK  rows={len(bond)}  {msg}")
except Exception as e:
    print(f"  FAILED: {e}")

print("[2/3] Nifty 50 close price...")
try:
    nifty, status = fetch_nifty()
    msg = status.get("message", str(status)) if isinstance(status, dict) else str(status)
    print(f"  OK  rows={len(nifty)}  {msg}")
except Exception as e:
    print(f"  FAILED: {e}")

print("[3/3] Nifty PE history...")
try:
    pe, status = fetch_pe_history(start_date="2000-01-01")
    msg = status.get("message", str(status)) if isinstance(status, dict) else str(status)
    print(f"  OK  rows={len(pe)}  {msg}")
except Exception as e:
    print(f"  FAILED: {e}")

print("\nDone.")

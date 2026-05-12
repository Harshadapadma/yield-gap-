"""
scripts/cache_breadth_prices.py
Run by GitHub Actions to incrementally update all Nifty 500 stock price CSVs.
Writes to: cache/breadth/prices/<ticker>.csv

Nifty 500 is the largest universe — caching it covers all smaller universes
(Nifty 50, 100, 200, Midcap 150, Smallcap 250, Next 50).
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.breadth_fetcher import tickers_for_universe, fetch_prices_batch

print("\n=== Breadth Stock Price Cache Update ===\n")

tickers = tickers_for_universe("Nifty 500")
if not tickers:
    print("ERROR: Could not load Nifty 500 constituent list. NSE may be unreachable.")
    sys.exit(1)

print(f"Updating prices for {len(tickers)} stocks (incremental — skips stocks already up to date)...\n")

def _progress(done: int, total: int, ticker: str) -> None:
    if done % 50 == 0 or done == total:
        print(f"  {done:>3}/{total}  {ticker}")

prices_df = fetch_prices_batch(tickers, progress_cb=_progress)

ok = prices_df.shape[1] if not prices_df.empty else 0
print(f"\nDone. {ok}/{len(tickers)} stocks have price data.")

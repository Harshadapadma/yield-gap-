"""
scripts/compute_breadth.py
Run by GitHub Actions after cache_breadth_prices.py.
Computes the breadth series (Nifty 500 vs Nifty 50, 1Y window)
and saves the result to cache/breadth/breadth_result.csv so the
app can load it instantly without reprocessing 459 CSVs.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd
from data.breadth_fetcher import (
    tickers_for_universe,
    fetch_prices_batch,
    fetch_single_price,
    compute_breadth_series,
    BENCHMARK_CATALOG,
)

OUT = ROOT / "cache" / "breadth" / "breadth_result.csv"

print("\n=== Computing Breadth Series ===\n")

bench_ticker = BENCHMARK_CATALOG["Nifty 50"]["ticker"]
print(f"Loading benchmark ({bench_ticker})…")
bench = fetch_single_price(bench_ticker)
if bench.empty:
    print("ERROR: Could not load benchmark price series.")
    sys.exit(1)
print(f"  Benchmark: {len(bench):,} days  ({bench.index[0].date()} → {bench.index[-1].date()})")

print("Loading Nifty 500 constituent list…")
tickers = tickers_for_universe("Nifty 500")
if not tickers:
    print("ERROR: Could not load constituent list.")
    sys.exit(1)
print(f"  {len(tickers)} tickers")

print("Reading price CSVs from cache (no network calls)…")
prices_df = fetch_prices_batch(tickers, cache_only=True)
if prices_df.empty:
    print("ERROR: No price data in cache.")
    sys.exit(1)
print(f"  Loaded {prices_df.shape[1]} stocks × {len(prices_df):,} days")

print("Computing breadth series…")
breadth_df = compute_breadth_series(
    prices_df,
    bench,
    window_days=252,
    min_coverage=0.20,
    freq="BME",
)
if breadth_df.empty:
    print("ERROR: Breadth computation returned empty result.")
    sys.exit(1)

OUT.parent.mkdir(parents=True, exist_ok=True)
breadth_df.to_csv(OUT)
print(f"\nSaved {len(breadth_df)} rows → {OUT.relative_to(ROOT)}")
print(f"Date range: {breadth_df.index[0].date()} → {breadth_df.index[-1].date()}")
print("\nDone.")

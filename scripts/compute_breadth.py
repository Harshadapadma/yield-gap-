"""
scripts/compute_breadth.py
Run by GitHub Actions after cache_breadth_prices.py.
Computes the breadth series + stock snapshot and saves both to cache/breadth/.
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
    get_latest_snapshot,
    BENCHMARK_CATALOG,
)

BREADTH_OUT  = ROOT / "cache" / "breadth" / "breadth_result.csv"
SNAPSHOT_OUT = ROOT / "cache" / "breadth" / "snapshot_result.csv"

print("\n=== Computing Breadth Series + Snapshot ===\n")

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

BREADTH_OUT.parent.mkdir(parents=True, exist_ok=True)
breadth_df.to_csv(BREADTH_OUT)
print(f"Saved {len(breadth_df)} breadth rows → {BREADTH_OUT.relative_to(ROOT)}")
print(f"  Date range: {breadth_df.index[0].date()} → {breadth_df.index[-1].date()}")

print("Computing stock snapshot…")
snapshot_df, bench_ret = get_latest_snapshot(prices_df, bench, 252, "Nifty 500")
if snapshot_df is not None and not snapshot_df.empty:
    snapshot_df.to_csv(SNAPSHOT_OUT, index=False)
    print(f"Saved {len(snapshot_df)} stocks → {SNAPSHOT_OUT.relative_to(ROOT)}")
    if bench_ret is not None:
        # Save bench_ret as first line comment so app can read it
        with open(SNAPSHOT_OUT, "r") as f:
            content = f.read()
        with open(SNAPSHOT_OUT, "w") as f:
            f.write(f"# bench_ret={bench_ret:.6f}\n" + content)
else:
    print("WARNING: Snapshot empty, skipping.")

print("\nDone.")

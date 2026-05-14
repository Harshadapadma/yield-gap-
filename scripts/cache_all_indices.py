"""
scripts/cache_all_indices.py
─────────────────────────────
Run on your local machine to pre-populate all index price CSVs.
These CSVs are committed to git so Streamlit Cloud always has full
data on deploy — no downloads needed.

Usage:
    cd <project root>
    python scripts/cache_all_indices.py

After running:
    git add data/live/indices/
    git commit -m "chore: pre-cache all index price history"
    git push
"""

import sys, time, warnings
warnings.filterwarnings("ignore")
from pathlib import Path
from datetime import date, timedelta, datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd

from data.index_store import (
    INSTRUMENTS, _TICKER_TO_NSE_NAME, _NSE_HIST_INDEX_TYPE,
    _csv_path, _load_csv, _save_csv,
    _fetch_yfinance, _fix_consolidation_spikes,
    _fetch_from_niftyindices, _fetch_nse_archive_days,
)

print("\n" + "=" * 60)
print("  Index Price Pre-Cache Script")
print(f"  {date.today()}")
print("=" * 60)


# ── Helper: fetch full yfinance history, force-refresh if data is old ─────────
def _yf_full(ticker: str, start: str = "2006-01-01") -> pd.Series:
    """Download full yfinance history from start → today."""
    end = str(date.today() + timedelta(days=1))
    s = _fetch_yfinance(ticker, start, end)
    if not s.empty:
        s = _fix_consolidation_spikes(s)
    return s


# ── Step 1: NSE archive — only useful for incremental (last ~6 months) ────────
# NSE archives publicly retain only recent files, so we skip the bulk
# historical fetch and rely on yfinance for the full history.
# The NSE archive is used at runtime (in get_price) for daily incremental updates.

print("\n[1/2] Fetching Indian index prices via yfinance (full history)…")
print("      (NSE archive used at runtime for daily incremental updates)\n")

# yfinance tickers for all NSE-archive-sourced Indian indices
INDIAN_INDICES = {
    "Nifty 50":           "^NSEI",
    "Nifty Bank":         "^NSEBANK",
    "Nifty Midcap 100":   "^NSMIDCP",
    # Use ^CNXSC (the INSTRUMENTS key) so the saved CSV matches what the runtime reads.
    # _fetch_yfinance internally maps ^CNXSC → ^CNXSMALL; niftyindices is the fallback.
    "Nifty Smallcap 100": "^CNXSC",
    "Nifty IT":           "^CNXIT",
    "Nifty Pharma":       "^CNXPHARMA",
    "Nifty Auto":         "^CNXAUTO",
    "Nifty FMCG":         "^CNXFMCG",
    "Nifty Metal":        "^CNXMETAL",
    "Nifty Energy":       "^CNXENERGY",
    "Nifty Realty":       "^CNXREALTY",
}

# Alternative tickers to try when the primary fails
ALTERNATIVES = {
    "^CNXSC":     ["SETFNN50.NS"],                 # Smallcap 100 alternatives (^CNXSMALL used internally by _fetch_yfinance)
    "^CNXENERGY": ["^CNXINFRA", "ENERGYBEES.NS"],  # Energy alternatives
    "^CNXREALTY": ["ITREIT.NS"],                    # Realty alternatives
}

# Minimum acceptable rows — if below this, try alternatives
MIN_ROWS = 500

saved, skipped, failed = [], [], []

for name, ticker in INDIAN_INDICES.items():
    path     = _csv_path(ticker)
    existing = _load_csv(path)
    today    = date.today()

    # ── Already have recent, complete data? ────────────────────────────────────
    if not existing.empty:
        last_d     = existing.index[-1].date()
        n_rows     = len(existing)
        # Check for internal gaps > 20 days
        diffs      = existing.index.to_series().diff().dt.days
        has_gap    = bool((diffs > 20).any())
        is_current = last_d >= today

        # Ignore gaps that are entirely older than 3 years — can't be patched.
        gap_cutoff_early = date.today() - timedelta(days=365 * 3)
        only_old_gaps = has_gap and not bool(
            (diffs > 20)[diffs.index >= pd.Timestamp(gap_cutoff_early)].any()
        )
        if is_current and n_rows >= MIN_ROWS and (not has_gap or only_old_gaps):
            print(f"  ✅ {name:30s}  already good ({existing.index[0].date()} → {last_d}, {n_rows} rows)")
            saved.append(name)
            continue

        if has_gap and not only_old_gaps:
            print(f"  🔧 {name:30s}  has gap — forcing fresh download…")
        elif not is_current:
            print(f"  🔧 {name:30s}  stale (last: {last_d}) — fetching incremental…")

    # ── Fresh or incremental download ──────────────────────────────────────────
    last_cached = existing.index[-1].date() if not existing.empty else None
    diffs       = existing.index.to_series().diff().dt.days if not existing.empty else pd.Series()
    has_gap     = bool((diffs > 20).any()) if not diffs.empty else False

    # Ignore gaps that are entirely in the distant past (>3 years ago).
    # Old historical gaps (e.g. 2008-2009 GFC missing data) can't be patched
    # by yfinance anyway; forcing a full re-download just wastes time and breaks
    # the incremental path that correctly appends today's close.
    if has_gap and not diffs.empty:
        gap_cutoff = pd.Timestamp(today - timedelta(days=365 * 3))
        recent_gap = (diffs > 20) & (diffs.index >= gap_cutoff)
        if not recent_gap.any():
            has_gap = False  # only old gaps — treat as fresh-enough for incremental

    nse_name     = _TICKER_TO_NSE_NAME.get(ticker)
    archive_name = _NSE_HIST_INDEX_TYPE.get(ticker)

    if has_gap or existing.empty:
        # Has a recent gap or no data at all — need a full re-fetch to fill the gap.
        # But if existing data is present, only fetch from last_cached onward; the
        # data before that is already in the CSV and doesn't need re-downloading.
        fetch_start = str(last_cached + timedelta(days=1)) if last_cached else "2006-01-01"
        new = _yf_full(ticker, start=fetch_start)
        if new.empty or len(new) < MIN_ROWS:
            if nse_name:
                print(f"      yfinance insufficient → trying niftyindices.com for '{nse_name}'…")
                new = _fetch_from_niftyindices(nse_name, start_date=fetch_start)
                if not new.empty:
                    print(f"      ✓ niftyindices: {len(new)} rows")
    else:
        # Incremental: only fetch the missing tail (from last_cached+1 to today)
        start_inc = str(last_cached + timedelta(days=1))
        # Source priority:
        # 1. NSE daily archive — best source for exact close; published after market close.
        # 2. yfinance — _fetch_yfinance internally falls back to period=5d when an explicit
        #    date-range returns 0 rows (e.g. session still open), so this always returns the
        #    most recent available price regardless of time of day.
        # 3. niftyindices.com — for tickers where yfinance is unreliable.
        new = pd.Series(dtype=float)
        if archive_name:
            new = _fetch_nse_archive_days(archive_name,
                                          last_cached + timedelta(days=1), today)
            if not new.empty:
                print(f"      ✓ NSE archive: {len(new)} rows")
        if new.empty:
            # _fetch_yfinance now includes a period=5d fallback inside it — this handles
            # the case where the market is still open and explicit date-range returns 0 rows.
            new = _fetch_yfinance(ticker, start_inc, str(today + timedelta(days=1)))
        if new.empty and nse_name:
            ni_tail = _fetch_from_niftyindices(nse_name, start_date=start_inc)
            if not ni_tail.empty:
                new = ni_tail[ni_tail.index >= pd.Timestamp(start_inc)]

    # ── Merge ──────────────────────────────────────────────────────────────────
    if not new.empty:
        if has_gap or existing.empty:
            combined = new
        else:
            combined = pd.concat([existing, new]).sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
    else:
        combined = existing

    # ── Try yfinance alternative tickers if still no data ────────────────────
    if combined.empty or len(combined) < MIN_ROWS:
        alts = ALTERNATIVES.get(ticker, [])
        for alt_ticker in alts:
            print(f"      Trying alternative ticker: {alt_ticker}…")
            alt = _yf_full(alt_ticker, start="2006-01-01")
            if not alt.empty and len(alt) >= MIN_ROWS:
                combined = alt
                print(f"      ✓ Got {len(combined)} rows from {alt_ticker}")
                break

    # ── Gap-fill: patch any remaining internal gaps via niftyindices.com ──────
    # e.g. ^NSMIDCP yfinance has a 256-day gap in 2016 that yfinance never fixed
    if nse_name and not combined.empty:
        diffs_check = combined.index.to_series().diff().dt.days
        inner_gaps  = diffs_check[diffs_check > 20]
        if not inner_gaps.empty:
            print(f"      Detected {len(inner_gaps)} gap(s) — patching via niftyindices.com…")
            try:
                full_ni = _fetch_from_niftyindices(nse_name, start_date="2006-01-01")
                if not full_ni.empty:
                    combined = pd.concat([combined, full_ni]).sort_index()
                    combined = combined[~combined.index.duplicated(keep="last")]
                    # Re-check gaps
                    diffs_after2 = combined.index.to_series().diff().dt.days
                    still_gaps   = (diffs_after2 > 20).sum()
                    msg = f"gaps closed" if not still_gaps else f"{still_gaps} gap(s) remain"
                    print(f"      ✓ niftyindices patch: {len(full_ni)} rows fetched — {msg}")
            except Exception as e:
                print(f"      ⚠️ niftyindices gap-fill failed: {e}")

    # ── Save result ────────────────────────────────────────────────────────────
    if not combined.empty and len(combined) >= MIN_ROWS:
        # Validate: no internal gaps > 20 days after re-download
        diffs_after = combined.index.to_series().diff().dt.days
        gaps_after  = (diffs_after > 20).sum()
        path.parent.mkdir(parents=True, exist_ok=True)
        _save_csv(combined, path)
        gap_note = f"  ⚠️ {gaps_after} gaps remain" if gaps_after else ""
        print(f"  ✅ {name:30s}  {combined.index[0].date()} → {combined.index[-1].date()}  ({len(combined)} rows){gap_note}")
        saved.append(name)
    else:
        n = len(combined) if not combined.empty else 0
        print(f"  ❌ {name:30s}  insufficient data ({n} rows) — skipping")
        failed.append(name)


# ── Step 2: ETFs + global indices via yfinance ────────────────────────────────
print(f"\n[2/2] Fetching ETF / global index prices via yfinance…\n")

_SECTION1_TICKERS = set(INDIAN_INDICES.values())  # already handled above; skip in section 2

yf_only = {
    n: t for n, t in INSTRUMENTS.items()
    if t not in _TICKER_TO_NSE_NAME and t != "NIFTY500_SEED" and t not in _SECTION1_TICKERS
}

for name, ticker in yf_only.items():
    path     = _csv_path(ticker)
    existing = _load_csv(path)
    today    = date.today()
    last_d   = existing.index[-1].date() if not existing.empty else None

    if last_d and last_d >= today:
        print(f"  ✅ {name:35s}  already up to date ({last_d})")
        continue

    try:
        start = str(last_d + timedelta(days=1)) if last_d else "2006-01-01"
        new   = _fetch_yfinance(ticker, start, str(today + timedelta(days=1)))

        if new.empty:
            print(f"  ⚠️  {name:35s}  no data ({ticker})")
            continue

        combined = pd.concat([existing, new]).sort_index() if not existing.empty else new
        combined = combined[~combined.index.duplicated(keep="last")]
        combined = _fix_consolidation_spikes(combined)
        path.parent.mkdir(parents=True, exist_ok=True)
        _save_csv(combined, path)
        print(f"  ✅ {name:35s}  {combined.index[0].date()} → {combined.index[-1].date()}  ({len(combined)} rows)")
    except Exception as e:
        print(f"  ❌ {name:35s}  error: {e}")


# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"  Indian indices saved:  {len(saved)}")
if failed:
    print(f"  Failed (no yfinance data):  {', '.join(failed)}")
    print("  → These pairs won't work in the app until a data source is found.")
print()
print("  Next steps:")
print("    git add data/live/indices/")
print("    git commit -m \"chore: pre-cache all index price history\"")
print("    git push")
print("=" * 60 + "\n")

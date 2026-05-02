"""
data/metrics.py
Computes Earnings Yield, Yield Gap, and derived metrics.
Accepts PE as either a daily Series OR a scalar float.
"""

from __future__ import annotations
import logging
import pandas as pd

logger = logging.getLogger(__name__)


def align_and_compute(
    bond_yield: pd.Series,
    nifty_close: pd.Series,
    pe: "pd.Series | float",
) -> pd.DataFrame:
    """
    Master metrics computation.

    Parameters
    ----------
    bond_yield  : daily India 10Y yield (%)
    nifty_close : daily Nifty 50 close price
    pe          : daily Nifty 50 PE Series  OR  a single float scalar.
                  If Series → earnings_yield varies daily (real data).
                  If float  → earnings_yield is constant (fallback).

    Returns DataFrame columns:
        bond_yield, nifty_close, nifty_pe,
        earnings_yield, yield_gap, yield_gap_ma20, yield_gap_std20
    """
    # ── 1. Align bond + nifty ─────────────────────────────────────────────────
    df = pd.DataFrame({"bond_yield": bond_yield, "nifty_close": nifty_close})
    df.index = pd.to_datetime(df.index).normalize()
    df.sort_index(inplace=True)
    df = df.ffill(limit=5).dropna()

    # ── 2. Attach PE ──────────────────────────────────────────────────────────
    if isinstance(pe, pd.Series) and not pe.empty:
        pe_s = pe.copy()
        pe_s.index = pd.to_datetime(pe_s.index).normalize()
        pe_s = pe_s.sort_index().dropna()
        pe_s = pe_s[~pe_s.index.duplicated(keep="last")]
        df["nifty_pe"] = (
            pe_s.reindex(df.index)
            .interpolate(method="linear", limit_area="inside")  # smooth across interior gaps
            .ffill()   # extend forward at trailing edge
            .bfill()   # extend backward at leading edge
        )
        logger.info("PE series: %d rows | %.2f – %.2f",
                    df["nifty_pe"].notna().sum(), df["nifty_pe"].min(), df["nifty_pe"].max())
    else:
        df["nifty_pe"] = float(pe) if pe else 22.0
        logger.info("PE scalar: %.2f", df["nifty_pe"].iloc[0])

    # ── 3. Derived metrics ────────────────────────────────────────────────────
    df["earnings_yield"]  = (1.0 / df["nifty_pe"]) * 100.0
    df["yield_gap"]       = df["bond_yield"] - df["earnings_yield"]
    df["yield_gap_ma20"]  = df["yield_gap"].rolling(20, min_periods=5).mean()
    df["yield_gap_std20"] = df["yield_gap"].rolling(20, min_periods=5).std()

    logger.info("Metrics: %d rows | bond=%.3f%% ey=%.3f%% gap=%.3f%%",
                len(df), float(df["bond_yield"].iloc[-1]),
                float(df["earnings_yield"].iloc[-1]), float(df["yield_gap"].iloc[-1]))
    return df


# Backward-compat alias (old code calls compute_metrics with a scalar)
def compute_metrics(bond_yield, nifty_close, pe_ratio=22.0):
    return align_and_compute(bond_yield, nifty_close, pe_ratio)


def get_summary_stats(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    return {
        "latest_bond_yield":     round(float(last["bond_yield"]),     3),
        "latest_earnings_yield": round(float(last["earnings_yield"]), 3),
        "latest_yield_gap":      round(float(last["yield_gap"]),      3),
        "latest_nifty_pe":       round(float(last["nifty_pe"]),       2),
        "yield_gap_1y_avg":      round(float(df["yield_gap"].tail(252).mean()), 3),
        "yield_gap_max":         round(float(df["yield_gap"].max()),  3),
        "yield_gap_min":         round(float(df["yield_gap"].min()),  3),
        "data_start":            df.index[0].date(),
        "data_end":              df.index[-1].date(),
    }

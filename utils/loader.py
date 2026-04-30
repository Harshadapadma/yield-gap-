"""
utils/loader.py
Streamlit cache wrappers. Now fetches daily PE history and passes it
to metrics so earnings_yield varies daily (not a flat line).
"""

from __future__ import annotations

import streamlit as st

from data.fetcher  import fetch_bond_yield, fetch_nifty, fetch_pe_ratio, fetch_pe_history
from data.metrics  import align_and_compute, get_summary_stats
from utils.config  import CACHE_TTL_SECONDS, DATA_START_DATE


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_bond(start_date: str):
    return fetch_bond_yield(start_date=start_date)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_nifty_price(period: str = "max"):
    return fetch_nifty(period=period)


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_pe_live():
    return fetch_pe_ratio()


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_pe_history(start_date: str):
    return fetch_pe_history(start_date=start_date)


def load_all(
    start_date: str = DATA_START_DATE,
    current_pe: float = 22.0,
) -> tuple:
    """
    Full pipeline → (df, stats, data_status).

    PE is now a daily time-series so earnings_yield is NOT a flat line.
    current_pe is still used as override/fallback when the PE series
    is missing for recent dates.
    """
    data_status: dict = {}

    # ── Bond yield ────────────────────────────────────────────────────────────
    bond, bond_status = load_bond(start_date)
    data_status["Bond Yield"] = bond_status

    # ── Nifty 50 price ────────────────────────────────────────────────────────
    nifty, nifty_status = load_nifty_price()
    data_status["Nifty 50"] = nifty_status

    # ── PE history (daily series) ─────────────────────────────────────────────
    pe_series, pe_status = load_pe_history(start_date)
    data_status["Nifty PE (daily)"] = pe_status

    # Override/patch the most-recent PE with the live-fetched scalar so
    # today's row always reflects the freshest PE, even if NSE archives lag.
    import pandas as pd
    from datetime import date
    try:
        live_pe, _ = load_pe_live()
        today_ts = pd.Timestamp(date.today())
        if pe_series is not None and not pe_series.empty:
            pe_series[today_ts] = live_pe
            pe_series = pe_series.sort_index()
    except Exception:
        pass

    # ── Compute all derived metrics ───────────────────────────────────────────
    df    = align_and_compute(bond, nifty, pe=pe_series)
    stats = get_summary_stats(df)

    return df, stats, data_status


def clear_all_caches() -> None:
    load_bond.clear()
    load_nifty_price.clear()
    load_pe_live.clear()
    load_pe_history.clear()

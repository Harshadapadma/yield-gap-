"""
utils/loader.py
Cache-only data loader for Streamlit.

Reads pre-fetched CSVs committed by GitHub Actions — zero live network calls.
GitHub Actions runs daily (Mon-Fri 16:30 IST) and commits updated CSVs.
Streamlit simply reads those files.
"""
from __future__ import annotations

import streamlit as st

from data.metrics import align_and_compute, get_summary_stats
from utils.config import CACHE_TTL_SECONDS, DATA_START_DATE
from web_load import load_bond_yield_cached, load_nifty_cached, load_pe_history_cached


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_bond(start_date: str):
    return load_bond_yield_cached()


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_nifty_price(period: str = "max"):
    return load_nifty_cached()


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False)
def load_pe_history(start_date: str):
    return load_pe_history_cached()


def load_pe_live():
    # No live PE fetch — GitHub Actions keeps the PE history CSV up to date
    return None, {"message": "Cached — see PE history", "live_ok": True}


def load_all(
    start_date: str = DATA_START_DATE,
    current_pe: float = 22.0,
) -> tuple:
    """Full pipeline → (df, stats, data_status). All data from cached CSVs."""
    data_status: dict = {}

    bond, bond_status = load_bond(start_date)
    data_status["Bond Yield"] = bond_status

    nifty, nifty_status = load_nifty_price()
    data_status["Nifty 50"] = nifty_status

    pe_series, pe_status = load_pe_history(start_date)
    data_status["Nifty PE (daily)"] = pe_status

    df    = align_and_compute(bond, nifty, pe=pe_series)
    stats = get_summary_stats(df)

    return df, stats, data_status


def clear_all_caches() -> None:
    load_bond.clear()
    load_nifty_price.clear()
    load_pe_history.clear()

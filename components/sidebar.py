"""
components/sidebar.py – Sidebar: controls, manual entry, and metric cards.
"""

from __future__ import annotations

from datetime import date

import streamlit as st

from data.cache import save_manual_entry
from utils.config import MANUAL_CACHE


def render_sidebar(fetched_pe: float = 21.27) -> dict:
    """Render sidebar controls and return user parameters.

    Live metric tiles and data-source status are injected by
    ``update_sidebar_metrics`` after data has been loaded.
    """
    st.sidebar.markdown(
        """
        <div style='padding:12px 0 4px 0'>
            <span style='font-size:22px;font-weight:700;letter-spacing:1px;
                         color:#58A6FF;font-family:IBM Plex Mono,monospace'>
                ⬡ YIELD GAP
            </span><br>
            <span style='font-size:11px;color:#8B949E;font-family:IBM Plex Mono,monospace'>
                INDIA MACRO DASHBOARD
            </span>
        </div>
        <hr style='border-color:#21262D;margin:8px 0 16px 0'>
        """,
        unsafe_allow_html=True,
    )

    # ── Controls ──────────────────────────────────────────────────────────────
    st.sidebar.markdown("**⚙️ Parameters**")

    pe_ratio = st.sidebar.number_input(
        "Nifty 50 PE Ratio",
        min_value=5.0, max_value=100.0,
        value=float(fetched_pe), step=0.5,
        help=(
            "Auto-fetched from nifty-pe-ratio.com or NSE. "
            "Override here if needed."
        ),
        key="pe_ratio",
    )

    today = date.today()

    # ── Chart Options ─────────────────────────────────────────────────────────
    st.sidebar.markdown("**🔧 Chart Options**")
    show_components = st.sidebar.checkbox("Show Bond & Earnings Yield", value=True)
    show_ma         = st.sidebar.checkbox("Show 20-day Moving Average",  value=True)

    st.sidebar.markdown("**📏 Reference Lines (%)**")
    col1, col2 = st.sidebar.columns(2)
    with col1:
        ref1 = st.number_input("Line 1", value=1.0,  step=0.5, key="ref1")
        ref3 = st.number_input("Line 3", value=3.0,  step=0.5, key="ref3")
    with col2:
        ref2 = st.number_input("Line 2", value=0.0,  step=0.5, key="ref2")
        ref4 = st.number_input("Line 4", value=-1.0, step=0.5, key="ref4")

    ref_lines = sorted({ref1, ref2, ref3, ref4})

    st.sidebar.divider()

    # ── Cache control ─────────────────────────────────────────────────────────
    if st.sidebar.button("🔄 Clear Cache & Reload", use_container_width=True,
                         help="Force fresh data fetch — use if chart looks stale, flat, or starts from wrong year"):
        st.cache_data.clear()
        st.rerun()

    st.sidebar.divider()

    # ── Full History Loader ───────────────────────────────────────────────────
    st.sidebar.markdown("**🗄️ Full History (one-time)**")
    st.sidebar.caption(
        "On first run, PE history loads only the last 2 years (fast). "
        "Click below to backfill all history from 2011. "
        "Takes ~60–90 s once; then it's cached permanently."
    )
    build_full_history = st.sidebar.button(
        "⏳ Build Full PE History (2011→today)",
        key="build_full_history",
        help="Downloads ~3,500 daily NSE archive files in parallel. One-time setup.",
    )

    st.sidebar.divider()

    # ── Manual Data Entry ─────────────────────────────────────────────────────
    st.sidebar.markdown("**✏️ Manual Data Entry**")
    st.sidebar.caption(
        "Can't fetch bond yield automatically? Enter today's value "
        "from [TradingView IN10Y](https://in.tradingview.com/symbols/TVC-IN10Y/)."
    )

    with st.sidebar.form("manual_entry", clear_on_submit=True):
        entry_date  = st.date_input("Date", value=today, key="manual_date")
        manual_bond = st.number_input(
            "Bond Yield (%)", min_value=0.0, max_value=20.0,
            value=0.0, step=0.01, format="%.3f", key="manual_bond",
            help="Enter 6.914 for 6.914%",
        )
        manual_pe = st.number_input(
            "Nifty PE (optional)", min_value=0.0, max_value=100.0,
            value=0.0, step=0.1, format="%.2f", key="manual_pe",
            help="Leave at 0 to skip",
        )
        submitted = st.form_submit_button("💾 Save Entry")
        if submitted:
            bond_val = manual_bond if manual_bond > 0 else None
            pe_val   = manual_pe   if manual_pe   > 0 else None
            if bond_val or pe_val:
                save_manual_entry(str(entry_date), bond_val, pe_val, MANUAL_CACHE)
                st.success(f"Saved entry for {entry_date}!")
                st.rerun()
            else:
                st.warning("Enter at least one value.")

    st.sidebar.divider()
    st.sidebar.markdown(
        "<div style='font-size:10px;color:#484F58;font-family:IBM Plex Mono,monospace'>"
        "Bond: Trading Economics / Investing.com (live)<br>"
        "Equity: yfinance ^NSEI (2000-present)<br>"
        "PE: NSE Archives daily (2011-present)<br>"
        "Cache: local CSV (persists)"
        "</div>",
        unsafe_allow_html=True,
    )

    return dict(
        pe_ratio=pe_ratio,
        show_components=show_components,
        show_ma=show_ma,
        ref_lines=ref_lines,
        build_full_history=build_full_history,
    )


def update_sidebar_metrics(stats: dict | None = None,
                           data_status: dict | None = None) -> None:
    """No-op — data sources and metric tiles are shown on the main page only."""
    pass
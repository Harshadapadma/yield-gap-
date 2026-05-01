"""
app.py
──────
Entry point for the Options dashboard.

Pages:
  1. Yield Gap      – India 10Y Bond Yield vs Nifty 50 Earnings Yield
  2. Return Spread  – Nifty 50 / Gold rolling return spread + SD bands
  3. Outperformance – % of Nifty 500 stocks beating Nifty 50

Run with:
    streamlit run app.py

Live-fetch behaviour (every app open / refresh):
  • Bond yield  → Stooq → tvDatafeed → yfinance → FRED → Nasdaq DL
                  Result upserted into data/live/bond_daily_live.csv
  • Nifty 50    → yfinance 5d window merged with cache/nifty_close.csv
  • PE ratio    → nifty-pe-ratio.com → NSE API → last known from CSV
  • All three fetched on every open during market hours (no stale cache served)
  • Outside market hours: session_state cached keyed on CSV mtime (auto-invalidates)
"""

import sys
import traceback
from pathlib import Path

# ── Ensure local packages take priority over any installed packages ────────────
_HERE     = Path(__file__).resolve().parent
_HERE_STR = str(_HERE)
if _HERE_STR in sys.path:
    sys.path.remove(_HERE_STR)
sys.path.insert(0, _HERE_STR)

for _pkg in list(sys.modules.keys()):
    if _pkg.split(".")[0] in {"components", "utils", "data", "pages"}:
        del sys.modules[_pkg]

import datetime as _dt
import streamlit as st
from utils.config import APP_TITLE, APP_ICON, setup_logging

# ── Page config (must be first Streamlit call) ─────────────────────────────────
st.set_page_config(
    page_title=APP_TITLE,
    page_icon=APP_ICON,
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global CSS ─────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;600&display=swap');

    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
        background-color: #0D1117;
        color: #E6EDF3;
    }
    .stApp { background-color: #0D1117; }

    header[data-testid="stHeader"] {
        background-color: #0D1117;
        border-bottom: 1px solid #21262D;
    }
    section[data-testid="stSidebar"] {
        background-color: #0D1117;
        border-right: 1px solid #21262D;
    }
    [data-testid="stSidebarNav"] { display: none !important; }
    [data-testid="collapsedControl"],
    button[kind="header"] { display: flex !important; visibility: visible !important; }

    div[data-testid="stMetric"] {
        background: #161B22;
        border: 1px solid #21262D;
        border-radius: 4px;
        padding: 10px 14px;
    }
    div[data-testid="stMetric"] label {
        color: #8B949E !important;
        font-size: 11px !important;
        font-weight: 500 !important;
        text-transform: uppercase;
        letter-spacing: 0.6px;
        font-family: 'Inter', sans-serif !important;
    }
    div[data-testid="stMetric"] [data-testid="stMetricValue"] {
        font-size: clamp(14px, 2.5vw, 20px) !important;
        font-family: 'IBM Plex Mono', monospace !important;
        font-weight: 600 !important;
    }

    div[data-testid="stRadio"] > div { gap: 4px; }
    div[data-testid="stRadio"] label {
        border: 1px solid #21262D;
        border-radius: 4px;
        padding: 5px 12px;
        background: #161B22;
        cursor: pointer;
        font-size: 13px !important;
        font-family: 'Inter', sans-serif !important;
    }
    div[data-testid="stRadio"] label:hover { border-color: #4A90D9; }

    .stAlert    { border-radius: 4px; }
    .block-container {
        padding-top: 3.5rem !important;
        padding-bottom: 2rem;
        max-width: 100% !important;
    }

    .pg-header {
        display: flex !important;
        flex-wrap: wrap !important;
        align-items: baseline !important;
        gap: 10px !important;
        margin-bottom: 12px !important;
        border-bottom: 1px solid #21262D;
        padding-bottom: 10px !important;
    }
    .pg-title {
        font-size: clamp(14px, 3vw, 18px) !important;
        font-weight: 600 !important;
        color: #E6EDF3 !important;
        font-family: 'Inter', sans-serif !important;
        letter-spacing: 0.5px !important;
        white-space: nowrap !important;
    }
    .pg-sub {
        font-size: clamp(10px, 2vw, 12px) !important;
        color: #6E7681 !important;
        font-family: 'Inter', sans-serif !important;
        white-space: normal !important;
        line-height: 1.4 !important;
        font-weight: 400 !important;
    }

    @media screen and (max-width: 768px) {
        .block-container {
            padding-left: 0.75rem !important;
            padding-right: 0.75rem !important;
            padding-top: 3rem !important;
        }
        div[data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 8px !important; }
        div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
            min-width: calc(50% - 8px) !important;
            flex: 1 1 calc(50% - 8px) !important;
        }
        div[data-testid="stRadio"] > div { flex-wrap: wrap !important; gap: 4px !important; }
    }
    @media screen and (max-width: 420px) {
        .block-container { padding-left: 0.25rem !important; padding-right: 0.25rem !important; }
        div[data-testid="stHorizontalBlock"] > div[data-testid="column"] {
            min-width: 100% !important; flex: 1 1 100% !important;
        }
        .pg-title { font-size: 15px !important; white-space: normal !important; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

setup_logging()

# ── Background data prefetch (once per Streamlit session) ─────────────────────
if "prefetch_started" not in st.session_state:
    st.session_state.prefetch_started = True
    import threading
    from data.index_store import prefetch_all_instruments
    threading.Thread(target=prefetch_all_instruments, daemon=True).start()


# ── IST helpers ────────────────────────────────────────────────────────────────
def _ist_now() -> _dt.datetime:
    IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    return _dt.datetime.now(IST)


def _market_status() -> tuple[bool, str, int | None]:
    """Returns (is_open, status_label, refresh_interval_seconds)."""
    now  = _ist_now()
    wd   = now.weekday()
    mins = now.hour * 60 + now.minute
    OPEN_MINS  = 9 * 60 + 15
    CLOSE_MINS = 15 * 60 + 30

    if wd >= 5:
        return False, "Opens Mon 09:15 IST", None
    if mins < OPEN_MINS:
        diff = OPEN_MINS - mins
        return False, f"Opens in {diff // 60}h {diff % 60}m (09:15 IST)", None
    if mins <= CLOSE_MINS:
        diff = CLOSE_MINS - mins
        return True, f"Closes in {diff // 60}h {diff % 60}m (15:30 IST)", 30 * 60
    return False, "Closed — opens tomorrow 09:15 IST", None


_mkt_open, _mkt_label, _refresh_secs = _market_status()


# ── Sidebar navigation ─────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(
        "<div style='padding:10px 0 4px 0'>"
        "<span style='font-size:15px;font-weight:600;letter-spacing:0.3px;"
        "color:#E6EDF3;font-family:Inter,sans-serif'>"
        "Options"
        "</span></div>"
        "<hr style='border-color:#21262D;margin:6px 0 14px 0'>",
        unsafe_allow_html=True,
    )

    PAGE_OPTIONS = {
        "Yield Gap":      "yield_gap",
        "Return Spread":  "spread",
        "Outperformance": "breadth",
    }
    page_label  = st.radio(
        "page",
        options=list(PAGE_OPTIONS.keys()),
        index=0,
        key="nav_page",
        label_visibility="collapsed",
    )
    active_page = PAGE_OPTIONS[page_label]

    st.markdown("<hr style='border-color:#21262D;margin:12px 0'>", unsafe_allow_html=True)

    # ── Market status + auto-refresh ──────────────────────────────────────────
    _dot   = "🟢" if _mkt_open else "🔴"
    _color = "#3FB950" if _mkt_open else "#8B949E"
    st.markdown(
        f"<div style='font-size:11px;color:{_color};font-family:IBM Plex Mono,monospace;"
        f"padding:4px 0 2px 0'>{_dot} {_mkt_label}</div>",
        unsafe_allow_html=True,
    )
    if _mkt_open and _refresh_secs:
        st.markdown(
            "<div style='font-size:10px;color:#484F58;padding-bottom:6px'>"
            "Auto-refreshing every 30 min</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<script>setTimeout(function(){{window.location.reload();}}, '
            f'{_refresh_secs * 1000});</script>',
            unsafe_allow_html=True,
        )
    st.markdown(
        f"<div style='font-size:10px;color:#484F58;margin-bottom:4px'>"
        f"Last load: {_ist_now().strftime('%H:%M IST')}</div>",
        unsafe_allow_html=True,
    )

    st.markdown("<hr style='border-color:#21262D;margin:6px 0 10px 0'>", unsafe_allow_html=True)

    # ── Page-specific sidebar controls ────────────────────────────────────────
    if active_page == "yield_gap":
        # Fetch live PE for the number_input default
        try:
            from data.fetcher import fetch_pe_ratio
            fetched_pe, _ = fetch_pe_ratio()
        except Exception:
            fetched_pe = 21.27

        from components.sidebar import render_sidebar
        params = render_sidebar(fetched_pe=fetched_pe)
        pe_ratio = params["pe_ratio"]

    elif active_page == "spread":
        st.caption("Rolling return diff between any two instruments.")
        if st.button("🔄 Refresh Data", use_container_width=True, key="spread_refresh"):
            from utils.loader import clear_all_caches
            clear_all_caches()
            st.rerun()
        pe_ratio = 21.27

    elif active_page == "breadth":
        st.caption(
            "% of Nifty 500 stocks beating Nifty 50's 1Y return.\n\n"
            "First run downloads ~500 stocks (~2 min). After that, only new days."
        )
        pe_ratio = 21.27

    st.markdown("<hr style='border-color:#21262D;margin:10px 0'>", unsafe_allow_html=True)


# ── Route to page ──────────────────────────────────────────────────────────────
if active_page == "yield_gap":
    from pages.page_yield_gap import render as render_yield_gap
    render_yield_gap(pe_ratio=pe_ratio)

elif active_page == "spread":
    try:
        from pages.page_spread import render as render_spread
        render_spread()
    except ModuleNotFoundError:
        st.markdown(
            "<div class='pg-header'><span class='pg-title'>Return Spread</span></div>",
            unsafe_allow_html=True,
        )
        st.info("📐 **Return Spread** page — coming soon.\n\nCreate `pages/page_spread.py` with a `render()` function.")

elif active_page == "breadth":
    try:
        from pages.breadth_analysis import render as render_breadth
        render_breadth()
    except ModuleNotFoundError:
        st.markdown(
            "<div class='pg-header'><span class='pg-title'>Outperformance</span></div>",
            unsafe_allow_html=True,
        )
        st.info("📊 **Outperformance** page — coming soon.\n\nCreate `pages/breadth_analysis.py` with a `render()` function.")
    except Exception:
        st.error("### ⚠️ Unexpected Error")
        st.code(traceback.format_exc())
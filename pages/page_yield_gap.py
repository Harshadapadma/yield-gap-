"""
pages/page_yield_gap.py
Yield Gap page: India 10Y Bond Yield vs Nifty 50 Earnings Yield.
Called from app.py via render(pe_ratio=...).

Fixes vs previous:
  • Data Sources expander → table with Date, Bond Yield, PE, EY, Yield Gap
  • Default date filter   → last 2 years (not full history)
  • X-axis tick spacing   → auto-scaled via charts._x_axis_dtick()
  • Earnings yield is now a real daily series (not a flat line)
"""
from __future__ import annotations

import traceback
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from components.charts  import (
    plot_yield_gap, plot_components_area,
    plot_distribution, build_data_source_table,
)
from components.sidebar import update_sidebar_metrics
from utils.loader       import load_all, load_pe_live, clear_all_caches
from utils.config       import DATA_START_DATE

_RANGE_CSS = """
<style>
/* Range selector row — tighter spacing */
div[data-testid="stSegmentedControl"] {
    gap: 4px !important;
}
div[data-testid="stSegmentedControl"] label {
    font-family: 'IBM Plex Mono', monospace !important;
    font-size: 12px !important;
    font-weight: 500 !important;
    letter-spacing: 0.4px !important;
    padding: 4px 10px !important;
    border-radius: 4px !important;
    cursor: pointer !important;
}
/* Custom date-picker row */
.custom-range-row {
    display: flex;
    gap: 10px;
    align-items: center;
    margin-top: 6px;
    margin-bottom: 2px;
}
</style>
"""


def _apply_range(selected: str, data_start: date) -> tuple[date, date]:
    """Return (date_from, date_to) for a preset range label."""
    today = date.today()
    mapping = {
        "1M":  today - timedelta(days=30),
        "3M":  today - timedelta(days=91),
        "6M":  today - timedelta(days=182),
        "YTD": date(today.year, 1, 1),
        "1Y":  today - timedelta(days=365),
        "2Y":  today - timedelta(days=730),
        "3Y":  today - timedelta(days=1095),
        "5Y":  today - timedelta(days=1825),
        "MAX": data_start,
    }
    return max(mapping[selected], data_start), today


def render(pe_ratio: float = 22.0) -> None:
    """Render the full Yield Gap page."""

    # ── Fetch live PE first (sidebar display) ─────────────────────────────────
    try:
        live_pe, _ = load_pe_live()
    except Exception:
        live_pe = pe_ratio

    # ── Load all data ──────────────────────────────────────────────────────────
    with st.spinner("⟳ Fetching live market data…"):
        try:
            df_full, stats, data_status = load_all(
                start_date=DATA_START_DATE,
                current_pe=pe_ratio,
            )
        except RuntimeError as exc:
            st.error("### ⚠️ Data Fetch Failed")
            st.markdown(
                f"```\n{exc}\n```\n\n"
                "**Quick fixes:**\n"
                "1. Place `india_10y_bond_yield_seed.csv` in `data/seed/`\n"
                "2. Check internet connectivity\n"
                "3. Hit **R** to rerun\n"
            )
            return
        except Exception:
            st.error("### ⚠️ Unexpected Error")
            st.code(traceback.format_exc(), language="python")
            return

    # ── Inject live metrics into sidebar ──────────────────────────────────────
    update_sidebar_metrics(stats=stats, data_status=data_status)

    today      = date.today()
    data_start = date.fromisoformat(DATA_START_DATE)

    # ── Page header ────────────────────────────────────────────────────────────
    bond_ok    = data_status.get("Bond Yield",      {}).get("live_ok", True)
    nifty_ok   = data_status.get("Nifty 50",        {}).get("live_ok", True)
    live_badge = "🟢 live" if (bond_ok and nifty_ok) else "🟡 partial"

    st.markdown(_RANGE_CSS, unsafe_allow_html=True)
    st.markdown(
        f"""
        <div class='pg-header'>
            <span class='pg-title'>Yield Gap</span>
            <span class='pg-sub'>
                India 10Y Bond Yield − Nifty 50 Earnings Yield
                &nbsp;|&nbsp; {live_badge}
                &nbsp;|&nbsp; PE={stats['latest_nifty_pe']:.2f}
                &nbsp;|&nbsp; EY={stats['latest_earnings_yield']:.3f}%
                &nbsp;|&nbsp; {stats['data_start']} → {stats['data_end']}
            </span>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Metric row ─────────────────────────────────────────────────────────────
    c1, c2, c3, c4, c5 = st.columns(5)
    gap   = stats["latest_yield_gap"]
    avg   = stats["yield_gap_1y_avg"]
    delta = gap - avg

    with c1:
        st.metric(
            "🏦 Bond Yield",
            f"{stats['latest_bond_yield']:.3f}%",
            delta="live ✅" if bond_ok else "last known ⚠️",
            delta_color="normal" if bond_ok else "off",
        )
    with c2:
        st.metric(
            "📈 Earnings Yield",
            f"{stats['latest_earnings_yield']:.3f}%",
            delta=f"PE={stats['latest_nifty_pe']:.2f}",
        )
    with c3:
        st.metric(
            "⚡ Yield Gap",
            f"{gap:+.3f}%",
            delta=f"{delta:+.3f}% vs 1Y avg",
        )
    with c4:
        st.metric("📊 1Y Avg Gap", f"{avg:+.3f}%")
    
    st.markdown("<div style='height:4px'></div>", unsafe_allow_html=True)

    # ── Range filter bar ───────────────────────────────────────────────────────
    PRESETS = ["1M", "3M", "6M", "YTD", "1Y", "2Y", "3Y", "5Y", "MAX", "Custom"]

    selected_range = st.segmented_control(
        "range",
        options=PRESETS,
        default="2Y",
        key="chart_range",
        label_visibility="collapsed",
    )

    if selected_range == "Custom":
        cc1, cc2, _ = st.columns([1, 1, 5])
        with cc1:
            date_from = st.date_input(
                "From", value=today - timedelta(days=730),
                min_value=data_start, max_value=today,
                key="custom_from",
            )
        with cc2:
            date_to = st.date_input(
                "To", value=today,
                min_value=date_from, max_value=today,
                key="custom_to",
            )
    else:
        date_from, date_to = _apply_range(selected_range or "2Y", data_start)

    # ── Filter data to selected range ──────────────────────────────────────────
    mask = (df_full.index.date >= date_from) & (df_full.index.date <= date_to)
    df   = df_full.loc[mask]

    if df.empty:
        st.warning("No data for the selected date range. Please widen the filter.")
        return

    # ── Chart options from sidebar ─────────────────────────────────────────────
    show_components = st.session_state.get("show_components", False)
    show_ma         = st.session_state.get("show_ma", True)
    ref_lines = [
        st.session_state.get("ref1",  1.0),
        st.session_state.get("ref2",  0.0),
        st.session_state.get("ref3",  3.0),
        st.session_state.get("ref4", -1.0),
    ]

    # ── Main yield gap chart ───────────────────────────────────────────────────
    st.plotly_chart(
        plot_yield_gap(df, show_components=show_components,
                       show_ma=show_ma, ref_lines=ref_lines),
        use_container_width=True,
    )

    # ── Secondary charts ───────────────────────────────────────────────────────
    col_a, col_b = st.columns([3, 2])
    with col_a:
        st.plotly_chart(plot_components_area(df), use_container_width=True)
    with col_b:
        st.plotly_chart(plot_distribution(df), use_container_width=True)

    # ── Data Sources & Values expander ────────────────────────────────────────
    with st.expander("📡 Data Sources & Latest Values", expanded=False):

        # ── Source status row ─────────────────────────────────────────────────
        src_cols = st.columns(3)
        for i, (key, info) in enumerate(data_status.items()):
            with src_cols[i % 3]:
                ok    = info.get("success", True) and info.get("live_ok", True)
                emoji = "✅" if ok else "⚠️"
                st.markdown(f"**{emoji} {key}**")
                st.caption(info.get("message", "—"))

        st.divider()

        # ── Latest values table ───────────────────────────────────────────────
        st.markdown("**Latest 10 trading days**")

        tbl = build_data_source_table(df)

        st.dataframe(
            tbl.style
               .format({
                   "Bond Yield (%)":     "{:.3f}",
                   "Nifty PE":           "{:.2f}",
                   "Earnings Yield (%)": "{:.3f}",
                   "Yield Gap (%)":      "{:+.3f}",
               })
               .background_gradient(subset=["Yield Gap (%)"], cmap="RdYlGn_r"),
            use_container_width=True,
            height=380,
        )

        # ── Full data download ─────────────────────────────────────────────────
        st.markdown("**Full filtered data**")
        all_cols = ["bond_yield", "nifty_pe", "earnings_yield",
                    "yield_gap", "yield_gap_ma20"]
        all_cols = [c for c in all_cols if c in df.columns]
        full_df  = df[all_cols].copy()
        full_df.index = pd.to_datetime(full_df.index).strftime("%Y-%m-%d")
        full_df.columns = [
            "Bond Yield (%)", "Nifty PE",
            "Earnings Yield (%)", "Yield Gap (%)", "Yield Gap MA20 (%)",
        ][:len(all_cols)]

        st.dataframe(
            full_df.sort_index(ascending=False)
                   .style.format("{:.3f}")
                   .background_gradient(subset=["Yield Gap (%)"], cmap="RdYlGn_r"),
            use_container_width=True,
            height=300,
        )
        st.download_button(
            "⬇ Download CSV",
            data=full_df.to_csv(),
            file_name=f"yield_gap_{date_from}_{date_to}.csv",
            mime="text/csv",
        )

    # ── Full PE history backfill (advanced) ────────────────────────────────────
    if st.session_state.get("build_full_history", False):
        _run_full_pe_backfill()


def _run_full_pe_backfill() -> None:
    """
    Download full Nifty 50 PE history from NSE archives (2011→today).
    Shows a progress bar. Runs once; subsequent loads use the saved CSV.
    """
    from data.fetcher import _pe_one_day, _save_pe_csv
    from datetime import date, timedelta
    from concurrent.futures import ThreadPoolExecutor, as_completed

    st.info("⏳ Building full PE history from NSE archives (2011→today)…")
    progress = st.progress(0)
    status   = st.empty()

    start = date(2011, 1, 1)
    today = date.today()
    all_weekdays = [
        start + timedelta(days=i)
        for i in range((today - start).days + 1)
        if (start + timedelta(days=i)).weekday() < 5
    ]

    fetched: dict = {}
    failed:  set  = set()
    total = len(all_weekdays)

    with ThreadPoolExecutor(max_workers=30) as exe:
        futs = {exe.submit(_pe_one_day, d): d for d in all_weekdays}
        done = 0
        for fut in as_completed(futs):
            done += 1
            res = fut.result()
            if res:
                fetched[res[0]] = res[1]
            else:
                failed.add(futs[fut])
            if done % 50 == 0:
                progress.progress(done / total)
                status.caption(f"Fetched {len(fetched)}/{done} | Failed {len(failed)}")

    progress.progress(1.0)
    _save_pe_csv({**{d: -1.0 for d in failed}, **fetched})
    st.success(
        f"✅ PE history built: {len(fetched)} rows fetched, "
        f"{len(failed)} dates unavailable. Reload to apply."
    )
    clear_all_caches()

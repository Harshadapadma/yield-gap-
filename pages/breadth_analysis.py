"""
pages/breadth_analysis.py
Index Breadth Analyser — renders the full UI for the breadth module.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from data.breadth_fetcher import (
    BENCHMARK_CATALOG,
    UNIVERSE_CATALOG,
    WINDOW_OPTIONS,
    clear_price_cache,
    compute_breadth_series,
    fetch_prices_batch,
    fetch_single_price,
    get_latest_snapshot,
    tickers_for_universe,
)

# Dark theme palette (matches existing app)
_BG       = "#0D1117"
_BG2      = "#161B22"
_BORDER   = "#21262D"
_BLUE     = "#58A6FF"
_GREEN    = "#3FB950"
_RED      = "#F85149"
_YELLOW   = "#F0883E"
_GREY     = "#8B949E"
_WHITE    = "#E6EDF3"
_FONT     = "IBM Plex Mono, monospace"


# ── Plotly chart builders ─────────────────────────────────────────────────────

def _base_layout(title: str, ytitle: str) -> dict:
    return dict(
        title=dict(text=title, font=dict(color=_WHITE, size=14, family=_FONT), x=0.01),
        paper_bgcolor=_BG,
        plot_bgcolor=_BG2,
        font=dict(family=_FONT, color=_GREY, size=11),
        xaxis=dict(
            gridcolor=_BORDER, showgrid=True,
            tickfont=dict(color=_GREY, size=10),
            hoverformat="%d %b %Y",
            dtick="M24", tickformat="%Y",   # show a year label every 2 years
        ),
        yaxis=dict(
            gridcolor=_BORDER, showgrid=True,
            tickfont=dict(color=_GREY, size=10),
            title=dict(text=ytitle, font=dict(color=_GREY, size=10)),
            ticksuffix="%",
        ),
        margin=dict(l=50, r=145, t=50, b=40),
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="top", y=1.08,
            xanchor="left", x=0,
            bgcolor="rgba(0,0,0,0)",
            font=dict(color=_GREY, size=10),
        ),
    )


def plot_breadth_time_series(
    df: pd.DataFrame,
    universe_name: str,
    benchmark_name: str,
    window_label: str,
    full_pct: "pd.Series | None" = None,
) -> go.Figure:
    """
    Plot breadth time series with statistical SD bands.

    Parameters
    ----------
    df         : date-filtered DataFrame (columns: pct_beating, count_eligible, benchmark_return)
    full_pct   : full-history pct_beating Series used for computing Avg/SD bands.
                 If None, falls back to df["pct_beating"].
    """
    pct = df["pct_beating"]

    # ── SD bands: always compute from monthly-resampled full history ──────────
    # Daily data has high autocorrelation → artificially narrow σ.
    # Resampling to monthly gives statistically independent points → correct σ.
    if full_pct is not None and len(full_pct) >= 10:
        _monthly = full_pct.resample("BME").last().dropna()
        _stats_pct = _monthly if len(_monthly) >= 10 else full_pct
    else:
        _stats_pct = pct
    mean = float(_stats_pct.mean())
    std  = float(_stats_pct.std())

    fig = go.Figure()

    # Subtle fill above/below the mean
    fig.add_trace(go.Scatter(
        x=pct.index, y=pct.clip(lower=mean),
        fill="tozeroy", fillcolor="rgba(63,185,80,0.07)",
        line=dict(width=0), showlegend=False, hoverinfo="skip",
    ))
    fig.add_trace(go.Scatter(
        x=pct.index, y=pct.clip(upper=mean),
        fill="tozeroy", fillcolor="rgba(248,81,73,0.07)",
        line=dict(width=0), showlegend=False, hoverinfo="skip",
    ))

    # Main line — green above mean, red below (thin, clean)
    colors = [_GREEN if v >= mean else _RED for v in pct]
    for i in range(len(pct) - 1):
        seg_x = pct.index[i : i + 2]
        seg_y = pct.iloc[i : i + 2]
        fig.add_trace(go.Scatter(
            x=seg_x, y=seg_y,
            mode="lines",
            line=dict(color=colors[i], width=1.5),
            showlegend=False,
            hoverinfo="skip",
        ))

    # Invisible hover trace (carries tooltip data)
    fig.add_trace(go.Scatter(
        x=pct.index, y=pct,
        mode="lines",
        name="% beating benchmark",
        line=dict(color=_BLUE, width=0),
        customdata=df[["count_eligible", "benchmark_return"]].values,
        hovertemplate=(
            "<b>%{y:.1f}%</b> of stocks beat benchmark<br>"
            "Benchmark return: %{customdata[1]:.1f}%<br>"
            "Eligible stocks: %{customdata[0]}<extra></extra>"
        ),
    ))

    # Thin 6-period MA (6 months for monthly data)
    ma = pct.rolling(6).mean()
    fig.add_trace(go.Scatter(
        x=ma.index, y=ma,
        mode="lines", name="6M Moving Avg",
        line=dict(color=_YELLOW, width=1.0, dash="dot"),
        opacity=0.6,
        hovertemplate="6M MA: %{y:.1f}%<extra></extra>",
    ))

    # ── Statistical SD bands (Avg, ±1σ, ±2σ) ─────────────────────────────────
    sd_levels = [
        (f"+2σ  {mean + 2*std:.1f}%",  min(mean + 2*std, 100), "#00CED1", "dash"),
        (f"+1σ  {mean +   std:.1f}%",  min(mean +   std, 100), "#3FB950", "dash"),
        (f"Avg  {mean:.1f}%",           mean,                   "#F0883E", "solid"),
        (f"−1σ  {mean -   std:.1f}%",  max(mean -   std, 0),   "#D2A8FF", "dash"),
        (f"−2σ  {mean - 2*std:.1f}%",  max(mean - 2*std, 0),   "#F85149", "dash"),
    ]
    for label, level, colour, dash in sd_levels:
        fig.add_hline(y=level, line=dict(color=colour, width=1.2, dash=dash))
        fig.add_annotation(
            x=1.01, xref="paper",
            y=level, yref="y",
            text=label, showarrow=False,
            xanchor="left", yanchor="middle",
            font=dict(color=colour, size=9, family=_FONT),
        )

    layout = _base_layout(
        f"Index Breadth · {universe_name} vs {benchmark_name}  ({window_label})",
        "% beating",
    )
    layout["yaxis"]["range"] = [0, 100]

    # ── Pin x-axis to actual data range — no empty left/right space ──────────
    if not pct.empty:
        layout["xaxis"]["range"] = [
            pct.index[0].strftime("%Y-%m-%d"),
            pct.index[-1].strftime("%Y-%m-%d"),
        ]

    fig.update_layout(**layout)
    fig.update_layout(height=480)
    return fig


def plot_return_distribution(
    snapshot_df: pd.DataFrame,
    bench_ret: float | None,
    benchmark_name: str,
) -> go.Figure:
    returns = snapshot_df["Return (%)"]
    fig = go.Figure()

    # Histogram
    fig.add_trace(go.Histogram(
        x=returns,
        nbinsx=30,
        marker=dict(
            color=[_GREEN if r > (bench_ret or 0) else _RED for r in returns],
            line=dict(color=_BG, width=0.5),
        ),
        name="Stock returns",
        hovertemplate="Return: %{x:.1f}%<br>Count: %{y}<extra></extra>",
    ))

    # Benchmark line
    if bench_ret is not None:
        fig.add_vline(
            x=bench_ret,
            line=dict(color=_YELLOW, width=2, dash="dash"),
            annotation_text=f"Benchmark: {bench_ret:.1f}%",
            annotation_position="top right",
            annotation_font=dict(color=_YELLOW, size=10, family=_FONT),
        )

    fig.update_layout(
        **_base_layout("Return Distribution (latest snapshot)", "# of stocks"),
    )
    return fig


def plot_benchmark_price(benchmark_series: pd.Series, benchmark_name: str) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=benchmark_series.index,
        y=benchmark_series,
        mode="lines",
        name=benchmark_name,
        line=dict(color=_BLUE, width=1.5),
        hovertemplate="%{y:,.2f}<extra></extra>",
    ))
    fig.update_layout(**_base_layout(f"{benchmark_name} — Price History", "Price"))
    return fig


# ── UI helpers ────────────────────────────────────────────────────────────────

def _metric(label: str, value: str, color: str = _BLUE) -> str:
    return (
        f"<div style='background:{_BG2};border:1px solid {_BORDER};"
        f"border-left:3px solid {color};border-radius:6px;"
        f"padding:10px 16px;font-family:{_FONT}'>"
        f"<div style='font-size:10px;color:{_GREY};text-transform:uppercase;"
        f"letter-spacing:1px'>{label}</div>"
        f"<div style='font-size:22px;font-weight:700;color:{color}'>{value}</div>"
        f"</div>"
    )


def _header() -> None:
    st.markdown(
        f"""
        <div style='margin-bottom:4px'>
            <span style='font-size:26px;font-weight:700;color:{_BLUE};
                         font-family:{_FONT};letter-spacing:2px'>
                📊 INDEX BREADTH ANALYSER
            </span><br>
            <span style='font-size:12px;color:{_GREY};font-family:{_FONT}'>
                "What % of Universe stocks outperform the Benchmark's rolling return?"
            </span>
        </div>
        <hr style='border-color:{_BORDER};margin:10px 0 18px 0'>
        """,
        unsafe_allow_html=True,
    )


# ── Main render function ──────────────────────────────────────────────────────

def render_breadth_analysis() -> None:
    _header()

    # ── Hardcoded settings ────────────────────────────────────────────────────
    universe_name  = "Nifty 500"
    benchmark_name = "Nifty 50"
    window_label   = "1 Year  (252 days)"
    window_days    = WINDOW_OPTIONS.get(window_label, 252)
    agg_freq       = "Monthly"     # monthly keeps clean SD bands; today's live point always appended
    min_coverage   = 80
    show_dist      = True
    show_snapshot  = True
    show_bench_px  = False

    # ── Sidebar: date filter + action buttons only ────────────────────────────
    with st.sidebar:
        st.markdown("**📅 Date Range**")
        today      = date.today()
        data_start = date(2006, 1, 1)
        from datetime import timedelta as _td
        _presets = {
            "1M": today - _td(days=30),
            "3M": today - _td(days=91),
            "6M": today - _td(days=182),
            "1Y": today - _td(days=365),
            "2Y": today - _td(days=730),
            "3Y": today - _td(days=1095),
            "5Y": today - _td(days=1825),
            "Max": data_start,
            "Custom": data_start,
        }
        _chosen = st.radio(
            "Quick range",
            list(_presets.keys()),
            index=7,
            horizontal=True,
            key="breadth_preset",
            label_visibility="collapsed",
        )
        if _chosen == "Custom":
            col_l, col_r = st.columns(2)
            with col_l:
                date_from = st.date_input(
                    "From", value=today - _td(days=365),
                    min_value=data_start, max_value=today,
                    key="breadth_from",
                )
            with col_r:
                date_to = st.date_input(
                    "To", value=today,
                    min_value=date_from, max_value=today,
                    key="breadth_to",
                )
        else:
            date_from = _presets[_chosen]
            date_to   = today

        st.divider()

        fetch_btn = st.button(
            "🚀 Fetch & Analyse",
            type="primary",
            use_container_width=True,
            key="breadth_fetch_btn",
        )
        clear_btn = st.button(
            "🗑️ Clear Price Cache",
            use_container_width=True,
            key="breadth_clear_btn",
        )

    # ── Cache clear ───────────────────────────────────────────────────────────
    if clear_btn:
        n = clear_price_cache()
        st.sidebar.success(f"Cleared {n} cached files.")

    # ── Info panel ────────────────────────────────────────────────────────────
    info_cols = st.columns([2, 2, 2, 2])
    univ_info = UNIVERSE_CATALOG[universe_name]
    bench_info = BENCHMARK_CATALOG[benchmark_name]

    with info_cols[0]:
        st.markdown(
            _metric("Universe", f"{universe_name}", _BLUE),
            unsafe_allow_html=True,
        )
    with info_cols[1]:
        st.markdown(
            _metric("Approx. size", f"~{univ_info['approx_size']} stocks", _GREY),
            unsafe_allow_html=True,
        )
    with info_cols[2]:
        st.markdown(
            _metric("Benchmark", benchmark_name, _YELLOW),
            unsafe_allow_html=True,
        )
    with info_cols[3]:
        st.markdown(
            _metric("Window", window_label.split("(")[0].strip(), _GREEN),
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

    # ── Main fetch-and-compute flow ───────────────────────────────────────────
    if not fetch_btn:
        # Check if we have cached results from a previous run in session
        cached_result = st.session_state.get("breadth_result")
        snap = cached_result.get("snapshot_df") if cached_result else None
        snap_has_price = snap is not None and "Price (₹)" in snap.columns
        if cached_result and snap_has_price \
                and cached_result.get("universe") == universe_name \
                and cached_result.get("benchmark") == benchmark_name \
                and cached_result.get("window") == window_days:
            _render_results(
                cached_result["breadth_df"],
                cached_result["bench_series"],
                cached_result["snapshot_df"],
                cached_result["bench_ret"],
                universe_name, benchmark_name, window_label,
                date_from, date_to, show_dist, show_snapshot, show_bench_px,
            )
        else:
            st.info(
                "👈 Select Universe, Benchmark, and Window in the sidebar, "
                "then click **🚀 Fetch & Analyse**."
            )
            st.markdown(
                f"""
                <div style='background:{_BG2};border:1px solid {_BORDER};
                            border-radius:8px;padding:16px 20px;
                            font-family:{_FONT};font-size:11px;color:{_GREY}'>
                    <b style='color:{_WHITE}'>ℹ️ How it works</b><br><br>
                    1. Fetches current {universe_name} constituents from NSE<br>
                    2. Downloads historical daily prices via yfinance (cached locally)<br>
                    3. At each month-end, computes each stock's {window_label} return<br>
                    4. Counts how many beat the {benchmark_name}'s {window_label} return<br>
                    5. Plots that percentage over time<br><br>
                    <b style='color:{_YELLOW}'>⚡ First run</b>: ~60–120s for 500 stocks.
                    Subsequent runs: &lt;5s from cache.<br><br>
                    <b style='color:{_RED}'>⚠️ Note</b>: Uses <i>current</i> index 
                    constituents applied historically (survivorship bias applies).
                </div>
                """,
                unsafe_allow_html=True,
            )
        return

    # ── Fetch + Compute ───────────────────────────────────────────────────────
    freq_map = {"Monthly": "BME", "Weekly": "W-FRI", "Daily": "B"}
    freq = freq_map.get(agg_freq, "BME")

    with st.status("Fetching benchmark data…", expanded=True) as status:
        bench_ticker  = bench_info["ticker"]
        bench_series  = fetch_single_price(bench_ticker)
        if bench_series.empty:
            st.error(f"Could not fetch benchmark data for {benchmark_name} ({bench_ticker})")
            return
        st.write(f"✅ Benchmark: {len(bench_series):,} daily prices for {benchmark_name}")

        st.write(f"📋 Fetching constituent list for {universe_name}…")
        tickers = tickers_for_universe(universe_name)
        if not tickers:
            st.error(
                f"Could not fetch constituent list for {universe_name}. "
                "NSE server may be unreachable. Try again later."
            )
            return
        st.write(f"✅ {len(tickers)} stocks in {universe_name}")

        # Progress state
        prog_text  = st.empty()
        prog_bar   = st.progress(0.0)

        def _progress(done: int, total: int, ticker: str) -> None:
            frac = done / total if total else 0
            prog_bar.progress(frac)
            prog_text.write(
                f"⬇️ Downloading prices: {done}/{total} tickers… ({ticker})"
            )

        st.write(f"⬇️ Downloading price data (cached tickers load instantly)…")
        prices_df = fetch_prices_batch(tickers, progress_cb=_progress)
        prog_bar.empty()
        prog_text.empty()

        if prices_df.empty:
            st.error("No price data could be fetched for the universe stocks.")
            return
        st.write(f"✅ Price data: {prices_df.shape[1]} stocks × {len(prices_df):,} days")

        st.write(f"⚙️ Computing breadth series ({agg_freq.lower()}, {window_label})…")
        breadth_df = compute_breadth_series(
            prices_df,
            bench_series,
            window_days=window_days,
            min_coverage=min_coverage / 100,
            freq=freq,
        )

        if breadth_df.empty:
            st.error(
                "Breadth computation returned no data. "
                "Try a shorter window or wider date range."
            )
            return
        st.write(f"✅ Breadth computed: {len(breadth_df)} data points")

        st.write("📸 Building latest snapshot…")
        snapshot_df, bench_ret = get_latest_snapshot(
            prices_df, bench_series, window_days, universe_name
        )
        status.update(label="✅ Done!", state="complete", expanded=False)

    # Cache in session state
    st.session_state["breadth_result"] = {
        "universe":    universe_name,
        "benchmark":   benchmark_name,
        "window":      window_days,
        "breadth_df":  breadth_df,
        "bench_series": bench_series,
        "snapshot_df": snapshot_df,
        "bench_ret":   bench_ret,
    }

    _render_results(
        breadth_df, bench_series, snapshot_df, bench_ret,
        universe_name, benchmark_name, window_label,
        date_from, date_to, show_dist, show_snapshot, show_bench_px,
    )


def _render_results(
    breadth_df: pd.DataFrame,
    bench_series: pd.Series,
    snapshot_df: pd.DataFrame,
    bench_ret: float | None,
    universe_name: str,
    benchmark_name: str,
    window_label: str,
    date_from: date,
    date_to: date,
    show_dist: bool,
    show_snapshot: bool,
    show_bench_px: bool,
) -> None:
    # Full-history pct series (for SD bands) — before date filter
    full_pct = breadth_df["pct_beating"].dropna()

    # Apply date filter
    mask = (
        (breadth_df.index.date >= date_from) &
        (breadth_df.index.date <= date_to)
    )
    df = breadth_df.loc[mask]

    if df.empty:
        st.warning("No breadth data for selected date range. Try widening it.")
        return

    latest       = df["pct_beating"].iloc[-1]
    latest_date  = df.index[-1].strftime("%Y-%m-%d")
    avg_breadth  = full_pct.mean()          # full-history average
    max_breadth  = df["pct_beating"].max()
    min_breadth  = df["pct_beating"].min()
    latest_elig  = df["count_eligible"].iloc[-1]

    # ── Key metrics ───────────────────────────────────────────────────────────
    col_color = _GREEN if latest >= 50 else _RED
    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        st.markdown(
            _metric("Latest Breadth", f"{latest:.1f}%", col_color),
            unsafe_allow_html=True,
        )
    with m2:
        st.markdown(
            _metric("Full-History Avg", f"{avg_breadth:.1f}%", _GREY),
            unsafe_allow_html=True,
        )
    with m3:
        pct_color = _GREEN if bench_ret and bench_ret > 0 else _RED
        bench_str = f"{bench_ret:.1f}%" if bench_ret is not None else "N/A"
        st.markdown(
            _metric(f"Benchmark {window_label.split('(')[0].strip()} Ret", bench_str, pct_color),
            unsafe_allow_html=True,
        )
    with m4:
        st.markdown(
            _metric("Period High", f"{max_breadth:.1f}%", _GREEN),
            unsafe_allow_html=True,
        )
    with m5:
        st.markdown(
            _metric("Period Low", f"{min_breadth:.1f}%", _RED),
            unsafe_allow_html=True,
        )

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    # ── Main breadth chart ────────────────────────────────────────────────────
    st.plotly_chart(
        plot_breadth_time_series(
            df, universe_name, benchmark_name, window_label,
            full_pct=full_pct,      # pass full history so SD bands use all data
        ),
        use_container_width=True,
    )

    # ── Secondary charts ──────────────────────────────────────────────────────
    if show_dist and show_bench_px:
        ca, cb = st.columns([3, 2])
        with ca:
            st.plotly_chart(
                plot_benchmark_price(bench_series, benchmark_name),
                use_container_width=True,
            )
        with cb:
            st.plotly_chart(
                plot_return_distribution(snapshot_df, bench_ret, benchmark_name),
                use_container_width=True,
            )
    elif show_bench_px:
        st.plotly_chart(
            plot_benchmark_price(bench_series, benchmark_name),
            use_container_width=True,
        )
    elif show_dist:
        st.plotly_chart(
            plot_return_distribution(snapshot_df, bench_ret, benchmark_name),
            use_container_width=True,
        )

    # ── Stock snapshot table ──────────────────────────────────────────────────
    if show_snapshot and not snapshot_df.empty:
        with st.expander(
            f"📋 Stock-Level Snapshot ({latest_elig} stocks · as of {latest_date})",
            expanded=False,
        ):
            col_a, col_b = st.columns([3, 1])
            with col_a:
                search = st.text_input(
                    "🔍 Filter by symbol", key="breadth_snap_search", placeholder="e.g. RELIANCE"
                )
            with col_b:
                only_winners = st.checkbox("Show only outperformers", key="breadth_snap_winners")

            disp = snapshot_df.copy()
            if search:
                disp = disp[disp["Symbol"].str.contains(search.upper(), na=False)]
            if only_winners:
                disp = disp[disp["Beats Bench"] == "✅"]

            def _color_row(row):
                c = "color: #3FB950" if row["Beats Bench"] == "✅" else "color: #F85149"
                return [c] * len(row)

            st.dataframe(
                disp.style.apply(_color_row, axis=1).format(
                    {"Price (₹)": "{:,.2f}", "Return (%)": "{:.2f}", "vs Benchmark": "{:+.2f}"},
                ),
                use_container_width=True,
                height=400,
            )
            st.download_button(
                "⬇️ Download CSV",
                data=disp.to_csv(index=False),
                file_name=f"breadth_snapshot_{universe_name.replace(' ','_')}.csv",
                mime="text/csv",
            )

    # ── Raw breadth data ──────────────────────────────────────────────────────
    with st.expander("📋 Raw Breadth Data", expanded=False):
        st.dataframe(
            df.sort_index(ascending=False)
              .style.format({
                  "pct_beating": "{:.1f}",
                  "benchmark_return": "{:+.2f}",
                  "median_stock_return": "{:+.2f}",
                  "mean_stock_return": "{:+.2f}",
              }),
            use_container_width=True,
            height=300,
        )
        st.download_button(
            "⬇️ Download Breadth CSV",
            data=df.reset_index().to_csv(index=False),
            file_name=f"breadth_{universe_name.replace(' ','_')}_vs_{benchmark_name.replace(' ','_')}.csv",
            mime="text/csv",
        )

    # ── Disclaimer ────────────────────────────────────────────────────────────
    st.markdown(
        f"""
        <div style='margin-top:16px;padding:10px 16px;border:1px solid {_BORDER};
                    border-left:3px solid {_YELLOW};border-radius:6px;
                    font-family:{_FONT};font-size:10px;color:{_GREY}'>
            ⚠️ <b style='color:{_YELLOW}'>Survivorship bias note:</b> This analysis uses 
            the <i>current</i> {universe_name} constituent list applied to all historical dates.
            Stocks that existed in 2008 but were removed later are not counted.
            Results are indicative, not investment advice.
        </div>
        """,
        unsafe_allow_html=True,
    )



# ── Alias so app.py can call render() consistently ───────────────────────────
def render() -> None:
    """Alias for render_breadth_analysis() — called by app.py routing."""
    render_breadth_analysis()

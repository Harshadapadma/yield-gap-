"""
components/charts.py
All Plotly chart construction for the Yield Gap dashboard.

Fixes vs previous version:
  • X-axis tick spacing: auto-scales by date range (no more 2-year gaps)
  • Default date range filter: last 2 years shown by default
  • Data sources table: rendered as a clean DataFrame
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go

THEME = {
    "bg":       "#0D1117",
    "paper":    "#161B22",
    "grid":     "#21262D",
    "text":     "#E6EDF3",
    "subtext":  "#8B949E",
    "yield_gap":"#58A6FF",
    "bond":     "#F0883E",
    "ey":       "#3FB950",
    "ma":       "#D2A8FF",
    "ref_pos":  "#F85149",
    "ref_neg":  "#3FB950",
    "band_fill":"rgba(88,166,255,0.07)",
}


def _x_axis_dtick(df: pd.DataFrame) -> dict:
    """
    Return xaxis tick settings that keep labels readable regardless of range.
    - > 8 years   → annual ticks,    format: 2015 / 2016 / 2020
    - 3–8 years   → 6-monthly ticks, format: Jan 2020 / Jul 2020
    - 6m–3 years  → monthly ticks,   format: Jan '22 / Feb '22
    - < 6 months  → weekly ticks
    """
    if df.empty:
        return {}
    n_days = (df.index[-1] - df.index[0]).days
    if n_days > 2920:      # > 8 years
        return dict(dtick="M12", tickformat="%Y")
    elif n_days > 1095:    # 3–8 years
        return dict(dtick="M6", tickformat="%b %Y")
    elif n_days > 182:     # 6 months–3 years
        return dict(dtick="M1", tickformat="%b '%y")
    else:                  # < 6 months
        return dict(dtick="W1", tickformat="%d %b '%y")


def _base_layout(fig: go.Figure, title: str = "", height: int = 480) -> go.Figure:
    tick_cfg = {}   # applied per chart after _x_axis_dtick is called
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color=THEME["text"]), x=0.01),
        paper_bgcolor=THEME["paper"],
        plot_bgcolor=THEME["bg"],
        font=dict(family="'IBM Plex Mono', monospace", color=THEME["text"], size=12),
        legend=dict(
            bgcolor=THEME["paper"], bordercolor=THEME["grid"],
            borderwidth=1, font=dict(size=11),
        ),
        margin=dict(l=55, r=20, t=55, b=50),
        hovermode="x unified",
        height=height,
        xaxis=dict(
            gridcolor=THEME["grid"], linecolor=THEME["grid"],
            tickfont=dict(color=THEME["subtext"]),
            rangeslider=dict(visible=False),
            **tick_cfg,
        ),
        yaxis=dict(
            gridcolor=THEME["grid"], linecolor=THEME["grid"],
            tickfont=dict(color=THEME["subtext"]),
            ticksuffix="%",
        ),
    )
    return fig


def plot_yield_gap(
    df: pd.DataFrame,
    show_components: bool = True,
    show_ma: bool = True,
    ref_lines: list[float] | None = None,
) -> go.Figure:
    if ref_lines is None:
        ref_lines = [0.0, 1.0, 3.0]

    fig = go.Figure()

    # ── ±1σ band ──────────────────────────────────────────────────────────────
    if "yield_gap_std20" in df.columns and show_ma:
        upper = df["yield_gap_ma20"] + df["yield_gap_std20"]
        lower = df["yield_gap_ma20"] - df["yield_gap_std20"]
        x = df.index.tolist() + df.index[::-1].tolist()
        y = upper.tolist() + lower[::-1].tolist()
        fig.add_trace(go.Scatter(
            x=x, y=y, fill="toself", fillcolor=THEME["band_fill"],
            line=dict(color="rgba(0,0,0,0)"),
            showlegend=True, name="±1σ Band (20d)", hoverinfo="skip",
        ))

    # ── Bond yield & earnings yield (optional) ────────────────────────────────
    if show_components:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["bond_yield"],
            name="India 10Y Bond Yield",
            line=dict(color=THEME["bond"], width=1.5, dash="dot"),
            opacity=0.85,
            customdata=df[["nifty_pe"]].values,
            hovertemplate="Bond: %{y:.3f}%<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=df.index, y=df["earnings_yield"],
            name="Earnings Yield (1/PE×100)",
            line=dict(color=THEME["ey"], width=1.5, dash="dash"),
            opacity=0.85,
            hovertemplate="EY: %{y:.3f}%<extra></extra>",
        ))

    # ── 20d MA ────────────────────────────────────────────────────────────────
    if show_ma and "yield_gap_ma20" in df.columns:
        fig.add_trace(go.Scatter(
            x=df.index, y=df["yield_gap_ma20"],
            name="Yield Gap MA20",
            line=dict(color=THEME["ma"], width=1.2),
            hovertemplate="MA20: %{y:.3f}%<extra></extra>",
        ))

    # ── Main yield gap line ────────────────────────────────────────────────────
    fig.add_trace(go.Scatter(
        x=df.index, y=df["yield_gap"],
        name="Yield Gap",
        line=dict(color=THEME["yield_gap"], width=2.5),
        fill="tozeroy", fillcolor="rgba(88,166,255,0.06)",
        hovertemplate=(
            "<b>%{x|%d %b %Y}</b><br>"
            "Gap: %{y:.3f}%<extra></extra>"
        ),
    ))

    # ── Reference lines ───────────────────────────────────────────────────────
    for level in sorted(set(ref_lines)):
        color = (THEME["ref_pos"] if level > 0
                 else THEME["ref_neg"] if level < 0
                 else THEME["subtext"])
        fig.add_hline(
            y=level,
            line=dict(color=color, width=1, dash="longdash"),
            annotation_text=f"  {level:+.1f}%",
            annotation_font=dict(color=color, size=10),
            annotation_position="right",
        )

    _base_layout(fig, "Yield Gap  (India 10Y Bond Yield − Nifty 50 Earnings Yield)", 490)

    # Apply proper x-axis tick spacing
    xt = _x_axis_dtick(df)
    fig.update_xaxes(**xt)

    return fig


def plot_components_area(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()

    fig.add_trace(go.Scatter(
        x=df.index, y=df["bond_yield"],
        name="India 10Y Bond Yield",
        line=dict(color=THEME["bond"], width=2),
        fill="tozeroy", fillcolor="rgba(240,136,62,0.12)",
        hovertemplate="Bond: %{y:.3f}%<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=df.index, y=df["earnings_yield"],
        name="Nifty 50 Earnings Yield",
        line=dict(color=THEME["ey"], width=2),
        fill="tozeroy", fillcolor="rgba(63,185,80,0.12)",
        hovertemplate="EY: %{y:.3f}%<extra></extra>",
    ))

    _base_layout(fig, "Bond Yield vs Earnings Yield", 320)
    fig.update_xaxes(**_x_axis_dtick(df))
    return fig


def plot_distribution(df: pd.DataFrame) -> go.Figure:
    latest = float(df["yield_gap"].iloc[-1])
    fig = go.Figure()
    fig.add_trace(go.Histogram(
        x=df["yield_gap"], nbinsx=40,
        name="Historical Distribution",
        marker=dict(
            color=THEME["yield_gap"], opacity=0.75,
            line=dict(color=THEME["bg"], width=0.5),
        ),
    ))
    fig.add_vline(
        x=latest,
        line=dict(color=THEME["ref_pos"], width=2, dash="dash"),
        annotation_text=f"  Now: {latest:.2f}%",
        annotation_font=dict(color=THEME["ref_pos"], size=11),
    )
    _base_layout(fig, "Yield Gap — Historical Distribution", 280)
    fig.update_layout(
        yaxis=dict(ticksuffix="", title="Count"),
        xaxis=dict(title="Yield Gap (%)", dtick=None),   # reset suffix for histogram
    )
    return fig


def build_data_source_table(df: pd.DataFrame) -> pd.DataFrame:
    """
    Returns a tidy DataFrame of the last 10 rows suitable for
    st.dataframe() in the Data Sources expander.
    Shows: Date | Bond Yield | Nifty PE | Earnings Yield | Yield Gap
    """
    cols = ["bond_yield", "nifty_pe", "earnings_yield", "yield_gap"]
    cols = [c for c in cols if c in df.columns]
    out = df[cols].tail(10).copy()
    out.index = pd.to_datetime(out.index).strftime("%d %b %Y")
    out.index.name = "Date"
    rename = {
        "bond_yield":     "Bond Yield (%)",
        "nifty_pe":       "Nifty PE",
        "earnings_yield": "Earnings Yield (%)",
        "yield_gap":      "Yield Gap (%)",
    }
    out.rename(columns=rename, inplace=True)
    return out.sort_index(ascending=False)

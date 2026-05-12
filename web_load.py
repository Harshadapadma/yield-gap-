"""
web_load.py — Cache-only data loading for Streamlit and Flask.

Reads pre-fetched CSVs committed by GitHub Actions.
Zero network calls — every function returns immediately from disk.
"""

from __future__ import annotations

import pandas as pd
from pathlib import Path

_ROOT     = Path(__file__).resolve().parent
_SEED_DIR = _ROOT / "data" / "seed"
_LIVE_DIR = _ROOT / "data" / "live"
_CACHE    = _ROOT / "cache"


# ── Bond yield ────────────────────────────────────────────────────────────────

def load_bond_yield_cached() -> tuple[pd.Series, dict]:
    """Merge seed CSVs + live CSV → DatetimeIndex Series of bond yields (%)."""
    frames = []

    # Seed 1 — Investing.com format: "Date","Price",...  date= "DD-MM-YYYY"
    s1 = _SEED_DIR / "india_10y_bond_yield_seed.csv"
    if s1.exists():
        try:
            df = pd.read_csv(s1, usecols=[0, 1])
            df.columns = ["date", "yield"]
            df["yield"] = pd.to_numeric(df["yield"].astype(str).str.replace(",", ""), errors="coerce")
            df["date"]  = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
            df = df.dropna().set_index("date")["yield"]
            frames.append(df)
        except Exception:
            pass

    # Seed 2 — same Investing.com format
    s2 = _SEED_DIR / "bond_seed_2026feb10_apr16.csv"
    if s2.exists():
        try:
            df = pd.read_csv(s2, usecols=[0, 1])
            df.columns = ["date", "yield"]
            df["yield"] = pd.to_numeric(df["yield"].astype(str).str.replace(",", ""), errors="coerce")
            df["date"]  = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
            df = df.dropna().set_index("date")["yield"]
            frames.append(df)
        except Exception:
            pass

    # Live CSV — date,yield  (ISO dates, updated by GH Actions)
    live = _LIVE_DIR / "bond_daily_live.csv"
    if live.exists():
        try:
            df = pd.read_csv(live, parse_dates=["date"])
            df = df.rename(columns={df.columns[1]: "yield"})
            df["yield"] = pd.to_numeric(df["yield"], errors="coerce")
            df = df.dropna().set_index("date")["yield"]
            frames.append(df)
        except Exception:
            pass

    if not frames:
        return pd.Series(dtype=float), {"message": "No bond yield data found", "live_ok": False}

    s = pd.concat(frames)
    s = s.sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s = s[s > 0]
    latest = s.index[-1].date() if not s.empty else None
    return s, {"message": f"Cached — latest {latest}", "live_ok": True, "source": "CSV cache"}


# ── Nifty 50 close ────────────────────────────────────────────────────────────

def load_nifty_cached() -> tuple[pd.Series, dict]:
    """Read Nifty 50 close prices from cache/nifty_close.csv."""
    f = _CACHE / "nifty_close.csv"
    if not f.exists():
        return pd.Series(dtype=float), {"message": "nifty_close.csv not found", "live_ok": False}
    try:
        df = pd.read_csv(f, parse_dates=["date"])
        s  = df.set_index("date")["close"]
        s  = s[s > 0].sort_index()
        s  = s[~s.index.duplicated(keep="last")]
        latest = s.index[-1].date() if not s.empty else None
        return s, {"message": f"Cached — latest {latest}", "live_ok": True, "source": "CSV cache"}
    except Exception as exc:
        return pd.Series(dtype=float), {"message": str(exc), "live_ok": False}


# ── Nifty PE ──────────────────────────────────────────────────────────────────

def load_pe_history_cached() -> tuple[pd.Series, dict]:
    """Merge PE seed CSVs + live CSV → DatetimeIndex Series of PE ratios."""
    frames = []

    for fname in ("nifty_pe_seed.csv", "nifty_pe_seed_2017_2022.csv"):
        f = _SEED_DIR / fname
        if f.exists():
            try:
                df = pd.read_csv(f, parse_dates=[0])
                df.columns = ["date", "pe"]
                df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
                df = df[df["pe"] > 0].dropna().set_index("date")["pe"]
                frames.append(df)
            except Exception:
                pass

    # data/live/nifty_pe_history.csv — date,pe  (updated by GH Actions)
    live = _LIVE_DIR / "nifty_pe_history.csv"
    if live.exists():
        try:
            df = pd.read_csv(live, parse_dates=["date"])
            df.columns = ["date", "pe"]
            df["pe"] = pd.to_numeric(df["pe"], errors="coerce")
            df = df[df["pe"] > 0].dropna().set_index("date")["pe"]
            frames.append(df)
        except Exception:
            pass

    if not frames:
        return pd.Series(dtype=float), {"message": "No PE data found", "live_ok": False}

    s = pd.concat(frames).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    latest = s.index[-1].date() if not s.empty else None
    return s, {"message": f"Cached — latest {latest}", "live_ok": True, "source": "CSV cache"}

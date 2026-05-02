"""
data/cache.py
─────────────
Thin read/write helpers for all CSV-backed caches.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd


def load_cache(path: Path) -> pd.Series | None:
    """Load a 2-column (date, value) CSV as a pd.Series. Returns None on failure."""
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
        df.columns    = [c.strip() for c in df.columns]
        date_col      = df.columns[0]
        value_col     = df.columns[1]
        df[date_col]  = pd.to_datetime(df[date_col], errors="coerce")
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
        df = df.dropna(subset=[date_col, value_col])
        if df.empty:
            return None
        s = df.set_index(date_col)[value_col].sort_index()
        s.index = pd.to_datetime(s.index).normalize()
        return s[~s.index.duplicated(keep="last")]
    except Exception:
        return None


def save_cache(series: pd.Series, path: Path) -> None:
    """Save a Series as a 2-column (date, close) CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df = series.reset_index()
    df.columns = ["date", "close"]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["close"])
    df = df.drop_duplicates(subset=["date"], keep="last").sort_values("date")
    df.to_csv(path, index=False)


def load_manual_entries(path: Path) -> pd.Series | None:
    """Load manual override CSV. Same format as load_cache."""
    return load_cache(path)


def save_manual_entry(
    entry_date: str,
    bond_yield: float | None,
    pe_val: float | None,
    path: Path,
) -> None:
    """
    Append or overwrite a manual bond-yield entry for entry_date.
    File format: date, bond_yield, pe  (pe may be NaN)
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    import pandas as _pd

    new_row = _pd.DataFrame({
        "date":       [entry_date],
        "bond_yield": [bond_yield],
        "pe":         [pe_val],
    })
    if path.exists():
        df = _pd.read_csv(path, dtype={"date": str})
        df = df[df["date"].astype(str).str.strip() != str(entry_date).strip()]
        df = _pd.concat([df, new_row], ignore_index=True)
    else:
        df = new_row
    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    df.to_csv(path, index=False)
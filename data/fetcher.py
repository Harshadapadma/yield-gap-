"""
data/fetcher.py
═══════════════════════════════════════════════════════════════════════════════
LIVE-FIRST FETCH STRATEGY
═══════════════════════════════════════════════════════════════════════════════

BOND YIELD  (India 10Y G-sec)
──────────────────────────────
Merge order on every app open:
  1. Seed CSV(s)   data/seed/india_10y_bond_yield_seed.csv  (static history)
  2. Seed gap-fill data/seed/bond_seed_2026feb10_apr16.csv  (optional)
  3. Live CSV      data/live/bond_daily_live.csv            (one row/trading day)
  4. Manual CSV    cache/manual_overrides.csv               (user entries)

Then ALWAYS fetches today's live value and UPSERTS into live CSV:
  • During market hours (09:15–16:00 IST) → fetch + overwrite today's row each load
  • After market close  (≥16:00 IST)      → same — official close written and kept
  • Weekend                               → still tries to fetch (yields move globally)

Bond live sources (tried in order, first success wins):
  1. Stooq.com    – 10inbnd.b  (free, no auth, most reliable)
  2. tvDatafeed   – TVC:IN10Y  (pip install tvdatafeed)
  3. yfinance     – IN10YT=RR / GIND10YR=X
  4. FRED         – IRLTLT01INM156N (monthly → last known)
  5. Nasdaq DL    – needs NASDAQ_API_KEY env var

NIFTY 50 PRICE  (^NSEI)
─────────────────────────
  1. Load cache/nifty_close.csv for full history (instant from disk)
  2. ALWAYS re-fetch last 5 days from yfinance / Stooq on every open
  3. Merge and save back to cache

NIFTY 50 PE
────────────
  fetch_pe_ratio()   → today's live scalar  (nifty-pe-ratio.com → NSE API → CSV fallback)
  fetch_pe_history() → full daily PE series (seed CSVs + NSE archives + live CSV)
"""

from __future__ import annotations

import io
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

from data.cache import load_cache, save_cache, load_manual_entries
from utils.config import (
    BOND_SEED_1, BOND_SEED_2, BOND_LIVE,
    PE_HISTORY, SEED_DIR,
    NIFTY_CACHE, MANUAL_CACHE,
)

logger = logging.getLogger(__name__)

# ── IST helpers ────────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    return datetime.now(IST)

def _today_ist() -> date:
    return _now_ist().date()

def _is_market_hours() -> bool:
    now  = _now_ist()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= mins < (16 * 60)

# ── Shared HTTP session ────────────────────────────────────────────────────────
_S = requests.Session()
_S.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

_NSE_ARCHIVE_START = date(2011, 1, 1)
_PE_SEED_GLOB      = "nifty_pe_seed*.csv"


# ══════════════════════════════════════════════════════════════════════════════
#  SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _clean(s: pd.Series, name: str) -> pd.Series:
    s = s.copy()
    s.index = pd.to_datetime(s.index).normalize()
    s = s.sort_index().dropna()
    s = s[~s.index.duplicated(keep="last")]
    s.name = name
    return s


def _yield_valid(v) -> bool:
    try:
        return 3.0 <= float(v) <= 15.0
    except Exception:
        return False


def _read_live_csv(path: Path, value_col: str) -> pd.Series:
    if not path.exists():
        return pd.Series(dtype=float, name=value_col)
    try:
        df = pd.read_csv(path, dtype={"date": str})
        df.columns    = [c.strip() for c in df.columns]
        if "date" not in df.columns or value_col not in df.columns:
            return pd.Series(dtype=float, name=value_col)
        df["date"]     = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
        df[value_col]  = pd.to_numeric(df[value_col], errors="coerce")
        df             = df.dropna(subset=["date", value_col])
        s              = df.set_index("date")[value_col].sort_index()
        s.name         = value_col
        return s
    except Exception as exc:
        logger.warning("_read_live_csv(%s): %s", path.name, exc)
        return pd.Series(dtype=float, name=value_col)


def _upsert_live_csv(path: Path, value_col: str, day: date, value: float) -> None:
    """
    Write (or overwrite) a single row for `day` in the live CSV.
    Called on EVERY app open → today's value is always the freshest fetched value.
    """
    day_str = day.strftime("%Y-%m-%d")
    try:
        if path.exists():
            df = pd.read_csv(path, dtype={"date": str})
            df.columns = [c.strip() for c in df.columns]
            if "date" not in df.columns:
                df = pd.DataFrame(columns=["date", value_col])
            df = df[df["date"].astype(str).str.strip() != day_str]
            df = df.dropna(subset=["date"])
        else:
            df = pd.DataFrame(columns=["date", value_col])

        new_row = pd.DataFrame({"date": [day_str], value_col: [round(float(value), 4)]})
        df = pd.concat([df, new_row], ignore_index=True)
        df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        df.to_csv(path, index=False)
        logger.info("Upserted %s %s=%.4f into %s", day_str, value_col, value, path.name)
    except Exception as exc:
        logger.warning("_upsert_live_csv(%s) failed: %s", path.name, exc)


def _read_bond_seed(path: Path) -> pd.Series:
    """
    Read a bond yield seed CSV.
    Supports:
      • Lean:           date,yield        (YYYY-MM-DD, float)
      • Investing.com:  "Date","Price",…  (quoted, dayfirst)
    """
    if not path.exists():
        return pd.Series(dtype=float, name="bond_yield")
    try:
        first_line = path.read_text(encoding="utf-8", errors="ignore").split("\n", 1)[0]
        cols_raw   = [c.strip().strip('"').lower() for c in first_line.split(",")]

        if "yield" in cols_raw or len(cols_raw) == 2:
            df          = pd.read_csv(path, dtype={"date": str})
            df.columns  = [c.strip().lower() for c in df.columns]
            val_col     = [c for c in df.columns if c != "date"][0]
            df["date"]  = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
            df[val_col] = pd.to_numeric(df[val_col], errors="coerce")
            s = df.dropna(subset=["date", val_col]).set_index("date")[val_col]
        else:
            # Investing.com quoted CSV format
            df = pd.read_csv(path, usecols=[0, 1])
            df.columns = ["date", "yield"]
            df["date"] = (
                df["date"].astype(str).str.strip().str.strip('"')
                .pipe(lambda x: pd.to_datetime(x, dayfirst=True, errors="coerce"))
            )
            df["yield"] = pd.to_numeric(
                df["yield"].astype(str).str.strip().str.strip('"'), errors="coerce"
            )
            s = df.dropna(subset=["date", "yield"]).set_index("date")["yield"]

        s = _clean(s, "bond_yield")
        s = s[(s >= 3.0) & (s <= 15.0)]
        return s
    except Exception as exc:
        logger.warning("_read_bond_seed(%s): %s", path.name, exc)
        return pd.Series(dtype=float, name="bond_yield")


# ══════════════════════════════════════════════════════════════════════════════
#  BOND YIELD — LIVE SOURCES
# ══════════════════════════════════════════════════════════════════════════════


def _live_bond_nse_gsec() -> float | None:
    """NSE India live G-Sec data — find the ~10Y benchmark bond's YTM."""
    try:
        # Warm up session to get cookies
        _S.get("https://www.nseindia.com", timeout=6)
        r = _S.get(
            "https://www.nseindia.com/api/liveBonds-traded-in-capital-market?type=gsec",
            headers={
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/market-data/bonds-traded-in-capital-market",
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=10,
        )
        if r.status_code != 200:
            return None
        bonds = r.json().get("data", [])
        today_d = _today_ist()
        ten_yr: list[float] = []
        for b in bonds:
            mat_str = b.get("maturityDate") or b.get("matDt") or ""
            ytm_raw = b.get("yieldToMaturity") or b.get("ytm") or b.get("yield") or 0
            if not mat_str or not ytm_raw:
                continue
            try:
                mat = pd.to_datetime(mat_str, dayfirst=True).date()
                years = (mat - today_d).days / 365.25
                if 8.0 <= years <= 12.0:
                    v = float(str(ytm_raw).replace(",", ""))
                    if _yield_valid(v):
                        ten_yr.append(v)
            except Exception:
                continue
        if ten_yr:
            v = float(sorted(ten_yr)[len(ten_yr) // 2])   # median
            logger.info("NSE G-Sec 10Y: %.3f%%", v)
            return v
    except Exception as exc:
        logger.debug("NSE G-Sec: %s", exc)
    return None



def _live_bond_trading_economics() -> float | None:
    """Trading Economics — India 10Y bond yield (inlined JS data)."""
    try:
        r = _S.get(
            "https://tradingeconomics.com/india/government-bond-yield",
            timeout=12,
        )
        if r.status_code != 200:
            return None
        m = re.search(r'"last"\s*:\s*([\d.]+)', r.text)
        if m:
            v = float(m.group(1))
            if _yield_valid(v):
                logger.info("Trading Economics bond: %.3f%%", v)
                return v
    except Exception as exc:
        logger.debug("Trading Economics: %s", exc)
    return None


def _live_bond_investing_com() -> float | None:
    """Investing.com — India 10Y bond yield (Chrome TLS fingerprint)."""
    try:
        from curl_cffi import requests as cfr  # type: ignore
        r = cfr.get(
            "https://in.investing.com/rates-bonds/india-10-year-bond-yield",
            impersonate="chrome",
            timeout=14,
        )
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")
        for selector in [
            '[data-test="instrument-price-last"]',
            ".text-5xl",
            "#last_last",
            ".pid-21622-last",
            '[class*="last-price"]',
        ]:
            el = soup.select_one(selector)
            if el:
                try:
                    v = float(el.get_text(strip=True).replace(",", ""))
                    if _yield_valid(v):
                        logger.info("Investing.com bond: %.3f%%", v)
                        return v
                except ValueError:
                    pass
        # Fallback: regex scan for yield-like number near key phrases
        m = re.search(
            r'(?:india|10.?year|bond)[^<]{0,80}?(\d+\.\d+)',
            r.text, re.IGNORECASE,
        )
        if m:
            v = float(m.group(1))
            if _yield_valid(v):
                logger.info("Investing.com bond (regex): %.3f%%", v)
                return v
    except Exception as exc:
        logger.debug("Investing.com: %s", exc)
    return None


def _live_bond_marketwatch() -> float | None:
    """MarketWatch — India 10Y bond yield CSV download."""
    try:
        end   = _today_ist()
        start = end - timedelta(days=14)
        url = (
            "https://www.marketwatch.com/investing/bond/ldbmkin-10y/downloadsreturns"
            f"?startdate={start.strftime('%m/%d/%Y')}"
            f"&enddate={end.strftime('%m/%d/%Y')}"
            "&frequency=p1d&csvdownload=true&downloadpartial=false"
            "&newdates=false&countrycode=bx"
        )
        r = _S.get(url, timeout=12, headers={
            "Referer": "https://www.marketwatch.com/",
            "Accept": "text/html,application/xhtml+xml,*/*",
        })
        if r.status_code != 200 or len(r.text) < 30:
            return None
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.strip() for c in df.columns]
        for col in ["Price", "Value", "Close", "Last", "Yield"]:
            if col in df.columns:
                vals = pd.to_numeric(
                    df[col].astype(str).str.replace("%", "").str.replace(",", ""),
                    errors="coerce",
                ).dropna()
                if not vals.empty:
                    v = float(vals.iloc[-1])
                    if _yield_valid(v):
                        logger.info("MarketWatch bond: %.3f%%", v)
                        return v
    except Exception as exc:
        logger.debug("MarketWatch: %s", exc)
    return None


def _live_bond_fred() -> float | None:
    """FRED — IRLTLT01INM156N (monthly → last known official rate)."""
    try:
        resp = _S.get(
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=IRLTLT01INM156N",
            timeout=12,
        )
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text.strip()))
        df.columns = ["date", "value"]
        df["value"] = pd.to_numeric(df["value"], errors="coerce")
        df = df.dropna(subset=["value"])
        if not df.empty:
            v = float(df["value"].iloc[-1])
            if _yield_valid(v):
                logger.info("FRED bond: %.3f%%", v)
                return v
    except Exception as exc:
        logger.debug("FRED: %s", exc)
    return None


def _live_bond_nasdaq() -> float | None:
    """Nasdaq Data Link — needs NASDAQ_API_KEY env var."""
    api_key = os.environ.get("NASDAQ_API_KEY", "")
    if not api_key:
        return None
    try:
        url  = (f"https://data.nasdaq.com/api/v3/datasets/RBI/GSEC_10Y.csv"
                f"?api_key={api_key}&rows=3&order=desc")
        resp = _S.get(url, timeout=12)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text))
        if not df.empty:
            v = float(df.iloc[0, 1])
            if _yield_valid(v):
                logger.info("Nasdaq DL bond: %.3f%%", v)
                return v
    except Exception as exc:
        logger.debug("Nasdaq DL: %s", exc)
    return None


def _fetch_live_bond_value() -> tuple[float | None, str]:
    """
    Try all live bond sources in order.
    Returns (value, source_name) or (None, "all_failed").
    Always called fresh — no caching here.
    """
    sources = [
        ("Trading Economics", _live_bond_trading_economics),  # ✅ confirmed working
        ("Investing.com",     _live_bond_investing_com),      # ✅ confirmed working
        ("NSE G-Sec",         _live_bond_nse_gsec),           # works when market is open
        ("FRED",              _live_bond_fred),               # monthly, last resort
        ("Nasdaq DL",         _live_bond_nasdaq),             # free with API key from data.nasdaq.com
    ]
    errors = {}
    for name, fn in sources:
        try:
            v = fn()
            if v is not None:
                return v, name
        except Exception as exc:
            errors[name] = str(exc)
            logger.warning("Live bond [%s]: %s", name, exc)
    logger.error("All live bond sources failed: %s", errors)
    return None, "all_failed"


# ══════════════════════════════════════════════════════════════════════════════
#  BOND YIELD — PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def fetch_bond_yield(start_date: str = "2006-01-01") -> tuple[pd.Series, dict]:
    """
    Returns (daily_series, status_dict).

    Called on every app open. Always fetches + upserts today's live value.
    Merge order: seed-1 → seed-2 → live_csv → manual_overrides → today's live fetch.

    status keys: source, today_value, today_source, live_ok, message
    """
    today    = _today_ist()
    today_ts = pd.Timestamp(today)

    # ── 1. Load all historical parts from disk ────────────────────────────────
    parts: list[pd.Series]  = []
    source_parts: list[str] = []

    s1 = _read_bond_seed(BOND_SEED_1)
    if not s1.empty:
        parts.append(s1)
        source_parts.append(f"seed1({s1.index[0].date()}→{s1.index[-1].date()})")

    s2 = _read_bond_seed(BOND_SEED_2)
    if not s2.empty:
        parts.append(s2)
        source_parts.append(f"seed2({len(s2)}d)")

    live_hist = _read_live_csv(BOND_LIVE, "yield")
    if not live_hist.empty:
        live_hist.name = "bond_yield"
        parts.append(live_hist)
        source_parts.append(
            f"live_csv({live_hist.index[0].date()}→{live_hist.index[-1].date()})"
        )

    # Manual overrides (highest priority for historical corrections)
    manual = load_manual_entries(MANUAL_CACHE)
    if manual is not None and not manual.empty:
        manual = _clean(manual, "bond_yield")
        manual = manual[(manual >= 3.0) & (manual <= 15.0)]
        if not manual.empty:
            parts.append(manual)
            source_parts.append(f"manual({len(manual)}d)")

    # Bootstrap from Stooq full history if no seeds present
    if not parts:
        logger.warning("No seed CSVs — bootstrapping from Stooq full history")
        try:
            r = _S.get("https://stooq.com/q/d/l/?s=10inbnd.b&i=d", timeout=20)
            r.raise_for_status()
            df_stooq = pd.read_csv(
                io.StringIO(r.text), parse_dates=["Date"], index_col="Date"
            )
            if not df_stooq.empty and "Close" in df_stooq.columns:
                s_stooq = _clean(df_stooq["Close"].dropna(), "bond_yield")
                s_stooq = s_stooq[(s_stooq >= 3.0) & (s_stooq <= 15.0)]
                if not s_stooq.empty:
                    parts.append(s_stooq)
                    source_parts.append(f"stooq_bootstrap({len(s_stooq)}d)")
        except Exception as exc:
            raise RuntimeError(
                f"No seed CSVs found in data/seed/ and Stooq bootstrap failed ({exc}).\n"
                "Download india_10y_bond_yield_seed.csv from Investing.com → "
                "India 10-Year Bond → Historical Data and place in data/seed/."
            )

    if not parts:
        raise RuntimeError(
            "Bond yield data completely unavailable.\n"
            "Place india_10y_bond_yield_seed.csv in data/seed/ and re-run."
        )

    # ── 2. Merge all parts (later wins for same date) ─────────────────────────
    historical = pd.concat(parts)
    historical = historical[~historical.index.duplicated(keep="last")].sort_index()
    historical = _clean(historical, "bond_yield")

    # ── 3. ALWAYS fetch today's live value on every app open ──────────────────
    live_val, live_src = _fetch_live_bond_value()
    live_ok = live_val is not None

    if live_ok:
        _upsert_live_csv(BOND_LIVE, "yield", today, live_val)
        historical[today_ts] = live_val
        historical = historical.sort_index()
        source_parts.append(f"LIVE({today}={live_val:.3f}%,{live_src})")
        logger.info("Bond yield %s: %.4f%% via %s", today, live_val, live_src)
    else:
        source_parts.append("LIVE(failed→last_known)")
        logger.warning("All live bond sources failed — showing last known value")

    # ── 4. Filter to requested start date ─────────────────────────────────────
    combined = historical[historical.index >= pd.Timestamp(start_date)]
    if combined.empty:
        raise RuntimeError(
            f"Bond yield series empty after filtering from {start_date}. "
            "Check seed CSV date range."
        )

    today_val = (
        float(combined.loc[today_ts])
        if today_ts in combined.index
        else float(combined.iloc[-1])
    )

    return combined, {
        "source":       " + ".join(source_parts),
        "today_value":  today_val,
        "today_source": live_src if live_ok else "last_known",
        "live_ok":      live_ok,
        "success":      True,
        "message": (
            f"{'✅' if live_ok else '⚠️'} {len(combined)} rows | "
            f"{combined.index[0].date()} → {combined.index[-1].date()} | "
            f"today={today_val:.3f}%"
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  NIFTY 50 PRICE — PUBLIC ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def fetch_nifty(period: str = "max") -> tuple[pd.Series, dict]:
    """
    Returns (daily_close_series, status_dict).

    Every app open:
      1. Load cache/nifty_close.csv (full history, instant)
      2. Always re-fetch last 5 days from yfinance / Stooq
      3. Merge + save back to cache
    """
    today = _today_ist()

    # ── 1. Load existing cache ────────────────────────────────────────────────
    cached: pd.Series | None = None
    if NIFTY_CACHE.exists():
        try:
            c = load_cache(NIFTY_CACHE)
            if c is not None and len(c) > 10:
                cached = _clean(c, "nifty_close")
        except Exception as exc:
            logger.warning("Nifty cache read: %s", exc)

    # ── 2. Always fetch latest 5 days ─────────────────────────────────────────
    recent: pd.Series | None = None
    live_src = ""

    try:
        import yfinance as yf  # type: ignore
        df_yf = yf.download(
            "^NSEI", period="5d", interval="1d",
            progress=False, auto_adjust=True,
        )
        if df_yf is not None and not df_yf.empty:
            col = df_yf["Close"]
            if isinstance(col, pd.DataFrame):
                col = col.squeeze()
            recent   = _clean(col.dropna(), "nifty_close")
            live_src = "yfinance"
            logger.info("Nifty 5d: %d rows, latest=%.2f",
                        len(recent), float(recent.iloc[-1]) if not recent.empty else 0)
    except Exception as exc:
        logger.debug("yfinance Nifty 5d: %s", exc)

    # Stooq fallback
    if recent is None or recent.empty:
        try:
            r = _S.get("https://stooq.com/q/d/l/?s=%5Ensei&i=d", timeout=12)
            if r.status_code == 200 and "Date" in r.text:
                df_sq = pd.read_csv(
                    io.StringIO(r.text), parse_dates=["Date"], index_col="Date"
                )
                recent   = _clean(df_sq["Close"].dropna().tail(10), "nifty_close")
                live_src = "Stooq"
        except Exception as exc:
            logger.debug("Stooq Nifty: %s", exc)

    # ── 3. Merge cache + recent ────────────────────────────────────────────────
    if cached is not None and recent is not None and not recent.empty:
        merged = pd.concat([cached, recent])
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        series = _clean(merged, "nifty_close")

    elif recent is not None and not recent.empty:
        # First run — no cache exists yet — download full history once
        try:
            import yfinance as yf  # type: ignore
            df_full = yf.download(
                "^NSEI", period="max", interval="1d",
                progress=False, auto_adjust=True,
            )
            col = df_full["Close"]
            if isinstance(col, pd.DataFrame):
                col = col.squeeze()
            series   = _clean(col.dropna(), "nifty_close")
            live_src = "yfinance (full history)"
            logger.info("Nifty full history: %d rows", len(series))
        except Exception:
            series = recent

    elif cached is not None:
        series   = cached
        live_src = "cache_only"
        logger.warning("Nifty: all live sources failed — using cached data")
    else:
        raise RuntimeError(
            "Nifty 50 unavailable: no cache and all live sources failed.\n"
            "Check connectivity to finance.yahoo.com or stooq.com"
        )

    # ── 4. Save merged series back to cache ───────────────────────────────────
    try:
        save_cache(series, NIFTY_CACHE)
    except Exception as exc:
        logger.warning("Nifty cache save: %s", exc)

    today_val = float(series.iloc[-1])
    return series, {
        "source":      live_src,
        "today_value": today_val,
        "live_ok":     live_src != "cache_only",
        "success":     True,
        "message": (
            f"{'✅' if live_src != 'cache_only' else '⚠️'} {len(series)} rows | "
            f"{series.index[0].date()} → {series.index[-1].date()} | "
            f"latest={today_val:,.0f}"
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  NIFTY 50 PE — TODAY'S LIVE SCALAR
# ══════════════════════════════════════════════════════════════════════════════

def fetch_pe_ratio() -> tuple[float, dict]:
    """
    Fetch today's live Nifty 50 PE ratio.
    Sources: nifty-pe-ratio.com → NSE API → PE history CSV → default 21.27
    """
    for name, fn in [
        ("nifty-pe-ratio.com", _pe_from_site),
        ("NSE India API",      _pe_from_nse),
    ]:
        try:
            pe = fn()
            if 10 <= pe <= 60:
                return pe, {
                    "source": name, "success": True,
                    "message": f"✅ {name} → PE={pe:.2f}",
                }
        except Exception as exc:
            logger.warning("PE [%s]: %s", name, exc)

    # Fallback: last known from PE history CSV
    s = _read_live_csv(PE_HISTORY, "pe")
    if not s.empty:
        pe = float(s.dropna().iloc[-1])
        if 10 <= pe <= 60:
            return pe, {
                "source": "pe_history_csv", "success": True,
                "message": f"⚠️ Last PE from history: {pe:.2f} ({s.index[-1].date()})",
            }

    return 21.27, {
        "source": "default", "success": False,
        "message": "⚠️ All PE sources failed — using default 21.27",
    }


def _pe_from_site() -> float:
    r = _S.get("https://nifty-pe-ratio.com/", timeout=8)
    r.raise_for_status()
    m = re.search(
        r"(?:current|latest|nifty\s*50)\s*(?:P/?E|PE)\s*(?:ratio)?"
        r"\s*(?:is)?\s*(\d+\.?\d*)",
        r.text, re.IGNORECASE,
    )
    if m:
        return float(m.group(1))
    soup = BeautifulSoup(r.text, "lxml")
    for el in soup.find_all(["span", "div", "strong", "b"]):
        t = el.get_text(strip=True)
        if re.match(r"^\d{1,2}\.\d{1,2}$", t):
            v = float(t)
            if 10 <= v <= 60:
                return v
    raise ValueError("Could not parse PE from nifty-pe-ratio.com")


def _pe_from_nse() -> float:
    _S.get("https://www.nseindia.com", timeout=6)
    r = _S.get(
        "https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050",
        headers={"Accept": "application/json", "Referer": "https://www.nseindia.com/"},
        timeout=8,
    )
    r.raise_for_status()
    data = r.json()
    if "metadata" in data and "pe" in data["metadata"]:
        return float(data["metadata"]["pe"])
    for e in data.get("data", []):
        if e.get("symbol") == "NIFTY 50" and "pe" in e:
            return float(e["pe"])
    raise ValueError("PE not found in NSE API response")


# ══════════════════════════════════════════════════════════════════════════════
#  NIFTY 50 PE — FULL DAILY HISTORY
# ══════════════════════════════════════════════════════════════════════════════

def fetch_pe_history(start_date: str = "2006-01-01") -> tuple[pd.Series, dict]:
    """
    Full daily Nifty 50 PE history.
    Returns (daily_pe_series, status_dict).

    Sources merged in order:
      1. Seed CSVs in data/seed/nifty_pe_seed*.csv
      2. Live CSV  data/live/nifty_pe_history.csv
      3. NSE archive per-day (last 30 days, parallelised)
    """
    today           = _today_ist()
    SENTINEL_DAYS   = 30   # skip dates with sentinel=-1.0 for this many days
    LOOKBACK_DAYS   = 30   # only fetch last N days on normal app loads

    # ── 1. Load existing live CSV (may have sentinel rows: pe == -1.0) ────────
    existing_raw: dict[date, float] = {}
    if PE_HISTORY.exists():
        try:
            df_ex = pd.read_csv(PE_HISTORY, dtype={"date": str, "pe": float})
            df_ex["date"] = pd.to_datetime(
                df_ex["date"], format="%Y-%m-%d", errors="coerce"
            )
            df_ex = df_ex.dropna(subset=["date"])
            existing_raw = dict(
                zip(df_ex["date"].dt.date, df_ex["pe"].astype(float))
            )
        except Exception as exc:
            logger.warning("PE history CSV read: %s", exc)

    real_known = {d: v for d, v in existing_raw.items() if v > 0}
    skip_dates = {
        d for d, v in existing_raw.items()
        if v < 0 and (today - d).days <= SENTINEL_DAYS
    }

    # ── 2. Load all seed PE CSVs ──────────────────────────────────────────────
    seed_pe  = _load_pe_seed_csv()
    has_seed = len(seed_pe) > 0
    all_real: dict[date, float] = {**seed_pe, **real_known}

    # ── 3. Determine which dates still need fetching ──────────────────────────
    lookback_start = today - timedelta(days=LOOKBACK_DAYS)
    all_weekdays   = [
        lookback_start + timedelta(days=i)
        for i in range((today - lookback_start).days + 1)
        if (lookback_start + timedelta(days=i)).weekday() < 5
    ]
    to_fetch = [
        d for d in all_weekdays
        if d not in all_real and d not in skip_dates and d <= today
    ]

    fetched: dict[date, float] = {}
    failed:  set[date]         = set()

    for d in [x for x in to_fetch if x < _NSE_ARCHIVE_START]:
        failed.add(d)
    archive_missing = [d for d in to_fetch if d >= _NSE_ARCHIVE_START]

    if archive_missing:
        logger.info(
            "Fetching PE for %d dates from NSE archives…", len(archive_missing)
        )
        with ThreadPoolExecutor(max_workers=20) as exe:
            futs = {exe.submit(_pe_one_day, d): d for d in archive_missing}
            for fut in as_completed(futs):
                res = fut.result()
                if res:
                    fetched[res[0]] = res[1]
                else:
                    failed.add(futs[fut])
        logger.info(
            "NSE PE archives: %d fetched, %d failed", len(fetched), len(failed)
        )

    # ── 4. Persist updates ────────────────────────────────────────────────────
    if fetched or failed:
        updated = dict(existing_raw)
        for d, pe in fetched.items():
            updated[d] = pe
        for d in failed:
            updated[d] = -1.0   # sentinel
        _save_pe_csv(updated)

    # ── 5. Build output series ────────────────────────────────────────────────
    combined_real = {**all_real, **fetched}
    if not combined_real:
        fallback = 21.27
        idx = pd.date_range(pd.Timestamp(start_date), pd.Timestamp(today), freq="D")
        return (
            pd.Series(fallback, index=idx, name="nifty_pe"),
            {
                "source": "flat_fallback", "success": False, "has_seed": False,
                "message": f"⚠️ All PE sources failed. Using flat PE={fallback:.2f}",
            },
        )

    s = pd.Series(
        {pd.Timestamp(k): v for k, v in sorted(combined_real.items())},
        name="nifty_pe",
    )
    s = _clean(s, "nifty_pe")
    full_idx = pd.date_range(pd.Timestamp(start_date), pd.Timestamp(today), freq="D")
    s = s.reindex(full_idx).ffill(limit=5)
    s.name = "nifty_pe"

    valid  = s.dropna()
    d0     = valid.index[0].date() if not valid.empty else None
    d1     = valid.index[-1].date() if not valid.empty else None
    n_real = len(combined_real)

    src_parts = []
    if has_seed:
        src_parts.append(f"seed({len(seed_pe)}d)")
    if fetched:
        src_parts.append(f"NSE(+{len(fetched)}new)")
    if n_real - len(fetched) > 0:
        src_parts.append(f"cache({n_real - len(fetched)}d)")
    src = " + ".join(src_parts) or "cache"

    return s, {
        "source":   src,
        "success":  True,
        "has_seed": has_seed,
        "message":  f"✅ {src} | {n_real} rows | {d0} → {d1}",
    }


# ── PE helpers ─────────────────────────────────────────────────────────────────

def _pe_one_day(d: date) -> tuple[date, float] | None:
    """Fetch Nifty 50 PE for one day from NSE archives CSV."""
    url = (
        f"https://archives.nseindia.com/content/indices/"
        f"ind_close_all_{d.strftime('%d%m%Y')}.csv"
    )
    hdrs = {
        "User-Agent": _S.headers["User-Agent"],
        "Referer": "https://archives.nseindia.com/",
    }

    def _parse(text: str) -> float | None:
        if "P/E" not in text:
            return None
        try:
            df  = pd.read_csv(io.StringIO(text))
            row = df[df["Index Name"] == "Nifty 50"]
            if row.empty:
                return None
            pe = float(row["P/E"].iloc[0])
            return pe if 5.0 < pe < 200.0 else None
        except Exception:
            return None

    # Attempt 1: plain requests
    try:
        r = requests.get(url, headers=hdrs, timeout=8)
        if r.status_code == 200:
            pe = _parse(r.text)
            if pe is not None:
                return (d, pe)
    except Exception:
        pass

    # Attempt 2: curl_cffi Chrome TLS fingerprint
    try:
        from curl_cffi import requests as cfr  # type: ignore
        r2 = cfr.get(url, headers=hdrs, impersonate="chrome", timeout=8)
        if r2.status_code == 200:
            pe = _parse(r2.text)
            if pe is not None:
                return (d, pe)
    except Exception:
        pass

    return None


def _load_pe_seed_csv() -> dict[date, float]:
    """Merge ALL nifty_pe_seed*.csv files from data/seed/."""
    seed_files = sorted(SEED_DIR.glob(_PE_SEED_GLOB))
    if not seed_files:
        return {}
    merged: dict[date, float] = {}
    for path in seed_files:
        merged.update(_parse_one_pe_seed(path))
    if merged:
        logger.info(
            "PE seed: %d rows from %d file(s) (%s → %s)",
            len(merged), len(seed_files), min(merged), max(merged),
        )
    return merged


def _parse_one_pe_seed(path: Path) -> dict[date, float]:
    """Parse a single PE seed CSV. Returns {} on any error."""
    try:
        raw = pd.read_csv(path)
        raw.columns = [
            c.strip().strip('"').lower().replace(" ", "_") for c in raw.columns
        ]
        cols = list(raw.columns)
        df   = pd.DataFrame()

        if "pe" in cols and "date" in cols:
            df["date"] = pd.to_datetime(raw["date"], dayfirst=False, errors="coerce")
            df["pe"]   = pd.to_numeric(raw["pe"], errors="coerce")
        elif "price" in cols and "date" in cols:
            date_str   = raw["date"].astype(str).str.strip('"')
            df["date"] = pd.to_datetime(date_str, format="%d-%m-%Y", errors="coerce")
            if df["date"].isna().all():
                df["date"] = pd.to_datetime(date_str, errors="coerce")
            df["pe"] = pd.to_numeric(
                raw["price"].astype(str).str.strip('"'), errors="coerce"
            )
        elif "p/e" in cols and "index_date" in cols:
            df["date"] = pd.to_datetime(
                raw["index_date"].astype(str), dayfirst=True, errors="coerce"
            )
            df["pe"] = pd.to_numeric(raw["p/e"], errors="coerce")
        elif "p/e" in cols and "date" in cols:
            df["date"] = pd.to_datetime(
                raw["date"].astype(str), dayfirst=True, errors="coerce"
            )
            df["pe"] = pd.to_numeric(raw["p/e"], errors="coerce")
        else:
            logger.debug("PE seed %s: unrecognised columns %s", path.name, cols)
            return {}

        df = df.dropna(subset=["date", "pe"])
        df = df[df["pe"].between(5.0, 200.0)]
        return dict(zip(df["date"].dt.date, df["pe"].astype(float)))
    except Exception as exc:
        logger.warning("PE seed %s parse error: %s", path.name, exc)
        return {}


def _save_pe_csv(data: dict[date, float]) -> None:
    """Write full sorted, deduped PE CSV (sentinel rows where pe == -1.0 included)."""
    if not data:
        return
    df = pd.DataFrame(sorted(data.items()), columns=["date", "pe"])
    df["date"] = df["date"].astype(str)
    df.to_csv(PE_HISTORY, index=False)
    logger.debug("Saved %d PE rows to %s", len(df), PE_HISTORY.name)
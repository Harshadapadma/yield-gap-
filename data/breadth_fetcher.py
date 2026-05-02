"""
data/breadth_fetcher.py
Fetches NSE index constituent lists and historical prices, then computes
rolling breadth (% of Universe stocks beating Benchmark return) over time.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd
import yfinance as yf

from data.index_store import INSTRUMENTS as INDEX_INSTRUMENTS
from data.index_store import get_price as get_index_price

log = logging.getLogger(__name__)

# ── Cache directories ─────────────────────────────────────────────────────────
_ROOT       = Path(__file__).resolve().parent.parent
_CACHE_DIR  = _ROOT / "cache" / "breadth"
_PRICE_DIR  = _CACHE_DIR / "prices"
_CONST_DIR  = _CACHE_DIR / "constituents"
for _d in [_PRICE_DIR, _CONST_DIR]:
    _d.mkdir(parents=True, exist_ok=True)

# ── Catalog ───────────────────────────────────────────────────────────────────

# Universes that have constituent lists (NSE indices)
UNIVERSE_CATALOG: dict[str, dict] = {
    "Nifty 50":           {"nse_slug": "nifty50",          "approx_size": 50},
    "Nifty 100":          {"nse_slug": "nifty100",         "approx_size": 100},
    "Nifty 200":          {"nse_slug": "nifty200",         "approx_size": 200},
    "Nifty 500":          {"nse_slug": "nifty500",         "approx_size": 500},
    "Nifty Midcap 150":   {"nse_slug": "niftymidcap150",   "approx_size": 150},
    "Nifty Smallcap 250": {"nse_slug": "niftysmallcap250", "approx_size": 250},
    "Nifty Next 50":      {"nse_slug": "niftynext50",      "approx_size": 50},
}

# Benchmark instruments (single price series)
BENCHMARK_CATALOG: dict[str, dict] = {
    "Nifty 50":         {"ticker": INDEX_INSTRUMENTS["Nifty 50"], "label": "Nifty 50"},
    "Sensex":           {"ticker": INDEX_INSTRUMENTS["Sensex"], "label": "Sensex"},
    "Bank Nifty":       {"ticker": INDEX_INSTRUMENTS["Nifty Bank"], "label": "Bank Nifty"},
    "Nifty Midcap 100": {"ticker": INDEX_INSTRUMENTS["Nifty Midcap 100"], "label": "Nifty Midcap 100"},
    "Nifty 500":        {"ticker": INDEX_INSTRUMENTS["Nifty 500"], "label": "Nifty 500"},
    "Gold (INR–ETF)":   {"ticker": "GOLDBEES.NS",  "label": "Gold INR"},
    "Gold (USD/oz)":    {"ticker": "GC=F",         "label": "Gold USD"},
    "Silver (USD/oz)":  {"ticker": "SI=F",         "label": "Silver USD"},
    "USD/INR":          {"ticker": "USDINR=X",     "label": "USD/INR"},
    "IT Index (Nifty IT)": {"ticker": "^CNXIT",    "label": "Nifty IT"},
    "Pharma (Nifty Pharma)": {"ticker": "^CNXPHARMA", "label": "Nifty Pharma"},
}

WINDOW_OPTIONS: dict[str, int] = {
    "1 Year  (252 days)":  252,
    "6 Months (126 days)": 126,
    "3 Months  (63 days)":  63,
    "1 Month   (21 days)":  21,
}

DATA_START = "2006-01-01"
_IST = timezone(timedelta(hours=5, minutes=30))

# ── Constituent list helpers ───────────────────────────────────────────────────

_NSE_CSV_URL = (
    "https://nsearchives.nseindia.com/content/indices/ind_{slug}list.csv"
)
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.nseindia.com/",
}

_NSE_FALLBACK_URLS = [
    "https://nsearchives.nseindia.com/content/indices/ind_{slug}list.csv",
    "https://www1.nseindia.com/content/indices/ind_{slug}list.csv",
]


def _nifty_symbols_from_csv(slug: str) -> list[str]:
    """Parse NSE CSV (columns: Company Name, Industry, Symbol, Series, ISIN Code)."""
    import io, requests
    for url_tmpl in _NSE_FALLBACK_URLS:
        url = url_tmpl.format(slug=slug)
        try:
            r = requests.get(url, headers=_NSE_HEADERS, timeout=20)
            r.raise_for_status()
            df = pd.read_csv(io.BytesIO(r.content))
            # Symbol column may be named "Symbol" directly
            sym_col = next(
                (c for c in df.columns if "symbol" in c.lower()),
                None,
            )
            if sym_col:
                symbols = df[sym_col].dropna().str.strip().tolist()
                return [s for s in symbols if s]
        except Exception as exc:
            log.warning("NSE CSV fetch failed (%s): %s", url, exc)
    return []


def fetch_constituent_list(universe_name: str, max_age_hours: int = 24) -> list[str]:
    """
    Return list of NSE symbols (without .NS suffix) for the given universe.
    Cached to disk; refreshed if cache is older than max_age_hours.
    """
    info  = UNIVERSE_CATALOG[universe_name]
    slug  = info["nse_slug"]
    cache = _CONST_DIR / f"{slug}.csv"

    # Use cache if fresh
    if cache.exists():
        age_h = (time.time() - cache.stat().st_mtime) / 3600
        if age_h < max_age_hours:
            return pd.read_csv(cache)["symbol"].tolist()

    symbols = _nifty_symbols_from_csv(slug)
    if symbols:
        pd.DataFrame({"symbol": symbols}).to_csv(cache, index=False)
        log.info("Fetched %d symbols for %s", len(symbols), universe_name)
    elif cache.exists():
        # stale but better than nothing
        log.warning("NSE fetch failed; using stale cache for %s", universe_name)
        return pd.read_csv(cache)["symbol"].tolist()

    return symbols


def tickers_for_universe(universe_name: str) -> list[str]:
    """Return yfinance tickers (with .NS suffix) for a universe."""
    symbols = fetch_constituent_list(universe_name)
    return [f"{s}.NS" for s in symbols]


# ── Price fetching ─────────────────────────────────────────────────────────────

def _price_cache_path(ticker: str) -> Path:
    safe = ticker.replace("/", "_").replace("^", "_").replace("=", "_")
    return _PRICE_DIR / f"{safe}.csv"


def _now_ist() -> datetime:
    return datetime.now(_IST)


def _is_market_hours() -> bool:
    now = _now_ist()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= mins < (16 * 60)


def _after_close() -> bool:
    return _now_ist().hour >= 16


def _load_cached_price(ticker: str) -> pd.Series | None:
    path = _price_cache_path(ticker)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        if "close" not in df.columns:
            return None
        s = df["close"].dropna()
        s.index = pd.to_datetime(s.index)
        return s.sort_index()
    except Exception:
        return None


def _save_cached_price(ticker: str, series: pd.Series, persist_today: bool | None = None) -> None:
    path = _price_cache_path(ticker)
    s = series.dropna().sort_index()
    s.index = pd.to_datetime(s.index).normalize()
    if persist_today is None:
        persist_today = not _is_market_hours()
    if not persist_today:
        today = _now_ist().date()
        s = s[s.index.date < today]
    if not s.empty:
        s.to_frame("close").to_csv(path)


def _is_cache_fresh(ticker: str, max_age_hours: int = 24) -> bool:
    path = _price_cache_path(ticker)
    if not path.exists():
        return False
    cached = _load_cached_price(ticker)
    if cached is None or cached.empty:
        return False

    now = _now_ist()
    today = now.date()
    last_date = cached.index[-1].date()

    if _is_market_hours():
        # Fetch once market is open when today's row is not yet available.
        return last_date >= today

    if _after_close():
        # After close, the persisted cache should have today's closing row.
        return last_date >= today

    age_h = (time.time() - path.stat().st_mtime) / 3600
    return age_h < max_age_hours and last_date >= today - timedelta(days=5)


def fetch_single_price(ticker: str, start: str = DATA_START) -> pd.Series:
    """Fetch a single instrument's daily close price, with cache."""
    if ticker in INDEX_INSTRUMENTS.values():
        s, _ = get_index_price(ticker, start_date=start)
        return s

    if _is_cache_fresh(ticker):
        cached = _load_cached_price(ticker)
        if cached is not None and not cached.empty:
            return cached[cached.index >= pd.Timestamp(start)]

    try:
        raw = yf.download(
            ticker,
            start=start,
            auto_adjust=True,
            progress=False,
            timeout=30,
        )
        if raw.empty:
            log.warning("Empty data for %s", ticker)
            return _load_cached_price(ticker) or pd.Series(dtype=float)

        # yfinance ≥0.2 returns MultiIndex columns when single ticker
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.droplevel(1)
        close_col = next(
            (c for c in raw.columns if "close" in c.lower()),
            raw.columns[0],
        )
        s = raw[close_col].dropna()
        s.index = pd.to_datetime(s.index).normalize()
        s = s.sort_index()
        _save_cached_price(ticker, s, persist_today=not _is_market_hours())
        return s
    except Exception as exc:
        log.warning("Price fetch error for %s: %s", ticker, exc)
        cached = _load_cached_price(ticker)
        if cached is not None:
            return cached[cached.index >= pd.Timestamp(start)]
        return pd.Series(dtype=float)


def fetch_prices_batch(
    tickers: list[str],
    start: str = DATA_START,
    batch_size: int = 50,
    progress_cb: Callable[[int, int, str], None] | None = None,
) -> pd.DataFrame:
    """
    Fetch Adj Close prices for many tickers in batches.
    Returns DataFrame with dates as index, tickers as columns.
    Skips tickers that have fresh cache entries.

    progress_cb(done, total, ticker_name) – called after each batch.
    """
    all_series: dict[str, pd.Series] = {}
    stale = [t for t in tickers if not _is_cache_fresh(t)]
    fresh = [t for t in tickers if _is_cache_fresh(t)]
    persist_today = not _is_market_hours()

    # Load fresh from disk
    for t in fresh:
        s = _load_cached_price(t)
        if s is not None and not s.empty:
            all_series[t] = s

    # Batch-download stale/missing
    total = len(stale)
    done  = 0
    for i in range(0, total, batch_size):
        batch = stale[i : i + batch_size]
        try:
            raw = yf.download(
                batch,
                start=start,
                auto_adjust=True,
                progress=False,
                timeout=60,
                group_by="ticker",
            )
            for t in batch:
                try:
                    if isinstance(raw.columns, pd.MultiIndex):
                        if t in raw.columns.get_level_values(0):
                            sub = raw[t]
                        else:
                            sub = pd.DataFrame()
                    else:
                        sub = raw if len(batch) == 1 else pd.DataFrame()

                    close_col = next(
                        (c for c in (sub.columns if hasattr(sub, "columns") else [])
                         if "close" in str(c).lower()),
                        None,
                    )
                    if close_col is not None and not sub.empty:
                        s = sub[close_col].dropna()
                        s.index = pd.to_datetime(s.index).normalize()
                        s = s.sort_index()
                        if not s.empty:
                            all_series[t] = s
                            _save_cached_price(t, s, persist_today=persist_today)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Batch download error (batch %d): %s", i, exc)

        done += len(batch)
        if progress_cb:
            last = batch[-1] if batch else ""
            progress_cb(done, total, last)

    # Build wide DataFrame
    if not all_series:
        return pd.DataFrame()

    df = pd.DataFrame(all_series)
    df.index = pd.to_datetime(df.index)
    df.sort_index(inplace=True)
    df = df[df.index >= pd.Timestamp(start)]
    return df


# ── Breadth computation ────────────────────────────────────────────────────────

def compute_breadth_series(
    universe_prices: pd.DataFrame,
    benchmark_series: pd.Series,
    window_days: int = 252,
    min_coverage: float = 0.80,
    freq: str = "BME",          # business month-end
) -> pd.DataFrame:
    """
    At each resampled date, compute:
      - benchmark N-day return
      - for each stock: N-day return
      - pct_beating = fraction of stocks that beat benchmark
      - count_eligible = number of stocks with enough data

    Returns DataFrame with columns:
      date | pct_beating | count_eligible | benchmark_return | median_stock_return
    """
    bench = benchmark_series.copy().sort_index().dropna()
    prices = universe_prices.copy().sort_index()

    # Common date grid (resample to business month-end for speed)
    end   = min(bench.index.max(), prices.index.max())
    start = max(bench.index.min(), prices.index.min())
    # Need lookback window before start
    calc_start = start + timedelta(days=window_days + 10)
    grid = pd.date_range(calc_start, end, freq=freq)

    records = []
    for dt in grid:
        # Get lookback date
        dt_back = dt - timedelta(days=window_days)

        # Benchmark return
        bench_at  = bench.asof(dt)
        bench_bk  = bench.asof(dt_back)
        if pd.isna(bench_at) or pd.isna(bench_bk) or bench_bk == 0:
            continue
        bench_ret = (bench_at / bench_bk) - 1.0

        # Stock returns
        stock_rets = []
        for col in prices.columns:
            s = prices[col].dropna()
            if len(s) < window_days * min_coverage:
                continue
            v_now  = s.asof(dt)
            v_back = s.asof(dt_back)
            if pd.isna(v_now) or pd.isna(v_back) or v_back <= 0:
                continue
            stock_rets.append((v_now / v_back) - 1.0)

        if not stock_rets:
            continue

        sr = pd.Series(stock_rets)
        pct_beating = (sr > bench_ret).mean() * 100.0

        records.append({
            "date":             dt,
            "pct_beating":      round(pct_beating, 2),
            "count_eligible":   len(sr),
            "benchmark_return": round(bench_ret * 100, 2),
            "median_stock_return": round(sr.median() * 100, 2),
            "mean_stock_return": round(sr.mean() * 100, 2),
        })

    if not records:
        return pd.DataFrame()

    df_out = pd.DataFrame(records).set_index("date")
    df_out.index = pd.to_datetime(df_out.index)
    return df_out


def get_latest_snapshot(
    universe_prices: pd.DataFrame,
    benchmark_series: pd.Series,
    window_days: int = 252,
    universe_name: str = "",
) -> pd.DataFrame:
    """
    Returns a per-stock table with their N-day return, benchmark return,
    and whether they beat the benchmark (as of the latest available date).
    """
    bench = benchmark_series.dropna().sort_index()
    prices = universe_prices.sort_index()

    dt      = min(bench.index.max(), prices.index.max())
    dt_back = dt - timedelta(days=window_days)

    bench_at  = bench.asof(dt)
    bench_bk  = bench.asof(dt_back)
    bench_ret = ((bench_at / bench_bk) - 1.0) * 100 if bench_bk else None

    rows = []
    for col in prices.columns:
        s  = prices[col].dropna()
        va = s.asof(dt)
        vb = s.asof(dt_back)
        if pd.isna(va) or pd.isna(vb) or vb <= 0:
            continue
        ret = ((va / vb) - 1.0) * 100
        sym = col.replace(".NS", "")
        rows.append({
            "Symbol":      sym,
            "Price (₹)":   round(float(va), 2),
            "Return (%)":  round(ret, 2),
            "Beats Bench": "✅" if (bench_ret is not None and ret > bench_ret) else "❌",
        })

    df = pd.DataFrame(rows)
    if bench_ret is not None:
        df["vs Benchmark"] = df["Return (%)"] - bench_ret
        df["vs Benchmark"] = df["vs Benchmark"].round(2)
    df.sort_values("Return (%)", ascending=False, inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df, bench_ret


def clear_price_cache(tickers: list[str] | None = None) -> int:
    """Delete cached price CSVs. If tickers=None, clear all."""
    count = 0
    if tickers is None:
        for p in _PRICE_DIR.glob("*.csv"):
            p.unlink()
            count += 1
    else:
        for t in tickers:
            p = _price_cache_path(t)
            if p.exists():
                p.unlink()
                count += 1
    return count

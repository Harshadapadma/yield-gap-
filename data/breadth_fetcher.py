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


def _is_cache_fresh(ticker: str) -> bool:
    """File-mtime freshness check — no CSV read needed. 4-day gap covers weekends/holidays."""
    path = _price_cache_path(ticker)
    if not path.exists():
        return False
    try:
        mod_date = datetime.fromtimestamp(path.stat().st_mtime, tz=_IST).date()
    except OSError:
        return False
    return (_now_ist().date() - mod_date).days <= 4


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
    cache_only: bool = False,
) -> pd.DataFrame:
    """
    Fetch Adj Close prices for many tickers in parallel batches.
    - Tickers with fresh cache are loaded from disk only.
    - Stale tickers that already have a cache get an incremental tail update
      (only the missing recent days), not a full re-download from DATA_START.
    - Brand-new tickers (no cache at all) are downloaded from `start`.
    All network batches run in parallel via ThreadPoolExecutor.

    cache_only=True: skip all yfinance calls; read committed CSVs only.
    progress_cb(done, total, ticker_name) – called after each batch completes.
    """
    import concurrent.futures

    # Cache-only mode: read every ticker from disk, no network calls at all.
    if cache_only:
        all_series: dict[str, pd.Series] = {}
        for i, t in enumerate(tickers):
            s = _load_cached_price(t)
            if s is not None and not s.empty:
                all_series[t] = s[s.index >= pd.Timestamp(start)]
            if progress_cb and (i % 50 == 0 or i == len(tickers) - 1):
                progress_cb(i + 1, len(tickers), t)
        if not all_series:
            return pd.DataFrame()
        df = pd.DataFrame(all_series)
        df.index = pd.to_datetime(df.index)
        df.sort_index(inplace=True)
        return df[df.index >= pd.Timestamp(start)]

    all_series: dict[str, pd.Series] = {}
    persist_today = not _is_market_hours()

    fresh = [t for t in tickers if _is_cache_fresh(t)]
    stale = [t for t in tickers if not _is_cache_fresh(t)]

    for t in fresh:
        s = _load_cached_price(t)
        if s is not None and not s.empty:
            all_series[t] = s[s.index >= pd.Timestamp(start)]

    # Split stale: incremental (cache gap ≤30 days) vs new/too-old (full re-fetch)
    today_dt = _now_ist().date()
    incremental: list[tuple[str, pd.Series]] = []
    new_tickers:  list[str] = []
    for t in stale:
        cached = _load_cached_price(t)
        if cached is not None and not cached.empty:
            gap_days = (today_dt - cached.index[-1].date()).days
            if gap_days <= 30:
                all_series[t] = cached[cached.index >= pd.Timestamp(start)]
                incremental.append((t, cached))
            else:
                new_tickers.append(t)
        else:
            new_tickers.append(t)

    total    = len(stale)
    done_ref = [0]

    def _report(n: int, last_ticker: str = "") -> None:
        done_ref[0] = min(done_ref[0] + n, total)
        if progress_cb:
            progress_cb(done_ref[0], total, last_ticker)

    def _extract(raw, batch: list[str]) -> dict[str, pd.Series]:
        """Pull per-ticker Close series out of a yf.download result."""
        out: dict[str, pd.Series] = {}
        for t in batch:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    sub = raw[t] if t in raw.columns.get_level_values(0) else pd.DataFrame()
                else:
                    sub = raw if len(batch) == 1 else pd.DataFrame()
                close_col = next(
                    (c for c in getattr(sub, "columns", []) if "close" in str(c).lower()),
                    None,
                )
                if close_col and not sub.empty:
                    s = sub[close_col].dropna()
                    s.index = pd.to_datetime(s.index).normalize()
                    out[t] = s.sort_index()
            except Exception:
                pass
        return out

    # ── Incremental: download only the recent tail and merge ──────────────────
    if incremental:
        cached_map = {t: s for t, s in incremental}
        max_last   = max(s.index[-1] for s in cached_map.values())
        inc_start  = (max_last - timedelta(days=10)).strftime("%Y-%m-%d")
        inc_tickers = [t for t, _ in incremental]

        def _inc_batch(batch: list[str]) -> dict[str, pd.Series]:
            try:
                raw = yf.download(batch, start=inc_start, auto_adjust=True,
                                  progress=False, timeout=60, group_by="ticker")
                out: dict[str, pd.Series] = {}
                for t, new_s in _extract(raw, batch).items():
                    merged = pd.concat([cached_map[t], new_s]).sort_index()
                    merged = merged[~merged.index.duplicated(keep="last")]
                    out[t]  = merged
                return out
            except Exception:
                return {}

        batches = [inc_tickers[i:i + batch_size]
                   for i in range(0, len(inc_tickers), batch_size)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(batches) or 1)) as pool:
            futs = {pool.submit(_inc_batch, b): b for b in batches}
            for fut in concurrent.futures.as_completed(futs):
                b = futs[fut]
                for t, s in fut.result().items():
                    all_series[t] = s[s.index >= pd.Timestamp(start)]
                    _save_cached_price(t, s, persist_today=persist_today)
                _report(len(b), b[-1] if b else "")

    # ── New tickers: parallel batch download, capped to 4 years ─────────────
    if new_tickers:
        _four_yr = (datetime.now(_IST) - timedelta(days=4 * 365)).strftime("%Y-%m-%d")
        effective_start = _four_yr if start < _four_yr else start

        def _new_batch(batch: list[str]) -> dict[str, pd.Series]:
            try:
                raw = yf.download(batch, start=effective_start, auto_adjust=True,
                                  progress=False, timeout=60, group_by="ticker")
                return _extract(raw, batch)
            except Exception:
                return {}

        batches = [new_tickers[i:i + batch_size]
                   for i in range(0, len(new_tickers), batch_size)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(batches) or 1)) as pool:
            futs = {pool.submit(_new_batch, b): b for b in batches}
            for fut in concurrent.futures.as_completed(futs):
                b = futs[fut]
                for t, s in fut.result().items():
                    all_series[t] = s
                    _save_cached_price(t, s, persist_today=persist_today)
                _report(len(b), b[-1] if b else "")

    if not all_series:
        return pd.DataFrame()

    df = pd.DataFrame(all_series)
    df.index = pd.to_datetime(df.index)
    df.sort_index(inplace=True)
    return df[df.index >= pd.Timestamp(start)]


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
    bench  = benchmark_series.copy().sort_index().dropna()
    prices = universe_prices.copy().sort_index()

    end   = min(bench.index.max(), prices.index.max())
    start = max(bench.index.min(), prices.index.min())

    # Reindex both to a shared business-day calendar and forward-fill small gaps
    bdays = pd.bdate_range(start, end)
    prices_bd = prices.reindex(bdays).ffill(limit=5)
    bench_bd  = bench.reindex(bdays).ffill(limit=5)

    # Drop stocks that never had enough data for the window
    min_obs = int(window_days * min_coverage)
    prices_bd = prices_bd.loc[:, prices_bd.notna().sum() >= min_obs]

    # Vectorised window returns for every business day (no Python loop over stocks)
    stock_rets = prices_bd.pct_change(window_days)   # (days × stocks)
    bench_rets = bench_bd.pct_change(window_days)    # (days,)

    # Resample to the requested frequency — keep last value in each period
    stock_monthly = stock_rets.resample(freq).last()
    bench_monthly = bench_rets.resample(freq).last()

    # Always include the most recent available date
    if stock_monthly.empty or stock_monthly.index[-1] < end:
        last_stock = stock_rets.loc[stock_rets.index <= end].tail(1)
        last_bench = bench_rets.loc[bench_rets.index <= end].tail(1)
        last_stock.index = pd.DatetimeIndex([end])
        last_bench.index = pd.DatetimeIndex([end])
        stock_monthly = pd.concat([stock_monthly, last_stock])
        bench_monthly = pd.concat([bench_monthly, last_bench])
        stock_monthly = stock_monthly[~stock_monthly.index.duplicated(keep="last")]
        bench_monthly = bench_monthly[~bench_monthly.index.duplicated(keep="last")]

    # Drop periods before we have a full window of data
    calc_start = start + timedelta(days=window_days + 10)
    stock_monthly = stock_monthly[stock_monthly.index >= calc_start]
    bench_monthly = bench_monthly.reindex(stock_monthly.index)

    records = []
    for dt in stock_monthly.index:
        b_ret = bench_monthly.get(dt)
        if b_ret is None or pd.isna(b_ret):
            continue
        row = stock_monthly.loc[dt].dropna()
        if row.empty:
            continue
        pct_beating = float((row > b_ret).mean() * 100)
        records.append({
            "date":                dt,
            "pct_beating":         round(pct_beating, 2),
            "count_eligible":      len(row),
            "benchmark_return":    round(float(b_ret * 100), 2),
            "median_stock_return": round(float(row.median() * 100), 2),
            "mean_stock_return":   round(float(row.mean() * 100), 2),
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
        if s.empty:
            continue
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

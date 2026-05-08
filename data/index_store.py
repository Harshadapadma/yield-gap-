"""
data/index_store.py
Unified fetch + persist layer for any price instrument (indices, ETFs, commodities).

All data is stored in  data/live/indices/<safe_ticker>.csv
  columns: date (YYYY-MM-DD), close

Nifty Smallcap 100 (^CNXSC)
----------------------------
Uses NSE's historical indices API:
  GET https://www.nseindia.com/api/historical/indices
      ?indexType=NIFTY%20SMALLCAP%20100&from=DD-MM-YYYY&to=DD-MM-YYYY

Returns CH_CLOSING_VALUE in actual index points (~17900). Correct scale.
Fetched in 90-day chunks, incrementally — only missing tail fetched after
the first full build. Real-time: latest quote injected from the same API
during market hours.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# ── Storage ───────────────────────────────────────────────────────────────────
_ROOT    = Path(__file__).resolve().parent.parent
_IDX_DIR = _ROOT / "data" / "live" / "indices"
_IDX_DIR.mkdir(parents=True, exist_ok=True)

from datetime import datetime as _datetime, timezone as _tz, timedelta as _td
_IST = _tz(_td(hours=5, minutes=30))

# ── NSE archive name → ticker ─────────────────────────────────────────────────
# ^CNXSC excluded — uses NSE historical indices API (correct ~17900 scale)
_NSE_NAME_TO_TICKER = {
    "Nifty 50":           "^NSEI",
    "Nifty Bank":         "^NSEBANK",
    "Nifty Midcap 100":   "NIFTY_MIDCAP_100.NS",
    "Nifty IT":           "^CNXIT",
    "Nifty Pharma":       "^CNXPHARMA",
    "Nifty Auto":         "^CNXAUTO",
    "Nifty FMCG":         "^CNXFMCG",
    "Nifty Metal":        "^CNXMETAL",
    "Nifty Energy":       "^CNXENERGY",
    "Nifty Realty":       "^CNXREALTY",
    "Nifty Next 50":      "NIFTYNEXT50",
    "Nifty 500":          "NIFTY500",
}
_TICKER_TO_NSE_NAME: dict[str, str] = {v: k for k, v in _NSE_NAME_TO_TICKER.items()}

# niftyindices.com name mapping (NIFTY500)
_NIFTYINDICES_NAME: dict[str, str] = {
    "NIFTY500": "Nifty 500",
}

# Yahoo Finance ticker overrides for internal ticker IDs that differ from Yahoo symbols
# Used as primary source now that yfinance is the default for all instruments
_YF_FETCH_TICKER: dict[str, str] = {
    "NIFTY500":   "^CRSLDX",    # Nifty 500 on Yahoo Finance
    "NIFTYNEXT50": "^NSMIDCP",  # Nifty Next 50 (Junior Nifty) on Yahoo Finance
    # ^CNXSC is the direct Yahoo Finance symbol for Nifty Smallcap 100
}

# NSE archive index name for each ticker (used for archive fallback when yfinance fails/bad)
_NSE_HIST_INDEX_TYPE: dict[str, str] = {
    "^CNXSC":              "NIFTY SMALLCAP 100",
    "NIFTY500":            "NIFTY 500",
    "NIFTYNEXT50":         "NIFTY NEXT 50",
    "NIFTY_MIDCAP_100.NS": "NIFTY MIDCAP 100",
    "^NSEI":               "NIFTY 50",
    "^NSEBANK":            "NIFTY BANK",
    "^CNXIT":              "NIFTY IT",
    "^CNXPHARMA":          "NIFTY PHARMA",
    "^CNXAUTO":            "NIFTY AUTO",
    "^CNXFMCG":            "NIFTY FMCG",
    "^CNXMETAL":           "NIFTY METAL",
    "^CNXENERGY":          "NIFTY ENERGY",
    "^CNXREALTY":          "NIFTY REALTY",
}

# ── Instrument catalogue ──────────────────────────────────────────────────────
_INSTRUMENT_DEFS: list[tuple[str, str, str]] = [
    ("Nifty 50",              "^NSEI",               "2000-01-01"),
    ("Nifty 500",             "NIFTY500",             "2000-01-01"),
    ("Sensex",                "^BSESN",               "2000-01-01"),
    ("Nifty Bank",            "^NSEBANK",             "2000-01-01"),
    ("Nifty Midcap 100",      "NIFTY_MIDCAP_100.NS",  "2000-01-01"),
    ("Nifty Smallcap 100",    "^CNXSC",               "2012-02-01"),
    ("Nifty IT",              "^CNXIT",               "2000-01-01"),
    ("Nifty Pharma",          "^CNXPHARMA",           "2000-01-01"),
    ("Nifty Auto",            "^CNXAUTO",             "2000-01-01"),
    ("Nifty FMCG",            "^CNXFMCG",             "2000-01-01"),
    ("Nifty Metal",           "^CNXMETAL",            "2000-01-01"),
    ("Nifty Energy",          "^CNXENERGY",           "2000-01-01"),
    ("Nifty Realty",          "^CNXREALTY",           "2000-01-01"),
    ("Gold BeES (Nippon)",    "GOLDBEES.NS",          "2000-01-01"),
    ("Nifty BeES (Nippon)",   "NIFTYBEES.NS",         "2000-01-01"),
    ("Junior BeES (NNext50)", "JUNIORBEES.NS",        "2000-01-01"),
    ("Bank BeES (Nippon)",    "BANKBEES.NS",          "2000-01-01"),
    ("USD/INR",               "USDINR=X",             "2000-01-01"),
    ("Gold (USD – Futures)",  "GC=F",                 "2000-01-01"),
    ("Crude Oil (WTI)",       "CL=F",                 "2000-01-01"),
    ("S&P 500",               "^GSPC",                "2000-01-01"),
    ("NASDAQ 100",            "^NDX",                 "2000-01-01"),
    ("US 10Y Yield",          "^TNX",                 "2000-01-01"),
]

INSTRUMENTS: dict[str, str] = {name: ticker for name, ticker, _ in _INSTRUMENT_DEFS}
_MIN_START:  dict[str, str] = {ticker: ms for _, ticker, ms in _INSTRUMENT_DEFS}
_TICKER_TO_NAME: dict[str, str] = {v: k for k, v in INSTRUMENTS.items()}

# ── Source routing sets ───────────────────────────────────────────────────────
_YFINANCE_PRIMARY: set[str] = {
    "^NSEI", "^BSESN", "^NSEBANK", "NIFTY_MIDCAP_100.NS",
    "^CNXIT", "^CNXPHARMA", "^CNXAUTO", "^CNXFMCG",
    "^CNXMETAL", "^CNXENERGY", "^CNXREALTY",
    "GOLDBEES.NS", "NIFTYBEES.NS", "JUNIORBEES.NS", "BANKBEES.NS",
    "USDINR=X", "GC=F", "CL=F", "^GSPC", "^NDX", "^TNX",
    # Smallcap 100: NSE API is dead, use yfinance (^CNXSC = Nifty Smallcap 100)
    "^CNXSC",
    # Nifty Next 50 & Nifty 500 via yfinance as well
    "NIFTYNEXT50", "NIFTY500",
}

_NIFTYINDICES_PRIMARY: set[str] = set()  # moved everything to yfinance

# Tickers fetched via NSE /api/historical/indices (correct scale, real-time capable)
# NOTE: ^CNXSC NSE API is dead as of 2026 — routed to yfinance above
_NSE_HIST_API_PRIMARY: set[str] = set()

# Yahoo Finance ticker overrides — also used for Nifty Next 50 & Nifty 500
# (overrides already defined above in _YF_FETCH_TICKER)


def clear_today_fetches() -> None:
    """No-op kept for backward compat."""
    pass


def _fetch_nse_archive_days(index_name: str, start_dt: date, end_dt: date) -> pd.Series:
    """
    Fetch index values from NSE daily archive CSVs for a date range.
    archives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv
    Used as fallback for ^CNXSC since the NSE hist API endpoint is gone.
    """
    import urllib.parse as _up
    _ARCH_HDRS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://archives.nseindia.com/",
    }
    try:
        from curl_cffi import requests as _cfr
        _sess = _cfr.Session()
    except ImportError:
        import requests as _cfr  # type: ignore
        _sess = None

    results: dict[pd.Timestamp, float] = {}
    current = start_dt
    while current <= end_dt:
        if current.weekday() < 5:  # skip weekends
            url = (f"https://archives.nseindia.com/content/indices/"
                   f"ind_close_all_{current.strftime('%d%m%Y')}.csv")
            try:
                if _sess is not None:
                    r = _sess.get(url, impersonate="chrome", timeout=12)
                else:
                    import requests as _req
                    r = _req.get(url, headers=_ARCH_HDRS, timeout=10)
                if r.status_code == 200 and "Index" in r.text:
                    import io as _io, csv as _csv
                    reader = _csv.DictReader(_io.StringIO(r.text))
                    for row in reader:
                        name_col  = row.get("Index Name") or row.get("IndexName") or ""
                        close_col = row.get("Closing Index Value") or row.get("ClosingIndexValue") or ""
                        if name_col.strip().upper() == index_name.upper() and close_col:
                            try:
                                val = float(str(close_col).replace(",", ""))
                                if val > 100:
                                    results[pd.Timestamp(current)] = val
                            except ValueError:
                                pass
            except Exception as exc:
                log.debug("archive day %s failed: %s", current, exc)
            time.sleep(0.3)
        current += timedelta(days=1)

    if not results:
        return pd.Series(dtype=float)
    return pd.Series(results).sort_index()


# ── File helpers ──────────────────────────────────────────────────────────────

def _safe_fname(ticker: str) -> str:
    return ticker.replace("/", "_").replace("^", "IDX_").replace("=", "_").replace(" ", "_")


def _csv_path(ticker: str) -> Path:
    return _IDX_DIR / f"{_safe_fname(ticker)}.csv"


def _fetch_start_path(ticker: str) -> Path:
    return _IDX_DIR / f"{_safe_fname(ticker)}.fetchstart"


def _get_fetch_start(ticker: str) -> str | None:
    """Return the start date used in the last full fetch, or None if unknown."""
    p = _fetch_start_path(ticker)
    try:
        return p.read_text().strip() if p.exists() else None
    except Exception:
        return None


def _set_fetch_start(ticker: str, start_date: str) -> None:
    """Record that we did a full fetch from start_date for this ticker."""
    try:
        _fetch_start_path(ticker).write_text(start_date)
    except Exception:
        pass


def _csv_is_fresh(path: Path) -> bool:
    if not path.exists():
        return False
    now_ist = _datetime.now(_IST)
    close_threshold = now_ist.replace(hour=16, minute=0, second=0, microsecond=0)
    mtime_ist = _datetime.fromtimestamp(path.stat().st_mtime, tz=_IST)
    if mtime_ist < close_threshold:
        return False
    today_str = str(now_ist.date())
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 60))
            tail = fh.read().decode(errors="replace")
        last_line = [ln for ln in tail.split("\n") if ln.strip()][-1]
        return last_line.split(",")[0].strip() == today_str
    except Exception:
        return False


def _is_market_hours(now_ist: _datetime) -> bool:
    if now_ist.weekday() >= 5:
        return False
    mins = now_ist.hour * 60 + now_ist.minute
    return (9 * 60 + 15) <= mins < (16 * 60)


def _load_csv(path: Path) -> pd.Series:
    if not path.exists() or path.stat().st_size == 0:
        return pd.Series(dtype=float)
    try:
        df = pd.read_csv(path, parse_dates=["date"])
        df = df.dropna(subset=["date", "close"])
        s  = df.set_index("date")["close"].sort_index()
        s.index = pd.to_datetime(s.index).normalize()
        return s[~s.index.duplicated(keep="last")]
    except Exception as exc:
        log.warning("Could not load %s: %s", path.name, exc)
        return pd.Series(dtype=float)


def _save_csv(s: pd.Series, path: Path) -> None:
    df = s.reset_index()
    df.columns = ["date", "close"]
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["close"])
    df = df.drop_duplicates(subset=["date"], keep="last")
    df = df.sort_values("date")
    df.to_csv(path, index=False)


def _drop_bad_first_row(s: pd.Series, max_day1_move: float = 0.10) -> pd.Series:
    """Drop the first row if it creates an impossible day-1 gap (>10% for indices)."""
    if len(s) < 2:
        return s
    ratio = float(s.iloc[1]) / float(s.iloc[0])
    if ratio > (1 + max_day1_move) or ratio < (1 - max_day1_move):
        log.info("Dropping bad first row %s=%.2f (day-1 ratio %.3f)",
                 s.index[0].date(), float(s.iloc[0]), ratio)
        return s.iloc[1:]
    return s


def _fix_consolidation_spikes(s: pd.Series, max_ratio: float = 5.0) -> pd.Series:
    if s.empty or len(s) < 2:
        return s
    s = s.copy().sort_index()
    ratio = s / s.shift(1)
    for spike_date in s.index[ratio > max_ratio]:
        factor = ratio.loc[spike_date]
        s.loc[s.index < spike_date] *= factor
        log.info("Spike correction ×%.1f before %s", factor, spike_date.date())
    for spike_date in s.index[ratio < (1 / max_ratio)]:
        factor = ratio.loc[spike_date]
        s.loc[s.index < spike_date] *= factor
        log.info("Spike correction ×%.4f before %s", factor, spike_date.date())
    return s


def _scale_ok(cached: pd.Series, new_data: pd.Series, tol: float = 0.30) -> bool:
    if cached.empty or new_data.empty:
        return True
    last_val  = float(cached.iloc[-1])
    first_new = float(new_data.iloc[0])
    if last_val == 0:
        return True
    return (1.0 - tol) <= (first_new / last_val) <= (1.0 + tol)


# ── NSE Historical Indices API ────────────────────────────────────────────────

def _make_nse_session():
    """Create a requests session with NSE cookies primed."""
    hdrs = {
        "User-Agent":  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":      "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":     "https://www.nseindia.com/",
    }
    try:
        from curl_cffi import requests as cfr  # type: ignore
        sess = cfr.Session()
        sess.get("https://www.nseindia.com", impersonate="chrome", timeout=12)
        time.sleep(0.5)
        return sess, True, hdrs
    except ImportError:
        import requests as req
        sess = req.Session()
        sess.headers.update(hdrs)
        sess.get("https://www.nseindia.com", timeout=10)
        time.sleep(0.5)
        return sess, False, hdrs


def _nse_hist_api_chunk(sess, use_cffi: bool, hdrs: dict,
                         index_type: str, from_dt: date, to_dt: date) -> list[dict]:
    """Fetch one chunk from NSE /api/historical/indices."""
    from_str = from_dt.strftime("%d-%m-%Y")
    to_str   = to_dt.strftime("%d-%m-%Y")
    idx_enc  = urllib.parse.quote(index_type)
    url = (f"https://www.nseindia.com/api/historical/indices"
           f"?indexType={idx_enc}&from={from_str}&to={to_str}")
    try:
        if use_cffi:
            r = sess.get(url, impersonate="chrome", timeout=15)
        else:
            r = sess.get(url, headers=hdrs, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data.get("data", [])
    except Exception as exc:
        log.debug("NSE hist API chunk %s→%s: %s", from_str, to_str, exc)
    return []


def _parse_nse_hist_rows(rows: list[dict]) -> dict[pd.Timestamp, float]:
    results: dict[pd.Timestamp, float] = {}
    for row in rows:
        try:
            dt_raw  = (row.get("CH_TIMESTAMP") or row.get("TIMESTAMP")
                       or row.get("HistoricalDate") or row.get("date", ""))
            val_raw = (row.get("CH_CLOSING_VALUE") or row.get("CLOSING_INDEX_VAL")
                       or row.get("close") or row.get("Close", ""))
            if not dt_raw or not val_raw:
                continue
            ts  = pd.Timestamp(str(dt_raw).strip()).normalize()
            val = float(str(val_raw).replace(",", ""))
            if val > 100:   # must be actual points, not base-100
                results[ts] = val
        except Exception:
            pass
    return results


def _fetch_nse_hist_api(index_type: str, start_date: str, end_date: str) -> pd.Series:
    """
    Fetch from NSE /api/historical/indices in 90-day chunks.
    Returns series in actual index points (correct scale).
    Incremental: caller passes start_date = last_cached - 5 days for updates.
    Session is refreshed every 20 chunks to prevent cookie expiry on large rebuilds.
    """
    start_dt = pd.Timestamp(start_date).date()
    end_dt   = pd.Timestamp(end_date).date()
    sess, use_cffi, hdrs = _make_nse_session()

    all_results: dict[pd.Timestamp, float] = {}
    chunk_start  = start_dt
    chunk_count  = 0

    while chunk_start <= end_dt:
        # Refresh NSE session every 20 chunks so cookies don't expire mid-rebuild
        if chunk_count > 0 and chunk_count % 20 == 0:
            try:
                sess, use_cffi, hdrs = _make_nse_session()
                log.debug("NSE session refreshed at chunk %d", chunk_count)
            except Exception as exc:
                log.debug("NSE session refresh failed: %s", exc)

        chunk_end = min(chunk_start + timedelta(days=89), end_dt)
        rows = _nse_hist_api_chunk(sess, use_cffi, hdrs, index_type, chunk_start, chunk_end)
        all_results.update(_parse_nse_hist_rows(rows))
        log.debug("NSE hist API '%s' %s→%s: %d rows",
                  index_type, chunk_start, chunk_end, len(rows))
        chunk_start  = chunk_end + timedelta(days=1)
        chunk_count += 1
        time.sleep(0.3)

    if not all_results:
        log.warning("NSE hist API '%s': no data for %s→%s", index_type, start_date, end_date)
        return pd.Series(dtype=float)

    s = pd.Series(all_results).sort_index()
    log.info("NSE hist API '%s': %d rows (%s → %s)",
             index_type, len(s), s.index[0].date(), s.index[-1].date())
    return s


def _fetch_nse_hist_latest_quote(index_type: str) -> pd.Series:
    """
    Fetch the latest real-time quote from NSE for intraday display.
    Uses /api/allIndices which returns the current value without date range params.
    """
    try:
        sess, use_cffi, hdrs = _make_nse_session()
        url = "https://www.nseindia.com/api/allIndices"
        if use_cffi:
            r = sess.get(url, impersonate="chrome", timeout=10)
        else:
            r = sess.get(url, headers=hdrs, timeout=10)
        if r.status_code != 200:
            return pd.Series(dtype=float)
        data = r.json()
        for item in data.get("data", []):
            if item.get("index", "").upper() == index_type.upper():
                val = float(str(item.get("last", item.get("previousClose", 0))).replace(",", ""))
                if val > 100:
                    today_ts = pd.Timestamp(_datetime.now(_IST).date())
                    log.info("NSE real-time '%s': %.2f", index_type, val)
                    return pd.Series({today_ts: val})
    except Exception as exc:
        log.debug("NSE allIndices quote failed: %s", exc)
    return pd.Series(dtype=float)


# ── NSE hist API primary flow ─────────────────────────────────────────────────

def _get_nse_hist_api_price(
    ticker: str,
    start_date: str,
    force_refresh: bool,
    cached: pd.Series,
    path: Path,
    now_ist: _datetime,
) -> tuple[pd.Series, dict]:
    """
    Full flow for ^CNXSC using NSE historical indices API.

    - No cache / force_refresh  → full fetch from start_date in 90d chunks
    - Cache exists, not fresh   → incremental fetch from last_cached - 5d
    - Market hours              → inject real-time quote for today's row (not saved)
    - After close               → save today's closing value
    - CSV fresh                 → return cache as-is
    """
    index_type  = _NSE_HIST_INDEX_TYPE[ticker]
    today       = now_ist.date()
    after_close = now_ist.hour >= 16
    market_hrs  = _is_market_hours(now_ist)
    status      = {"ticker": ticker, "source": "NSE hist API",
                   "success": True, "message": ""}

    # ── Fresh cache: return immediately ──────────────────────────────────────
    if _csv_is_fresh(path) and not force_refresh:
        # Still inject live quote during market hours (display only)
        combined = cached.copy()
        if market_hrs:
            quote = _fetch_nse_hist_latest_quote(index_type)
            if not quote.empty:
                ts = quote.index[0]
                combined = pd.concat([combined, quote])
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
                status["message"] = (f"Cached + live quote {float(quote.iloc[0]):.2f} "
                                     f"({len(combined)} rows)")
            else:
                status["message"] = (f"Cached {len(combined)} rows "
                                     f"({cached.index[0].date()} → {cached.index[-1].date()})")
        else:
            status["message"] = (f"Cached {len(combined)} rows "
                                 f"({cached.index[0].date()} → {cached.index[-1].date()})")
        status["source"] = "cache"
        return combined[combined.index >= pd.Timestamp(start_date)], status

    # ── Decide fetch range ────────────────────────────────────────────────────
    if force_refresh or cached.empty:
        fetch_start = start_date
        fetch_type  = "full rebuild"
    else:
        last_cached = cached.index[-1].date()
        fetch_start = str(last_cached - timedelta(days=5))
        fetch_type  = "incremental"

    # NSE hist API (/api/historical/indices) returns 404 as of 2026 — use daily archives
    fresh = _fetch_nse_hist_api(index_type, fetch_start, str(today))

    if fresh.empty:
        # Hist API dead: fall back to daily archive CSVs (archives.nseindia.com)
        arch_start = date.fromisoformat(fetch_start)
        log.info("NSE hist API failed; trying daily archives for '%s' (%s→%s)",
                 index_type, arch_start, today)
        fresh = _fetch_nse_archive_days(index_type, arch_start, today)
        if not fresh.empty:
            status["source"] = "NSE daily archives"

    if fresh.empty:
        if cached.empty:
            return cached, {**status, "success": False,
                            "message": f"NSE hist API: no data for '{index_type}'"}
        # Try yfinance as fallback for incremental tail update
        if fetch_type == "incremental":
            yf_tail = _fetch_yfinance(ticker, fetch_start, str(today + timedelta(days=1)))
            if not yf_tail.empty and _scale_ok(cached, yf_tail.iloc[:1]):
                combined = pd.concat([cached, yf_tail]).sort_index()
                combined = combined[~combined.index.duplicated(keep="last")]
                combined.index = pd.to_datetime(combined.index).normalize()
                persist = combined
                if market_hrs and not after_close:
                    persist = combined[combined.index.date < today]
                if not persist.empty:
                    _save_csv(persist, path)
                status["source"]  = "yfinance (NSE fallback)"
                status["message"] = (f"NSE API failed, yfinance tail: {len(combined)} rows "
                                     f"({combined.index[0].date()} → {combined.index[-1].date()})")
                return combined[combined.index >= pd.Timestamp(start_date)], status
        # Fallback to cache, but still try live quote
        combined = cached.copy()
        if market_hrs:
            quote = _fetch_nse_hist_latest_quote(index_type)
            if not quote.empty:
                combined = pd.concat([combined, quote])
                combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        status["source"]  = "cache"
        status["message"] = (f"NSE API failed, using cache "
                             f"({cached.index[0].date()} → {cached.index[-1].date()})")
        return combined[combined.index >= pd.Timestamp(start_date)], status

    # ── Merge fresh with cached ───────────────────────────────────────────────
    if not cached.empty:
        combined = pd.concat([cached, fresh]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
    else:
        combined = fresh.sort_index()
    combined.index = pd.to_datetime(combined.index).normalize()

    # ── Persist: save everything except today's row if before close ───────────
    persist = combined
    if market_hrs and not after_close:
        persist = combined[combined.index.date < today]
    if not persist.empty:
        _save_csv(persist, path)

    # ── Inject live quote for display during market hours ─────────────────────
    if market_hrs:
        quote = _fetch_nse_hist_latest_quote(index_type)
        if not quote.empty:
            combined = pd.concat([combined, quote])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
            status["message"] = (f"NSE hist API {fetch_type}: {len(combined)} rows "
                                 f"({combined.index[0].date()} → {combined.index[-1].date()}) "
                                 f"+ live {float(quote.iloc[0]):.2f}")
        else:
            status["message"] = (f"NSE hist API {fetch_type}: {len(combined)} rows "
                                 f"({combined.index[0].date()} → {combined.index[-1].date()})")
    else:
        status["message"] = (f"NSE hist API {fetch_type}: {len(combined)} rows "
                             f"({combined.index[0].date()} → {combined.index[-1].date()})")

    return combined[combined.index >= pd.Timestamp(start_date)], status


# ── NSE Equity Historical API ─────────────────────────────────────────────────

def _fetch_stock_full(ticker: str) -> pd.Series:
    """
    Fetch complete price history for an NSE stock via yfinance.
    Tries period='max' first (single call, all available history),
    then falls back to year-by-year download from 2000.
    """
    def _clean(raw) -> pd.Series:
        if raw is None or (hasattr(raw, "empty") and raw.empty):
            return pd.Series(dtype=float)
        if isinstance(raw, pd.DataFrame):
            if isinstance(raw.columns, pd.MultiIndex):
                raw = raw["Close"].iloc[:, 0]
            else:
                raw = raw["Close"] if "Close" in raw.columns else raw.iloc[:, 0]
        s = raw.dropna().squeeze()
        if not isinstance(s, pd.Series):
            return pd.Series(dtype=float)
        s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
        return s[~s.index.duplicated(keep="last")].sort_index()

    # Try period="max" — gets all available history in one call
    try:
        h = yf.Ticker(ticker).history(period="max", auto_adjust=True)
        s = _clean(h)
        if len(s) > 30:
            return s
    except Exception:
        pass

    # Fallback: download from 2000 year-by-year (handles tickers that reject period=max)
    return _fetch_yfinance(ticker, "2000-01-01", str(date.today() + timedelta(days=1)))


def get_stock_price(
    ticker: str,
    start_date: str = "2000-01-01",
    force_refresh: bool = False,
) -> tuple[pd.Series, dict]:
    """
    Fetch NSE stock price series for ticker like 'RELIANCE.NS' via yfinance.
    Full history (period='max') is fetched on first access or when data is stale.
    Subsequent accesses do incremental updates only.
    Data is cached in data/live/indices/<safe_ticker>.csv.
    """
    now_ist = _datetime.now(_IST)
    today   = now_ist.date()
    path    = _csv_path(ticker)
    status  = {"ticker": ticker, "source": "yfinance", "success": True, "message": ""}

    cached    = _load_csv(path)
    fetch_end = str(today + timedelta(days=1))

    # Full rebuild conditions:
    #   - no cache, force-refresh, or too few rows
    #   - data starts >2yr after requested start AND we haven't already done a full
    #     fetch from this start date (tracked in .fetchstart sidecar to avoid infinite
    #     re-fetches for stocks simply listed after req_start)
    req_start            = date.fromisoformat(start_date)
    last_fetch_start     = _get_fetch_start(ticker)
    already_full_fetched = (last_fetch_start is not None
                            and last_fetch_start <= start_date)
    needs_full = (
        force_refresh
        or cached.empty
        or (len(cached) < 100 and (today - req_start).days > 365)
        or (not cached.empty
            and cached.index[0].date() > req_start + timedelta(days=730)
            and not already_full_fetched)
    )

    if needs_full:
        fresh = _fetch_stock_full(ticker)
        # Record the attempted start so we don't re-fetch infinitely for stocks
        # whose actual listing date is after req_start.
        _set_fetch_start(ticker, start_date)
        if fresh.empty:
            if cached.empty:
                status["success"] = False
                status["message"] = f"No data for {ticker}"
                return cached, status
            combined = cached
        else:
            combined = fresh.sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = _fix_consolidation_spikes(combined)
            _save_csv(combined, path)
        status["message"] = (
            f"yfinance full: {len(combined)} rows"
            f" ({combined.index[0].date()} → {combined.index[-1].date()})"
            if not combined.empty else "No data"
        )

    elif _csv_is_fresh(path) and not force_refresh:
        combined = cached
        status["source"]  = "cache"
        status["message"] = (
            f"Cached {len(combined)} rows"
            f" ({combined.index[0].date()} → {combined.index[-1].date()})"
        )

    else:
        # Incremental: fetch only new days since last cached date
        last_cached = cached.index[-1].date()
        if last_cached >= today:
            combined = cached
            status["source"]  = "cache"
            status["message"] = f"Cached {len(combined)} rows (up to date)"
        else:
            fwd   = str(last_cached + timedelta(days=1))
            fresh = _fetch_yfinance(ticker, fwd, fetch_end)
            if not fresh.empty and _scale_ok(cached, fresh):
                combined = pd.concat([cached, fresh]).sort_index()
                combined = combined[~combined.index.duplicated(keep="last")]
                combined = _fix_consolidation_spikes(combined)
                _save_csv(combined, path)
                status["source"]  = "yfinance (incremental)"
                status["message"] = (
                    f"Updated {len(combined)} rows"
                    f" ({combined.index[0].date()} → {combined.index[-1].date()})"
                )
            else:
                combined = cached
                status["source"]  = "cache"
                status["message"] = (
                    f"Cached {len(combined)} rows"
                    f" ({combined.index[0].date()} → {combined.index[-1].date()})"
                )

    if not combined.empty:
        combined = combined[combined.index >= pd.Timestamp(start_date)]
    return combined, status


# ── Other fetch helpers ───────────────────────────────────────────────────────

def _yf_raw_to_series(raw, ticker: str) -> pd.Series:
    """Extract Close column from a yf.download result into a named Series."""
    if isinstance(raw.columns, pd.MultiIndex):
        close_df = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw.iloc[:, [0]]
        close = close_df.iloc[:, 0] if isinstance(close_df, pd.DataFrame) else close_df
    else:
        close = raw["Close"]
    s = close.dropna()
    s.index = pd.to_datetime(s.index).normalize()
    s.name = ticker
    return s


def _fetch_yfinance(ticker: str, start: str, end: str) -> pd.Series:
    """
    Fetch daily closes from Yahoo Finance in 1-year chunks.

    Year-by-year fetching is used because some tickers (e.g. ^CNXSC,
    ^NSMIDCP) raise YFInvalidPeriodError when a full-range download
    triggers yfinance's internal period= fallback.  Chunking avoids this.
    """
    yf_sym   = _YF_FETCH_TICKER.get(ticker, ticker)
    start_dt = pd.Timestamp(start).date()
    end_dt   = pd.Timestamp(end).date()

    chunks: list[pd.Series] = []
    year = start_dt.year

    while True:
        chunk_start = date(year, 1, 1) if year > start_dt.year else start_dt
        chunk_end   = date(year, 12, 31)
        if chunk_start > end_dt:
            break
        chunk_end = min(chunk_end, end_dt)

        try:
            raw = yf.download(
                yf_sym,
                start=str(chunk_start),
                end=str(chunk_end + timedelta(days=1)),
                auto_adjust=True,
                progress=False,
                timeout=45,
            )
            if raw is not None and not raw.empty:
                s_chunk = _yf_raw_to_series(raw, ticker)
                if not s_chunk.empty:
                    chunks.append(s_chunk)
        except Exception as exc:
            log.debug("yfinance chunk %s→%s for %s: %s", chunk_start, chunk_end, yf_sym, exc)

        year += 1
        if year > end_dt.year:
            break
        time.sleep(0.1)

    if not chunks:
        # Fallback: Ticker.history() when yf.download() fails (e.g. ^CNXSC, some NSE tickers)
        try:
            h = yf.Ticker(yf_sym).history(
                start=str(start_dt),
                end=str(end_dt + timedelta(days=1)),
                auto_adjust=True,
            )
            if not h.empty and "Close" in h.columns:
                s = h["Close"].dropna()
                s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
                s = s[~s.index.duplicated(keep="last")].sort_index()
                s.name = ticker
                if not s.empty:
                    log.info("Ticker.history fallback for %s: %d rows", yf_sym, len(s))
                    return s
        except Exception as exc:
            log.debug("Ticker.history fallback for %s: %s", yf_sym, exc)
        log.warning("yfinance returned no data for %s (%s → %s)", yf_sym, start, end)
        return pd.Series(dtype=float)

    combined = pd.concat(chunks).sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    combined.name = ticker
    return combined


def _fetch_yfinance_latest_quote(ticker: str) -> pd.Series:
    yf_sym = _YF_FETCH_TICKER.get(ticker, ticker)
    try:
        t     = yf.Ticker(yf_sym)
        info  = t.info or {}
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        ts    = info.get("regularMarketTime")
        if price is None:
            try:
                price = t.fast_info.get("lastPrice")
            except Exception:
                price = None
        if ts is None:
            try:
                ts = t.fast_info.get("lastTime")
            except Exception:
                ts = None
        if price is None:
            return pd.Series(dtype=float)
        dt = _datetime.fromtimestamp(int(ts), tz=_IST).date() if ts else _datetime.now(_IST).date()
        return pd.Series({pd.Timestamp(dt): float(price)}, name=ticker)
    except Exception as exc:
        log.debug("Yahoo latest quote failed for %s: %s", ticker, exc)
        return pd.Series(dtype=float)


def _fetch_from_niftyindices(ni_name: str, start_date: str = "2006-01-01") -> pd.Series:
    import requests as _req, json as _json, time as _time
    sess = _req.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": "https://www.niftyindices.com/",
        "Origin":  "https://www.niftyindices.com",
    })
    start_dt    = pd.Timestamp(start_date)
    end_dt      = pd.Timestamp(date.today())
    all_records: list[dict] = []
    chunk_start = start_dt
    while chunk_start <= end_dt:
        chunk_end = min(chunk_start + pd.DateOffset(years=2), end_dt)
        payload = {
            "name":      ni_name.upper(),
            "startDate": chunk_start.strftime("%d-%b-%Y"),
            "endDate":   chunk_end.strftime("%d-%b-%Y"),
        }
        try:
            r = sess.post("https://www.niftyindices.com/Backpage.aspx/getHistoricalData",
                          json=payload, timeout=20)
            if r.status_code == 200:
                outer   = r.json()
                raw_str = outer.get("d", "[]")
                rows    = _json.loads(raw_str) if isinstance(raw_str, str) else raw_str
                if isinstance(rows, list):
                    all_records.extend(rows)
        except Exception as exc:
            log.debug("niftyindices chunk %s→%s: %s", chunk_start.date(), chunk_end.date(), exc)
        chunk_start = chunk_end + pd.DateOffset(days=1)
        _time.sleep(0.1)
    if not all_records:
        return pd.Series(dtype=float)
    results: dict[pd.Timestamp, float] = {}
    for row in all_records:
        try:
            dt_raw  = row.get("TIMESTAMP") or row.get("HistoricalDate") or row.get("date", "")
            val_raw = (row.get("CLOSING_INDEX_VAL") or row.get("Close")
                       or row.get("close") or row.get("closingIndexVal", ""))
            ts  = pd.Timestamp(str(dt_raw).strip()).normalize()
            val = float(str(val_raw).replace(",", ""))
            if val > 0:
                results[ts] = val
        except Exception:
            pass
    if not results:
        return pd.Series(dtype=float)
    s = pd.Series(results, name=ni_name).sort_index()
    log.info("niftyindices '%s': %d rows (%s → %s)",
             ni_name, len(s), s.index[0].date(), s.index[-1].date())
    return s


def _fetch_nsei_from_existing_cache() -> pd.Series:
    try:
        from utils.config import NIFTY_CACHE
        if NIFTY_CACHE.exists():
            df = pd.read_csv(NIFTY_CACHE, parse_dates=["date"])
            val_col = "close" if "close" in df.columns else "value"
            s = df.set_index("date")[val_col].sort_index()
            s.index = pd.to_datetime(s.index).normalize()
            s.name = "^NSEI"
            return s
    except Exception:
        pass
    return pd.Series(dtype=float)


def _fetch_from_nse_archive(nse_name: str, start_date: str, end_date: str) -> pd.Series:
    try:
        from data.fetcher import fetch_nse_index_bulk
        series_dict, _ = fetch_nse_index_bulk(start_date=start_date, end_date=end_date)
        s = series_dict.get(nse_name, pd.Series(dtype=float))
        s.name = nse_name
        return s
    except Exception as exc:
        log.warning("NSE archive fetch for '%s' failed: %s", nse_name, exc)
        return pd.Series(dtype=float)


# ── niftyindices-primary flow ─────────────────────────────────────────────────

def _get_niftyindices_primary_price(
    ticker: str, start_date: str, force_refresh: bool,
    cached: pd.Series, path: Path,
) -> tuple[pd.Series, dict]:
    ni_name = _NIFTYINDICES_NAME.get(ticker, ticker)
    today   = _datetime.now(_IST).date()
    status  = {"ticker": ticker, "source": "niftyindices.com", "success": True, "message": ""}
    if _csv_is_fresh(path) and not force_refresh:
        status["source"]  = "cache"
        status["message"] = (f"Cached {len(cached)} rows "
                             f"({cached.index[0].date()} → {cached.index[-1].date()})")
        return cached[cached.index >= pd.Timestamp(start_date)], status
    if force_refresh or cached.empty:
        ni_fetch_start = start_date
        fresh = _fetch_from_niftyindices(ni_name, ni_fetch_start)
        fetch_type = "full rebuild"
    else:
        last_cached    = cached.index[-1].date()
        ni_fetch_start = str(last_cached - timedelta(days=5))
        fresh = _fetch_from_niftyindices(ni_name, ni_fetch_start)
        fetch_type = "incremental"
    if fresh.empty:
        # Try yfinance as fallback for known Yahoo Finance tickers
        yf_tk = _YF_FETCH_TICKER.get(ticker)
        if yf_tk:
            yf_start = ni_fetch_start if fetch_type == "incremental" else start_date
            yf_fresh = _fetch_yfinance(ticker, yf_start, str(today + timedelta(days=1)))
            if not yf_fresh.empty and (cached.empty or _scale_ok(cached, yf_fresh.iloc[:1])):
                fresh = yf_fresh
                status["source"] = f"yfinance/{yf_tk}"
                log.info("niftyindices fallback → yfinance/%s for '%s'", yf_tk, ticker)
        if fresh.empty:
            if cached.empty:
                return cached, {**status, "success": False,
                                "message": f"niftyindices.com returned no data for '{ni_name}'"}
            status["source"]  = "cache"
            status["message"] = (f"niftyindices fetch failed, using cache "
                                 f"({cached.index[0].date()} → {cached.index[-1].date()})")
            return cached[cached.index >= pd.Timestamp(start_date)], status
    combined = pd.concat([cached, fresh]).sort_index() if not cached.empty else fresh.sort_index()
    combined = combined[~combined.index.duplicated(keep="last")]
    combined.index = pd.to_datetime(combined.index).normalize()
    _save_csv(combined, path)
    status["message"] = (f"niftyindices.com {fetch_type}: {len(combined)} rows "
                         f"({combined.index[0].date()} → {combined.index[-1].date()})")
    return combined[combined.index >= pd.Timestamp(start_date)], status


# ── yfinance-primary flow ─────────────────────────────────────────────────────

def _get_yfinance_primary_price(
    ticker: str, start_date: str, force_refresh: bool,
    cached: pd.Series, path: Path, now_ist: _datetime,
) -> tuple[pd.Series, dict]:
    today        = now_ist.date()
    after_close  = now_ist.hour >= 16
    market_hours = _is_market_hours(now_ist)
    fetch_end    = str(today + timedelta(days=1))
    status       = {"ticker": ticker, "source": "yfinance", "success": True, "message": ""}

    def _merge_quote(s: pd.Series) -> tuple[pd.Series, bool]:
        q = _fetch_yfinance_latest_quote(ticker)
        if q.empty:
            return s, False
        if s.empty or q.index[-1] >= s.index[-1]:
            merged = pd.concat([s, q]).sort_index()
            return merged[~merged.index.duplicated(keep="last")], True
        return s, False

    # Full rebuild when: no cache, force-refresh, or cache looks truncated (< 100 rows
    # while years of history were requested — indicates a past partial write).
    req_start  = date.fromisoformat(start_date)
    needs_full = (
        force_refresh
        or cached.empty
        or (len(cached) < 100 and (today - req_start).days > 365)
    )

    if needs_full:
        # Try period="max" first — gets all available history in one call
        fresh = pd.Series(dtype=float)
        try:
            yf_sym = _YF_FETCH_TICKER.get(ticker, ticker)
            h = yf.Ticker(yf_sym).history(period="max", auto_adjust=True)
            if not h.empty and "Close" in h.columns:
                s = h["Close"].dropna()
                s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
                s = s[~s.index.duplicated(keep="last")].sort_index()
                if len(s) > 30:
                    fresh = s
        except Exception:
            pass
        if fresh.empty:
            fresh = _fetch_yfinance(ticker, start_date, fetch_end)
        if fresh.empty:
            if cached.empty:
                return cached, {**status, "success": False,
                                "message": f"No Yahoo Finance data for {ticker}"}
            combined = cached
            status["message"] = (f"Yahoo fetch failed, using cache "
                                 f"({combined.index[0].date()} → {combined.index[-1].date()})")
        else:
            combined = fresh.sort_index()
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = _drop_bad_first_row(combined)
            combined = _fix_consolidation_spikes(combined)

            # ── Backfill from niftyindices.com for pre-yfinance history ──────
            ni_name = _TICKER_TO_NSE_NAME.get(ticker)
            if ni_name and not combined.empty:
                req_start  = date.fromisoformat(start_date)
                data_start = combined.index[0].date()
                if data_start > req_start + timedelta(days=30):
                    log.info("yfinance %s starts %s; backfilling from niftyindices.com", ticker, data_start)
                    ni_pre = _fetch_from_niftyindices(ni_name, start_date)
                    if not ni_pre.empty:
                        ni_pre = ni_pre[ni_pre.index < combined.index[0]]
                        if not ni_pre.empty and _scale_ok(ni_pre.iloc[-1:], combined.iloc[:1]):
                            combined = pd.concat([ni_pre, combined]).sort_index()
                            combined = combined[~combined.index.duplicated(keep="last")]
                            status["source"] = "niftyindices.com + yfinance"
                            log.info("Backfill OK: %s now starts %s", ticker, combined.index[0].date())
            # ─────────────────────────────────────────────────────────────────

            combined, used_quote = _merge_quote(combined)
            persist = combined if not (market_hours and not after_close) else combined[combined.index.date < today]
            if not persist.empty:
                _save_csv(persist, path)
            status["message"] = (f"Yahoo Finance full rebuild {len(combined)} rows "
                                 f"({combined.index[0].date()} → {combined.index[-1].date()})")

    elif _csv_is_fresh(path):
        combined = cached
        status["source"]  = "cache"
        status["message"] = (f"Cached {len(combined)} rows "
                             f"({combined.index[0].date()} → {combined.index[-1].date()})")

    else:
        last_cached = cached.index[-1].date()
        days_stale  = (today - last_cached).days
        # Only skip fetch if data is from today (already current) and market isn't open yet
        if not market_hours and not after_close and days_stale == 0:
            combined = cached
            status["source"]  = "cache"
            status["message"] = (f"Cached {len(combined)} rows "
                                 f"({combined.index[0].date()} → {combined.index[-1].date()})")
        else:
            fetch_start = max(cached.index[0].date(), last_cached - timedelta(days=10))
            fresh = _fetch_yfinance(ticker, str(fetch_start), fetch_end)
            if fresh.empty:
                _arc_type = _NSE_HIST_INDEX_TYPE.get(ticker)
                if _arc_type:
                    fresh = _fetch_nse_archive_days(_arc_type, fetch_start, today)
                    if not fresh.empty:
                        status["source"] = "NSE daily archives"
            if fresh.empty:
                combined = cached
                status["source"]  = "cache"
                status["message"] = (f"Yahoo fetch failed, using cache "
                                     f"({combined.index[0].date()} → {combined.index[-1].date()})")
            else:
                combined = pd.concat([cached, fresh]).sort_index()
                combined = combined[~combined.index.duplicated(keep="last")]
                combined = _fix_consolidation_spikes(combined)

                # ── Backfill pre-yfinance history if cache still starts too late ──
                ni_name = _TICKER_TO_NSE_NAME.get(ticker)
                if ni_name and not combined.empty:
                    req_start  = date.fromisoformat(start_date)
                    data_start = combined.index[0].date()
                    if data_start > req_start + timedelta(days=30):
                        ni_pre = _fetch_from_niftyindices(ni_name, start_date)
                        if not ni_pre.empty:
                            ni_pre = ni_pre[ni_pre.index < combined.index[0]]
                            if not ni_pre.empty and _scale_ok(ni_pre.iloc[-1:], combined.iloc[:1]):
                                combined = pd.concat([ni_pre, combined]).sort_index()
                                combined = combined[~combined.index.duplicated(keep="last")]
                                status["source"] = "niftyindices.com + yfinance"
                # ─────────────────────────────────────────────────────────────────

                combined, used_quote = _merge_quote(combined)
                persist = combined
                if market_hours and not after_close:
                    persist = combined[combined.index.date < today]
                    status["message"] = (f"Yahoo intraday ({combined.index[-1].date()}) "
                                         "displayed, not saved until 16:00 IST")
                else:
                    status["message"] = (f"Yahoo closing update {len(combined)} rows "
                                         f"({combined.index[-1].date()})")
                if not persist.empty:
                    _save_csv(persist, path)

    if not combined.empty:
        combined = combined[combined.index >= pd.Timestamp(start_date)]
    return combined, status


# ── Public API ────────────────────────────────────────────────────────────────

def get_price(
    ticker: str,
    start_date: str = "2000-01-01",
    force_refresh: bool = False,
) -> tuple[pd.Series, dict]:
    """
    Return (price_series, status_dict) for *ticker* from *start_date* to today.

    Routing:
      ^CNXSC              → NSE /api/historical/indices  (correct scale, real-time)
      NIFTYNEXT50/500     → niftyindices.com
      _YFINANCE_PRIMARY   → Yahoo Finance
      everything else     → NSE daily archives
    """
    now_ist = _datetime.now(_IST)
    today   = now_ist.date()
    path    = _csv_path(ticker)
    status  = {"ticker": ticker, "source": "cache", "success": True, "message": ""}

    if ticker == "^NSEI" and not path.exists() and not force_refresh:
        existing = _fetch_nsei_from_existing_cache()
        if not existing.empty:
            _save_csv(existing, path)

    cached = _load_csv(path)
    if cached.empty and ticker == "NIFTY500":
        seed = _load_csv(_csv_path("NIFTY500_SEED"))
        if not seed.empty:
            cached = seed
            status["source"] = "local seed"

    # ── Route ────────────────────────────────────────────────────────────────

    if ticker in _NSE_HIST_API_PRIMARY:
        return _get_nse_hist_api_price(
            ticker=ticker, start_date=start_date, force_refresh=force_refresh,
            cached=cached, path=path, now_ist=now_ist,
        )

    if ticker in _NIFTYINDICES_PRIMARY:
        return _get_niftyindices_primary_price(
            ticker=ticker, start_date=start_date, force_refresh=force_refresh,
            cached=cached, path=path,
        )

    if ticker in _YFINANCE_PRIMARY:
        return _get_yfinance_primary_price(
            ticker=ticker, start_date=start_date, force_refresh=force_refresh,
            cached=cached, path=path, now_ist=now_ist,
        )

    # ── NSE daily archive flow ────────────────────────────────────────────────
    if _csv_is_fresh(path) and not force_refresh:
        combined = cached
        last_d   = combined.index[-1].date() if not combined.empty else today
        status["message"] = f"Cached {len(combined)} rows ({combined.index[0].date()} → {last_d})"
        return combined[combined.index >= pd.Timestamp(start_date)], status

    nse_name  = _TICKER_TO_NSE_NAME.get(ticker)
    use_nse   = nse_name is not None
    fetch_end = str(today + timedelta(days=1))
    pieces: list[pd.Series] = []

    def _nse_full() -> pd.Series:
        return _fetch_from_nse_archive(nse_name, max(start_date, "2011-01-03"), str(today))

    def _ni_pre2011() -> pd.Series:
        if not nse_name:
            return pd.Series(dtype=float)
        s = _fetch_from_niftyindices(nse_name, start_date)
        return s[s.index < pd.Timestamp("2011-01-03")] if not s.empty else s

    if force_refresh:
        if use_nse:
            fresh = _nse_full()
            if not fresh.empty:
                pre = _ni_pre2011()
                if not pre.empty:
                    fresh = pd.concat([pre, fresh]).sort_index()
                    fresh = fresh[~fresh.index.duplicated(keep="last")]
                pieces.append(fresh)
                status["source"] = "NSE archives (force)"
            else:
                yf_s = _fetch_yfinance(ticker, start_date, fetch_end)
                if not yf_s.empty:
                    pieces.append(yf_s)
                    status["source"] = "yfinance (force-fallback)"
        else:
            fresh = _fetch_yfinance(ticker, start_date, fetch_end)
            if not fresh.empty:
                pieces.append(fresh)
            status["source"] = "yfinance (force)"

    elif cached.empty:
        if use_nse:
            arc = _nse_full()
            if not arc.empty:
                pre = _ni_pre2011()
                if not pre.empty:
                    arc = pd.concat([pre, arc]).sort_index()
                    arc = arc[~arc.index.duplicated(keep="last")]
                pieces.append(arc)
                status["source"] = "NSE archives (full)"
            else:
                ni = _fetch_from_niftyindices(nse_name, start_date) if nse_name else pd.Series(dtype=float)
                if not ni.empty:
                    pieces.append(ni)
                    status["source"] = "niftyindices.com (full)"
                else:
                    yf_s = _fetch_yfinance(ticker, start_date, fetch_end)
                    if not yf_s.empty and len(yf_s) > 100:
                        pieces.append(yf_s)
                        status["source"] = "yfinance (full-fallback)"
        else:
            new = _fetch_yfinance(ticker, start_date, fetch_end)
            if not new.empty:
                pieces.append(new)
            status["source"] = "yfinance (full)"

    else:
        first_cached = cached.index[0].date()
        last_cached  = cached.index[-1].date()
        req_start    = date.fromisoformat(start_date)

        if first_cached > req_start + timedelta(days=30):
            if use_nse:
                back = _fetch_from_nse_archive(nse_name, max(start_date, "2011-01-03"), str(first_cached))
                if not back.empty:
                    pieces.append(back)
                    status["source"] = "NSE archives (backfill)"
            else:
                back = _fetch_yfinance(ticker, start_date, str(first_cached))
                if not back.empty:
                    pieces.append(back)
                    status["source"] = "yfinance (backfill)"

        if last_cached <= today:
            fwd_start = str(last_cached + timedelta(days=1)) if last_cached < today else str(today)
            fwd = _fetch_from_nse_archive(nse_name, fwd_start, str(today)) if use_nse else pd.Series(dtype=float)
            if not fwd.empty and _scale_ok(cached, fwd):
                pieces.append(fwd)
                if status["source"] == "cache":
                    status["source"] = "NSE archives (incremental)"
            else:
                fwd_yf = _fetch_yfinance(ticker, fwd_start, fetch_end)
                if not fwd_yf.empty and _scale_ok(cached, fwd_yf):
                    pieces.append(fwd_yf)
                    if status["source"] == "cache":
                        status["source"] = "yfinance (incremental)"

        if use_nse and not cached.empty and not pieces:
            diffs    = cached.index.to_series().diff().dt.days
            big_gaps = diffs[diffs > 20]
            if not big_gaps.empty:
                for gap_end_ts in big_gaps.index:
                    gap_start_ts = cached.index[cached.index < gap_end_ts][-1]
                    gap_s  = str(gap_start_ts.date() + timedelta(days=1))
                    gap_e  = str(gap_end_ts.date())
                    anchor = cached[cached.index < pd.Timestamp(gap_end_ts)].iloc[-1:]
                    fill   = _fetch_from_nse_archive(nse_name, gap_s, gap_e)
                    if fill.empty or not _scale_ok(anchor, fill):
                        ni_fill = _fetch_from_niftyindices(nse_name, gap_s) if nse_name else pd.Series(dtype=float)
                        if not ni_fill.empty:
                            ni_fill = ni_fill[(ni_fill.index >= pd.Timestamp(gap_s)) &
                                              (ni_fill.index <= pd.Timestamp(gap_e))]
                            if _scale_ok(anchor, ni_fill):
                                fill = ni_fill
                    if not fill.empty and _scale_ok(anchor, fill):
                        pieces.append(fill)
                        status["source"] = "NSE archives (gap-fill)"

    if pieces:
        combined = pd.concat([cached] + pieces).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        combined.index = pd.to_datetime(combined.index).normalize()
        if not use_nse:
            combined = _fix_consolidation_spikes(combined)
        _save_csv(combined, path)
        status["message"] = (f"Updated {len(combined)} rows "
                             f"({combined.index[0].date()} → {combined.index[-1].date()})")
    else:
        combined = cached
        if combined.empty:
            status["success"] = False
            status["message"] = f"No data for {ticker}"
        else:
            last_d   = combined.index[-1].date()
            lag      = (today - last_d).days
            lag_note = f" (data lags {lag}d)" if lag > 5 else ""
            if not status["message"]:
                status["message"] = (f"Cached {len(combined)} rows "
                                     f"({combined.index[0].date()} → {last_d}){lag_note}")

    if not combined.empty:
        combined = combined[combined.index >= pd.Timestamp(start_date)]
    return combined, status


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_display_name(ticker: str) -> str:
    return _TICKER_TO_NAME.get(ticker, ticker)


def get_min_start(ticker: str) -> str:
    return _MIN_START.get(ticker, "2000-01-01")


def list_cached_tickers() -> list[str]:
    cached = []
    for path in sorted(_IDX_DIR.glob("*.csv")):
        for ticker in INSTRUMENTS.values():
            if _safe_fname(ticker) == path.stem:
                cached.append(ticker)
                break
        else:
            cached.append(path.stem)
    return cached


def load_cached_price(ticker: str, start_date: str = "2000-01-01") -> pd.Series:
    s = _load_csv(_csv_path(ticker))
    if s.empty and ticker == "NIFTY500":
        s = _load_csv(_csv_path("NIFTY500_SEED"))
    if not s.empty:
        s = s[s.index >= pd.Timestamp(start_date)]
    return s


def clear_cache(ticker: str) -> bool:
    path = _csv_path(ticker)
    if path.exists():
        path.unlink()
        return True
    return False


def prefetch_all_instruments() -> None:
    """
    Refresh every instrument whose CSV is stale (last date ≥ 2 calendar days old).
    Designed to run in a daemon background thread on app startup so the UI is
    never blocked.  Each ticker uses its normal get_price() routing so all
    existing source-fallback logic applies.
    """
    import concurrent.futures
    today   = _datetime.now(_IST).date()
    tickers = list(INSTRUMENTS.values())

    def _refresh(ticker: str) -> None:
        try:
            path   = _csv_path(ticker)
            cached = _load_csv(path)
            if not cached.empty:
                days_stale = (today - cached.index[-1].date()).days
                if days_stale < 2:
                    return
            ms = _MIN_START.get(ticker, "2000-01-01")
            get_price(ticker, start_date=ms)
            log.debug("prefetch OK: %s", ticker)
        except Exception as exc:
            log.debug("prefetch failed for %s: %s", ticker, exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_refresh, tickers))
"""
data/nse_stock_list.py
Full NSE equity stock catalog.

Primary source: NSE archives EQUITY_L.csv — all ~1800 EQ-series stocks.
Cache: data/live/nse_equity_list.json, refreshed every 7 days.
Fallback: built-in list of ~30 major stocks if NSE is unreachable.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_FILE    = Path(__file__).resolve().parent / "live" / "nse_equity_list.json"
_EQUITY_L_URL  = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
_CACHE_MAX_AGE = timedelta(days=7)

_HDRS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
}

# Minimal built-in fallback so the app never fails completely
_FALLBACK: dict[str, str] = {
    "RELIANCE – Reliance Industries":      "RELIANCE.NS",
    "TCS – Tata Consultancy Services":     "TCS.NS",
    "HDFCBANK – HDFC Bank":                "HDFCBANK.NS",
    "ICICIBANK – ICICI Bank":              "ICICIBANK.NS",
    "INFY – Infosys":                      "INFY.NS",
    "SBIN – State Bank of India":          "SBIN.NS",
    "KOTAKBANK – Kotak Mahindra Bank":     "KOTAKBANK.NS",
    "AXISBANK – Axis Bank":                "AXISBANK.NS",
    "HINDUNILVR – Hindustan Unilever":     "HINDUNILVR.NS",
    "ITC – ITC":                           "ITC.NS",
    "BHARTIARTL – Bharti Airtel":          "BHARTIARTL.NS",
    "WIPRO – Wipro":                       "WIPRO.NS",
    "TATAMOTORS – Tata Motors":            "TATAMOTORS.NS",
    "TATASTEEL – Tata Steel":              "TATASTEEL.NS",
    "SUNPHARMA – Sun Pharmaceutical":      "SUNPHARMA.NS",
    "BAJFINANCE – Bajaj Finance":          "BAJFINANCE.NS",
    "MARUTI – Maruti Suzuki":              "MARUTI.NS",
    "NTPC – NTPC":                         "NTPC.NS",
    "POWERGRID – Power Grid Corp":         "POWERGRID.NS",
    "ONGC – ONGC":                         "ONGC.NS",
}


def _fetch_from_nse() -> dict[str, str]:
    """Download EQUITY_L.csv and return {label: ticker.NS} for all EQ-series stocks."""
    import requests as _req
    r = _req.get(_EQUITY_L_URL, headers=_HDRS, timeout=20)
    r.raise_for_status()

    result: dict[str, str] = {}
    reader = csv.DictReader(io.StringIO(r.text), skipinitialspace=True)
    for row in reader:
        sym    = (row.get("SYMBOL") or "").strip()
        name   = (row.get("NAME OF COMPANY") or "").strip()
        series = (row.get("SERIES") or "").strip()
        if not sym or series != "EQ":
            continue
        label         = f"{sym} – {name}" if name else sym
        result[label] = f"{sym}.NS"
    return result


def load_nse_stocks() -> dict[str, str]:
    """Return {display_label: ticker.NS} for all NSE equity stocks."""
    if _CACHE_FILE.exists():
        age = datetime.now() - datetime.fromtimestamp(_CACHE_FILE.stat().st_mtime)
        if age < _CACHE_MAX_AGE:
            try:
                with open(_CACHE_FILE, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict) and len(data) > 100:
                    log.debug("nse_equity_list loaded from cache: %d stocks", len(data))
                    return data
            except Exception as exc:
                log.debug("nse_equity_list cache read failed: %s", exc)

    try:
        stocks = _fetch_from_nse()
        if len(stocks) > 100:
            _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
                json.dump(stocks, fh, ensure_ascii=False)
            log.info("NSE equity list fetched and cached: %d stocks", len(stocks))
            return stocks
    except Exception as exc:
        log.warning("NSE equity list fetch failed, using fallback: %s", exc)

    return _FALLBACK


NSE_STOCKS: dict[str, str] = load_nse_stocks()

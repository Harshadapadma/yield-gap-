"""
Standalone script — fetch Nifty Smallcap 100 historical data from 2004 to today.
Saves ONLY to: nifty_smallcap100_since2004.csv  (date, close)
Does NOT touch any existing project files.

Source: niftyindices.com API (getHistoricaldatatabletoString)
  - Index name: "NIFTY SMLCAP 100"  (abbreviated — "NIFTY SMALLCAP 100" returns 0 rows)
  - 3-month chunks (1-year chunks time out)
  - 90-second timeout per request
  - 3-second sleep + session re-prime every 5 requests

Nifty Smallcap 100 was launched Dec 29 2004; data before that won't exist.
Expected coverage: ~Dec 2004 → today  (~5,500 trading rows).
"""

import json
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd
from curl_cffi import requests as cfr

INDEX_NAME  = "NIFTY SMLCAP 100"
OUTPUT_CSV  = Path(__file__).parent / "nifty_smallcap100_since2004.csv"
START_DATE  = date(2005, 1, 1)   # index launched Dec 29 2004; Q1 2005 is first full quarter
TODAY       = date.today()
CHUNK_DAYS  = 90    # 3-month windows — 1-year windows time out on the server
SLEEP_SEC   = 4.0   # between requests; server rate-limits shorter intervals
REPRIME_N   = 4     # re-prime cookies every N requests

HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.niftyindices.com/reports/historical-data",
    "Origin": "https://www.niftyindices.com",
}


def _prime(sess) -> bool:
    try:
        sess.get("https://www.niftyindices.com/", timeout=20)
        time.sleep(0.8)
        sess.get("https://www.niftyindices.com/reports/historical-data", timeout=20)
        time.sleep(1.0)
        return True
    except Exception as exc:
        print(f"  [prime failed: {type(exc).__name__}]")
        return False


def _fetch_chunk(sess, start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    cinfo = (
        f"{{'name':'{INDEX_NAME}','startDate':'{start.strftime('%d-%b-%Y')}'"
        f",'endDate':'{end.strftime('%d-%b-%Y')}','indexName':'{INDEX_NAME}'}}"
    )
    r = sess.post(
        "https://www.niftyindices.com/Backpage.aspx/getHistoricaldatatabletoString",
        data=json.dumps({"cinfo": cinfo}),
        headers=HEADERS,
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    raw = r.json().get("d", "[]")
    return json.loads(raw) if isinstance(raw, str) else raw


def fetch() -> pd.Series:
    sess = cfr.Session(impersonate="chrome124")
    print("Priming cookies…")
    _prime(sess)
    print("  OK\n")

    start_ts = pd.Timestamp(START_DATE)
    end_ts   = pd.Timestamp(TODAY)
    all_records: dict[pd.Timestamp, float] = {}
    chunk_start = start_ts
    request_count = 0
    errors = 0

    total_chunks = 0
    cur = chunk_start
    while cur <= end_ts:
        total_chunks += 1
        cur += pd.DateOffset(days=CHUNK_DAYS)

    chunk_num = 0
    while chunk_start <= end_ts:
        chunk_end = min(chunk_start + pd.DateOffset(days=CHUNK_DAYS - 1), end_ts)
        chunk_num += 1

        # Re-prime session every REPRIME_N requests
        if request_count > 0 and request_count % REPRIME_N == 0:
            print("  [re-priming session…]")
            _prime(sess)

        print(f"  [{chunk_num:3d}/{total_chunks}] {chunk_start.date()} → {chunk_end.date()} … ",
              end="", flush=True)

        try:
            rows = _fetch_chunk(sess, chunk_start, chunk_end)
            for row in rows:
                try:
                    dt_raw  = row.get("HistoricalDate") or row.get("TIMESTAMP") or row.get("date","")
                    val_raw = row.get("CLOSE") or row.get("Close") or row.get("CLOSING_INDEX_VAL","")
                    ts  = pd.Timestamp(str(dt_raw).strip()).normalize()
                    val = float(str(val_raw).replace(",", ""))
                    if val > 100:
                        all_records[ts] = val
                except Exception:
                    pass
            print(f"{len(rows)} rows  (running total: {len(all_records):,})")
            errors = 0
        except Exception as exc:
            errors += 1
            print(f"skip ({type(exc).__name__})")
            # Consecutive failures → re-prime and retry once, then move on
            if errors >= 2:
                print("  re-priming session…")
                _prime(sess)
                errors = 0
                try:
                    rows = _fetch_chunk(sess, chunk_start, chunk_end)
                    for row in rows:
                        try:
                            dt_raw  = row.get("HistoricalDate") or row.get("TIMESTAMP") or ""
                            val_raw = row.get("CLOSE") or row.get("CLOSING_INDEX_VAL","")
                            ts  = pd.Timestamp(str(dt_raw).strip()).normalize()
                            val = float(str(val_raw).replace(",", ""))
                            if val > 100:
                                all_records[ts] = val
                        except Exception:
                            pass
                    print(f"  retry OK: {len(rows)} rows")
                except Exception:
                    print("  retry also failed — skipping chunk")

        chunk_start = chunk_end + pd.DateOffset(days=1)
        request_count += 1
        time.sleep(SLEEP_SEC)

    if not all_records:
        return pd.Series(dtype=float)
    s = pd.Series(all_records).sort_index()
    return s[~s.index.duplicated(keep="last")]


if __name__ == "__main__":
    print("=" * 60)
    print("  Nifty Smallcap 100 — full history (2004 → today)")
    print(f"  Source: niftyindices.com  index='{INDEX_NAME}'")
    print("=" * 60)
    print()

    series = fetch()

    if series.empty:
        print("\nNo data retrieved. The API may be rate-limiting. Try again in a few minutes.")
        sys.exit(1)

    df = series.reset_index()
    df.columns = ["date", "close"]
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    df.to_csv(OUTPUT_CSV, index=False)

    print(f"\n{'='*60}")
    print(f"Saved {len(df):,} rows to {OUTPUT_CSV}")
    print(f"Range: {df['date'].iloc[0]}  →  {df['date'].iloc[-1]}")
    print()
    print(df.tail(5).to_string(index=False))

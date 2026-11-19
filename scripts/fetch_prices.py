#!/usr/bin/env python3
"""FMP price ingestion for event studies (materiality calibration).

Fetches daily closes from Financial Modeling Prep for universe tickers (plus
the benchmark) into the prices table. Idempotent upserts; safe to re-run.

    python scripts/fetch_prices.py --tickers AAPL,MSFT --start 2020-01-01
    python scripts/fetch_prices.py --all --start 2014-01-01   # replay range
    python scripts/fetch_prices.py --dry-run

Requires FMP_API_KEY in .env (never committed). Ticker caveat: universe
tickers are as-listed in the source universe file; non-US listings may need
exchange suffixes — mismatches are logged to data/fetch_prices_misses.log,
not silently dropped.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import ROOT, load_env, params
from src.store import connect
from src.event_study import ensure_schema

BASE = "https://financialmodelingprep.com/stable/historical-price-eod/full"


def fetch_series(ticker: str, start: str, end: str, key: str) -> list:
    url = (f"{BASE}?symbol={ticker}&from={start}&to={end}&apikey={key}")
    with urllib.request.urlopen(url, timeout=30) as r:
        js = json.loads(r.read().decode())
    if isinstance(js, dict) and js.get("Error Message"):
        raise ValueError(js["Error Message"])
    return [(row["date"], float(row["close"])) for row in (js or [])
            if row.get("close") is not None]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="comma-separated; default: benchmark only")
    ap.add_argument("--all", action="store_true",
                    help="all universe tickers with a ticker symbol")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default=None)
    ap.add_argument("--benchmark", default="^GSPC")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = connect(ROOT / "data" / "controversy.db")
    ensure_schema(conn)
    tickers = []
    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    elif args.all:
        tickers = [r[0] for r in conn.execute(
            "SELECT DISTINCT ticker FROM companies WHERE ticker IS NOT NULL "
            "AND map_status='auto_mapped' ORDER BY ticker").fetchall()]
    tickers = [args.benchmark] + [t for t in tickers if t != args.benchmark]
    print(f"plan: {len(tickers)} tickers, {args.start}..{args.end or 'today'}")
    if args.dry_run:
        return

    load_env()
    key = os.environ.get("FMP_API_KEY")
    if not key:
        sys.exit("FMP_API_KEY not found in .env")
    end = args.end or params().get("as_of")
    misses = []
    for i, t in enumerate(tickers):
        try:
            rows = fetch_series(t, args.start, end, key)
        except Exception as e:
            misses.append((t, str(e)[:120]))
            print(f"[{i+1}/{len(tickers)}] {t}: MISS {e}", flush=True)
            time.sleep(args.sleep)
            continue
        conn.executemany(
            "INSERT OR REPLACE INTO prices (ticker, date, close) VALUES (?,?,?)",
            [(t, d, c) for d, c in rows])
        conn.commit()
        print(f"[{i+1}/{len(tickers)}] {t}: {len(rows)} rows", flush=True)
        time.sleep(args.sleep)
    if misses:
        log = ROOT / "data" / "fetch_prices_misses.log"
        with open(log, "a") as f:
            for t, e in misses:
                f.write(f"{t}\t{e}\n")
        print(f"{len(misses)} misses logged to {log}")
    print("done")


if __name__ == "__main__":
    main()

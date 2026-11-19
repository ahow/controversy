"""Event studies for the materiality score: abnormal returns around confirmed
incidents, used to calibrate the expected-CAR table in parameters.yaml
(impact.materiality.expected_car_bps).

Method (deliberately simple, documented, auditable):
- Market-adjusted model: abnormal return = company daily return minus
  benchmark daily return (default ^GSPC). No estimation window, no betas —
  with thin per-issue samples, extra model complexity is false precision.
- Event date = incident first_seen; day 0 = first trading day on/after it.
- CAR[a,b] = sum of abnormal returns over trading days a..b (inclusive),
  reported in basis points.
- Cells with n < min_cell_n are FLAGGED low_evidence and never smoothed;
  the proposal is written to data/materiality_proposal.json and merged into
  parameters.yaml only by a human with a version bump (same discipline as the
  theme scorecard).
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

_DDL = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (ticker, date)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.commit()


def _series(conn, ticker: str) -> list[tuple[str, float]]:
    return [(r["date"], r["close"]) for r in conn.execute(
        "SELECT date, close FROM prices WHERE ticker=? ORDER BY date",
        (ticker,)).fetchall()]


def _returns(series: list[tuple[str, float]]) -> dict[str, float]:
    return {series[i][0]: series[i][1] / series[i - 1][1] - 1.0
            for i in range(1, len(series))}


def compute_car(co_series: list[tuple[str, float]],
                bench_series: list[tuple[str, float]],
                event_date: str, window: tuple[int, int]) -> dict | None:
    """Market-adjusted CAR in bps over trading days [a, b] around event_date.

    Day 0 is the first trading day on/after event_date. Returns None when the
    window cannot be fully observed (honest miss, never padded).
    """
    a, b = window
    co_dates = [d for d, _ in co_series]
    be_dates = [d for d, _ in bench_series]
    try:
        day0_idx = next(i for i, d in enumerate(co_dates) if d >= event_date)
    except StopIteration:
        return None
    lo, hi = day0_idx + a, day0_idx + b
    if lo < 1 or hi >= len(co_series):
        return None                                    # window not observable
    # The company window must be exactly the benchmark's trading days. A gap
    # in the company series (trading halt, missing ingest) must fail loudly —
    # otherwise a multi-day move is silently read as a one-day return.
    b0 = next((i for i, d in enumerate(be_dates)
               if d >= co_dates[day0_idx]), None)
    if b0 is None or co_dates[lo:hi + 1] != be_dates[b0 + a:b0 + b + 1]:
        return None                                    # calendar mismatch/gap
    co_ret, be_ret = _returns(co_series), _returns(bench_series)
    car = 0.0
    n = 0
    for d in co_dates[lo:hi + 1]:
        if d not in co_ret or d not in be_ret:
            return None                                # gap in either series
        car += co_ret[d] - be_ret[d]
        n += 1
    return {"car_bps": round(car * 1e4, 1), "n_days": n,
            "day0": co_dates[day0_idx]}


def build_materiality_table(conn: sqlite3.Connection, benchmark: str,
                            windows: list[list[int]],
                            min_cell_n: int = 5) -> dict:
    """Calibration proposal: median CAR per issue x tier (x window), with
    explicit cell sizes. Confirmed incidents only — indicative coverage is
    not evidence of price-relevant substance.

    Tier falls back to 'S2' when the incident was never severity-graded
    (shadow mode: most incidents) and is recorded as such.

    `cells` keeps every ISSUE|TIER|a-b window for audit; `expected_car_bps`
    is keyed ISSUE|TIER from the primary window (windows[0]) — the lookup
    contract used by impact.score_company.
    """
    ensure_schema(conn)
    cells: dict[str, list[float]] = {}
    meta: dict[str, dict] = {}
    rows = conn.execute(
        """SELECT i.event_uri, i.issue_id, i.severity_tier, i.first_seen,
                  c.ticker
           FROM incidents i JOIN companies c ON c.id = i.company_id
           WHERE i.layer='confirmed' AND i.issue_id IS NOT NULL
             AND c.ticker IS NOT NULL""").fetchall()
    used = skipped = 0
    for r in rows:
        tick = r["ticker"]
        co, be = _series(conn, tick), _series(conn, benchmark)
        if len(co) < 25 or len(be) < 25:
            skipped += 1
            continue
        tier = r["severity_tier"] or "S2"
        for w in windows:
            res = compute_car(co, be, r["first_seen"], tuple(w))
            if res is None:
                continue
            key = f"{r['issue_id']}|{tier}|{w[0]}-{w[1]}"
            cells.setdefault(key, []).append(res["car_bps"])
            used += 1
    table = {}
    # expected_car_bps is keyed "ISSUE|TIER" (the lookup contract in
    # impact.score_company) and is built from the PRIMARY window
    # (windows[0]); the full per-window cells stay in `cells` for audit.
    primary = f"{windows[0][0]}-{windows[0][1]}"
    for key, vals in sorted(cells.items()):
        med = statistics.median(vals)
        meta[key] = {"n": len(vals), "median_car_bps": round(med, 1),
                     "low_evidence": len(vals) < min_cell_n,
                     "min": round(min(vals), 1), "max": round(max(vals), 1)}
        issue_tier, win = key.rsplit("|", 1)
        if win == primary:
            table[issue_tier] = round(med, 1)
    return {"status": "fitted" if table else "insufficient_data",
            "benchmark": benchmark, "min_cell_n": min_cell_n,
            "n_incidents_used": used, "n_incidents_skipped": skipped,
            "cells": meta, "expected_car_bps": table}

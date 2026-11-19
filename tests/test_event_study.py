"""Tests for the materiality event study (src/event_study.py).

Synthetic price series with known abnormal returns; fixtures build the REAL
schema (store + prices DDL)."""
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.event_study import (build_materiality_table, compute_car,
                             ensure_schema)
from src.store import connect

RAW = '{"confirmed_triggers": ["regulator_action"]}'


def _mk():
    conn = connect(":memory:")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO companies (id, name, ticker, map_status) "
        "VALUES (1, 'Acme', 'ACM', 'auto_mapped')")
    conn.commit()
    return conn


def _cal(start_day=3, n=45):
    """45 trading days from Mon 2025-11-03, skipping weekends."""
    out, d = [], dt.date(2025, 11, start_day)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _prices(conn, ticker, series):
    for d, c in series:
        conn.execute(
            "INSERT OR REPLACE INTO prices (ticker, date, close) "
            "VALUES (?,?,?)", (ticker, d, c))
    conn.commit()


def _inc(conn, first, issue="ENV_DISASTER", tier=None):
    conn.execute(
        "INSERT INTO incidents (company_id, event_uri, first_seen, last_seen,"
        " issue_id, layer, n_articles, n_sources, severity_tier, raw_json)"
        " VALUES (1, ?, ?, ?, ?, 'confirmed', 5, 3, ?, ?)",
        (f"ev:{first}", first, first, issue, tier, RAW))
    conn.commit()


def test_car_positive_shock_flat_benchmark():
    days = _cal()
    bench = [(d, 100.0) for d in days]
    ev = days[20]
    co, px = [], 50.0
    for d in days:
        if d == ev:
            px *= 1.10
        co.append((d, px))
    r = compute_car(co, bench, ev, (0, 5))
    assert r is not None
    # +10% on day 0 vs flat benchmark -> +1000 bps.
    assert abs(r["car_bps"] - 1000.0) < 1.0
    assert r["n_days"] == 6
    assert r["day0"] == ev


def test_car_negative_shock_and_benchmark_drift_removed():
    days = _cal()
    bench = [(d, 100.0 * (1.001 ** i)) for i, d in enumerate(days)]
    ev = days[20]
    co, px = [], 80.0
    for d in days:
        if d == ev:
            px *= 0.90
        co.append((d, px))
    r = compute_car(co, bench, ev, (0, 20))
    assert r is not None
    # Company -10% on day 0 then flat; benchmark drifts +0.1%/day for
    # 21 days (~+2.1%) -> CAR about -1000 - 210 = -1210 bps.
    assert -1250.0 < r["car_bps"] < -1150.0


def test_event_on_nontrading_day_shifts_to_next():
    days = _cal()
    bench = [(d, 100.0) for d in days]
    d0 = dt.date.fromisoformat(days[20])
    sat = (d0 + dt.timedelta(days=(5 - d0.weekday()) % 7)).isoformat()
    day0_expected = next(d for d in days if d >= sat)
    co, px = [], 50.0
    for d in days:
        if d == day0_expected:
            px *= 1.10
        co.append((d, px))
    r = compute_car(co, bench, sat, (0, 5))
    assert r is not None
    assert r["day0"] == day0_expected
    assert abs(r["car_bps"] - 1000.0) < 1.0


def test_unobservable_window_returns_none_not_zero():
    days = _cal()
    bench = [(d, 100.0) for d in days]
    co = [(d, 50.0) for d in days]
    # Event 2 trading days before the series ends: [0,5] cannot complete.
    assert compute_car(co, bench, days[-2], (0, 5)) is None
    # Event before the series starts: no day0 -> None.
    assert compute_car(co, bench, "2025-10-01", (0, 5)) is None
    # Event on the very first series date: no prior close for the day-0
    # return -> None (lo < 1), never padded.
    assert compute_car(co, bench, days[0], (0, 5)) is None


def test_gap_in_series_returns_none():
    days = _cal()
    bench = [(d, 100.0) for d in days]
    # Company series missing the day after the event.
    ev = days[20]
    co = [(d, 50.0) for d in days if d != days[21]]
    assert compute_car(co, bench, ev, (0, 5)) is None


def test_table_low_evidence_cell_flagged_not_smoothed():
    conn = _mk()
    days = _cal()
    _prices(conn, "^GSPC", [(d, 100.0) for d in days])
    _prices(conn, "ACM", [(d, 50.0) for d in days])
    for k in range(4):  # below min_cell_n=5
        _inc(conn, days[5 + k])
    res = build_materiality_table(conn, "^GSPC", [[0, 5]], min_cell_n=5)
    assert res["status"] == "fitted"          # cells exist, but flagged
    cell = res["cells"]["ENV_DISASTER|S2|0-5"]  # ungraded tier falls back to S2
    assert cell["n"] == 4
    assert cell["low_evidence"] is True
    assert res["n_incidents_used"] == 4


def test_table_insufficient_when_prices_missing():
    conn = _mk()
    _inc(conn, "2025-11-10")  # no prices at all -> incident skipped
    res = build_materiality_table(conn, "^GSPC", [[0, 5]], min_cell_n=5)
    assert res["status"] == "insufficient_data"
    assert res["cells"] == {}
    assert res["expected_car_bps"] == {}
    assert res["n_incidents_skipped"] == 1


def test_table_fitted_median_car():
    conn = _mk()
    days = _cal()
    _prices(conn, "^GSPC", [(d, 100.0) for d in days])
    events = [days[3], days[10], days[17], days[24], days[31]]
    evset = set(events)
    co, px = [], 50.0
    for d in days:
        if d in evset:
            px *= 0.95
        co.append((d, px))
    _prices(conn, "ACM", co)
    for e in events:
        _inc(conn, e, tier="S3")
    res = build_materiality_table(conn, "^GSPC", [[0, 5]], min_cell_n=5)
    assert res["status"] == "fitted"
    cell = res["cells"]["ENV_DISASTER|S3|0-5"]
    assert cell["n"] == 5
    assert cell["low_evidence"] is False
    assert abs(cell["median_car_bps"] - (-500.0)) < 5.0
    assert res["expected_car_bps"]["ENV_DISASTER|S3"] == \
        cell["median_car_bps"]


def test_only_confirmed_incidents_enter_the_table():
    conn = _mk()
    days = _cal()
    _prices(conn, "^GSPC", [(d, 100.0) for d in days])
    _prices(conn, "ACM", [(d, 50.0) for d in days])
    _inc(conn, days[5])
    conn.execute("UPDATE incidents SET layer='indicative'")
    conn.commit()
    res = build_materiality_table(conn, "^GSPC", [[0, 5]], min_cell_n=5)
    assert res["status"] == "insufficient_data"
    assert res["n_incidents_used"] == 0

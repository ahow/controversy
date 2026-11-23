"""Tests for the controversy level/direction engine (v0.5.0): step response,
exact decay rate, never-summed-across-issues, layer/tier/charge weighting,
sub-gate exclusion, and per-day rebuild idempotency."""
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.controversy import rebuild_controversy, _lambda
from src.store import connect

D0 = "2026-01-10"

P = {
    "controversy": {
        "half_life_days": {"FRAUD_CORRUPTION": 45, "TAX_CONDUCT": 30,
                           "_UNCLASSIFIED": 14, "_default": 21},
        "layer_weight": {"confirmed": 1.0, "indicative": 0.5},
        "tier_factor": {"S1": 0.6, "S2": 0.8, "S3": 1.0, "S4": 1.25,
                        "_default": 1.0},
        "theme_boost_weight": 1.0,
        "min_level": 1e-9,          # tests inspect the full decay tail
    },
    "impact": {"outrage": {
        "w_articles": 0.5, "w_sources": 0.7, "w_tier1": 0.8, "w_langs": 0.6,
        "caps": {"articles": 50, "sources": 50, "tier1": 3, "langs": 5,
                 "actors": 5},
        "ngo_charge_floor": 0.8,
        "charge": {"FRAUD_CORRUPTION": 0.85, "TAX_CONDUCT": 0.5,
                   "_default": 0.35}}},
    "themes": {"authority_tier1": ["reuters.com"]},
}


def _mk(tmp_path):
    conn = connect(tmp_path / "t.db")
    conn.execute("INSERT INTO companies (id, name, concept_uri, map_status)"
                 " VALUES (1, 'Acme', 'uri:acme', 'auto_mapped')")
    conn.execute("INSERT INTO companies (id, name, concept_uri, map_status)"
                 " VALUES (2, 'Beta', 'uri:beta', 'auto_mapped')")
    conn.commit()
    return conn


def _day(day_offset):
    from datetime import date, timedelta
    return (date.fromisoformat(D0) + timedelta(days=day_offset)).isoformat()


def _article(conn, uri, day, cid=1, ev="evt1", score=5, title_match=0,
             source="reuters.com", lang="eng"):
    conn.execute(
        "INSERT OR IGNORE INTO articles (uri,date,lang,title,url,source,"
        "sentiment,event_uri,raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (uri, day, lang, f"t {uri}", f"http://x/{uri}", source, -0.5, ev,
         "{}"))
    conn.execute(
        "INSERT OR IGNORE INTO article_companies"
        " (article_uri, company_id, score, title_match) VALUES (?,?,?,?)",
        (uri, cid, score, title_match))
    conn.commit()


def _incident(conn, ev, cid=1, issue="FRAUD_CORRUPTION", layer="confirmed",
              tier="S2", day=D0):
    conn.execute(
        "INSERT OR REPLACE INTO incidents (company_id, event_uri, first_seen,"
        " last_seen, issue_id, layer, n_articles, n_sources, severity_tier,"
        " raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, ev, day, day, issue, layer, 1, 1, tier, "{}"))
    conn.commit()


def _row(conn, cid, issue, day):
    return conn.execute(
        "SELECT * FROM company_issue_daily WHERE company_id=? AND issue_id=?"
        " AND date=?", (cid, issue, day)).fetchone()


def test_step_response_and_exact_decay(tmp_path):
    conn = _mk(tmp_path)
    _article(conn, "a1", D0)
    _incident(conn, "evt1")
    rebuild_controversy(conn, D0, P)
    r1 = _row(conn, 1, "FRAUD_CORRUPTION", D0)
    lam = _lambda(45)
    assert abs(r1["level"] - (1 - lam) * r1["influx"]) < 1e-6
    assert r1["direction"] > 0                     # building

    d1, d2 = _day(1), _day(2)
    rebuild_controversy(conn, d1, P)
    rebuild_controversy(conn, d2, P)
    r2, r3 = _row(conn, 1, "FRAUD_CORRUPTION", d1), \
        _row(conn, 1, "FRAUD_CORRUPTION", d2)
    assert abs(r2["level"] - lam * r1["level"]) < 1e-6   # exact decay rate
    assert abs(r3["level"] - lam * r2["level"]) < 1e-6
    assert r2["influx"] == 0.0                     # continuation row, no flow
    assert r2["direction"] < 0 and r3["direction"] < 0   # fading, no lag
    # direction is the exact first difference of the level (net flow)
    assert abs(r2["direction"] - (r2["level"] - r1["level"])) < 1e-6
    assert abs(r3["direction"] - (r3["level"] - r2["level"])) < 1e-6


def test_half_life_reaches_half_at_h_days(tmp_path):
    conn = _mk(tmp_path)
    _article(conn, "a1", D0)
    _incident(conn, "evt1", issue="TAX_CONDUCT")
    rebuild_controversy(conn, D0, P)
    peak = _row(conn, 1, "TAX_CONDUCT", D0)["level"]
    for k in range(1, 31):
        rebuild_controversy(conn, _day(k), P)
    assert abs(_row(conn, 1, "TAX_CONDUCT", _day(30))["level"] - peak / 2) \
        < peak * 0.02                              # 30-day half-life, 2% tol


def test_never_summed_across_issues(tmp_path):
    conn = _mk(tmp_path)
    _article(conn, "a1", D0, ev="evt1")
    _article(conn, "a2", D0, ev="evt2", source="bbc.com")
    _incident(conn, "evt1", issue="FRAUD_CORRUPTION")
    _incident(conn, "evt2", issue="TAX_CONDUCT", layer="indicative")
    rebuild_controversy(conn, D0, P)
    rows = conn.execute("SELECT issue_id FROM company_issue_daily "
                        "WHERE company_id=1 AND date=?", (D0,)).fetchall()
    issues = sorted(r["issue_id"] for r in rows)
    assert issues == ["FRAUD_CORRUPTION", "TAX_CONDUCT"]   # two rows, no aggregate
    # confirmed fraud outweighs indicative tax on identical raw volume
    f = _row(conn, 1, "FRAUD_CORRUPTION", D0)["influx"]
    t = _row(conn, 1, "TAX_CONDUCT", D0)["influx"]
    assert f > t


def test_layer_tier_charge_weighting(tmp_path):
    conn = _mk(tmp_path)
    for i, (ev, layer, tier) in enumerate(
            [("e1", "confirmed", "S4"), ("e2", "indicative", "S1")]):
        _article(conn, f"a{i}", D0, ev=ev)
        _incident(conn, ev, layer=layer, tier=tier)
    # identical raw volume: confirmed/S4 fraud must dominate indicative/S1
    rebuild_controversy(conn, D0, P)
    r = _row(conn, 1, "FRAUD_CORRUPTION", D0)
    comp = json.loads(r["components_json"])
    assert comp["volume_w"] == 1 * 1.0 * 1.25 + 1 * 0.5 * 0.6   # 1.55


def test_sub_gate_links_excluded(tmp_path):
    conn = _mk(tmp_path)
    _article(conn, "a1", D0, score=2, title_match=0)   # passing mention only
    _incident(conn, "evt1")
    rebuild_controversy(conn, D0, P)
    assert _row(conn, 1, "FRAUD_CORRUPTION", D0) is None


def test_rebuild_idempotent_and_stale_rows_removed(tmp_path):
    conn = _mk(tmp_path)
    _article(conn, "a1", D0)
    _incident(conn, "evt1")
    rebuild_controversy(conn, D0, P)
    first = _row(conn, 1, "FRAUD_CORRUPTION", D0)
    rebuild_controversy(conn, D0, P)                   # rerun same day
    second = _row(conn, 1, "FRAUD_CORRUPTION", D0)
    assert (first["level"], first["direction"]) == \
        (second["level"], second["direction"])
    # tighten the world: incident reclassified away -> stale row must vanish
    conn.execute("DELETE FROM incidents WHERE event_uri='evt1'")
    conn.commit()
    rebuild_controversy(conn, D0, P)
    assert _row(conn, 1, "FRAUD_CORRUPTION", D0) is None

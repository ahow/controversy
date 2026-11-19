"""Tests for the three-score impact model (src/impact.py).

Fixtures build the REAL schema (store/incidents/themes/impact DDLs) so the
tests track the production contract rather than a private copy.
"""
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import impact
from src.config import params, taxonomy
from src.incidents import ensure_schema as ensure_daily
from src.impact import (company_features, ensure_schema, rebuild_company_scores,
                        score_company)
from src.store import connect
from src.themes import ensure_schema as ensure_themes

DAY = "2026-11-18"
IP = params()["impact"]
THEMES_P = params()["themes"]
TAX = taxonomy()
SEV_PRIOR = {i["id"]: i.get("prior_severity", 2) for i in TAX}

REG = '{"confirmed_triggers": ["regulator_action"]}'
LIT = '{"confirmed_triggers": ["litigation_filed"]}'
NGO = '{"confirmed_triggers": ["credible_ngo_campaign"]}'


def _mk():
    conn = connect(":memory:")
    ensure_daily(conn)
    ensure_themes(conn)
    ensure_schema(conn)
    for cid, name, tick in ((1, "Big Corp", "BIG"), (2, "Small Co", "SML")):
        conn.execute(
            "INSERT INTO companies (id, name, ticker, map_status) "
            "VALUES (?,?,?, 'auto_mapped')", (cid, name, tick))
    conn.commit()
    return conn


def _hit(conn, cid, uri, day, source="wire.com", lang="eng",
         title="company story", score=5, title_match=1):
    """One article on `day` attributed to company `cid` (subject link)."""
    conn.execute(
        "INSERT OR REPLACE INTO articles (uri, date, lang, title, url, "
        " source, raw_json) VALUES (?,?,?,?,?,?,?)",
        (uri, day, lang, title, f"http://x/{uri}", source,
         json.dumps({"source": {"uri": source}})))
    conn.execute(
        "INSERT OR IGNORE INTO article_companies"
        " (article_uri, company_id, score, title_match) VALUES (?,?,?,?)",
        (uri, cid, score, title_match))
    conn.commit()


def _daily(conn, cid, day, n_articles, n_sources, z=None, thin=1):
    conn.execute(
        "INSERT OR REPLACE INTO company_daily (company_id, date, n_articles,"
        " n_sources, z, thin_data) VALUES (?,?,?,?,?,?)",
        (cid, day, n_articles, n_sources, z, thin))
    conn.commit()


def _inc(conn, cid, issue, raw_json, first=DAY, last=DAY, layer="confirmed",
         tier=None, event_uri=None, n_sources=3):
    conn.execute(
        "INSERT INTO incidents (company_id, event_uri, first_seen, last_seen,"
        " issue_id, layer, n_articles, n_sources, severity_tier, raw_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (cid, event_uri or f"ev:{cid}:{issue}:{first}:{raw_json[25:40]}",
         first, last, issue, layer, 5, n_sources, tier, raw_json))
    conn.commit()


def _score(conn, cid, ip=None):
    feats = company_features(conn, cid, DAY, ip or IP, SEV_PRIOR, THEMES_P)
    return feats, score_company(feats, ip or IP)


def _terms(sc, block):
    return {t["feature"]: t for t in sc["decomposition"][block]["terms"]}


def test_trigger_hierarchy_regulator_beats_litigation():
    conn = _mk()
    for i in range(5):
        _hit(conn, 1, f"a{i}", DAY)
        _hit(conn, 2, f"b{i}", DAY)
    _daily(conn, 1, DAY, 5, 3)
    _daily(conn, 2, DAY, 5, 3)
    _inc(conn, 1, "ENV_DISASTER", REG)
    _inc(conn, 2, "ENV_DISASTER", LIT)
    f1, s1 = _score(conn, 1)
    f2, s2 = _score(conn, 2)
    assert f1["sev"]["trigger_strength"] == 1.0
    assert f2["sev"]["trigger_strength"] == 0.75
    assert s1["severity"] > s2["severity"]


def test_indicative_layer_contributes_no_trigger_or_issue():
    conn = _mk()
    for i in range(5):
        _hit(conn, 1, f"a{i}", DAY)
    _daily(conn, 1, DAY, 5, 3)
    _inc(conn, 1, "ENV_DISASTER", REG, layer="indicative")
    feats, sc = _score(conn, 1)
    assert feats["sev"]["trigger_strength"] == 0.0
    assert feats["sev"]["issue_sev"] == 0.0


def test_thin_excludes_z_never_imputes():
    conn = _mk()
    for i in range(5):
        _hit(conn, 1, f"a{i}", DAY)
        _hit(conn, 2, f"b{i}", DAY)
    # Same z on the books; company 1 is THIN, company 2 is not.
    _daily(conn, 1, DAY, 5, 3, z=3.0, thin=1)
    _daily(conn, 2, DAY, 5, 3, z=3.0, thin=0)
    _inc(conn, 1, "ENV_DISASTER", REG)
    _inc(conn, 2, "ENV_DISASTER", REG)
    f1, s1 = _score(conn, 1)
    f2, s2 = _score(conn, 2)
    assert f1["thin"] is True and f1["sev"]["z_excess"] is None
    assert f2["thin"] is False and f2["sev"]["z_excess"] == 0.75  # 3 / z_cap 4
    zt1 = _terms(s1, "severity")["z_excess"]
    assert zt1["value"] is None and zt1["contribution"] == 0.0
    assert s2["severity"] > s1["severity"]  # z rewards only when evidenced


def test_size_adjustment_property_equal_incidents_equal_severity():
    """Identical confirmed incidents, both THIN, wildly different raw volume:
    severity identical (raw volume never an input), outrage different
    (raw volume is the point there)."""
    conn = _mk()
    for i in range(50):
        _hit(conn, 1, f"a{i}", DAY, source=f"s{i % 40}.com")
    for i in range(5):
        _hit(conn, 2, f"b{i}", DAY)
    _daily(conn, 1, DAY, 50, 40, z=2.0, thin=1)
    _daily(conn, 2, DAY, 5, 3, z=2.0, thin=1)
    _inc(conn, 1, "ENV_DISASTER", REG)
    _inc(conn, 2, "ENV_DISASTER", REG)
    f1, s1 = _score(conn, 1)
    f2, s2 = _score(conn, 2)
    assert f1["thin"] and f2["thin"]
    assert s1["severity"] == s2["severity"]
    assert s1["outrage"] > s2["outrage"]


def test_outrage_charge_prior_for_issue():
    conn = _mk()
    _hit(conn, 1, "a0", DAY)
    _daily(conn, 1, DAY, 1, 1)
    _inc(conn, 1, "ENV_DISASTER", REG)
    feats, sc = _score(conn, 1)
    # ENV_DISASTER moral-charge prior is 0.9 in parameters.yaml.
    assert feats["out"]["charge"] == IP["outrage"]["charge"]["ENV_DISASTER"]
    assert _terms(sc, "outrage")["charge"]["value"] == 0.9


def test_ngo_campaign_forces_charge_floor():
    conn = _mk()
    _hit(conn, 1, "a0", DAY)
    _daily(conn, 1, DAY, 1, 1)
    _inc(conn, 1, "AI_HARM", NGO)  # AI_HARM prior charge 0.6 < floor 0.8
    feats, sc = _score(conn, 1)
    assert feats["out"]["charge"] == IP["outrage"]["ngo_charge_floor"]
    assert _terms(sc, "outrage")["charge"]["value"] == 0.8


def test_theme_boost_raises_outrage():
    conn = _mk()
    for i in range(5):
        _hit(conn, 1, f"a{i}", DAY)
        _hit(conn, 2, f"b{i}", DAY)
    _daily(conn, 1, DAY, 5, 3)
    _daily(conn, 2, DAY, 5, 3)
    _inc(conn, 1, "ENV_DISASTER", REG, event_uri="ev:boost")
    _inc(conn, 2, "ENV_DISASTER", REG, event_uri="ev:noboost")
    conn.execute(
        "INSERT INTO themes (id, label, first_seen, last_seen, status,"
        " n_events, escalation_score) VALUES ('th:1','t',?,?, 'emerging', 10,"
        " 80.0)", (DAY, DAY))
    conn.execute(
        "INSERT INTO theme_events (theme_id, event_uri) VALUES ('th:1',"
        " 'ev:boost')")
    conn.commit()
    f1, s1 = _score(conn, 1)
    f2, s2 = _score(conn, 2)
    assert f1["out"]["theme_boost"] == 0.8
    assert f2["out"]["theme_boost"] == 0.0
    assert s1["outrage"] > s2["outrage"]


def test_materiality_uncalibrated_is_null_not_zero():
    conn = _mk()
    for i in range(5):
        _hit(conn, 1, f"a{i}", DAY)
    _daily(conn, 1, DAY, 5, 3)
    _inc(conn, 1, "ENV_DISASTER", REG)
    feats, sc = _score(conn, 1)
    assert sc["materiality_bps"] is None
    assert sc["materiality"] is None
    assert sc["materiality_bps"] != 0  # never zero-filled
    assert sc["decomposition"]["materiality"]["status"] == "uncalibrated"


def test_materiality_with_fitted_table():
    conn = _mk()
    for i in range(10):
        _hit(conn, 1, f"a{i}", DAY, source=f"s{i}.com")
    _daily(conn, 1, DAY, 10, 10)
    _inc(conn, 1, "ENV_DISASTER", REG, n_sources=10)
    ip = copy.deepcopy(IP)
    ip["materiality"]["expected_car_bps"] = {"ENV_DISASTER|S2": -480}
    feats, sc = _score(conn, 1, ip)
    m = sc["decomposition"]["materiality"]
    # p_esc = max(theme_boost 0, min(10/25, 1)) = 0.4
    assert m["p_escalation"] == 0.4
    assert sc["materiality_bps"] == -192.0       # -480 x 0.4
    assert sc["materiality"] == 9.6              # 192/2000 x 100
    assert m["status"] == "ok"


def test_materiality_wildcard_fallback():
    conn = _mk()
    _hit(conn, 1, "a0", DAY)
    _daily(conn, 1, DAY, 1, 25)
    _inc(conn, 1, "FRAUD_CORRUPTION", REG, n_sources=25)
    ip = copy.deepcopy(IP)
    ip["materiality"]["expected_car_bps"] = {"FRAUD_CORRUPTION|*": -300}
    feats, sc = _score(conn, 1, ip)
    assert sc["materiality_bps"] == -300.0       # p_esc = 1.0 at 25 sources


def test_recurrence_increments_severity():
    conn = _mk()
    for i in range(5):
        _hit(conn, 1, f"a{i}", DAY)
        _hit(conn, 2, f"b{i}", DAY)
    _daily(conn, 1, DAY, 5, 3)
    _daily(conn, 2, DAY, 5, 3)
    _inc(conn, 1, "ENV_DISASTER", REG)
    _inc(conn, 2, "ENV_DISASTER", REG)
    # Company 1: two prior same-issue incidents before the active window.
    _inc(conn, 1, "ENV_DISASTER", REG, first="2026-10-01", last="2026-10-02",
         event_uri="ev:past1")
    _inc(conn, 1, "ENV_DISASTER", REG, first="2026-09-01", last="2026-09-02",
         event_uri="ev:past2")
    f1, s1 = _score(conn, 1)
    f2, s2 = _score(conn, 2)
    assert f1["sev"]["recurrence"] == 2 / 3
    assert f2["sev"]["recurrence"] == 0.0
    assert s1["severity"] > s2["severity"]


def test_actor_patterns_in_titles():
    conn = _mk()
    _hit(conn, 1, "a0", DAY, title="Regulator opens investigation")
    _daily(conn, 1, DAY, 1, 1)
    _inc(conn, 1, "ENV_DISASTER", REG)
    feats, _ = _score(conn, 1)
    # "regulator" + "investigation" both in actor_patterns -> 2/5.
    assert feats["sev"]["actors"] == 0.4


def test_decomposition_contract():
    conn = _mk()
    _hit(conn, 1, "a0", DAY)
    _daily(conn, 1, DAY, 1, 1)
    _inc(conn, 1, "FRAUD_CORRUPTION", REG)
    _, sc = _score(conn, 1)
    assert set(_terms(sc, "severity")) == {
        "trigger_strength", "issue_severity", "recurrence", "z_excess",
        "actors"}
    assert set(_terms(sc, "outrage")) == {
        "articles", "sources", "tier1", "langs", "theme_boost", "charge"}
    for t in _terms(sc, "severity").values():
        assert set(t) == {"feature", "value", "weight", "contribution"}
    for key in ("severity", "outrage"):
        assert 0.0 <= sc[key] <= 100.0


def test_rebuild_idempotent():
    conn = _mk()
    for i in range(5):
        _hit(conn, 1, f"a{i}", DAY)
    _daily(conn, 1, DAY, 5, 3)
    _inc(conn, 1, "ENV_DISASTER", REG)
    r1 = rebuild_company_scores(conn, DAY, IP, TAX, THEMES_P)
    r2 = rebuild_company_scores(conn, DAY, IP, TAX, THEMES_P)
    assert r1 == r2 == {"day": DAY, "scored": 1, "materiality_calibrated": 0}
    n = conn.execute("SELECT COUNT(*) FROM company_scores").fetchone()[0]
    assert n == 1
    # scores_json round-trips the full audit decomposition
    sj = json.loads(conn.execute(
        "SELECT scores_json FROM company_scores").fetchone()[0])
    assert "severity" in sj and "outrage" in sj and "materiality" in sj
    assert sj["thin"] is True

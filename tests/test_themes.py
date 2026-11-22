"""Tests for the emergent-theme layer (Channel B): signature extraction,
theme formation, lifecycle roll-up, escalation features, outcome labelling
and the scorecard fit."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.store import connect
from src.themes import (ensure_schema, event_signature, escalation_features,
                        promotion_candidates, rebuild_themes, score_theme)
from src.calibration import (FEATURE_ORDER, auc, calibrate, fit_logistic,
                             label_outcomes)

DAY = "2026-11-18"

TP = {
    "top_k_concepts": 5, "min_concept_score": 3, "max_doc_freq": 0.9,
    "merge_shared": 3, "assign_overlap": 2, "snapshot_days": 3,
    "status": {"emerging_score": 40, "dead_after_days": 7},
    "outcome": {"window_days": 7, "within_days": 90,
                "min_articles": 5, "min_sources": 3},
    "promotion": {"min_companies": 1, "min_days_alive": 2, "min_score": 50},
    "features": {"cap_articles_day": 50, "cap_sources": 50, "cap_langs": 5,
                 "cap_companies": 10, "cap_actor_hits": 5, "cap_days_alive": 30},
    "weights": {"intercept": -2.0, "accel": 0.8, "spread_sources": 0.6,
                "spread_langs": 0.7, "authority": 0.7, "actors": 0.9,
                "attach": 0.8, "persist": 0.4},
    "authority_tier1": ["reuters.com"],
    "actor_patterns": ["court", "regulator", "greenpeace"],
}


def _mk(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    conn.execute("INSERT INTO companies (id, name, concept_uri, map_status)"
                 " VALUES (1, 'Acme', 'uri:acme', 'auto_mapped')")
    conn.execute("INSERT INTO companies (id, name, concept_uri, map_status)"
                 " VALUES (2, 'Beta', 'uri:beta', 'auto_mapped')")
    conn.commit()
    return conn


def _article(conn, uri, event, day, source="reuters.com", lang="eng", cid=1,
             concepts=None, title="t"):
    raw = {"title": title, "body": "",
           "source": {"uri": source, "title": source},
           "concepts": concepts or []}
    conn.execute(
        "INSERT OR IGNORE INTO articles (uri,date,lang,title,url,source,"
        "sentiment,event_uri,raw_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (uri, day, lang, title, f"http://x/{uri}", source, -0.5, event,
         json.dumps(raw)))
    conn.execute("INSERT OR IGNORE INTO article_companies"
                 " (article_uri, company_id) VALUES (?,?)",
                 (uri, cid))
    conn.commit()


def _orphan_incident(conn, event, cid=1, day=DAY):
    conn.execute(
        "INSERT INTO incidents (company_id, event_uri, first_seen, last_seen,"
        " issue_id, layer, n_articles, n_sources)"
        " VALUES (?,?,?,?,NULL,'indicative',1,1)",
        (cid, event, day, day))
    conn.commit()


def _concept(uri, score=5, ctype="wiki", label=None):
    return {"uri": uri, "type": ctype, "score": score,
            "label": {"eng": label or uri.split("/")[-1]}}


def _shared(n=3):
    return [_concept(f"uri:c{i}") for i in range(n)]


# ------------------------------------------------------------------ basics

def test_signature_excludes_company_concepts_and_ranks(tmp_path):
    conn = _mk(tmp_path)
    _article(conn, "a1", "evt-1", DAY, concepts=[
        _concept("uri:acme", 10, "org"),        # universe company -> excluded
        _concept("uri:big", 9), _concept("uri:small", 1),  # below min_score
        _concept("uri:mid", 5)])
    sig, labels = event_signature(conn, "evt-1", {"uri:acme"},
                                  top_k=5, min_score=3)
    assert "uri:acme" not in sig and "uri:small" not in sig
    assert sig["uri:big"] == 9 and sig["uri:mid"] == 5
    assert labels["uri:big"] == "uri:big"          # fallback when no eng label


# Chaining tests need the df-cut disabled: with a 2-event fixture the shared
# concepts sit in 100% of orphan events and any cut < 1.0 drops them all.
TP_NOCUT = dict(TP, max_doc_freq=1.0)


def test_events_sharing_three_concepts_chain_into_theme(tmp_path):
    conn = _mk(tmp_path)
    for ev, arts in (("evt-1", ["a1", "a2"]), ("evt-2", ["a3"])):
        for au in arts:
            _article(conn, au, ev, DAY, source=f"{au}.com",
                     cid=1 if ev == "evt-1" else 2,
                     concepts=_shared(3))
        _orphan_incident(conn, ev, cid=1 if ev == "evt-1" else 2)
    stats = rebuild_themes(conn, DAY, TP_NOCUT)
    assert stats["themes_created"] == 1
    row = conn.execute("SELECT * FROM themes").fetchone()
    assert row["n_events"] == 2 and row["n_companies"] == 2
    assert row["thin"] == 1                       # no prior-day history
    td = conn.execute("SELECT * FROM theme_daily WHERE theme_id=?",
                      (row["id"],)).fetchone()
    assert td["n_articles"] == 3 and td["n_sources"] == 3


def test_lone_event_never_forms_theme(tmp_path):
    conn = _mk(tmp_path)
    _article(conn, "a1", "evt-1", DAY, concepts=_shared(3))
    _orphan_incident(conn, "evt-1")
    stats = rebuild_themes(conn, DAY, TP)
    assert stats["themes_created"] == 0 and stats["ungrouped"] == 1
    assert conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0] == 0


def test_df_cut_prevents_generic_blob(tmp_path):
    conn = _mk(tmp_path)
    tp = dict(TP, max_doc_freq=0.4)             # >40% of events -> dropped
    # 3 events sharing only a generic concept + pairwise-distinct specifics
    for i in range(3):
        ev = f"evt-{i}"
        _article(conn, f"a{i}", ev, DAY, concepts=[
            _concept("uri:generic", 9), _concept(f"uri:spec{i}")])
        _orphan_incident(conn, ev)
    stats = rebuild_themes(conn, DAY, tp)
    # 'generic' appears in 100% > 40% -> dropped; no 3-way overlap remains
    assert stats["themes_created"] == 0


def test_incremental_attach_and_fixed_point(tmp_path):
    conn = _mk(tmp_path)
    for ev, au in (("evt-1", "a1"), ("evt-2", "a2")):
        _article(conn, au, ev, DAY, concepts=_shared(3))
        _orphan_incident(conn, ev)
    rebuild_themes(conn, DAY, TP_NOCUT)
    tid = conn.execute("SELECT id FROM themes").fetchone()["id"]
    # day 2: new event sharing 2 concepts (>= assign_overlap) attaches
    _article(conn, "a3", "evt-3", "2026-11-19", concepts=_shared(2))
    _orphan_incident(conn, "evt-3", day="2026-11-19")
    s2 = rebuild_themes(conn, "2026-11-19", TP_NOCUT)
    assert s2["themes_created"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM theme_events WHERE theme_id=?",
        (tid,)).fetchone()[0] == 3
    # third run: no new events -> nothing changes (fixed point)
    s3 = rebuild_themes(conn, "2026-11-19", TP_NOCUT)
    assert s3["themes_created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM theme_events").fetchone()[0] == 3


# ---------------------------------------------------------------- features

def test_features_authority_actors_and_thin(tmp_path):
    conn = _mk(tmp_path)
    for i, (ev, au) in enumerate((("evt-1", "a1"), ("evt-2", "a2"))):
        _article(conn, au, ev, DAY, concepts=_shared(3) + [
            _concept("uri:court", 5, "wiki", "High Court")])
        _orphan_incident(conn, ev, cid=i + 1)
    rebuild_themes(conn, DAY, TP_NOCUT)
    tid = conn.execute("SELECT id FROM themes").fetchone()["id"]
    f = escalation_features(conn, tid, DAY, TP_NOCUT)
    assert f["thin"] is True                       # no prior-day history
    assert f["authority"] == 1.0 / 3               # one tier-1 outlet (reuters)
    assert f["n_actor_hits"] >= 1 and f["actors"] > 0
    assert f["attach"] == 2 / TP["features"]["cap_companies"]


def test_accel_after_history(tmp_path):
    conn = _mk(tmp_path)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO themes (id, label, first_seen, last_seen) VALUES ('t1','x','2026-11-10','2026-11-18')")
    for d, n in (("2026-11-16", 2), ("2026-11-17", 2)):
        conn.execute("INSERT INTO theme_daily (theme_id, date, n_articles)"
                     " VALUES (?,?,?)", ("t1", d, n))
    for ev in ("e1", "e2"):
        conn.execute("INSERT INTO theme_events VALUES ('t1',?)", (ev,))
        for i in range(4):
            _article(conn, f"{ev}{i}", ev, DAY)
    conn.commit()
    f = escalation_features(conn, "t1", DAY, TP)
    assert f["thin"] is False
    assert f["arts_day"] == 8 and f["arts_prev_avg"] == 2
    assert f["accel_raw"] == 4.0 and f["accel"] == 1.0   # capped at 3x


def test_score_decomposition_matches_weights(tmp_path):
    feats = {"accel": 0.5, "spread_sources": 0.2, "spread_langs": 0.0,
             "authority": 0.33, "actors": 0.2, "attach": 0.1, "persist": 0.5}
    score, terms = score_theme(feats, TP["weights"])
    assert [t["feature"] for t in terms] == [
        "accel", "spread_sources", "spread_langs", "authority",
        "actors", "attach", "persist"]
    z = -2.0 + sum(t["contribution"] for t in terms)
    import math
    assert abs(score - 100 / (1 + math.exp(-z))) < 0.15
    # monotonic: richer features -> higher score
    s2, _ = score_theme({k: min(v + 0.2, 1.0) for k, v in feats.items()},
                        TP["weights"])
    assert s2 > score


# ---------------------------------------------------------------- outcomes

def test_outcome_labeller(tmp_path):
    conn = _mk(tmp_path)
    for ev in ("e1", "e2"):
        conn.execute("INSERT INTO theme_events VALUES (?,?)", ("t1", ev))
    conn.execute("INSERT INTO themes (id, label, first_seen, last_seen)"
                 " VALUES ('t1','x','2026-11-01','2026-11-05')")
    # 6 articles across 3 sources on consecutive days -> window meets 5/3
    for i in range(6):
        _article(conn, f"a{i}", "e1" if i < 4 else "e2",
                 f"2026-11-0{1 + i % 2}", source=f"src{i % 3}.com")
    conn.execute("INSERT INTO themes (id, label, first_seen, last_seen)"
                 " VALUES ('t2','y','2026-11-01','2026-11-01')")
    conn.execute("INSERT INTO theme_events VALUES ('t2','e3')")
    _article(conn, "b1", "e3", "2026-11-01", source="lone.com")
    conn.commit()
    labels = label_outcomes(conn, TP)
    assert labels["t1"]["label"] == 1 and labels["t1"]["escalated_date"]
    assert labels["t2"]["label"] == 0


def test_promotion_gate(tmp_path):
    conn = _mk(tmp_path)
    feats = {"days_alive": 10}
    conn.execute(
        "INSERT INTO themes (id, label, first_seen, last_seen, status,"
        " n_events, n_companies, escalation_score, score_json)"
        " VALUES ('t1','x','2026-11-01','2026-11-18','emerging',5,3,72,?)",
        (json.dumps(feats),))
    conn.execute(
        "INSERT INTO themes (id, label, first_seen, last_seen, status,"
        " n_events, n_companies, escalation_score, score_json)"
        " VALUES ('t2','y','2026-11-17','2026-11-18','candidate',2,1,20,?)",
        (json.dumps({"days_alive": 1}),))
    conn.commit()
    cands = {c["theme"]["id"]: c for c in
             promotion_candidates(conn, TP["promotion"])}
    assert cands["t1"]["qualifies"] is True
    assert cands["t2"]["qualifies"] is False      # fails days_alive + score


# ---------------------------------------------------------------- scorecard

def test_auc_and_fit_on_separable_data():
    assert auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == 1.0
    assert auc([0, 0, 0], [0.1, 0.2, 0.3]) is None
    X = [[0.0] * len(FEATURE_ORDER)] * 6 + [[1.0] * len(FEATURE_ORDER)] * 6
    y = [0] * 6 + [1] * 6
    model = fit_logistic(X, y)
    # symmetric balanced data -> intercept near 0; check separation instead
    import math
    def p(row):
        z = model["intercept"] + sum(
            model["weights"][f] * (row[i] - model["means"][f]) / model["stds"][f]
            for i, f in enumerate(FEATURE_ORDER))
        return 1 / (1 + math.exp(-z))
    scores = [p(r) for r in X]
    assert all(w > 0 for w in model["weights"].values())
    assert auc(y, scores) == 1.0


def test_calibrate_insufficient_data_refuses(tmp_path):
    conn = _mk(tmp_path)
    for ev, au in (("evt-1", "a1"), ("evt-2", "a2")):
        _article(conn, au, ev, DAY, concepts=_shared(3))
        _orphan_incident(conn, ev)
    rebuild_themes(conn, DAY, TP_NOCUT)
    result = calibrate(conn, TP_NOCUT)
    assert result["status"] == "insufficient_data"
    assert result["n_themes"] == 1

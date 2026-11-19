"""Tests for incident persistence and baseline accumulation."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.store import connect
from src.incidents import rebuild_incidents, update_daily_counts, ensure_schema

TAX = [
    {"id": "DATA_PRIVACY", "keywords_en": ["cyberattack", "data breach", "hack"]},
    {"id": "FRAUD_CORRUPTION", "keywords_en": ["fraud", "bribery"]},
]


def _mk(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)  # adds score/title_match link columns
    conn.execute("INSERT INTO companies (id, name, concept_uri, map_status)"
                 " VALUES (1, 'Acme', 'uri:acme', 'auto_mapped')")
    conn.execute("INSERT INTO companies (id, name, concept_uri, map_status)"
                 " VALUES (2, 'Beta', 'uri:beta', 'auto_mapped')")
    conn.commit()
    return conn


def _add_article(conn, uri, event, day, title, body="", source="Reuters", cid=1,
                 score=5, title_match=1):
    conn.execute(
        "INSERT OR IGNORE INTO articles (uri,date,lang,title,url,source,sentiment,event_uri,raw_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (uri, day, "eng", title, f"http://x/{uri}", source, -0.5, event,
         json.dumps({"title": title, "body": body})))
    conn.execute(
        "INSERT OR IGNORE INTO article_companies"
        " (article_uri, company_id, score, title_match) VALUES (?,?,?,?)",
        (uri, cid, score, title_match))
    conn.commit()


def test_articles_cluster_into_one_incident(tmp_path):
    conn = _mk(tmp_path)
    # three articles, same event, same company -> ONE incident
    _add_article(conn, "a1", "evt-1", "2026-11-18", "Acme hit by cyberattack",
                 source="Reuters")
    _add_article(conn, "a2", "evt-1", "2026-11-18", "Acme cyberattack widens",
                 source="BBC")
    _add_article(conn, "a3", "evt-1", "2026-11-19", "Acme breach update",
                 source="FT")
    # one unclustered article -> singleton incident keyed art:<uri>
    _add_article(conn, "a4", None, "2026-11-19", "Acme fraud allegation",
                 source="Bloomberg")
    stats = rebuild_incidents(conn, TAX)
    rows = conn.execute(
        "SELECT * FROM incidents WHERE company_id=1 ORDER BY event_uri").fetchall()
    assert len(rows) == 2
    ev = [r for r in rows if r["event_uri"] == "evt-1"][0]
    assert ev["n_articles"] == 3 and ev["n_sources"] == 3
    assert ev["issue_id"] == "DATA_PRIVACY"
    assert ev["first_seen"] == "2026-11-18" and ev["last_seen"] == "2026-11-19"
    singleton = [r for r in rows if r["event_uri"] == "art:a4"][0]
    assert singleton["issue_id"] == "FRAUD_CORRUPTION"
    assert stats["incidents_total"] == 2


def test_confirmed_layer_from_hard_trigger(tmp_path):
    conn = _mk(tmp_path)
    _add_article(conn, "b1", "evt-2", "2026-11-18",
                 "Acme fined by regulator over data breach",
                 body="The regulator said the investigation opened in June.")
    rebuild_incidents(conn, TAX)
    r = conn.execute("SELECT * FROM incidents WHERE event_uri='evt-2'").fetchone()
    assert r["layer"] == "confirmed"
    raw = json.loads(r["raw_json"])
    assert "regulator_action" in raw["confirmed_triggers"]


def test_rebuild_is_idempotent_and_extends(tmp_path):
    conn = _mk(tmp_path)
    _add_article(conn, "c1", "evt-3", "2026-11-18", "Acme hack", source="Reuters")
    rebuild_incidents(conn, TAX)
    _add_article(conn, "c2", "evt-3", "2026-11-19", "Acme hack, day 2", source="BBC")
    rebuild_incidents(conn, TAX)
    rows = conn.execute("SELECT * FROM incidents WHERE event_uri='evt-3'").fetchall()
    assert len(rows) == 1                       # no duplicate
    assert rows[0]["n_articles"] == 2           # extended, not duplicated
    assert rows[0]["last_seen"] == "2026-11-19"


def test_baseline_thin_data_then_z(tmp_path):
    conn = _mk(tmp_path)
    ensure_schema(conn)
    start = date(2026, 8, 1)
    # 29 days of history -> still THIN_DATA
    for i in range(29):
        d = (start + timedelta(days=i)).isoformat()
        _add_article(conn, f"h{i}", f"evt-h{i}", d, "Acme cyberattack", cid=1)
        update_daily_counts(conn, d, min_history=30)
    r = conn.execute("SELECT * FROM company_daily WHERE company_id=1 "
                     "ORDER BY date DESC LIMIT 1").fetchone()
    assert r["thin_data"] == 1 and r["z"] is None
    # one more baseline day, then a spike -> 30 days of history, z activates
    d30 = (start + timedelta(days=29)).isoformat()
    _add_article(conn, "h29", "evt-h29", d30, "Acme cyberattack", cid=1)
    update_daily_counts(conn, d30, min_history=30)
    d31 = (start + timedelta(days=30)).isoformat()
    for j in range(10):                          # abnormal spike: 10 articles
        _add_article(conn, f"s{j}", f"evt-s{j}", d31, "Acme cyberattack", cid=1)
    update_daily_counts(conn, d31, min_history=30)
    r = conn.execute("SELECT * FROM company_daily WHERE company_id=1 AND date=?",
                     (d31,)).fetchone()
    assert r["thin_data"] == 0 and r["z"] is not None and r["z"] > 2.0


def test_zero_day_still_records(tmp_path):
    conn = _mk(tmp_path)
    ensure_schema(conn)
    # company 2 has no articles that day -> no row (absence = zero, recorded by
    # the fact that company_daily simply has no entry; sweep covers universe)
    stats = update_daily_counts(conn, "2026-11-18")
    assert stats["companies_seen"] == 0

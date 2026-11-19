"""Tests for the subject-salience filter between the recall layer
(article_companies: every link kept) and the precision layer (incidents,
daily counts, impact scores: subject links only).

Contract under test:
- backfill_link_scores fills score/title_match from stored raw_json (offline)
- score >= min_concept_score OR title match -> link enters precision layer
- NULL score fails closed (never imputed, never kept on salience)
- generic first tokens ('japan', 'national', ...) are not title-match aliases
- raising the threshold and rebuilding removes stale incidents (full rebuild)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.store import connect
from src.incidents import (backfill_link_scores, company_core_names,
                           ensure_schema, rebuild_incidents,
                           update_daily_counts)

DAY = "2026-11-18"
TESLA_URI = "http://en.wikipedia.org/wiki/Tesla,_Inc."
JT_URI = "http://en.wikipedia.org/wiki/Japan_Tobacco"
TAX = [{"id": "DATA_PRIVACY",
        "keywords_en": ["cyberattack", "data breach", "hack"]}]


def _mk(tmp_path):
    conn = connect(tmp_path / "t.db")
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO companies (id, name, concept_uri, map_status)"
        " VALUES (1, 'TESLA, INC.', ?, 'auto_mapped')", (TESLA_URI,))
    conn.execute(
        "INSERT INTO companies (id, name, concept_uri, map_status)"
        " VALUES (2, 'JAPAN TOBACCO INC.', ?, 'auto_mapped')", (JT_URI,))
    conn.commit()
    return conn


def _art(conn, uri, title, scores=None, day=DAY, event=None, cid=1,
         source="wire.com"):
    """One article linked to company `cid` as a LEGACY row (score/title_match
    NULL) — backfill must resolve them from raw_json. `scores` maps the
    company id to its ER concept score in the article payload."""
    concepts = []
    if scores is not None and cid in scores:
        uri_map = {1: TESLA_URI, 2: JT_URI}
        concepts.append({"uri": uri_map[cid], "score": scores[cid],
                         "type": "org"})
    conn.execute(
        "INSERT OR REPLACE INTO articles (uri, date, lang, title, url, "
        " source, raw_json, event_uri) VALUES (?,?,?,?,?,?,?,?)",
        (uri, day, "eng", title, f"http://x/{uri}", source,
         json.dumps({"concepts": concepts, "body": title}), event))
    conn.execute(
        "INSERT OR IGNORE INTO article_companies (article_uri, company_id)"
        " VALUES (?,?)", (uri, cid))
    conn.commit()


def test_core_names_suffix_strip_and_first_token():
    assert company_core_names("TESLA, INC.") == {"tesla"}
    # 'meta' is only 4 chars — below the distinctive-alias bar (5+ chars),
    # so no first-token alias (deliberate: 'meta' is also an English word)
    assert company_core_names("META PLATFORMS, INC.") == {"meta platforms"}
    assert company_core_names("Sony Group Corporation") == {"sony"}
    assert company_core_names("VOLKSWAGEN AG") == {"volkswagen"}
    # generic first tokens never become standalone aliases
    assert company_core_names("JAPAN TOBACCO INC.") == {"japan tobacco"}
    assert company_core_names("NATIONAL BANK OF CANADA") == {
        "national bank of canada"}


def test_backfill_fills_from_raw_json(tmp_path):
    conn = _mk(tmp_path)
    _art(conn, "a1", "Chip CEO scandal widens", scores={1: 2})
    n = backfill_link_scores(conn)
    assert n == 1
    row = conn.execute("SELECT score, title_match FROM article_companies"
                       " WHERE article_uri='a1'").fetchone()
    assert row["score"] == 2 and row["title_match"] == 0
    # idempotent: second run finds nothing pending
    assert backfill_link_scores(conn) == 0


def test_low_score_no_title_forms_no_incident(tmp_path):
    conn = _mk(tmp_path)
    _art(conn, "a1", "Chip CEO scandal widens", scores={1: 2})
    stats = rebuild_incidents(conn, TAX)
    assert stats["incidents_total"] == 0
    assert stats["links_total"] == 1 and stats["links_dropped"] == 1
    assert stats["min_concept_score"] == 4
    # raw link survives — recall layer untouched
    assert conn.execute("SELECT COUNT(*) FROM article_companies").fetchone()[0] == 1


def test_high_score_forms_incident(tmp_path):
    conn = _mk(tmp_path)
    _art(conn, "a1", "Cyberattack hits carmaker", scores={1: 5},
         event="evt-1")
    stats = rebuild_incidents(conn, TAX)
    assert stats["incidents_total"] == 1
    inc = conn.execute("SELECT * FROM incidents").fetchone()
    assert inc["issue_id"] == "DATA_PRIVACY"


def test_title_match_overrides_low_score(tmp_path):
    conn = _mk(tmp_path)
    # score 1 (passing concept mention) BUT company name in title = subject
    _art(conn, "a1", "Tesla hit by cyberattack", scores={1: 1})
    stats = rebuild_incidents(conn, TAX)
    assert stats["incidents_total"] == 1
    row = conn.execute("SELECT title_match FROM article_companies"
                       " WHERE article_uri='a1'").fetchone()
    assert row["title_match"] == 1


def test_generic_first_token_is_not_a_title_alias(tmp_path):
    conn = _mk(tmp_path)
    # 'Japan' in title must NOT title-match JAPAN TOBACCO INC.
    _art(conn, "a1", "Japan PM holds talks with trading partners",
         scores={2: 1}, cid=2)
    stats = rebuild_incidents(conn, TAX)
    assert stats["incidents_total"] == 0
    row = conn.execute("SELECT score, title_match FROM article_companies"
                       " WHERE article_uri='a1'").fetchone()
    assert row["score"] == 1 and row["title_match"] == 0


def test_null_score_fails_closed(tmp_path):
    conn = _mk(tmp_path)
    # link exists but the concept is absent from the stored payload:
    # salience unverifiable -> excluded from precision layer, never imputed
    _art(conn, "a1", "Tesla cyberattack", scores=None)
    stats = rebuild_incidents(conn, TAX)
    row = conn.execute("SELECT score, title_match FROM article_companies"
                       " WHERE article_uri='a1'").fetchone()
    assert row["score"] is None
    # title match still applies as an independent subject signal
    assert row["title_match"] == 1
    assert stats["incidents_total"] == 1
    # ...but without the title match it must fail closed:
    _art(conn, "a2", "Carmaker cyberattack widens", scores=None)
    stats = rebuild_incidents(conn, TAX)
    assert stats["incidents_total"] == 1  # only the title-matched one


def test_daily_counts_respect_filter(tmp_path):
    conn = _mk(tmp_path)
    _art(conn, "a1", "Tesla cyberattack", scores={1: 5}, source="s1")
    _art(conn, "a2", "Unrelated chip roundup", scores={1: 2}, source="s2")
    rebuild_incidents(conn, TAX)
    stats = update_daily_counts(conn, DAY)
    row = conn.execute("SELECT n_articles FROM company_daily"
                       " WHERE company_id=1 AND date=?", (DAY,)).fetchone()
    assert row["n_articles"] == 1
    assert stats["companies_seen"] == 1


def test_raising_threshold_removes_stale_incidents(tmp_path):
    conn = _mk(tmp_path)
    _art(conn, "a1", "Carmaker cyberattack", scores={1: 3}, event="evt-1")
    assert rebuild_incidents(conn, TAX, min_concept_score=3)["incidents_total"] == 1
    # full rebuild under a stricter threshold must not leave stale rows
    assert rebuild_incidents(conn, TAX, min_concept_score=4)["incidents_total"] == 0


def test_daily_counts_drop_stale_company_rows(tmp_path):
    """A company seen under a loose filter but with no subject links under
    the current one must lose its company_daily row for the day."""
    conn = _mk(tmp_path)
    _art(conn, "a1", "Tesla cyberattack", scores={1: 5})
    # stale row for company 2 (no links at all on DAY)
    conn.execute(
        "INSERT INTO company_daily (company_id, date, n_articles, n_sources,"
        " n_incidents, thin_data) VALUES (2, ?, 9, 9, 9, 1)", (DAY,))
    conn.commit()
    rebuild_incidents(conn, TAX)
    update_daily_counts(conn, DAY)
    gone = conn.execute(
        "SELECT COUNT(*) FROM company_daily WHERE company_id=2 AND date=?",
        (DAY,)).fetchone()[0]
    assert gone == 0
    kept = conn.execute(
        "SELECT n_articles FROM company_daily WHERE company_id=1 AND date=?",
        (DAY,)).fetchone()
    assert kept["n_articles"] == 1


def test_company_scores_drop_stale_rows(tmp_path):
    """rebuild_company_scores rebuilds the day fully: rows for companies no
    longer in company_daily must not survive from an earlier run."""
    from src.impact import rebuild_company_scores, ensure_schema as imp_schema
    from src.config import params
    conn = _mk(tmp_path)
    imp_schema(conn)
    ip = params()["impact"]
    conn.execute(
        "INSERT INTO company_scores (company_id, date, severity, outrage,"
        " thin) VALUES (2, ?, 50, 50, 1)", (DAY,))
    conn.commit()
    out = rebuild_company_scores(conn, DAY, ip, TAX, params()["themes"])
    assert out["scored"] == 0  # nobody in company_daily for DAY
    assert conn.execute(
        "SELECT COUNT(*) FROM company_scores WHERE date=?", (DAY,)
    ).fetchone()[0] == 0

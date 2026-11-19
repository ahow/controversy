"""Incident persistence + baseline accumulation.

Incidents: articles grouped into stories. The unit of decision for the state
machine is the INCIDENT (one company x one event cluster), not the article.
Grouping key is Event Registry's native event_uri (dedup: native_eventuri in
parameters.yaml); articles ER failed to cluster fall back to singleton
incidents keyed "art:<uri>" so nothing is silently dropped.

Baselines: per-company daily counts of controversy-tagged articles. The stream
is controversy-filtered, so baselines are of *controversy-relevant* coverage —
the right denominator for controversy salience. z-scores stay None (THIN_DATA)
until min_history days accumulate; silence before that is never treated as clean.

Both steps are idempotent and run at the end of every sweep.
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from collections import Counter

from .scoring import assign_issue, detect_confirmed_triggers

_EXTRA_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_incidents_company_event
    ON incidents (company_id, event_uri);
CREATE TABLE IF NOT EXISTS company_daily (
    company_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    n_articles INTEGER DEFAULT 0,
    n_sources INTEGER DEFAULT 0,
    n_incidents INTEGER DEFAULT 0,
    z REAL,                                   -- None until min_history reached
    thin_data INTEGER DEFAULT 1,              -- 1 while history < min_history
    PRIMARY KEY (company_id, date)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_EXTRA_DDL)
    # Link-quality columns (v0.4.0): ER concept salience score + title-match
    # flag per company-article link. Raw links are NEVER deleted (recall layer);
    # these columns let the precision layer filter by subject salience.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(article_companies)")}
    if "score" not in cols:
        conn.execute("ALTER TABLE article_companies ADD COLUMN score REAL")
    if "title_match" not in cols:
        conn.execute(
            "ALTER TABLE article_companies ADD COLUMN title_match INTEGER")
    conn.commit()


# --- subject-salience filter (precision layer) -------------------------------
#
# Evidence (2026-11-18 corpus, 6,004 links): ER concept score <= 2 with no
# title mention is ~100% bystander noise (passing mentions, market roundups);
# confirmed incidents whose best link is score 3 are ~98% noise; verified
# true-subject incidents (e.g. Deutsche Bank submarine leak, 42 articles)
# score 5 across the board. Default cut 4 + title-match override therefore
# keeps every verified real incident while removing ~3/4 of noise links.
# The threshold lives in parameters.yaml (attach.min_concept_score) and can be
# flexed without re-fetching: raw links + scores are preserved.

DEFAULT_MIN_CONCEPT_SCORE = 4

_LEGAL = re.compile(
    r"(?i)\b(inc|ltd|plc|corp|corporation|company|co|sa|ag|se|nv|asa|ab|spa|"
    r"srl|llc|lp|llp|limited|aktiengesellschaft|aktiebolag|holdings|holding|"
    r"group|berhad|oyj|pcl|pjsc|ojsc)\b")
# First tokens too generic to stand alone as a title-match alias (country
# names catch "Japan Tobacco" matching "Japan PM ..."; corporate generics
# catch "National ...", "Royal ..." etc.).
_GENERIC_FIRST = {
    "general", "united", "national", "first", "royal", "american", "china",
    "japan", "india", "korea", "taiwan", "brazil", "canada", "france",
    "germany", "spain", "italy", "bank", "group", "international", "global",
    "state", "standard", "new", "the", "de", "san", "british", "australian",
    "european",
}


def company_core_names(name: str) -> set[str]:
    """Title-match candidates for a company: legal-suffix-stripped core name,
    plus the first token when it is distinctive (>=5 chars, not generic).
    'META PLATFORMS, INC.' -> {'meta platforms', 'meta'};
    'JAPAN TOBACCO INC.' -> {'japan tobacco'}  ('japan' is generic)."""
    n = re.sub(r"[,\.]", " ", (name or "").lower().replace("&", " and "))
    toks = [t for t in n.split() if len(t) > 1 and not _LEGAL.fullmatch(t)]
    core = " ".join(toks).strip()
    cands = {core} if core else set()
    if len(toks) >= 2 and len(toks[0]) >= 5 and toks[0] not in _GENERIC_FIRST:
        cands.add(toks[0])
    return cands


def backfill_link_scores(conn: sqlite3.Connection) -> int:
    """Fill score/title_match on article_companies rows lacking them, from the
    stored article raw_json (offline, idempotent, zero API tokens). Rows whose
    concept is absent from the stored payload keep score=NULL and are treated
    as unverified — the precision-layer filter fails closed on NULLs."""
    ensure_schema(conn)
    pending = conn.execute(
        "SELECT article_uri, company_id FROM article_companies "
        "WHERE score IS NULL AND title_match IS NULL").fetchall()
    if not pending:
        return 0
    comps = {r["id"]: (r["concept_uri"], company_core_names(r["name"]))
             for r in conn.execute(
                 "SELECT id, name, concept_uri FROM companies")}
    n = 0
    for p in pending:
        a = conn.execute("SELECT raw_json, title FROM articles WHERE uri=?",
                         (p["article_uri"],)).fetchone()
        score, tm = None, 0
        cu, cands = comps.get(p["company_id"], (None, set()))
        if a:
            try:
                raw = json.loads(a["raw_json"] or "{}")
            except Exception:
                raw = {}
            hit = next((x for x in (raw.get("concepts") or [])
                        if x.get("uri") == cu), None)
            if hit is not None:
                score = hit.get("score")
            title = (a["title"] or "").lower()
            tm = 1 if any(cn and cn in title for cn in cands) else 0
        conn.execute(
            "UPDATE article_companies SET score=?, title_match=? "
            "WHERE article_uri=? AND company_id=?",
            (score, tm, p["article_uri"], p["company_id"]))
        n += 1
    conn.commit()
    return n


def _subject_filter_sql(min_concept_score: float) -> tuple[str, tuple]:
    """Fail-closed subject filter: NULL score never passes on salience."""
    return ("(COALESCE(ac.score, 0) >= ? OR COALESCE(ac.title_match, 0) = 1)",
            (min_concept_score,))


def _issue_for_incident(texts: list[str], taxonomy: list[dict]) -> str | None:
    """Majority-vote issue across an incident's articles; ties broken by first
    taxonomy order (deterministic). Unmatched -> None (ORPHAN feed for the
    living taxonomy)."""
    votes = Counter()
    for t in texts:
        issue = assign_issue(t, taxonomy)
        if issue:
            votes[issue] += 1
    if not votes:
        return None
    return sorted(votes.items(), key=lambda kv: -kv[1])[0][0]


def rebuild_incidents(conn: sqlite3.Connection, taxonomy: list[dict],
                      min_concept_score: float = DEFAULT_MIN_CONCEPT_SCORE
                      ) -> dict:
    """(Re)build the incidents table from article_companies x articles.

    Full rebuild (DELETE + re-derive): incidents are a derived table, and a
    full rebuild keeps the result identical under changed filters — stale
    rows from an older, looser filter can never linger.

    Only subject links enter: a link qualifies when the company's ER concept
    salience score >= min_concept_score OR the company's name appears in the
    article title. Low-salience links remain in article_companies (recall
    layer) but form no incidents here.
    """
    ensure_schema(conn)
    backfill_link_scores(conn)
    where, args = _subject_filter_sql(min_concept_score)
    conn.execute("DELETE FROM incidents")
    rows = conn.execute(
        f"""
        SELECT ac.company_id AS cid,
               COALESCE(a.event_uri, 'art:' || a.uri) AS ev,
               a.uri AS uri, a.date AS date, a.title AS title,
               a.source AS source, a.raw_json AS raw_json
        FROM article_companies ac
        JOIN articles a ON a.uri = ac.article_uri
        WHERE {where}
        """, args
    ).fetchall()

    groups: dict[tuple[int, str], list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault((r["cid"], r["ev"]), []).append(r)

    for (cid, ev), arts in groups.items():
        texts = []
        triggers: set[str] = set()
        titles = []
        for a in arts:
            body = ""
            if a["raw_json"]:
                try:
                    body = (json.loads(a["raw_json"]).get("body") or "")[:1500]
                except Exception:
                    pass
            texts.append(f"{a['title'] or ''} {body}")
            triggers.update(detect_confirmed_triggers(a["title"] or "", body))
            if a["title"]:
                titles.append(a["title"])
        issue = _issue_for_incident(texts, taxonomy)
        layer = "confirmed" if triggers else "indicative"
        dates = [a["date"] for a in arts if a["date"]]
        first_seen, last_seen = (min(dates), max(dates)) if dates else (None, None)
        n_sources = len({a["source"] for a in arts if a["source"]})
        raw = json.dumps({
            "article_uris": [a["uri"] for a in arts][:50],
            "sample_titles": titles[:5],
            "confirmed_triggers": sorted(triggers),
        })
        conn.execute(
            """
            INSERT INTO incidents (company_id, event_uri, first_seen, last_seen,
                                   issue_id, layer, n_articles, n_sources, raw_json)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(company_id, event_uri) DO UPDATE SET
                last_seen=excluded.last_seen,
                n_articles=excluded.n_articles,
                n_sources=excluded.n_sources,
                issue_id=excluded.issue_id,
                layer=excluded.layer,
                raw_json=excluded.raw_json
            """,
            (cid, ev, first_seen, last_seen, issue, layer,
             len(arts), n_sources, raw),
        )
    conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
    confirmed = conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE layer='confirmed'").fetchone()[0]
    orphan = conn.execute(
        "SELECT COUNT(*) FROM incidents WHERE issue_id IS NULL").fetchone()[0]
    links_total = conn.execute(
        "SELECT COUNT(*) FROM article_companies").fetchone()[0]
    links_dropped = conn.execute(
        "SELECT COUNT(*) FROM article_companies ac WHERE NOT "
        + _subject_filter_sql(min_concept_score)[0],
        (min_concept_score,)).fetchone()[0]
    return {"incidents_total": total, "confirmed": confirmed,
            "indicative": total - confirmed, "orphan": orphan,
            "links_total": links_total, "links_dropped": links_dropped,
            "min_concept_score": min_concept_score}


def update_daily_counts(conn: sqlite3.Connection, day: str,
                        min_history: int = 30, window: int = 90,
                        min_concept_score: float = DEFAULT_MIN_CONCEPT_SCORE
                        ) -> dict:
    """Upsert per-company counts for `day`, then compute z vs the trailing
    `window` days of history (excluding `day` itself).

    Counts use subject links only (same filter as rebuild_incidents): a
    passing mention in someone else's story is not attention to this company.

    z uses median/MAD (robust to the skewed, zero-heavy distribution of
    controversy coverage). thin_data=1 while history < min_history: callers
    must never treat a thin baseline as a clean company.
    """
    ensure_schema(conn)
    backfill_link_scores(conn)
    where, args = _subject_filter_sql(min_concept_score)
    counts = conn.execute(
        f"""
        SELECT ac.company_id AS cid,
               COUNT(DISTINCT a.uri) AS n_art,
               COUNT(DISTINCT a.source) AS n_src
        FROM article_companies ac
        JOIN articles a ON a.uri = ac.article_uri
        WHERE a.date = ? AND {where}
        GROUP BY ac.company_id
        """,
        (day,) + args,
    ).fetchall()

    # Full rebuild for the day: rows for companies with no subject links left
    # must not linger from an earlier, looser-filter run.
    conn.execute("DELETE FROM company_daily WHERE date=?", (day,))

    for c in counts:
        cid = c["cid"]
        n_inc = conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE company_id=? AND first_seen<=? AND last_seen>=?",
            (cid, day, day)).fetchone()[0]
        hist = [r[0] for r in conn.execute(
            "SELECT n_articles FROM company_daily WHERE company_id=? AND date<? "
            "ORDER BY date DESC LIMIT ?", (cid, day, window)).fetchall()]
        z, thin = None, 1
        if len(hist) >= min_history:
            med = statistics.median(hist)
            mad = statistics.median([abs(x - med) for x in hist]) or 1.0
            z = (c["n_art"] - med) / (1.4826 * mad)
            thin = 0
        conn.execute(
            """
            INSERT INTO company_daily (company_id, date, n_articles, n_sources,
                                       n_incidents, z, thin_data)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(company_id, date) DO UPDATE SET
                n_articles=excluded.n_articles, n_sources=excluded.n_sources,
                n_incidents=excluded.n_incidents, z=excluded.z,
                thin_data=excluded.thin_data
            """,
            (cid, day, c["n_art"], c["n_src"], n_inc, z, thin))
    conn.commit()
    thin = conn.execute(
        "SELECT COUNT(*) FROM company_daily WHERE date=? AND thin_data=1",
        (day,)).fetchone()[0]
    return {"day": day, "companies_seen": len(counts), "thin_data": thin}

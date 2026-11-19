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
    conn.commit()


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


def rebuild_incidents(conn: sqlite3.Connection, taxonomy: list[dict]) -> dict:
    """(Re)build the incidents table from article_companies x articles.

    Upsert per (company_id, event_uri): safe to run after every sweep — new
    articles extend an existing incident's last_seen / counts rather than
    duplicating it.
    """
    ensure_schema(conn)
    rows = conn.execute(
        """
        SELECT ac.company_id AS cid,
               COALESCE(a.event_uri, 'art:' || a.uri) AS ev,
               a.uri AS uri, a.date AS date, a.title AS title,
               a.source AS source, a.raw_json AS raw_json
        FROM article_companies ac
        JOIN articles a ON a.uri = ac.article_uri
        """
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
    return {"incidents_total": total, "confirmed": confirmed,
            "indicative": total - confirmed, "orphan": orphan}


def update_daily_counts(conn: sqlite3.Connection, day: str,
                        min_history: int = 30, window: int = 90) -> dict:
    """Upsert per-company counts for `day`, then compute z vs the trailing
    `window` days of history (excluding `day` itself).

    z uses median/MAD (robust to the skewed, zero-heavy distribution of
    controversy coverage). thin_data=1 while history < min_history: callers
    must never treat a thin baseline as a clean company.
    """
    ensure_schema(conn)
    counts = conn.execute(
        """
        SELECT ac.company_id AS cid,
               COUNT(DISTINCT a.uri) AS n_art,
               COUNT(DISTINCT a.source) AS n_src
        FROM article_companies ac
        JOIN articles a ON a.uri = ac.article_uri
        WHERE a.date = ?
        GROUP BY ac.company_id
        """,
        (day,),
    ).fetchall()

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

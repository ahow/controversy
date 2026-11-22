"""SQLite persistence. Schema is the audit contract: every state transition
carries evidence, score decomposition and the parameter version that drove it.

SCHEMA below is the single source of truth and must match the production DB
byte-for-byte in column coverage. _migrate() upgrades older databases in
place (idempotent ALTER TABLE ADD COLUMN) so cold CI starts and long-lived
local DBs stay at parity.
"""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    ticker TEXT, isin TEXT, lei TEXT, country TEXT, sector TEXT,
    concept_uri TEXT UNIQUE,
    map_status TEXT NOT NULL DEFAULT 'pending',  -- pending|auto_mapped|needs_review|unmapped
    candidates_json TEXT, market_cap REAL
);
CREATE TABLE IF NOT EXISTS articles (
    uri TEXT PRIMARY KEY,
    date TEXT, lang TEXT, title TEXT, url TEXT, source TEXT,
    sentiment REAL, event_uri TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS article_companies (
    article_uri TEXT, company_id INTEGER, score REAL, title_match INTEGER,
    PRIMARY KEY (article_uri, company_id)
);
CREATE TABLE IF NOT EXISTS incidents (          -- one row per event_uri x company
    id INTEGER PRIMARY KEY,
    company_id INTEGER, event_uri TEXT, first_seen TEXT, last_seen TEXT,
    issue_id TEXT, layer TEXT,                  -- confirmed|indicative
    n_articles INTEGER DEFAULT 0, n_sources INTEGER DEFAULT 0,
    salience_z REAL, severity_tier TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS states (             -- current state per company
    company_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'NORMAL',       -- NORMAL|WATCH|FLAGGED|EXCLUDED|PROBATION
    entered TEXT, severity_tier TEXT,
    exclusion_date TEXT, probation_until TEXT,
    response_grade TEXT, response_assessed TEXT
);
CREATE TABLE IF NOT EXISTS transitions (        -- full audit log
    id INTEGER PRIMARY KEY,
    company_id INTEGER, ts TEXT,
    from_state TEXT, to_state TEXT,
    trigger TEXT,                               -- rule name or OVERRIDE
    evidence_json TEXT,                         -- incident ids, score decomposition, rationale
    param_version TEXT
);
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
CREATE TABLE IF NOT EXISTS company_scores (
    company_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    severity REAL,              -- 0-100
    outrage REAL,               -- 0-100
    materiality_bps REAL,       -- expected CAR bps; NULL while uncalibrated
    materiality REAL,           -- 0-100 magnitude rank; NULL while uncalibrated
    thin INTEGER DEFAULT 1,     -- baseline evidence thin (z excluded, not imputed)
    scores_json TEXT,           -- full decomposition of all three scores
    PRIMARY KEY (company_id, date)
);
CREATE TABLE IF NOT EXISTS event_signatures (   -- computed once per orphan event
    event_uri TEXT PRIMARY KEY,
    signature_json TEXT NOT NULL,      -- {concept_uri: weight}
    labels_json TEXT NOT NULL,         -- {concept_uri: label}
    first_seen TEXT
);
CREATE TABLE IF NOT EXISTS themes (
    id TEXT PRIMARY KEY,               -- th:<first_seen>:<slug>
    label TEXT,
    first_seen TEXT, last_seen TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',  -- candidate|emerging|escalated|dead|promoted
    n_events INTEGER DEFAULT 0,
    n_companies INTEGER DEFAULT 0,
    escalation_score REAL,             -- latest score, 0-100
    score_json TEXT,                   -- full feature decomposition (audit)
    thin INTEGER DEFAULT 1,            -- 1 while history < thin_days
    promoted_issue_id TEXT
);
CREATE TABLE IF NOT EXISTS theme_events (
    theme_id TEXT, event_uri TEXT,
    PRIMARY KEY (theme_id, event_uri)
);
CREATE TABLE IF NOT EXISTS theme_daily (
    theme_id TEXT, date TEXT,
    n_articles INTEGER DEFAULT 0,
    n_sources INTEGER DEFAULT 0,
    n_langs INTEGER DEFAULT 0,
    n_companies INTEGER DEFAULT 0,
    new_events INTEGER DEFAULT 0,
    PRIMARY KEY (theme_id, date)
);
"""

# Columns added after the v0.1 schema, for in-place upgrade of older DBs.
# (table, column, DDL fragment) — applied only when the column is absent.
_MIGRATIONS = [
    ("companies", "candidates_json", "candidates_json TEXT"),
    ("companies", "market_cap", "market_cap REAL"),
    ("article_companies", "score", "score REAL"),
    ("article_companies", "title_match", "title_match INTEGER"),
]


def connect(path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent column upgrades for databases created before the schema
    was consolidated. New whole tables are covered by IF NOT EXISTS above."""
    for table, column, ddl in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    conn.commit()


def log_transition(conn, company_id, from_state, to_state, trigger, evidence: dict, param_version):
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO transitions (company_id, ts, from_state, to_state, trigger, evidence_json, param_version)"
        " VALUES (?,?,?,?,?,?,?)",
        (company_id, datetime.now(timezone.utc).isoformat(), from_state, to_state,
         trigger, json.dumps(evidence), param_version))
    conn.commit()

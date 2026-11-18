"""SQLite persistence. Schema is the audit contract: every state transition
carries evidence, score decomposition and the parameter version that drove it.
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
    map_status TEXT NOT NULL DEFAULT 'pending'  -- pending|auto_mapped|needs_review|unmapped
);
CREATE TABLE IF NOT EXISTS articles (
    uri TEXT PRIMARY KEY,
    date TEXT, lang TEXT, title TEXT, url TEXT, source TEXT,
    sentiment REAL, event_uri TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS article_companies (
    article_uri TEXT, company_id INTEGER,
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
"""


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def log_transition(conn, company_id, from_state, to_state, trigger, evidence: dict, param_version):
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO transitions (company_id, ts, from_state, to_state, trigger, evidence_json, param_version)"
        " VALUES (?,?,?,?,?,?,?)",
        (company_id, datetime.now(timezone.utc).isoformat(), from_state, to_state,
         trigger, json.dumps(evidence), param_version))
    conn.commit()

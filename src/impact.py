"""Company-level impact scoring: three scores, NEVER summed.

Each score answers a different question, for a different decision:

- SEVERITY (sev): how bad is the underlying conduct — the substance of the
  issue, size-adjusted. Built from hard evidence (confirmed-trigger hierarchy,
  issue prior severity, recurrence, institutional actors) and, where coverage
  must enter, only size-adjusted coverage (z vs the company's OWN baseline).
  Raw volume is never a severity input: Meta-scale coverage of a minor story
  must not read as severe. Feeds exclusion/probation policy (human-signed).

- OUTRAGE (out): how visible and how morally charged is the story for a
  sustainable-fund audience — the "investors are outraged we hold this" risk.
  Deliberately built on RAW reach (articles, sources, tier-1 outlets,
  languages, attached emergent-theme escalation) times moral charge (per-issue
  priors; a credible NGO campaign forces a high floor). The large-company
  skew is legitimate here: outrage risk IS partly a function of visibility.
  Feeds review priority and client-comms risk only.

- MATERIALITY (mat): expected share-price response = expected CAR bps for the
  issue x severity tier (from the event-study calibration table) x escalation
  probability proxy. UNCALIBRATED until scripts/replay_themes.py +
  src/event_study.py produce labelled price responses; while the table is
  empty the score is None (never zero-filled) and marked "uncalibrated".

Audit contract: every score ships its full feature decomposition in
company_scores.scores_json; weights live in parameters.yaml impact.* as
PRIORS until calibrated by evidence; the three scores are stored in separate
columns and are never combined into a composite.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, timedelta

from .incidents import DEFAULT_MIN_CONCEPT_SCORE

_DDL = """
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
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    # market_cap is populated by the FMP price/profile ingestion (step 2);
    # until then the size adjustment is the per-company baseline z-score.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if "market_cap" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN market_cap REAL")
    conn.commit()


def _sigmoid_score(z: float) -> float:
    return round(100.0 / (1.0 + math.exp(-z)), 1)


def _term(feature: str, value: float | None, weight: float) -> dict:
    contribution = 0.0 if value is None else round(weight * value, 4)
    return {"feature": feature,
            "value": None if value is None else round(value, 4),
            "weight": weight, "contribution": contribution}


# ------------------------------------------------------------------ features

def _active_incidents(conn, cid: int, day: str, window_days: int) -> list:
    """Incidents active as of `day`: seen within the trailing window."""
    start = (date.fromisoformat(day) - timedelta(days=window_days - 1)).isoformat()
    return conn.execute(
        "SELECT * FROM incidents WHERE company_id=? AND first_seen<=? "
        "AND last_seen>=?", (cid, day, start)).fetchall()


def _day_articles(conn, cid: int, day: str,
                  min_score: float = DEFAULT_MIN_CONCEPT_SCORE) -> list:
    """Subject articles only: a passing mention in someone else's story is
    not media attention to THIS company (same filter as incident build)."""
    return conn.execute(
        "SELECT a.title, a.lang, a.source, a.raw_json FROM articles a "
        "JOIN article_companies ac ON ac.article_uri = a.uri "
        "WHERE ac.company_id=? AND a.date=? AND "
        "(COALESCE(ac.score,0) >= ? OR COALESCE(ac.title_match,0) = 1)",
        (cid, day, min_score)).fetchall()


def _is_tier1(domain: str, tier1: list[str]) -> bool:
    d = (domain or "").lower().removeprefix("www.")
    return any(d == t or d.endswith("." + t) or t.split(".")[0] in d
               for t in tier1 if t)


def _domains(arts: list) -> list[str]:
    out = []
    for a in arts:
        dom = None
        if a["raw_json"]:
            try:
                dom = (json.loads(a["raw_json"]).get("source") or {}).get("uri")
            except Exception:
                dom = None
        out.append((dom or a["source"] or "").lower())
    return out


def company_features(conn, cid: int, day: str, ip: dict,
                     sev_prior: dict, themes_tp: dict,
                     min_score: float = DEFAULT_MIN_CONCEPT_SCORE) -> dict:
    """All raw inputs for the three scores, computed as of `day`."""
    win = ip["active_window_days"]
    inc = _active_incidents(conn, cid, day, win)
    arts = _day_articles(conn, cid, day, min_score)

    # confirmed-trigger strength (hierarchy from parameters) + issue priors
    trig_map = ip["severity"]["trigger_strength"]
    triggers = set()
    for i in inc:
        if i["layer"] != "confirmed":
            continue
        try:
            triggers.update(json.loads(i["raw_json"] or "{}")
                            .get("confirmed_triggers") or [])
        except Exception:
            pass
    trig_strength = max([trig_map.get(t, 0.0) for t in triggers], default=0.0)
    conf_issues = [i["issue_id"] for i in inc
                   if i["layer"] == "confirmed" and i["issue_id"]]
    issue_sev = max([sev_prior.get(iu, 0) / 4.0 for iu in conf_issues],
                    default=0.0)

    # recurrence: prior incidents of the SAME issue before the active window
    start = (date.fromisoformat(day) - timedelta(days=win - 1)).isoformat()
    recurrence = 0
    for iu in {i["issue_id"] for i in inc if i["issue_id"]}:
        recurrence += conn.execute(
            "SELECT COUNT(*) FROM incidents WHERE company_id=? AND issue_id=? "
            "AND first_seen<?", (cid, iu, start)).fetchone()[0]
    recurrence = min(recurrence, 3) / 3.0

    # size-adjusted salience: z vs own baseline; THIN -> excluded, not imputed
    row = conn.execute(
        "SELECT z, thin_data, n_articles, n_sources FROM company_daily "
        "WHERE company_id=? AND date=?", (cid, day)).fetchone()
    z = row["z"] if row else None
    thin = bool(row["thin_data"]) if row else True
    z_excess = None if (thin or z is None) else min(max(z, 0.0),
                                                    ip["z_cap"]) / ip["z_cap"]

    # institutional actors in today's coverage (titles)
    pats = [p.lower() for p in themes_tp.get("actor_patterns", [])]
    text = " ".join(a["title"] or "" for a in arts).lower()
    n_actor_hits = len({p for p in pats if p in text})
    actors = min(n_actor_hits, ip["outrage"]["caps"]["actors"]) \
        / ip["outrage"]["caps"]["actors"]

    # raw reach (outrage): NOT size-adjusted, by design
    n_articles = row["n_articles"] if row else len(arts)
    n_sources = row["n_sources"] if row else len(
        {a["source"] for a in arts if a["source"]})
    doms = _domains(arts)
    n_tier1 = len({d for d in doms if _is_tier1(d, themes_tp["authority_tier1"])})
    n_langs = len({a["lang"] for a in arts if a["lang"]})

    # emergent-theme attachment: strongest live theme touching this company
    theme_score = conn.execute(
        "SELECT MAX(t.escalation_score) FROM themes t "
        "JOIN theme_events te ON te.theme_id = t.id "
        "JOIN incidents i ON i.event_uri = te.event_uri "
        "WHERE i.company_id=? AND t.status NOT IN ('dead')",
        (cid,)).fetchone()[0] or 0.0

    # moral charge: worst active issue; NGO campaign forces the floor
    charge_map = ip["outrage"]["charge"]
    charge = max([charge_map.get(iu, charge_map["_default"])
                  for iu in {i["issue_id"] for i in inc}] or [0.0])
    if "credible_ngo_campaign" in triggers:
        charge = max(charge, ip["outrage"]["ngo_charge_floor"])

    return {
        "thin": thin, "n_active_incidents": len(inc),
        "triggers": sorted(triggers),
        "sev": {"trigger_strength": trig_strength, "issue_sev": issue_sev,
                "recurrence": recurrence, "z_excess": z_excess,
                "actors": actors},
        "out": {"n_articles": n_articles, "n_sources": n_sources,
                "n_tier1": n_tier1, "n_langs": n_langs,
                "theme_boost": (theme_score or 0.0) / 100.0,
                "charge": charge},
        "issues": sorted({i["issue_id"] for i in inc if i["issue_id"]}),
    }


# ------------------------------------------------------------------- scoring

def score_company(feats: dict, ip: dict) -> dict:
    """Three scores from features; full decomposition returned for audit."""
    sp, op = ip["severity"], ip["outrage"]
    caps = op["caps"]

    s = feats["sev"]
    sev_terms = [
        _term("trigger_strength", s["trigger_strength"], sp["w_trigger"]),
        _term("issue_severity", s["issue_sev"], sp["w_issue"]),
        _term("recurrence", s["recurrence"], sp["w_recurrence"]),
        _term("z_excess", s["z_excess"], sp["w_z"]),   # None when THIN
        _term("actors", s["actors"], sp["w_actors"]),
    ]
    sev = _sigmoid_score(sp["intercept"]
                         + sum(t["contribution"] for t in sev_terms))

    o = feats["out"]
    out_terms = [
        _term("articles", math.log1p(o["n_articles"]) / math.log1p(caps["articles"]),
              op["w_articles"]),
        _term("sources", min(o["n_sources"], caps["sources"]) / caps["sources"],
              op["w_sources"]),
        _term("tier1", min(o["n_tier1"], caps["tier1"]) / caps["tier1"],
              op["w_tier1"]),
        _term("langs", min(o["n_langs"], caps["langs"]) / caps["langs"],
              op["w_langs"]),
        _term("theme_boost", o["theme_boost"], op["w_theme"]),
        _term("charge", o["charge"], op["w_charge"]),
    ]
    out = _sigmoid_score(op["intercept"]
                         + sum(t["contribution"] for t in out_terms))

    # materiality: expected CAR x escalation probability; None until the
    # event-study calibration table has entries (never zero-filled).
    mp = ip["materiality"]
    table = mp.get("expected_car_bps") or {}
    mat_bps = mat_score = None
    p_esc = round(min(max(feats["out"]["theme_boost"],
                          min(feats["out"]["n_sources"]
                              / mp["p_escalation_cap_sources"], 1.0)), 1.0), 4)
    if table and feats["issues"]:
        car = max((table.get(f"{iu}|{t}", table.get(f"{iu}|*"))
                   for iu in feats["issues"] for t in ("S4", "S3", "S2", "S1")),
                  default=None, key=lambda v: abs(v) if v is not None else -1)
        if car is not None:
            mat_bps = round(car * p_esc, 1)
            mat_score = round(min(abs(mat_bps) / mp["cap_bps"], 1.0) * 100, 1)
    mat_note = ("uncalibrated" if not table else
                "no_table_entry" if mat_bps is None else "ok")

    return {
        "severity": sev, "outrage": out,
        "materiality_bps": mat_bps, "materiality": mat_score,
        "thin": feats["thin"],
        "decomposition": {
            "severity": {"intercept": sp["intercept"], "terms": sev_terms},
            "outrage": {"intercept": op["intercept"], "terms": out_terms},
            "materiality": {"status": mat_note, "p_escalation": p_esc,
                            "issues": feats["issues"]},
        },
    }


# ------------------------------------------------------------------- roll-up

def rebuild_company_scores(conn: sqlite3.Connection, day: str, ip: dict,
                           taxonomy: list[dict], themes_tp: dict,
                           min_score: float = DEFAULT_MIN_CONCEPT_SCORE
                           ) -> dict:
    """Compute and upsert the three scores for every company seen on `day`.
    Idempotent; zero API tokens (reads articles/incidents/company_daily/themes).
    """
    ensure_schema(conn)
    sev_prior = {i["id"]: i.get("prior_severity", 2) for i in taxonomy}
    # Full rebuild for the day: scores for companies no longer seen must not
    # linger from an earlier, looser-filter run.
    conn.execute("DELETE FROM company_scores WHERE date=?", (day,))
    cids = [r[0] for r in conn.execute(
        "SELECT company_id FROM company_daily WHERE date=?", (day,)).fetchall()]
    n_scored = n_mat = 0
    for cid in cids:
        feats = company_features(conn, cid, day, ip, sev_prior, themes_tp,
                                 min_score)
        sc = score_company(feats, ip)
        conn.execute(
            """INSERT INTO company_scores (company_id, date, severity, outrage,
                     materiality_bps, materiality, thin, scores_json)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(company_id, date) DO UPDATE SET
                 severity=excluded.severity, outrage=excluded.outrage,
                 materiality_bps=excluded.materiality_bps,
                 materiality=excluded.materiality, thin=excluded.thin,
                 scores_json=excluded.scores_json""",
            (cid, day, sc["severity"], sc["outrage"], sc["materiality_bps"],
             sc["materiality"], 1 if sc["thin"] else 0,
             json.dumps({**feats, **sc["decomposition"]})))
        n_scored += 1
        n_mat += sc["materiality"] is not None
    conn.commit()
    return {"day": day, "scored": n_scored, "materiality_calibrated": n_mat}

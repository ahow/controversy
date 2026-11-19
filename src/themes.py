"""Emergent-theme layer (Channel B): lifecycle tracking of orphan event clusters.

Themes are NOT pre-defined. They emerge from the orphan feed (incidents with
issue_id NULL) by chaining event clusters that share Event Registry concepts
across days. A theme therefore has its own daily time series (theme_daily),
which is what makes escalation observable: acceleration, spread, authority
migration, actor engagement, attachment and persistence are all computed from
the theme's own history rather than from static rules.

Layer discipline (audit contract):
- Escalation scores are INDICATIVE-only. They rank attention; they never drive
  the state machine or exclusion decisions.
- THIN evidence is flagged, never smoothed: a theme with < thin_days of history
  reports thin=1 and its score must be read as low-confidence, not as clean.
- Weights live in parameters.yaml (themes.weights) as PRIORS until the replay
  calibration (src/calibration.py) produces an evidence-fitted replacement.

Idempotent: rebuild_themes runs after every sweep; existing themes/events are
never duplicated, new events are attached to live themes or seeded new ones.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from datetime import date, timedelta

_THEME_DDL = """
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
CREATE TABLE IF NOT EXISTS event_signatures (   -- computed once per orphan event
    event_uri TEXT PRIMARY KEY,
    signature_json TEXT NOT NULL,      -- {concept_uri: weight}
    labels_json TEXT NOT NULL,         -- {concept_uri: label}
    first_seen TEXT
);
"""

# Concept types eligible for theme signatures. Universe companies are excluded
# separately (they are attachment, not theme content).
_SIG_TYPES = {"wiki", "person", "loc", "org"}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_THEME_DDL)
    conn.commit()


# ---------------------------------------------------------------- signatures

def event_signature(conn: sqlite3.Connection, event_uri: str,
                    company_uris: set[str], top_k: int = 8,
                    min_score: int = 3) -> tuple[dict, dict]:
    """Weighted concept signature for one event cluster, from member articles'
    raw_json. Weight = max ER concept score across articles (an article citing
    a concept weakly should not outvote one centred on it). Company concepts
    are stripped: they record WHO is attached, not WHAT the theme is about.
    """
    sig: dict[str, float] = {}
    labels: dict[str, str] = {}
    rows = conn.execute(
        "SELECT raw_json FROM articles WHERE event_uri=?", (event_uri,)).fetchall()
    for r in rows:
        if not r["raw_json"]:
            continue
        try:
            a = json.loads(r["raw_json"])
        except Exception:
            continue
        for c in (a.get("concepts") or []):
            uri, ctype = c.get("uri"), c.get("type")
            score = c.get("score") or 0
            if (not uri or ctype not in _SIG_TYPES or score < min_score
                    or uri in company_uris):
                continue
            if score > sig.get(uri, 0):
                sig[uri] = float(score)
            lab = c.get("label")
            if isinstance(lab, dict):
                labels[uri] = lab.get("eng") or next(iter(lab.values()), uri)
            elif lab:
                labels[uri] = str(lab)
    top = sorted(sig.items(), key=lambda kv: -kv[1])[:top_k]
    keep = dict(top)
    return keep, {u: labels.get(u, u) for u in keep}


def _get_signature(conn, event_uri, company_uris, tp) -> dict:
    """Signature cache: computed once, reused on every later run."""
    row = conn.execute(
        "SELECT signature_json FROM event_signatures WHERE event_uri=?",
        (event_uri,)).fetchone()
    if row:
        return json.loads(row["signature_json"])
    sig, labels = event_signature(conn, event_uri, company_uris,
                                  tp["top_k_concepts"], tp["min_concept_score"])
    first = conn.execute(
        "SELECT MIN(date) FROM articles WHERE event_uri=?",
        (event_uri,)).fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO event_signatures"
        " (event_uri, signature_json, labels_json, first_seen) VALUES (?,?,?,?)",
        (event_uri, json.dumps(sig), json.dumps(labels), first))
    return sig


# ------------------------------------------------------------------ scoring

def _norm(x: float, cap: float) -> float:
    return min(max(x, 0.0), cap) / cap if cap else 0.0


def _source_domains(conn, event_uris: list[str], day: str | None = None) -> list[str]:
    """Source domains for articles in the given events (raw_json source.uri
    preferred, source column as fallback)."""
    out = []
    marks = ",".join("?" * len(event_uris))
    sql = f"SELECT source, raw_json FROM articles WHERE event_uri IN ({marks})"
    args: list = list(event_uris)
    if day:
        sql += " AND date=?"
        args.append(day)
    for r in conn.execute(sql, args).fetchall():
        dom = None
        if r["raw_json"]:
            try:
                dom = (json.loads(r["raw_json"]).get("source") or {}).get("uri")
            except Exception:
                dom = None
        out.append((dom or r["source"] or "").lower())
    return out


def _is_tier1(domain: str, tier1: list[str]) -> bool:
    d = domain.removeprefix("www.")
    return any(d == t or d.endswith("." + t) or t.split(".")[0] in d
               for t in tier1 if t)


def escalation_features(conn: sqlite3.Connection, theme_id: str, day: str,
                        tp: dict) -> dict:
    """Named, auditable features for the scorecard, computed as of `day`
    (cumulative counts use data <= day; 'today' series use date == day).

    Returns thin=True when the theme has no prior-day history — the score is
    still computed for ranking continuity but must be displayed as THIN.
    """
    caps = tp["features"]
    evs = [r[0] for r in conn.execute(
        "SELECT event_uri FROM theme_events WHERE theme_id=?", (theme_id,)).fetchall()]
    f = {"theme_id": theme_id, "day": day, "n_events": len(evs)}
    if not evs:
        f.update(thin=True, arts_day=0)
        return f

    marks = ",".join("?" * len(evs))
    # today's series
    r = conn.execute(
        f"""SELECT COUNT(*) na, COUNT(DISTINCT source) ns, COUNT(DISTINCT lang) nl
            FROM articles WHERE event_uri IN ({marks}) AND date=?""",
        (*evs, day)).fetchone()
    f["arts_day"] = r["na"] or 0
    f["srcs_day"] = r["ns"] or 0
    f["langs_day"] = r["nl"] or 0
    # cumulative series (<= day)
    r = conn.execute(
        f"""SELECT COUNT(DISTINCT source) cs, COUNT(DISTINCT lang) cl
            FROM articles WHERE event_uri IN ({marks}) AND date<=?""",
        (*evs, day)).fetchone()
    f["cum_sources"] = r["cs"] or 0
    f["cum_langs"] = r["cl"] or 0
    f["n_companies"] = conn.execute(
        f"""SELECT COUNT(DISTINCT i.company_id) FROM incidents i
            WHERE i.event_uri IN ({marks})""", evs).fetchone()[0]
    # attachment to investable names = relevance gate, not severity
    f["attach"] = _norm(f["n_companies"], caps["cap_companies"])
    # history / acceleration
    hist = [r[0] for r in conn.execute(
        "SELECT n_articles FROM theme_daily WHERE theme_id=? AND date<? "
        "ORDER BY date", (theme_id, day)).fetchall()]
    prev_avg = sum(hist) / len(hist) if hist else None
    f["arts_prev_avg"] = prev_avg
    f["accel_raw"] = (f["arts_day"] / max(prev_avg, 1.0)) if prev_avg else None
    f["accel"] = _norm(f["accel_raw"] or 0.0, 3.0)   # 3x growth = full marks
    f["thin"] = prev_avg is None
    # persistence
    first = conn.execute(
        "SELECT first_seen FROM themes WHERE id=?", (theme_id,)).fetchone()[0]
    try:
        days_alive = (date.fromisoformat(day) - date.fromisoformat(first)).days + 1
    except Exception:
        days_alive = 1
    f["days_alive"] = days_alive
    f["persist"] = _norm(days_alive, caps["cap_days_alive"])
    # spread
    f["spread_sources"] = _norm(f["cum_sources"], caps["cap_sources"])
    f["spread_langs"] = _norm(f["cum_langs"], caps["cap_langs"])
    # authority migration: tier-1 outlet presence among today's coverage
    doms = _source_domains(conn, evs, day)
    n_t1 = sum(1 for d in set(doms) if _is_tier1(d, tp["authority_tier1"]))
    f["n_tier1"] = n_t1
    f["authority"] = _norm(n_t1, 3)                  # 3+ tier-1 outlets = full
    # actor engagement: regulators/courts/NGOs/politicians in concept labels
    pats = [p.lower() for p in tp["actor_patterns"]]
    labels = set()
    for ev in evs:
        row = conn.execute(
            "SELECT labels_json FROM event_signatures WHERE event_uri=?",
            (ev,)).fetchone()
        if row:
            labels.update(json.loads(row["labels_json"]).values())
    titles = " ".join(
        r[0] or "" for r in conn.execute(
            f"SELECT title FROM articles WHERE event_uri IN ({marks}) AND date=?",
            (*evs, day)).fetchall()).lower()
    hits = set()
    for lab in labels:
        low = lab.lower()
        hits.update(p for p in pats if p in low)
    hits.update(p for p in pats if p in titles)
    f["n_actor_hits"] = len(hits)
    f["actors"] = _norm(len(hits), caps["cap_actor_hits"])
    return f


def score_theme(features: dict, weights: dict) -> tuple[float, list]:
    """Transparent scorecard: sigmoid(intercept + sum w_i f_i) x 100.
    Every contribution is returned for the audit decomposition."""
    terms = []
    z = weights.get("intercept", 0.0)
    for name in ("accel", "spread_sources", "spread_langs", "authority",
                 "actors", "attach", "persist"):
        w = weights.get(name, 0.0)
        v = features.get(name) or 0.0
        z += w * v
        terms.append({"feature": name, "value": round(v, 4),
                      "weight": w, "contribution": round(w * v, 4)})
    score = 100.0 / (1.0 + math.exp(-z))
    return round(score, 1), terms


# ------------------------------------------------------------------ roll-up

def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return s[:40] or "theme"


def _theme_label(conn, theme_id, drop: frozenset | set = ()) -> str:
    weights = Counter()
    for (ev,) in conn.execute(
            "SELECT event_uri FROM theme_events WHERE theme_id=?",
            (theme_id,)).fetchall():
        row = conn.execute(
            "SELECT signature_json, labels_json FROM event_signatures "
            "WHERE event_uri=?", (ev,)).fetchone()
        if row:
            sig = json.loads(row["signature_json"])
            labs = json.loads(row["labels_json"])
            for u, w in sig.items():
                if u not in drop:
                    weights[labs.get(u, u)] += w
    return " / ".join(l for l, _ in weights.most_common(3)) or "untitled"


def promotion_candidates(conn: sqlite3.Connection, gate: dict) -> list:
    """Themes eligible for taxonomy promotion (scripts/promote_themes.py).
    Gate: status=escalated OR latest score >= min_score, plus min_companies
    and min_days_alive. Listing is indicative; --apply is always human."""
    rows = conn.execute(
        "SELECT id, label, first_seen, last_seen, status, n_events, "
        "n_companies, escalation_score, score_json FROM themes "
        "WHERE status NOT IN ('dead','promoted')").fetchall()
    out = []
    for r in rows:
        f = json.loads(r["score_json"] or "{}")
        days_alive = f.get("days_alive", 0)
        qualifies = (
            (r["status"] == "escalated"
             or (r["escalation_score"] or 0) >= gate["min_score"])
            and (r["n_companies"] or 0) >= gate["min_companies"]
            and days_alive >= gate["min_days_alive"])
        out.append({"theme": r, "days_alive": days_alive,
                    "qualifies": qualifies, "features": f})
    return sorted(out, key=lambda c: -(c["theme"]["escalation_score"] or 0))


def rebuild_themes(conn: sqlite3.Connection, day: str, tp: dict) -> dict:
    """Post-sweep theme roll-up for `day`. Idempotent; zero API tokens.

    1. Collect orphan events (incidents with issue_id NULL, real clusters —
       'art:' singletons stay in the raw orphan feed for now).
    2. Attach unattached events to the live theme with the strongest concept
       overlap (>= assign_overlap), else union-find them into NEW themes with
       other unattached events (>= merge_shared shared concepts, min 2 events).
       A lone event never forms a theme: single clusters are already tracked
       at incident level; the theme layer exists for cross-event chains.
    3. Upsert theme_daily for `day`; refresh label, counts, status, score.
    """
    ensure_schema(conn)
    company_uris = {r[0] for r in conn.execute(
        "SELECT concept_uri FROM companies WHERE concept_uri IS NOT NULL").fetchall()}

    orphan_evs = {r[0] for r in conn.execute(
        "SELECT DISTINCT event_uri FROM incidents "
        "WHERE issue_id IS NULL AND event_uri NOT LIKE 'art:%'").fetchall()}
    assigned = {r[0] for r in conn.execute(
        "SELECT event_uri FROM theme_events").fetchall()}
    new_evs = sorted(orphan_evs - assigned)

    sigs = {ev: _get_signature(conn, ev, company_uris, tp) for ev in new_evs}

    # Document-frequency cut: concepts shared by > max_doc_freq of ALL orphan
    # events are non-discriminative (e.g. "Artificial intelligence", "US
    # dollar" in market coverage) and would chain unrelated clusters into one
    # blob. Dropped at comparison time; cached signatures stay unfiltered.
    df = Counter()
    cached = {}
    for ev in orphan_evs - set(new_evs):
        row = conn.execute(
            "SELECT signature_json FROM event_signatures WHERE event_uri=?",
            (ev,)).fetchone()
        if row:
            cached[ev] = json.loads(row["signature_json"])
    for sig in list(sigs.values()) + list(cached.values()):
        df.update(sig.keys())
    total = len(sigs) + len(cached)
    drop = {u for u, n in df.items()
            if total and n / total > tp.get("max_doc_freq", 0.15)}
    fsigs = {ev: set(s) - drop for ev, s in sigs.items()}

    # Steps 1+2 run as a bounded fixed-point loop: attach leftover events to
    # live themes (whose signature sets GROW as events attach), then chain the
    # remainder into new themes — which may themselves attract leftovers. The
    # loop stops when nothing attaches and nothing is created, so the result
    # is a deterministic function of the data, not of how many times the
    # roll-up has run.
    live = {}
    for r in conn.execute(
            "SELECT id FROM themes WHERE status NOT IN ('dead','promoted')").fetchall():
        tset = set()
        for (ev,) in conn.execute(
                "SELECT event_uri FROM theme_events WHERE theme_id=?",
                (r["id"],)).fetchall():
            row = conn.execute(
                "SELECT signature_json FROM event_signatures WHERE event_uri=?",
                (ev,)).fetchone()
            if row:
                tset.update(json.loads(row["signature_json"]))
        live[r["id"]] = tset - drop

    leftovers = list(new_evs)
    created = 0
    for _ in range(10):                     # bounded; each pass only shrinks leftovers
        still = []
        attached = 0
        for ev in leftovers:
            s = fsigs[ev]
            best, best_n = None, 0
            for tid, tset in live.items():
                n = len(s & tset)
                if n > best_n:
                    best, best_n = tid, n
            if best and best_n >= tp["assign_overlap"]:
                conn.execute(
                    "INSERT OR IGNORE INTO theme_events (theme_id, event_uri) VALUES (?,?)",
                    (best, ev))
                live[best] |= s
                attached += 1
            else:
                still.append(ev)

        # union-find the remainder into new themes
        parent = {ev: ev for ev in still}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i, a in enumerate(still):
            for b in still[i + 1:]:
                if len(fsigs[a] & fsigs[b]) >= tp["merge_shared"]:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra
        clusters: dict[str, list] = {}
        for ev in still:
            clusters.setdefault(find(ev), []).append(ev)

        new_themes = 0
        singletons = []
        for members in clusters.values():
            if len(members) < 2:
                singletons.extend(members)  # lone event: stays in the raw orphan feed
                continue
            first = min((conn.execute(
                "SELECT MIN(date) FROM articles WHERE event_uri=?",
                (ev,)).fetchone()[0] or day) for ev in members)
            seed = max(members, key=lambda ev: sum(sigs[ev].values()))
            label0 = next(iter(json.loads(conn.execute(
                "SELECT labels_json FROM event_signatures WHERE event_uri=?",
                (seed,)).fetchone()[0]).values()), "theme")
            tid = f"th:{first}:{_slug(label0)}"
            n = 2
            while conn.execute("SELECT 1 FROM themes WHERE id=?", (tid,)).fetchone():
                tid = f"th:{first}:{_slug(label0)}-{n}"
                n += 1
            conn.execute(
                "INSERT INTO themes (id, label, first_seen, last_seen) VALUES (?,?,?,?)",
                (tid, label0, first, day))
            live[tid] = set()
            for ev in members:
                conn.execute(
                    "INSERT OR IGNORE INTO theme_events (theme_id, event_uri) VALUES (?,?)",
                    (tid, ev))
                live[tid] |= fsigs[ev]
            new_themes += 1
        created += new_themes
        leftovers = singletons
        if not attached and not new_themes:
            break

    # 3. per-theme daily rollup + status + score
    themes_today = 0
    for r in conn.execute("SELECT id, first_seen, status FROM themes").fetchall():
        tid, status = r["id"], r["status"]
        evs = [x[0] for x in conn.execute(
            "SELECT event_uri FROM theme_events WHERE theme_id=?", (tid,)).fetchall()]
        if not evs:
            continue
        marks = ",".join("?" * len(evs))
        d = conn.execute(
            f"""SELECT COUNT(*) na, COUNT(DISTINCT source) ns,
                       COUNT(DISTINCT lang) nl
                FROM articles WHERE event_uri IN ({marks}) AND date=?""",
            (*evs, day)).fetchone()
        nco = conn.execute(
            f"""SELECT COUNT(DISTINCT company_id) FROM incidents
                WHERE event_uri IN ({marks})""", evs).fetchone()[0]
        new_ev = sum(1 for ev in evs if conn.execute(
            "SELECT first_seen FROM event_signatures WHERE event_uri=?",
            (ev,)).fetchone()[0] == day)
        conn.execute(
            """INSERT INTO theme_daily (theme_id, date, n_articles, n_sources,
                                       n_langs, n_companies, new_events)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(theme_id, date) DO UPDATE SET
                 n_articles=excluded.n_articles, n_sources=excluded.n_sources,
                 n_langs=excluded.n_langs, n_companies=excluded.n_companies,
                 new_events=excluded.new_events""",
            (tid, day, d["na"] or 0, d["ns"] or 0, d["nl"] or 0, nco, new_ev))

        feats = escalation_features(conn, tid, day, tp)
        score, terms = score_theme(feats, tp["weights"])
        feats["score_terms"] = terms
        # status machine (indicative only; promotion happens via script)
        if status not in ("promoted",):
            if status != "escalated":
                if (d["na"] or 0) == 0:
                    try:
                        gap = (date.fromisoformat(day)
                               - date.fromisoformat(r["first_seen"])).days
                    except Exception:
                        gap = 0
                    last = conn.execute(
                        "SELECT MAX(date) FROM theme_daily WHERE theme_id=? "
                        "AND n_articles>0", (tid,)).fetchone()[0]
                    if last:
                        gap = (date.fromisoformat(day)
                               - date.fromisoformat(last)).days
                    if gap >= tp["status"]["dead_after_days"]:
                        status = "dead"
                elif (not feats["thin"]
                      and score >= tp["status"]["emerging_score"]):
                    status = "emerging"
        conn.execute(
            """UPDATE themes SET label=?, last_seen=?, status=?, n_events=?,
                 n_companies=?, escalation_score=?, score_json=?, thin=?
               WHERE id=?""",
            (_theme_label(conn, tid, drop), day, status, len(evs), nco,
             score, json.dumps(feats), 1 if feats["thin"] else 0, tid))
        themes_today += 1
    conn.commit()
    return {"orphan_events": len(orphan_evs), "new_events": len(new_evs),
            "themes_created": created, "themes_total": themes_today,
            "ungrouped": len(leftovers)}

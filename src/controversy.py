"""Controversy level & direction (v0.5.0) — per (company, issue) temporal
integrator over the OUTRAGE family of attention features.

Design contract (audit):
- One row per (company_id, issue_id, date). NEVER summed across issues —
  a company's controversy position is a vector, not a scalar.
- INFLUX (flow): attention entering on the day. Reuses the impact.outrage
  weights/caps/charges (single source in parameters.yaml; not duplicated):
    volume_w   = Σ_incidents n_articles × layer_w(layer) × tier_f(tier)
    attention  = w_a·log1p(volume_w)/log1p(cap_a) + w_s·src/cap_s
                 + w_t1·tier1/cap_t1 + w_l·langs/cap_l      (capped as in outrage)
    influx     = attention × (1 + theme_boost_w × theme_boost) × charge(issue)
  Breadth (sources/tier1/langs) is computed over the union of the issue's
  subject-gated articles that day — breadth is reach, never layer-weighted.
  theme_boost = max live (non-dead) theme escalation_score/100 attached to
  the issue's incidents (diffusion AMPLIFY mode; theme_boost_weight=0 is
  the neutral mode. The amplify/dilute fork is decided by archive evidence,
  not preference).
- LEVEL (stock): EMA form, level(d) = λ·level(d−1) + (1−λ)·influx(d) with
  λ = 2^(−1/h), h = per-issue half-life in days. The EMA form keeps level
  comparable across issues with different half-lives: identical sustained
  attention converges to the same level regardless of h; h controls memory,
  not scale. (The un-normalised accumulation form would double the level of
  a 60d issue vs a 30d issue for identical attention — rejected as
  misleading for cross-issue comparison.)
- DIRECTION: direction(d) = level(d) − level(d−1) ≡ (1−λ)(influx(d) −
  level(d−1)) — the net accumulation rate: today's attention vs what the
  stock shed overnight. Positive = building faster than it fades. NOT
  smoothed: the level is already an EMA, and an EMA of its deltas is a
  lagging indicator — after a one-day spike it kept reporting "building"
  for several days while the level fell (caught by
  test_step_response_and_exact_decay). The first difference of an already-
  smoothed stock is the honest, lag-free signal.
- Continuation rows are written while level ≥ min_level (decay is data;
  truncating the tail would falsify the timeseries). No z-features are used,
  so the 30-day min_history rule does not apply to level; it is meaningful
  from day 1 and merely shallow early — the display flags days before
  min_history as THIN for honesty.
- Full per-day rebuild (DELETE-first) like every other derived table.
- Zero API tokens: reads articles / article_companies / incidents / themes.
"""
import json
import math
import sqlite3
from collections import defaultdict

from .impact import _domains, _is_tier1, DEFAULT_MIN_CONCEPT_SCORE

UNCLASSIFIED = "_UNCLASSIFIED"

SCHEMA = """
CREATE TABLE IF NOT EXISTS company_issue_daily (
    company_id INTEGER NOT NULL,
    issue_id TEXT NOT NULL,
    date TEXT NOT NULL,
    influx REAL,               -- attention entering that day (see docstring)
    level REAL,                -- EMA stock of controversy intensity
    direction REAL,            -- EMA of daily level deltas (net flow)
    components_json TEXT,      -- full audit: counts, factors, terms, lambdas
    PRIMARY KEY (company_id, issue_id, date)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _lambda(half_life_days: float) -> float:
    return 2.0 ** (-1.0 / half_life_days)


def _day_influx(conn, day: str, cp: dict, outrage_p: dict, tier1: list[str],
                min_score: float) -> dict:
    """Per (company, issue) attention entering on `day`. Returns
    {(cid, issue): {influx, components}}."""
    rows = conn.execute(
        """SELECT ac.company_id AS cid,
                  COALESCE(i.issue_id, ?) AS issue,
                  i.layer AS layer, i.severity_tier AS tier,
                  i.event_uri AS ev, i.raw_json AS inc_raw,
                  a.uri AS uri, a.lang AS lang, a.source AS source,
                  a.raw_json AS raw_json
           FROM article_companies ac
           JOIN articles a ON a.uri = ac.article_uri
           JOIN incidents i ON i.company_id = ac.company_id
               AND i.event_uri = COALESCE(a.event_uri, 'art:' || a.uri)
           WHERE a.date = ?
             AND (COALESCE(ac.score,0) >= ? OR COALESCE(ac.title_match,0) = 1)
           """, (UNCLASSIFIED, day, min_score)).fetchall()

    caps = outrage_p["caps"]
    charge_map = outrage_p["charge"]
    layer_w = cp["layer_weight"]
    tier_f = cp["tier_factor"]
    ngo_floor = outrage_p["ngo_charge_floor"]

    # group articles by (cid, issue), keeping per-incident subgroups
    by_ci: dict[tuple, dict] = defaultdict(lambda: defaultdict(list))
    meta: dict[tuple, list] = defaultdict(list)   # (layer,tier,triggers) per incident
    for r in rows:
        key = (r["cid"], r["issue"])
        by_ci[key][r["ev"]].append(r)
        if not any(m[2] == r["ev"] for m in meta[key]):
            try:
                trig = json.loads(r["inc_raw"] or "{}").get("confirmed_triggers") or []
            except Exception:
                trig = []
            meta[key].append((r["layer"], r["tier"], r["ev"], trig))

    # live-theme escalation per event (diffusion amplifier)
    evs = {r["ev"] for rows_ in by_ci.values() for r in
           [x for sub in rows_.values() for x in sub]}
    theme_boost: dict[str, float] = {}
    for ev in evs:
        sc = conn.execute(
            "SELECT MAX(t.escalation_score) FROM themes t "
            "JOIN theme_events te ON te.theme_id = t.id "
            "WHERE te.event_uri=? AND t.status NOT IN ('dead')",
            (ev,)).fetchone()[0]
        theme_boost[ev] = (sc or 0.0) / 100.0

    out = {}
    for (cid, issue), incidents in by_ci.items():
        all_arts = [a for sub in incidents.values() for a in sub]
        volume_w = 0.0
        ngo = False
        layers, tiers = set(), set()
        for ev, arts in incidents.items():
            layer, tier, _, trig = next(m for m in meta[(cid, issue)]
                                        if m[2] == ev)
            layers.add(layer)
            tiers.add(tier or "_default")
            ngo = ngo or "credible_ngo_campaign" in trig
            volume_w += len(arts) * layer_w.get(layer, 1.0) \
                * tier_f.get(tier or "_default", tier_f["_default"])
        doms = _domains(all_arts)
        n_src = len({a["source"] for a in all_arts if a["source"]})
        n_t1 = len({d for d in doms if _is_tier1(d, tier1)})
        n_lg = len({a["lang"] for a in all_arts if a["lang"]})
        t_boost = max((theme_boost.get(ev, 0.0) for ev in incidents),
                      default=0.0)
        attention = (
            outrage_p["w_articles"] * math.log1p(volume_w) / math.log1p(caps["articles"])
            + outrage_p["w_sources"] * min(n_src, caps["sources"]) / caps["sources"]
            + outrage_p["w_tier1"] * min(n_t1, caps["tier1"]) / caps["tier1"]
            + outrage_p["w_langs"] * min(n_lg, caps["langs"]) / caps["langs"])
        charge = charge_map.get(issue, charge_map["_default"])
        if ngo:
            charge = max(charge, ngo_floor)
        mult = 1.0 + cp["theme_boost_weight"] * t_boost
        influx = attention * mult * charge
        out[(cid, issue)] = {
            "influx": influx,
            "components": {
                "arts": len(all_arts), "volume_w": round(volume_w, 3),
                "sources": n_src, "tier1": n_t1, "langs": n_lg,
                "layers": sorted(x for x in layers if x),
                "tiers": sorted(tiers), "theme_boost": round(t_boost, 3),
                "theme_mult": round(mult, 3), "charge": charge,
                "attention": round(attention, 4),
                "n_incidents": len(incidents)}}
    return out


def rebuild_controversy(conn: sqlite3.Connection, day: str, p: dict,
                        min_score: float = DEFAULT_MIN_CONCEPT_SCORE) -> dict:
    """Full per-day rebuild of company_issue_daily. `p` is params(); reads
    p['controversy'] and reuses p['impact']['outrage'] weights/caps/charges.
    Idempotent; zero API tokens."""
    ensure_schema(conn)
    cp = p["controversy"]
    outrage_p = p["impact"]["outrage"]
    tier1 = p["themes"]["authority_tier1"]
    min_level = cp["min_level"]
    hl = cp["half_life_days"]
    default_hl = hl.get("_default", 21)

    conn.execute("DELETE FROM company_issue_daily WHERE date=?", (day,))
    influx = _day_influx(conn, day, cp, outrage_p, tier1, min_score)

    prev_rows = conn.execute(
        """SELECT company_id, issue_id, level
           FROM company_issue_daily
           WHERE date = (SELECT MAX(date) FROM company_issue_daily
                         WHERE date < ?)""", (day,)).fetchall()
    prev = {(r["company_id"], r["issue_id"]): r["level"]
            for r in prev_rows if r["level"] is not None
            and r["level"] >= min_level}

    keys = set(influx) | set(prev)
    n = 0
    for (cid, issue) in sorted(keys):
        h = hl.get(issue, default_hl)
        lam = _lambda(h)
        infl = influx.get((cid, issue), {}).get("influx", 0.0)
        prev_level = prev.get((cid, issue), 0.0)
        level = lam * prev_level + (1.0 - lam) * infl
        if level < min_level and infl <= 0.0:
            continue
        direction = level - prev_level   # net accumulation rate (no lag)
        comp = influx.get((cid, issue), {}).get(
            "components", {"arts": 0, "decay_only": True})
        comp.update({"half_life_days": h, "lambda": round(lam, 6),
                     "prev_level": round(prev_level, 6)})
        conn.execute(
            """INSERT INTO company_issue_daily
               (company_id, issue_id, date, influx, level, direction,
                components_json) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(company_id, issue_id, date) DO UPDATE SET
                 influx=excluded.influx, level=excluded.level,
                 direction=excluded.direction,
                 components_json=excluded.components_json""",
            (cid, issue, day, round(infl, 6), round(level, 6),
             round(direction, 6), json.dumps(comp)))
        n += 1
    conn.commit()
    return {"rows": n, "with_influx": len(influx),
            "decay_continuations": n - len(influx)}

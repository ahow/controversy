#!/usr/bin/env python3
"""Static dashboard generator for GitHub Pages.

Reads data/controversy.db and writes docs/index.html — a self-contained
page (embedded JSON, vanilla JS, no build step) that GitHub Pages serves
from the /docs folder. Regenerate after every sweep:

    python scripts/build_dashboard.py

Design: dark mission-control aesthetic (JetBrains Mono data chrome, cyan/
amber/red semantic accents). All state transitions remain human decisions;
this page is read-only by design.
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import ROOT, params  # noqa: E402

OUT = ROOT / "docs"
MAX_QUEUE = 200
MAX_ORPHANS = 40
MAX_THEMES = 30


def collect(db_path: Path) -> dict:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    def q1(sql, args=()):
        return conn.execute(sql, args).fetchone()[0]

    meta = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_date": q1("SELECT MAX(date) FROM articles") or "—",
        "param_version": params()["version"],
        "n_universe": q1("SELECT COUNT(*) FROM companies"),
        "n_mapped": q1("SELECT COUNT(*) FROM companies WHERE map_status='auto_mapped'"),
        "n_companies_seen": q1(
            "SELECT COUNT(*) FROM company_daily "
            "WHERE date=(SELECT MAX(date) FROM articles)"),
        "n_links_dropped": q1(
            "SELECT COUNT(*) FROM article_companies ac WHERE NOT "
            "(COALESCE(ac.score,0) >= ? OR COALESCE(ac.title_match,0) = 1)",
            (params().get("attach", {}).get("min_concept_score", 4),))
        if any(c[1] == "score" for c in conn.execute(
            "PRAGMA table_info(article_companies)")) else 0,
        "n_articles": q1("SELECT COUNT(*) FROM articles"),
        "n_incidents": q1("SELECT COUNT(*) FROM incidents"),
        "n_confirmed": q1("SELECT COUNT(*) FROM incidents WHERE layer='confirmed'"),
        "n_orphans": q1("SELECT COUNT(*) FROM incidents WHERE issue_id IS NULL"),
    }

    def incident_rows(where="", limit=MAX_QUEUE):
        rows = conn.execute(
            f"""
            SELECT i.id, i.company_id, c.name AS company, c.sector, c.country,
                   i.event_uri, i.issue_id, i.layer, i.n_articles, i.n_sources,
                   i.first_seen, i.last_seen, i.raw_json
            FROM incidents i JOIN companies c ON c.id = i.company_id
            {where}
            ORDER BY CASE i.layer WHEN 'confirmed' THEN 0 ELSE 1 END,
                     i.n_sources DESC, i.n_articles DESC
            LIMIT ?
            """, (limit,)).fetchall()
        out = []
        for r in rows:
            raw = json.loads(r["raw_json"] or "{}")
            links = []
            for au in (raw.get("article_uris") or [])[:5]:
                u = conn.execute("SELECT url, title, source FROM articles WHERE uri=?",
                                 (au,)).fetchone()
                if u:
                    links.append({"url": u["url"], "title": u["title"],
                                  "source": u["source"]})
            out.append({
                "id": r["id"], "company": r["company"], "sector": r["sector"],
                "country": r["country"], "issue": r["issue_id"], "layer": r["layer"],
                "n_articles": r["n_articles"], "n_sources": r["n_sources"],
                "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                "titles": (raw.get("sample_titles") or [])[:5],
                "triggers": raw.get("confirmed_triggers") or [],
                "links": links,
            })
        return out

    incidents = incident_rows()
    orphans = incident_rows("WHERE i.issue_id IS NULL", MAX_ORPHANS)

    # Channel B: emergent themes rolled up from the orphan feed (indicative-only)
    themes = []
    if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='themes'"
            ).fetchone():
        for r in conn.execute(
                """
                SELECT t.id, t.label, t.status, t.first_seen, t.last_seen,
                       t.n_events, t.n_companies, t.escalation_score, t.thin,
                       t.score_json, t.promoted_issue_id
                FROM themes t
                WHERE t.status != 'dead'
                ORDER BY t.escalation_score DESC
                LIMIT ?
                """, (MAX_THEMES,)).fetchall():
            sj = json.loads(r["score_json"] or "{}")
            terms = sorted(sj.get("score_terms") or [],
                           key=lambda x: -x["contribution"])[:3]
            co = [row[0] for row in conn.execute(
                """
                SELECT c.name FROM theme_events te
                JOIN incidents i ON i.event_uri = te.event_uri
                JOIN companies c ON c.id = i.company_id
                WHERE te.theme_id = ?
                GROUP BY c.name ORDER BY COUNT(*) DESC LIMIT 4
                """, (r["id"],)).fetchall()]
            themes.append({
                "id": r["id"], "label": r["label"], "status": r["status"],
                "score": r["escalation_score"] or 0.0,
                "thin": bool(r["thin"]),
                "n_events": r["n_events"], "n_companies": r["n_companies"],
                "days_alive": sj.get("days_alive") or 0,
                "promoted": r["promoted_issue_id"],
                "companies": co,
                "top_terms": [{"feature": t["feature"],
                               "contribution": t["contribution"]}
                              for t in terms],
            })

    companies = []
    latest = meta["data_date"]
    has_scores = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='company_scores'").fetchone()
    if latest and latest != "—":
        score_join = ("LEFT JOIN company_scores sc ON sc.company_id = c.id "
                      "AND sc.date = d.date") if has_scores else ""
        score_cols = ("sc.severity, sc.outrage, sc.materiality_bps,"
                      " sc.thin AS score_thin") if has_scores else "NULL, NULL, NULL, NULL"
        for r in conn.execute(
                f"""
                SELECT c.id, c.name, c.sector, c.country, d.n_articles, d.n_sources,
                       d.n_incidents, d.z, d.thin_data,
                       COALESCE(s.state, 'NORMAL') AS state, {score_cols}
                FROM company_daily d
                JOIN companies c ON c.id = d.company_id
                LEFT JOIN states s ON s.company_id = c.id
                {score_join}
                WHERE d.date = ?
                ORDER BY d.n_articles DESC
                """, (latest,)).fetchall():
            companies.append({
                "id": r["id"], "name": r["name"], "sector": r["sector"],
                "country": r["country"], "n_articles": r["n_articles"],
                "n_sources": r["n_sources"], "n_incidents": r["n_incidents"],
                "z": r["z"], "thin": bool(r["thin_data"]), "state": r["state"],
                "sev": r["severity"] if has_scores else None,
                "out": r["outrage"] if has_scores else None,
                "mat": r["materiality_bps"] if has_scores else None,
                "score_thin": bool(r["score_thin"]) if has_scores else False,
            })

        # --- per-company drill-down: incidents + evidence + decomposition ---
        # Incidents are already subject-filtered at build (v0.4.0 salience
        # gate); each carries up to 5 clickable evidence links.
        inc_by_co: dict[int, list] = {}
        for r in conn.execute(
                """
                SELECT i.company_id, i.issue_id, i.layer, i.n_articles,
                       i.n_sources, i.first_seen, i.last_seen, i.raw_json
                FROM incidents i
                ORDER BY i.company_id,
                         CASE i.layer WHEN 'confirmed' THEN 0 ELSE 1 END,
                         i.n_sources DESC, i.n_articles DESC
                """).fetchall():
            cid = r["company_id"]
            if len(inc_by_co.setdefault(cid, [])) >= 15:
                continue
            raw = json.loads(r["raw_json"] or "{}")
            links = []
            for au in (raw.get("article_uris") or [])[:5]:
                u = conn.execute(
                    "SELECT url, title, source FROM articles WHERE uri=?",
                    (au,)).fetchone()
                if u:
                    links.append({"url": u["url"], "title": u["title"],
                                  "source": u["source"]})
            inc_by_co[cid].append({
                "issue": r["issue_id"], "layer": r["layer"],
                "n_articles": r["n_articles"], "n_sources": r["n_sources"],
                "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                "triggers": raw.get("confirmed_triggers") or [],
                "links": links,
            })
        decomp = {}
        if has_scores:
            for r in conn.execute(
                    "SELECT company_id, scores_json FROM company_scores "
                    "WHERE date=?", (latest,)).fetchall():
                sj = json.loads(r["scores_json"] or "{}")
                dec = {}
                for k in ("severity", "outrage", "materiality"):
                    blk = sj.get(k) or {}
                    if k == "materiality":
                        dec[k] = {"status": blk.get("status")}
                    else:
                        dec[k] = {"intercept": blk.get("intercept"),
                                  "terms": blk.get("terms") or []}
                decomp[r["company_id"]] = {
                    "scores": dec,
                    "triggers": sj.get("triggers") or [],
                    "issues": sj.get("issues") or [],
                }
        for co in companies:
            co["inc"] = inc_by_co.get(co["id"], [])
            co["dec"] = decomp.get(co["id"])

    trend = [{"date": r["date"], "articles": r["n"], "incidents": r["i"]}
             for r in conn.execute(
                 """
                 SELECT d.date, SUM(d.n_articles) n, SUM(d.n_incidents) i
                 FROM company_daily d GROUP BY d.date ORDER BY d.date
                 """).fetchall()]

    issues = [{"issue": r["issue_id"] or "ORPHAN", "n": r["n"]}
              for r in conn.execute(
                  "SELECT issue_id, COUNT(*) n FROM incidents "
                  "GROUP BY issue_id ORDER BY n DESC").fetchall()]

    ticker = [t for i in incidents if i["layer"] == "confirmed"
              for t in i["titles"][:1]][:12]
    conn.close()
    return {"meta": meta, "incidents": incidents, "orphans": orphans,
            "themes": themes,
            "companies": companies, "trend": trend, "issues": issues,
            "ticker": ticker}


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Controversy Monitor — Shadow Mode</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700&family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#050810; --panel:#0c1220; --panel2:#111a2b; --line:#1a2540;
  --txt:#d7e1f0; --dim:#7d8db0; --cyan:#22d3ee; --mint:#34d399;
  --amber:#fbbf24; --red:#f87171; --mono:'JetBrains Mono',ui-monospace,monospace;
  --sans:'Inter',system-ui,sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
html{min-height:100dvh}
body{background:var(--bg);color:var(--txt);font-family:var(--mono);font-size:13px;
  min-height:100dvh;background-image:radial-gradient(ellipse 80% 50% at 50% -20%,#0d1530 0%,transparent 60%)}
::selection{background:var(--cyan);color:#050810}
:focus-visible{outline:none;box-shadow:0 0 0 2px var(--bg),0 0 0 4px var(--cyan)}
header{position:sticky;top:0;z-index:10;display:flex;justify-content:space-between;align-items:center;
  gap:16px;padding:10px 20px;background:rgba(5,8,16,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:.18em;font-size:13px}
.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--cyan);
  box-shadow:0 0 8px var(--cyan);animation:pulse 1.5s ease-in-out infinite}
.brand small{color:var(--dim);font-weight:400;letter-spacing:.08em;font-size:10px}
.hright{display:flex;align-items:center;gap:14px;font-size:10px;letter-spacing:.1em;color:var(--dim)}
.badge{padding:3px 10px;border:1px solid var(--amber);color:var(--amber);border-radius:100px;
  font-size:10px;letter-spacing:.14em;display:inline-flex;align-items:center;gap:6px}
.badge::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--amber);
  animation:pulse 1.5s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}
.ticker{border-bottom:1px solid var(--line);overflow:hidden;background:var(--panel);height:30px;
  display:flex;align-items:center}
.ticker-track{display:flex;gap:48px;white-space:nowrap;animation:tick 60s linear infinite;
  padding-left:20px;font-size:11px;color:var(--dim)}
.ticker:hover .ticker-track{animation-play-state:paused}
.ticker-track b{color:var(--red);font-weight:600;letter-spacing:.1em;margin-right:8px}
@keyframes tick{to{transform:translateX(-50%)}}
main{max-width:1440px;margin:0 auto;padding:20px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line);margin-bottom:20px}
.kpi{background:var(--panel);padding:14px 16px}
.kpi .v{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
.kpi .l{font-size:9px;letter-spacing:.18em;color:var(--dim);margin-top:4px;text-transform:uppercase}
.kpi.warn .v{color:var(--amber)} .kpi.crit .v{color:var(--red)} .kpi.ok .v{color:var(--mint)}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 360px;gap:20px;align-items:start}
.grid>*{min-width:0}
.panel>*{min-width:0}
.panel{background:var(--panel);border:1px solid var(--line)}
.panel>h2{font-size:10px;letter-spacing:.2em;color:var(--dim);text-transform:uppercase;
  padding:12px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between}
.filters{display:flex;gap:8px;flex-wrap:wrap;padding:12px 16px;border-bottom:1px solid var(--line);
  align-items:center}
.tabs{display:flex;gap:4px}
.tab{background:none;border:1px solid var(--line);color:var(--dim);font-family:var(--mono);
  font-size:10px;letter-spacing:.12em;padding:5px 12px;border-radius:100px;cursor:pointer;
  transition:all .15s ease}
.tab:hover{border-color:var(--cyan);color:var(--cyan)}
.tab[aria-pressed="true"]{background:var(--cyan);border-color:var(--cyan);color:#050810;font-weight:600}
select,input[type=search]{background:var(--panel2);border:1px solid var(--line);color:var(--txt);
  font-family:var(--mono);font-size:11px;padding:5px 10px;border-radius:4px}
select:focus-visible,input:focus-visible{box-shadow:0 0 0 2px var(--bg),0 0 0 4px var(--cyan);outline:none}
input[type=search]{flex:1;min-width:140px}
.incident{border-left:3px solid var(--line);border-bottom:1px solid var(--line);padding:12px 16px;
  cursor:pointer;transition:background .15s ease}
.incident:hover{background:var(--panel2)}
.incident.confirmed{border-left-color:var(--red)}
.incident.indicative{border-left-color:var(--amber)}
.incident.orphan{border-left-color:#3a4a6b}
.i-top{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.i-co{font-weight:600;font-size:13px}
.i-meta{color:var(--dim);font-size:10px;letter-spacing:.06em;white-space:nowrap}
.i-tags{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
.pill{font-size:9px;letter-spacing:.1em;padding:2px 8px;border-radius:100px;border:1px solid var(--line);
  color:var(--dim)}
.pill.confirmed{color:var(--red);border-color:var(--red)}
.pill.indicative{color:var(--amber);border-color:var(--amber)}
.pill.issue{color:var(--cyan);border-color:#1e4b5a}
.i-title{color:var(--dim);font-family:var(--sans);font-size:12px;margin-top:6px;line-height:1.5}
.i-detail{display:none;margin-top:10px;padding-top:10px;border-top:1px dashed var(--line)}
.incident.open .i-detail{display:block}
.i-detail ul{list-style:none}
.i-detail li{margin-bottom:6px;font-family:var(--sans);font-size:12px;line-height:1.5}
.i-detail a{color:var(--cyan);text-decoration:none}
.i-detail a:hover{text-decoration:underline}
.i-detail .src{color:var(--dim);font-size:10px;font-family:var(--mono)}
aside{display:flex;flex-direction:column;gap:20px}
.bars{padding:12px 16px;display:flex;flex-direction:column;gap:8px}
.bar-row{display:grid;grid-template-columns:150px 1fr 40px;gap:10px;align-items:center;font-size:10px}
.bar-row .n{color:var(--dim);letter-spacing:.04em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-row .c{text-align:right;font-variant-numeric:tabular-nums}
.bar{height:8px;background:var(--panel2);position:relative}
.bar i{position:absolute;inset:0 auto 0 0;background:var(--cyan);display:block}
.bar-row.orphan .bar i{background:#3a4a6b}
.chart{padding:12px 16px}
.chart svg{width:100%;height:120px;display:block}
.chart .axis{stroke:var(--line);stroke-width:1}
.chart .line{fill:none;stroke:var(--cyan);stroke-width:1.5}
.chart .area{fill:rgba(34,211,238,.08)}
.chart .cap{font-size:9px;color:var(--dim);letter-spacing:.1em;margin-top:6px}
.orphan-item{padding:10px 16px;border-bottom:1px solid var(--line);font-family:var(--sans);font-size:12px}
.orphan-item .oc{font-family:var(--mono);font-size:10px;color:var(--dim);letter-spacing:.08em;margin-bottom:3px}
.orphan-item .ot{color:var(--txt);line-height:1.45}
.theme-item{padding:10px 16px;border-bottom:1px solid var(--line)}
.theme-item .th-top{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.theme-item .th-label{font-family:var(--sans);font-size:12px;color:var(--txt);line-height:1.4}
.theme-item .th-score{font-size:15px;font-weight:700;color:var(--cyan);font-variant-numeric:tabular-nums}
.theme-item .th-score.thin{color:var(--dim);font-size:10px;letter-spacing:.12em}
.theme-item .th-bar{height:3px;background:var(--panel2);border-radius:2px;margin:6px 0;overflow:hidden}
.theme-item .th-bar i{display:block;height:100%;background:linear-gradient(90deg,#155e75,var(--cyan))}
.theme-item .th-meta{display:flex;align-items:center;gap:8px;font-size:9px;color:var(--dim);letter-spacing:.06em}
.theme-item .th-co{font-family:var(--sans);font-size:11px;color:var(--dim);margin-top:5px;line-height:1.4}
.theme-item .th-terms{font-size:9px;color:var(--dim);margin-top:4px;letter-spacing:.04em}
.pill.candidate{color:var(--dim)}
.pill.emerging{color:var(--amber);border-color:var(--amber)}
.pill.escalated{color:var(--red);border-color:var(--red)}
.pill.promoted{color:var(--mint);border-color:var(--mint)}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.table-wrap{overflow-x:auto}
th{font-size:9px;letter-spacing:.16em;color:var(--dim);text-transform:uppercase;text-align:left;
  padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;
  white-space:nowrap;background:var(--panel)}
th:hover{color:var(--cyan)}
th.sorted{color:var(--cyan)}
td{padding:8px 12px;border-bottom:1px solid var(--line);font-size:12px}
tbody tr{transition:background .15s ease}
tbody tr:hover{background:var(--panel2)}
td.num,th.num{text-align:right}
.state{font-size:9px;letter-spacing:.1em;padding:2px 8px;border-radius:100px;border:1px solid var(--mint);
  color:var(--mint)}
.state-FLAGGED,.state-WATCH{border-color:var(--amber);color:var(--amber)}
.state-EXCLUDED{border-color:var(--red);color:var(--red)}
.thin{color:var(--dim);font-size:9px;letter-spacing:.1em}
.score-high{color:var(--red);font-weight:700}
.score-mid{color:var(--amber);font-weight:600}
tbody tr.co-row{cursor:pointer}
tbody tr.co-row:hover td{border-bottom-color:var(--cyan)}
tr.co-detail>td{background:var(--panel2);padding:14px 18px;border-bottom:1px solid var(--line)}
.dec{display:flex;gap:22px;flex-wrap:wrap;font-size:10px;color:var(--dim);
  letter-spacing:.06em;margin-bottom:10px}
.dec b{color:var(--txt);letter-spacing:.12em}
.dec .dz{color:var(--cyan)}
.co-inc{border-left:2px solid var(--line);padding:8px 12px;margin:8px 0}
.co-inc.confirmed{border-left-color:var(--red)}
.co-inc.indicative{border-left-color:var(--amber)}
.co-inc.orphan{border-left-color:#3a4a6b}
.co-inc ul{list-style:none;margin-top:6px}
.co-inc li{margin-bottom:4px;font-family:var(--sans);font-size:12px;line-height:1.5}
.co-inc a{color:var(--cyan);text-decoration:none}
.co-inc a:hover{text-decoration:underline}
.co-inc .src{color:var(--dim);font-size:10px;font-family:var(--mono)}
footer{margin-top:20px;padding:16px;border:1px solid var(--line);background:var(--panel);
  color:var(--dim);font-size:10px;line-height:1.8;letter-spacing:.04em}
footer b{color:var(--txt);letter-spacing:.14em}
.count-note{color:var(--dim);font-size:10px;padding:8px 16px;border-bottom:1px solid var(--line)}
.empty{padding:24px 16px;color:var(--dim);font-size:11px;letter-spacing:.08em;text-align:center;
  border:1px dashed var(--line);margin:12px 16px}
@media (max-width:960px){
  .grid{grid-template-columns:minmax(0,1fr)}
  .hright span{display:none}
  header{flex-wrap:wrap}
  .i-top{flex-wrap:wrap}
  .i-meta{white-space:normal}
}
@media (prefers-reduced-motion:reduce){
  .ticker-track,.brand .dot,.badge::before{animation:none}
  *{transition:none!important}
}
</style>
</head>
<body>
<header>
  <div class="brand"><span class="dot" aria-hidden="true"></span>CONTROVERSY&nbsp;MONITOR
    <small>REPUTATIONAL RISK EARLY WARNING</small></div>
  <div class="hright">
    <span>DATA __DATA_DATE__</span>
    <span>GEN __GENERATED_AT__</span>
    <span class="badge">SHADOW MODE</span>
  </div>
</header>
__TICKER__
<main>
  <section class="kpis" aria-label="Key indicators">
    <div class="kpi"><div class="v">__N_MAPPED__</div><div class="l">Companies monitored</div></div>
    <div class="kpi"><div class="v">__N_SEEN__</div><div class="l">Seen in coverage</div></div>
    <div class="kpi"><div class="v">__N_ARTICLES__</div><div class="l">Articles captured</div></div>
    <div class="kpi"><div class="v">__N_INCIDENTS__</div><div class="l">Incidents (events)</div></div>
    <div class="kpi crit"><div class="v">__N_CONFIRMED__</div><div class="l">Confirmed layer</div></div>
    <div class="kpi warn"><div class="v">__N_ORPHANS__</div><div class="l">Orphans (taxonomy feed)</div></div>
  </section>
  <section class="panel" style="margin-bottom:20px" aria-label="Company coverage">
    <h2><span>COMPANY COVERAGE — LATEST DAY</span><span id="co-count"></span></h2>
    <div class="count-note">Click a company row for its incidents (with evidence links)
      and score decomposition. Scores are three separate axes — never summed.</div>
    <div class="table-wrap">
    <table id="co-table">
      <thead><tr>
        <th data-k="name">COMPANY</th><th data-k="sector">SECTOR</th>
        <th data-k="country">CTRY</th><th data-k="n_articles" class="num sorted">ARTICLES</th>
        <th data-k="n_sources" class="num">SOURCES</th><th data-k="n_incidents" class="num">INCIDENTS</th>
        <th data-k="z" class="num">Z</th><th data-k="sev" class="num">SEV</th>
        <th data-k="out" class="num">OUTRAGE</th><th data-k="mat" class="num">MAT bps</th><th data-k="state">STATE</th>
      </tr></thead>
      <tbody></tbody>
    </table>
    </div>
  </section>
  <div class="grid">
    <section class="panel" aria-label="Review queue">
      <h2><span>REVIEW QUEUE</span><span id="queue-count"></span></h2>
      <div class="filters">
        <div class="tabs" role="group" aria-label="Layer filter">
          <button class="tab" data-layer="all" aria-pressed="true">ALL</button>
          <button class="tab" data-layer="confirmed" aria-pressed="false">CONFIRMED</button>
          <button class="tab" data-layer="indicative" aria-pressed="false">INDICATIVE</button>
          <button class="tab" data-layer="orphan" aria-pressed="false">ORPHAN</button>
        </div>
        <select id="issue-filter" aria-label="Issue filter"><option value="">ALL ISSUES</option></select>
        <input type="search" id="q" placeholder="FILTER COMPANY…" aria-label="Filter by company">
      </div>
      <div id="queue"></div>
    </section>
    <aside>
      <section class="panel" aria-label="Issue mix">
        <h2><span>ISSUE MIX</span></h2>
        <div class="bars" id="issue-bars"></div>
      </section>
      <section class="panel" aria-label="Coverage trend">
        <h2><span>COVERAGE TREND</span></h2>
        <div class="chart" id="trend"></div>
      </section>
      <section class="panel" aria-label="Emerging themes">
        <h2><span>EMERGING THEMES — CHANNEL B</span><span id="theme-count"></span></h2>
        <div class="count-note">Clusters formed from the orphan feed. Escalation scores are
          INDICATIVE-only priors (uncalibrated until the replay produces labelled outcomes) —
          they rank attention, never drive exclusion.</div>
        <div id="themes"></div>
      </section>
      <section class="panel" aria-label="Orphan feed">
        <h2><span>ORPHAN FEED — RAW</span></h2>
        <div class="count-note">Ungrouped orphan incidents: candidate new taxonomy categories. Threshold: 20/quarter.</div>
        <div id="orphans"></div>
      </section>
    </aside>
  </div>
  <footer>
    <b>METHODOLOGY.</b> Two signal layers, never summed: <b>confirmed</b> (hard triggers — regulator
    action, litigation filed, company acknowledgment, credible NGO campaign) and <b>indicative</b>
    (media salience composite). Company–article links enter the incident layer only when the
    company's Event Registry concept salience score is ≥ 4 or the company is named in the
    article title — passing mentions (bystanders) stay in the raw recall layer
    (__N_DROPPED__ low-salience links currently held back) and form no incidents. Salience
    z-scores are computed per company against its own 90-day
    median/MAD baseline of controversy-relevant coverage and stay inactive
    (<span class="thin">THIN</span>) until 30 days of history accumulate — silence before that is
    never treated as clean. State changes (WATCH → FLAGGED → EXCLUDED) are human decisions recorded
    with full evidence chains; this dashboard is read-only. Channel B clusters orphan incidents
    into emergent themes by shared concept signatures (top-8 concepts, generic concepts above 8%
    document frequency excluded, ≥3 shared to merge); escalation scores are uncalibrated priors
    until the replay harness yields labelled outcomes. Company impact is scored on three
    separate axes that are NEVER summed: <b>SEV</b> (issue substance, size-adjusted via the
    company's own baseline — raw volume is never an input), <b>OUTRAGE</b> (raw visibility ×
    moral charge of the issue — the sustainable-fund headline risk), and <b>MAT</b> (expected
    share-price response in CAR bps; shown as UNCAL until the FMP event-study calibration
    produces an evidence table — never zero-filled). Parameter register v__PARAM_VERSION__
    (config/parameters.yaml); thresholds: z_watch 2.0 / z_flag 3.0 / z_exclude 4.0.
    Sweep: newsapi.ai batched concept queries, 40-keyword server-side recall filter.
  </footer>
</main>
<script id="cm-data" type="application/json">__JSON__</script>
<script>
const D = JSON.parse(document.getElementById('cm-data').textContent);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

/* --- review queue --- */
const layerOf = i => i.issue ? i.layer : 'orphan';
let fLayer = 'all', fIssue = '', fQ = '';
const issues = [...new Set(D.incidents.map(i => i.issue).filter(Boolean))].sort();
const sel = document.getElementById('issue-filter');
issues.forEach(i => sel.add(new Option(i, i)));

function renderQueue(){
  const rows = D.incidents.filter(i =>
    (fLayer === 'all' || layerOf(i) === fLayer) &&
    (!fIssue || i.issue === fIssue) &&
    (!fQ || i.company.toLowerCase().includes(fQ)));
  document.getElementById('queue-count').textContent = rows.length + ' SHOWN';
  const el = document.getElementById('queue');
  if (!rows.length){ el.innerHTML = '<div class="empty">NO INCIDENTS MATCH FILTER</div>'; return; }
  el.innerHTML = rows.map(i => `
    <article class="incident ${layerOf(i)}" tabindex="0" role="button" aria-expanded="false" data-id="${i.id}">
      <div class="i-top">
        <span class="i-co">${esc(i.company)}</span>
        <span class="i-meta">${i.n_articles} ART · ${i.n_sources} SRC · ${esc(i.first_seen)}${i.last_seen !== i.first_seen ? ' → ' + esc(i.last_seen) : ''}</span>
      </div>
      <div class="i-tags">
        <span class="pill ${layerOf(i)}">${layerOf(i).toUpperCase()}</span>
        ${i.issue ? `<span class="pill issue">${esc(i.issue)}</span>` : '<span class="pill">UNCLASSIFIED</span>'}
        ${i.triggers.map(t => `<span class="pill">${esc(t.replace(/_/g,' '))}</span>`).join('')}
      </div>
      <div class="i-title">${esc(i.titles[0] || '')}</div>
      <div class="i-detail"><ul>
        ${i.links.map(l => `<li><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.title)}</a> <span class="src">${esc(l.source || '')}</span></li>`).join('')}
      </ul></div>
    </article>`).join('');
}
document.getElementById('queue').addEventListener('click', e => {
  const card = e.target.closest('.incident');
  if (card && !e.target.closest('a')) toggleCard(card);
});
document.getElementById('queue').addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('incident')){
    e.preventDefault(); toggleCard(e.target);
  }
});
function toggleCard(c){
  c.classList.toggle('open');
  c.setAttribute('aria-expanded', c.classList.contains('open'));
}
document.querySelectorAll('.tab').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.setAttribute('aria-pressed', 'false'));
  b.setAttribute('aria-pressed', 'true');
  fLayer = b.dataset.layer; renderQueue();
}));
sel.addEventListener('change', () => { fIssue = sel.value; renderQueue(); });
document.getElementById('q').addEventListener('input', e => {
  fQ = e.target.value.trim().toLowerCase(); renderQueue();
});

/* --- issue bars --- */
const maxI = Math.max(...D.issues.map(i => i.n), 1);
document.getElementById('issue-bars').innerHTML = D.issues.map(i => `
  <div class="bar-row ${i.issue === 'ORPHAN' ? 'orphan' : ''}">
    <span class="n" title="${esc(i.issue)}">${esc(i.issue)}</span>
    <span class="bar"><i style="width:${(i.n / maxI * 100).toFixed(1)}%"></i></span>
    <span class="c">${i.n}</span>
  </div>`).join('');

/* --- trend chart --- */
(function(){
  const t = D.trend, el = document.getElementById('trend');
  if (!t.length){ el.innerHTML = '<div class="empty">NO DATA</div>'; return; }
  const W = 320, H = 110, P = 8;
  const max = Math.max(...t.map(d => d.articles), 1);
  const pts = t.map((d, i) => [P + i * (W - 2*P) / Math.max(t.length - 1, 1),
                               H - P - (d.articles / max) * (H - 2*P)]);
  let inner;
  if (t.length === 1){
    const [x, y] = pts[0];
    inner = `<line class="axis" x1="${P}" y1="${H-P}" x2="${W-P}" y2="${H-P}"/>
             <circle cx="${(W/2).toFixed(1)}" cy="${y.toFixed(1)}" r="4" fill="var(--cyan)"/>
             <text x="${W/2}" y="${(y-10).toFixed(1)}" fill="var(--txt)" font-size="11"
               text-anchor="middle" font-family="inherit">${t[0].articles}</text>`;
  } else {
    const line = pts.map(p => p.map(v => v.toFixed(1)).join(',')).join(' ');
    inner = `<polygon class="area" points="${P},${H-P} ${line} ${W-P},${H-P}"/>
             <polyline class="line" points="${line}"/>
             <line class="axis" x1="${P}" y1="${H-P}" x2="${W-P}" y2="${H-P}"/>`;
  }
  el.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Daily controversy-tagged articles">${inner}</svg>
    <div class="cap">${esc(t[0].date)} → ${esc(t[t.length-1].date)} · ARTICLES/DAY · BASELINE ACCUMULATING (30d)</div>`;
})();

/* --- themes board (Channel B) --- */
document.getElementById('theme-count').textContent =
  D.themes.length ? D.themes.length + ' LIVE' : '';
document.getElementById('themes').innerHTML = D.themes.length ? D.themes.map(t => `
  <div class="theme-item">
    <div class="th-top">
      <span class="th-label">${esc(t.label)}</span>
      <span class="th-score ${t.thin ? 'thin' : ''}">${t.thin ? 'THIN' : t.score.toFixed(0)}</span>
    </div>
    <div class="th-bar"><i style="width:${Math.min(t.score, 100).toFixed(0)}%"></i></div>
    <div class="th-meta">
      <span class="pill ${esc(t.status)}">${esc(t.status.toUpperCase())}</span>
      ${t.promoted ? `<span class="pill promoted">→ ${esc(t.promoted)}</span>` : ''}
      <span>${t.n_events} EVT · ${t.n_companies} CO · ${t.days_alive}D</span>
    </div>
    ${t.companies.length ? `<div class="th-co">${esc(t.companies.join(' · '))}</div>` : ''}
    ${t.top_terms.length ? `<div class="th-terms">DRIVERS: ${t.top_terms.map(x => esc(x.feature) + ' +' + x.contribution.toFixed(2)).join(' · ')}</div>` : ''}
  </div>`).join('') : '<div class="empty">NO THEMES YET — ORPHAN FEED ACCUMULATING</div>';

/* --- orphan feed --- */
document.getElementById('orphans').innerHTML = D.orphans.length ? D.orphans.map(o => `
  <div class="orphan-item">
    <div class="oc">${esc(o.company)} · ${o.n_articles} ART · ${o.n_sources} SRC</div>
    <div class="ot">${esc(o.titles[0] || '')}</div>
  </div>`).join('') : '<div class="empty">NO ORPHANS</div>';

/* --- company table --- */
let sortK = 'n_articles', sortDir = -1;

function termLine(label, blk){
  if (!blk || !blk.terms) return '';
  const terms = blk.terms.filter(t => t.contribution).sort((a, b) => b.contribution - a.contribution)
    .map(t => `${esc(t.feature)} ${t.contribution >= 0 ? '+' : ''}${t.contribution.toFixed(2)}`);
  return `<span><b>${label}</b> intercept ${blk.intercept} · ${terms_join(terms)}</span>`;
}
function terms_join(t){ return t.length ? t.join(' · ') : 'no active terms'; }

function coDetail(c){
  const d = c.dec;
  let decHtml = '';
  if (d && d.scores){
    const mat = d.scores.materiality;
    decHtml = `<div class="dec">
      ${termLine('SEV', d.scores.severity)}
      ${termLine('OUTRAGE', d.scores.outrage)}
      <span><b>MAT</b> ${mat && mat.status === 'uncalibrated'
        ? 'UNCAL — no event-study CAR table yet'
        : esc((mat && mat.status) || '—')}</span>
    </div>`;
  }
  if (!c.inc || !c.inc.length)
    return decHtml + '<div class="thin">NO SUBJECT INCIDENTS ON RECORD</div>';
  const incHtml = c.inc.map(i => {
    const layer = i.issue ? i.layer : 'orphan';
    return `<div class="co-inc ${layer}">
      <div class="i-tags">
        <span class="pill ${layer}">${layer.toUpperCase()}</span>
        ${i.issue ? `<span class="pill issue">${esc(i.issue)}</span>` : '<span class="pill">UNCLASSIFIED</span>'}
        ${i.triggers.map(t => `<span class="pill">${esc(t.replace(/_/g,' '))}</span>`).join('')}
        <span class="i-meta">${i.n_articles} ART · ${i.n_sources} SRC · ${esc(i.first_seen)}${i.last_seen !== i.first_seen ? ' → ' + esc(i.last_seen) : ''}</span>
      </div>
      <ul>${i.links.map(l => `<li><a href="${esc(l.url)}" target="_blank" rel="noopener noreferrer">${esc(l.title)}</a> <span class="src">${esc(l.source || '')}</span></li>`).join('')}</ul>
    </div>`;
  }).join('');
  return decHtml + incHtml;
}

function renderTable(){
  const rows = [...D.companies].sort((a, b) => {
    const x = a[sortK], y = b[sortK];
    return (typeof x === 'string' ? String(x).localeCompare(String(y)) : (x ?? -1) - (y ?? -1)) * sortDir;
  });
  document.getElementById('co-count').textContent = rows.length + ' COMPANIES';
  document.querySelector('#co-table tbody').innerHTML = rows.map(c => `
    <tr class="co-row" tabindex="0" aria-expanded="false">
      <td>${esc(c.name)}</td><td>${esc(c.sector || '')}</td><td>${esc(c.country || '')}</td>
      <td class="num">${c.n_articles}</td><td class="num">${c.n_sources}</td>
      <td class="num">${c.n_incidents}</td>
      <td class="num">${c.thin ? '<span class="thin">THIN</span>' : (c.z == null ? '—' : c.z.toFixed(2))}</td>
      <td class="num">${c.sev == null ? '—' : `<span class="${c.sev >= 60 ? 'score-high' : c.sev >= 40 ? 'score-mid' : ''}">${c.sev.toFixed(0)}</span>${c.score_thin ? '<span class="thin">*</span>' : ''}`}</td>
      <td class="num">${c.out == null ? '—' : `<span class="${c.out >= 60 ? 'score-high' : c.out >= 40 ? 'score-mid' : ''}">${c.out.toFixed(0)}</span>`}</td>
      <td class="num" title="${c.mat == null ? 'uncalibrated — no event-study CAR table yet' : 'expected CAR bps'}">${c.mat == null ? '<span class="thin">UNCAL</span>' : c.mat.toFixed(0)}</td>
      <td><span class="state state-${esc(c.state)}">${esc(c.state)}</span></td>
    </tr>
    <tr class="co-detail" style="display:none"><td colspan="11">${coDetail(c)}</td></tr>`).join('');
  document.querySelectorAll('#co-table th').forEach(th =>
    th.classList.toggle('sorted', th.dataset.k === sortK));
}
function toggleCoRow(tr){
  const d = tr.nextElementSibling;
  if (!d || !d.classList.contains('co-detail')) return;
  const open = d.style.display === 'none';
  d.style.display = open ? '' : 'none';
  tr.setAttribute('aria-expanded', open);
}
document.querySelector('#co-table tbody').addEventListener('click', e => {
  const tr = e.target.closest('tr.co-row');
  if (tr && !e.target.closest('a')) toggleCoRow(tr);
});
document.querySelector('#co-table tbody').addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.classList.contains('co-row')){
    e.preventDefault(); toggleCoRow(e.target);
  }
});
document.querySelectorAll('#co-table th').forEach(th => th.addEventListener('click', () => {
  const k = th.dataset.k;
  if (sortK === k) sortDir *= -1; else { sortK = k; sortDir = -1; }
  renderTable();
}));

renderQueue(); renderTable();
</script>
</body>
</html>
"""


def render(data: dict) -> str:
    m = data["meta"]
    html = TEMPLATE
    html = html.replace("__DATA_DATE__", m["data_date"])
    html = html.replace("__GENERATED_AT__", m["generated_at"])
    html = html.replace("__N_MAPPED__", str(m["n_mapped"]))
    html = html.replace("__N_SEEN__", str(m["n_companies_seen"]))
    html = html.replace("__N_ARTICLES__", str(m["n_articles"]))
    html = html.replace("__N_INCIDENTS__", str(m["n_incidents"]))
    html = html.replace("__N_CONFIRMED__", str(m["n_confirmed"]))
    html = html.replace("__N_ORPHANS__", str(m["n_orphans"]))
    html = html.replace("__N_DROPPED__", str(m.get("n_links_dropped", 0)))
    html = html.replace("__PARAM_VERSION__", m["param_version"])
    if data["ticker"]:
        import html as _h
        items = "".join(f"<span><b>CONFIRMED</b>{_h.escape(t)}</span>"
                        for t in data["ticker"])
        html = html.replace("__TICKER__",
                            f'<div class="ticker" aria-label="Confirmed incident headlines">'
                            f'<div class="ticker-track">{items}{items}</div></div>')
    else:
        html = html.replace("__TICKER__", "")
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    html = html.replace("__JSON__", payload)
    return html


def main():
    db = ROOT / "data" / "controversy.db"
    if not db.exists():
        sys.exit(f"database not found: {db} — run the sweep first")
    OUT.mkdir(exist_ok=True)
    (OUT / ".nojekyll").touch()
    data = collect(db)
    (OUT / "index.html").write_text(render(data), encoding="utf-8")
    print(f"dashboard written: {OUT/'index.html'} "
          f"({len(data['incidents'])} queue rows, {len(data['companies'])} companies, "
          f"{len(data['ticker'])} ticker items)")


if __name__ == "__main__":
    main()

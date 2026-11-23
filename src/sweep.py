# Sweep v2 (shadow mode): batch-ranged, paginated, idempotent. See docstring.
"""Daily sweep orchestration (shadow mode v1: WATCH/FLAG only; exclusions are
human decisions recorded via the dashboard/CLI, never automated in v1).

Sweep design (token-budgeted):
1. Batched concept queries over the mapped universe (default 30 concepts/batch;
   the API caps a query at 80 terms — 30 concepts + 40 sweep keywords = 70).
2. Server-side controversy filter: single-word keywords only (every word counts
   toward the limit). Recall layer; precision happens client-side.
3. Pagination: up to 3 pages/batch (1 token per page).
4. Articles persisted idempotently (INSERT OR IGNORE) — batch ranges let an
   interrupted sweep resume without token double-spend.
5. Baseline note: the ingested stream is controversy-filtered, so salience
   baselines are baselines of *controversy-relevant* coverage — the right
   denominator for controversy salience, documented in the parameter register.
"""
from datetime import date, timedelta


def chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def run_sweep(client, conn, sweep_keywords, taxonomy_issues, batch_size: int = 30,
              batch_start: int = 0, batch_end: int | None = None,
              max_pages: int = 3, target_date: str | None = None,
              min_history: int = 30, baseline_window: int = 90,
              page_cost: int = 1, graceful_quota: bool = False) -> dict:
    """page_cost: tokens charged per search call (1 = recent window, 5 =
    archive per searched year). On BudgetExceeded the sweep stops mid-day,
    SKIPS the roll-ups (a partial day must not be rolled up) and returns with
    stats['budget_exceeded']=True so the caller can checkpoint and resume at
    stats['batches_done'].
    graceful_quota: replay drivers set True so a server-side quota stop
    (401/402/403) is treated exactly like BudgetExceeded (checkpoint, no
    roll-up). The daily sweep leaves it False — a blind monitor must fail
    loudly, not quietly."""
    import json
    import requests
    from .er_client import BudgetExceeded, company_concept_query, is_quota_error
    from .incidents import company_core_names
    from .scoring import assign_issue, detect_confirmed_triggers

    day = target_date or (date.today() - timedelta(days=1)).isoformat()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, concept_uri FROM companies "
        "WHERE map_status='auto_mapped'").fetchall()]
    for r in rows:
        r["_cores"] = company_core_names(r["name"])
    batches = list(chunk(rows, batch_size))
    if batch_end is None:
        batch_end = len(batches)

    stats = {"batches_done": 0, "articles": 0, "incidents": 0, "tokens_start": client.spent}
    budget_hit = False
    for bi, batch in enumerate(batches[batch_start:batch_end], start=batch_start):
        q = {"$and": [
            company_concept_query([r["concept_uri"] for r in batch]),
            {"$or": [{"keyword": kw} for kw in sweep_keywords]},
            {"dateStart": day, "dateEnd": day},
        ]}
        try:
            for page in range(1, max_pages + 1):
                js = client.search_articles(q, count=100, page=page,
                                            cost=page_cost)
                arts = js.get("articles", {})
                results = arts.get("results", [])
                for a in results:
                    stats["articles"] += 1
                    text = f"{a.get('title','')} {a.get('body','')[:1500]}"
                    issue = assign_issue(text, taxonomy_issues)
                    triggers = detect_confirmed_triggers(a.get("title", ""), a.get("body", ""))
                    conn.execute(
                        "INSERT OR IGNORE INTO articles (uri,date,lang,title,url,source,sentiment,event_uri,raw_json)"
                        " VALUES (?,?,?,?,?,?,?,?,?)",
                        (a.get("uri"), a.get("date"), a.get("lang"), a.get("title"), a.get("url"),
                         (a.get("source") or {}).get("title"), a.get("sentiment"),
                         a.get("eventUri"), json.dumps(a)))
                    title_l = (a.get("title") or "").lower()
                    for c in (a.get("concepts") or []):
                        hit = next((r for r in batch if r["concept_uri"] == c.get("uri")), None)
                        if hit:
                            tm = 1 if any(cn and cn in title_l
                                          for cn in hit["_cores"]) else 0
                            conn.execute(
                                "INSERT OR IGNORE INTO article_companies"
                                " (article_uri, company_id, score, title_match)"
                                " VALUES (?,?,?,?)",
                                (a.get("uri"), hit["id"], c.get("score"), tm))
                            if issue or triggers:
                                stats["incidents"] += 1
                conn.commit()
                if len(results) < 100:
                    break  # last page
        except BudgetExceeded:
            budget_hit = True
            conn.commit()
            break
        except requests.HTTPError as e:
            if graceful_quota and is_quota_error(e):
                print(f"quota stop ({e.response.status_code}) mid-day "
                      f"{day} at batch {bi} — checkpointing", flush=True)
                budget_hit = True
                conn.commit()
                break
            raise
        stats["batches_done"] += 1
        print(f"batch {bi}: articles={stats['articles']} tokens={client.spent}", flush=True)

    stats["tokens_used"] = client.spent - stats["tokens_start"]
    if budget_hit:
        # Partial day: caller checkpoints batches_done and resumes later.
        # Roll-ups are deliberately skipped — never roll up a partial day.
        stats["budget_exceeded"] = True
        return stats
    stats["budget_exceeded"] = False
    stats.update(rollup_day(conn, day, taxonomy_issues, min_history,
                            baseline_window))
    return stats


def rollup_day(conn, day: str, taxonomy_issues, min_history: int = 30,
               baseline_window: int = 90) -> dict:
    """Post-sweep roll-ups (idempotent, local — zero API tokens):
    1. articles -> incidents (the unit the state machine decides on)
    2. per-company daily counts + z-scores (baseline accumulation)
    3. orphan events -> emergent themes (Channel B lifecycle + escalation)
    4. company impact scores (severity / outrage / materiality — never summed)
    Exposed separately so a replay driver can roll up a day that completed
    across resumed batch-range calls."""
    from .incidents import rebuild_incidents, update_daily_counts
    from .config import params as _params
    _p = _params()
    _min_score = _p.get("attach", {}).get("min_concept_score", 4)
    out = {}
    out["incidents_built"] = rebuild_incidents(conn, taxonomy_issues,
                                               _min_score)
    out["daily"] = update_daily_counts(conn, day, min_history,
                                       baseline_window, _min_score)
    from .themes import rebuild_themes
    out["themes"] = rebuild_themes(conn, day, _p["themes"])
    from .impact import rebuild_company_scores
    out["impact"] = rebuild_company_scores(
        conn, day, _p["impact"], taxonomy_issues, _p["themes"],
        min_score=_min_score)
    print(f"roll-up: {out['incidents_built']} daily={out['daily']} "
          f"themes={out['themes']} impact={out['impact']}", flush=True)
    return out

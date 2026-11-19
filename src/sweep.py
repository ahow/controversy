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
              min_history: int = 30, baseline_window: int = 90) -> dict:
    import json
    from .er_client import company_concept_query
    from .incidents import rebuild_incidents, update_daily_counts
    from .scoring import assign_issue, detect_confirmed_triggers

    day = target_date or (date.today() - timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT id, concept_uri FROM companies WHERE map_status='auto_mapped'").fetchall()
    batches = list(chunk(rows, batch_size))
    if batch_end is None:
        batch_end = len(batches)

    stats = {"batches_done": 0, "articles": 0, "incidents": 0, "tokens_start": client.spent}
    for bi, batch in enumerate(batches[batch_start:batch_end], start=batch_start):
        q = {"$and": [
            company_concept_query([r["concept_uri"] for r in batch]),
            {"$or": [{"keyword": kw} for kw in sweep_keywords]},
            {"dateStart": day, "dateEnd": day},
        ]}
        for page in range(1, max_pages + 1):
            js = client.search_articles(q, count=100, page=page)
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
                for c in (a.get("concepts") or []):
                    hit = next((r for r in batch if r["concept_uri"] == c.get("uri")), None)
                    if hit:
                        conn.execute("INSERT OR IGNORE INTO article_companies VALUES (?,?)",
                                     (a.get("uri"), hit["id"]))
                        if issue or triggers:
                            stats["incidents"] += 1
            conn.commit()
            if len(results) < 100:
                break  # last page
        stats["batches_done"] += 1
        print(f"batch {bi}: articles={stats['articles']} tokens={client.spent}", flush=True)

    # Post-sweep roll-ups (idempotent, local — zero API tokens):
    # 1. articles -> incidents (the unit the state machine decides on)
    # 2. per-company daily counts + z-scores (baseline accumulation)
    stats["incidents_built"] = rebuild_incidents(conn, taxonomy_issues)
    stats["daily"] = update_daily_counts(conn, day, min_history, baseline_window)
    print(f"roll-up: {stats['incidents_built']} daily={stats['daily']}", flush=True)
    stats["tokens_used"] = client.spent - stats["tokens_start"]
    return stats

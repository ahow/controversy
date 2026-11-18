"""Daily sweep orchestration (shadow mode v1: WATCH/FLAG only; exclusions are
human decisions recorded via the dashboard/CLI, never automated in v1).

Sweep design (token-budgeted):
1. Batched concept queries over the mapped universe (~50 concepts per call),
   filtered to controversy-relevant content, yesterday's articles.
2. Articles -> incidents via native event clustering (event_uri).
3. Baselines updated from own ingested stream; indicative score computed.
4. Confirmed-layer triggers checked per incident.
5. State transitions proposed with full evidence; logged to audit table.
"""
from datetime import date, timedelta


def chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def run_sweep(client, conn, params, taxonomy, batch_size: int = 50) -> dict:
    from .er_client import company_concept_query
    from .scoring import assign_issue, detect_confirmed_triggers

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT id, concept_uri FROM companies WHERE map_status='auto_mapped'").fetchall()
    kw_union = sorted({kw for i in taxonomy for kw in i["keywords_en"]})

    stats = {"batches": 0, "articles": 0, "incidents": 0, "tokens": 0}
    for batch in chunk(rows, batch_size):
        q = {"$and": [
            company_concept_query([r["concept_uri"] for r in batch]),
            {"$or": [{"keyword": kw} for kw in kw_union]},
            {"dateStart": yesterday, "dateEnd": yesterday},
        ]}
        js = client.search_articles(q, count=100)
        arts = js.get("articles", {})
        stats["batches"] += 1
        stats["tokens"] = client.spent
        for a in arts.get("results", []):
            stats["articles"] += 1
            text = f"{a.get('title','')} {a.get('body','')[:1500]}"
            issue = assign_issue(text, taxonomy)
            triggers = detect_confirmed_triggers(a.get("title", ""), a.get("body", ""))
            # persistence + incident assembly handled by store layer (v1: raw insert)
            conn.execute(
                "INSERT OR IGNORE INTO articles (uri,date,lang,title,url,source,sentiment,event_uri,raw_json)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (a.get("uri"), a.get("date"), a.get("lang"), a.get("title"), a.get("url"),
                 (a.get("source") or {}).get("title"), a.get("sentiment"),
                 a.get("eventUri"), __import__("json").dumps(a)))
            for c in (a.get("concepts") or []):
                hit = next((r for r in batch if r["concept_uri"] == c.get("uri")), None)
                if hit:
                    conn.execute("INSERT OR IGNORE INTO article_companies VALUES (?,?)",
                                 (a.get("uri"), hit["id"]))
            if issue or triggers:
                stats["incidents"] += 1
        conn.commit()
    return stats

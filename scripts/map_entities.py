#!/usr/bin/env python3
"""One-time (per universe change) entity mapping.
Usage: python scripts/map_entities.py --universe data/universe.csv [--limit N] [--workers 4]

- Resumable: rows already mapped in the DB (auto_mapped / needs_review) are skipped,
  so an interrupted run can simply be re-launched.
- --workers uses bounded concurrency (API hard limit is 5 concurrent requests;
  we default to 4). Each worker gets its own HTTP session.
Outputs: SQLite companies table + data/entity_qa_queue.csv for human review.
"""
import argparse, csv, os, sys, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_env, ROOT
from src.er_client import ERClient
from src.universe import load_universe
from src.entity_mapper import map_company
from src.store import connect

_write_lock = threading.Lock()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--db", default=str(ROOT / "data" / "controversy.db"))
    args = ap.parse_args()

    load_env()
    api_key = os.environ["NEWSAPI_AI_KEY"]
    conn = connect(args.db, check_same_thread=False)  # writes are lock-guarded across workers
    companies = load_universe(args.universe)
    if args.limit:
        companies = companies[:args.limit]

    done = {r["id"] for r in conn.execute(
        "SELECT id FROM companies WHERE map_status IN ('auto_mapped','needs_review')")}
    todo = [c for c in companies if c["row"] not in done]
    print(f"universe {len(companies)}; already mapped {len(done)}; to do {len(todo)}", flush=True)

    counters = {"done": 0}

    def work(comp):
        client = ERClient(api_key, token_budget=0)  # suggest endpoint is free
        res = map_company(client, comp)
        with _write_lock:
            conn.execute(
                "INSERT OR REPLACE INTO companies (id,name,ticker,isin,lei,country,sector,concept_uri,map_status)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (comp["row"], comp["name"], comp.get("ticker"), comp.get("isin"), comp.get("lei"),
                 comp.get("country"), comp.get("sector"), res.concept_uri, res.status))
            conn.commit()
            counters["done"] += 1
            if counters["done"] % 50 == 0:
                print(f"progress {counters['done']}/{len(todo)}", flush=True)
        return res

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for res in ex.map(work, todo):
            results.append(res)

    qa = [{"row": c["row"], "name": c["name"], "status": r.status, "reason": r.reason,
           "candidates": " | ".join(f"{x['label']} <{x['uri']}>" for x in r.candidates)}
          for c, r in zip(todo, results) if r.status != "auto_mapped"]
    out = ROOT / "data" / "entity_qa_queue.csv"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["row", "name", "status", "reason", "candidates"])
        w.writeheader(); w.writerows(qa)
    auto = sum(1 for r in results if r.status == "auto_mapped")
    print(f"Done this run: {auto} auto-mapped, {len(qa)} flagged. QA queue -> {out}", flush=True)


if __name__ == "__main__":
    main()

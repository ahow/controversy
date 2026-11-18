#!/usr/bin/env python3
"""One-time (per universe change) entity mapping.
Usage: python scripts/map_entities.py --universe data/universe.csv [--limit N]
Writes results to SQLite and exports a QA queue CSV for human review.
"""
import argparse, csv, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_env, ROOT
from src.er_client import ERClient
from src.universe import load_universe
from src.entity_mapper import map_company
from src.store import connect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--db", default=str(ROOT / "data" / "controversy.db"))
    args = ap.parse_args()

    load_env()
    client = ERClient(os.environ["NEWSAPI_AI_KEY"], token_budget=0)  # suggest is free
    conn = connect(args.db)
    companies = load_universe(args.universe)
    if args.limit:
        companies = companies[:args.limit]

    qa_rows = []
    for i, comp in enumerate(companies):
        res = map_company(client, comp)
        conn.execute(
            "INSERT OR REPLACE INTO companies (id,name,ticker,isin,lei,country,sector,concept_uri,map_status)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (comp["row"], comp["name"], comp.get("ticker"), comp.get("isin"), comp.get("lei"),
             comp.get("country"), comp.get("sector"), res.concept_uri, res.status))
        if res.status != "auto_mapped":
            qa_rows.append({"row": comp["row"], "name": comp["name"], "status": res.status,
                            "reason": res.reason,
                            "candidates": " | ".join(f"{c['label']} <{c['uri']}>" for c in res.candidates)})
        if (i + 1) % 100 == 0:
            conn.commit()
            print(f"{i+1}/{len(companies)} mapped...", flush=True)
    conn.commit()

    out = ROOT / "data" / "entity_qa_queue.csv"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["row", "name", "status", "reason", "candidates"])
        w.writeheader(); w.writerows(qa_rows)
    auto = len(companies) - len(qa_rows)
    print(f"Done: {auto} auto-mapped, {len(qa_rows)} in QA queue -> {out}")


if __name__ == "__main__":
    main()

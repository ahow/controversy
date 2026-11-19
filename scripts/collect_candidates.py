#!/usr/bin/env python3
"""Deep candidate pass over unmapped companies.

Unlike the first-pass mapper, this NEVER early-stops: it queries every alias and
collects ALL org and wiki candidates so a human can select the right one.
Promotes to auto_mapped only on an unambiguous exact org match; subsidiary->parent
URI collisions become needs_review; candidates are stored on the company row
(candidates_json) for QA review. Rows are only marked processed ('[]') when at
least one API call succeeded — transient failures stay eligible for future passes.

Usage: python scripts/collect_candidates.py [--workers 4] [--limit N]
Env: ER_SUGGEST_TIMEOUT (default 60), ER_SUGGEST_RETRIES (default 2)
"""
import argparse, json, os, sys, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_env, ROOT
from src.er_client import ERClient
from src.universe import load_universe
from src.entity_mapper import _norm
from src.store import connect

_lock = threading.Lock()


def collect(client, company):
    """Exhaustive over aliases; keeps org+wiki candidates. Returns (candidates, any_ok)."""
    seen, any_ok = {}, False
    for alias in company["aliases"]:
        try:
            for c in client.suggest_concepts(alias):
                uri = c.get("uri") or ""
                label = (c.get("label") or {}).get("eng", "")
                ctype = c.get("type", "")
                if uri and uri not in seen and ctype in ("org", "wiki"):
                    seen[uri] = {"uri": uri, "label": label, "type": ctype, "via": alias}
            any_ok = True
        except Exception:
            continue  # transient: skip this alias, keep going
    return seen, any_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default="/mnt/agents/upload/MSCI_ACWI.csv")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--db", default=str(ROOT / "data" / "controversy.db"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    load_env()
    api_key = os.environ["NEWSAPI_AI_KEY"]
    conn = connect(args.db, check_same_thread=False)
    # candidates_json column (idempotent)
    try:
        conn.execute("ALTER TABLE companies ADD COLUMN candidates_json TEXT")
        conn.commit()
    except Exception:
        pass

    comps = {c["row"]: c for c in load_universe(args.universe)}
    todo_rows = [r[0] for r in conn.execute(
        "SELECT id FROM companies WHERE map_status='unmapped' AND candidates_json IS NULL")]
    todo = [comps[i] for i in sorted(todo_rows) if i in comps]
    if args.limit:
        todo = todo[:args.limit]
    print(f"deep pass: {len(todo)} unmapped companies to process", flush=True)

    counters = {"done": 0, "promoted": 0, "with_cands": 0}

    def work(comp):
        client = ERClient(api_key, token_budget=0)
        seen, any_ok = collect(client, comp)
        orgs = [c for c in seen.values() if c["type"] == "org"]
        norms = {_norm(a) for a in comp["aliases"]}
        exact = [c for c in orgs if _norm(c["label"]) in norms]
        with _lock:
            if len(exact) == 1:
                try:
                    conn.execute("UPDATE companies SET map_status='auto_mapped', concept_uri=?, candidates_json=? WHERE id=?",
                                 (exact[0]["uri"], json.dumps(list(seen.values())), comp["row"]))
                    counters["promoted"] += 1
                except Exception:
                    # concept URI already claimed by another issuer (subsidiary->parent
                    # collision) — record candidates and flag for review instead
                    conn.execute("UPDATE companies SET map_status='needs_review', candidates_json=? WHERE id=?",
                                 (json.dumps(list(seen.values())), comp["row"]))
            elif seen:
                conn.execute("UPDATE companies SET candidates_json=? WHERE id=?",
                             (json.dumps(list(seen.values())), comp["row"]))
                counters["with_cands"] += 1
            elif any_ok:
                # genuinely no candidates — mark processed so we don't retry forever
                conn.execute("UPDATE companies SET candidates_json='[]' WHERE id=?", (comp["row"],))
            # else: all calls failed transiently — leave NULL for a future pass
            conn.commit()
            counters["done"] += 1
            if counters["done"] % 50 == 0:
                print(f"progress {counters['done']}/{len(todo)} "
                      f"(promoted {counters['promoted']}, candidates {counters['with_cands']})", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(work, todo))
    print(f"DONE: {counters['done']} processed, {counters['promoted']} promoted to auto_mapped, "
          f"{counters['with_cands']} with candidates for review", flush=True)


if __name__ == "__main__":
    main()

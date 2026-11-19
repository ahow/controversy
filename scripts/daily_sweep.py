#!/usr/bin/env python3
"""Daily shadow-mode sweep.
Usage: python scripts/daily_sweep.py [--batch-start N] [--batch-end M] [--batch-size 30]
Cron example: 0 6 * * * cd /path/to/controversy && python scripts/daily_sweep.py
Batch args let an interrupted sweep resume without token double-spend.
"""
import argparse, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_env, params, taxonomy, ROOT
import yaml
from src.er_client import ERClient
from src.store import connect
from src.sweep import run_sweep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-start", type=int, default=0)
    ap.add_argument("--batch-end", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=30)
    args = ap.parse_args()

    load_env()
    p = params()
    tax = yaml.safe_load((ROOT / "config" / "taxonomy.yaml").read_text())
    sweep_kw = tax["sweep_keywords"]

    client = ERClient(os.environ["NEWSAPI_AI_KEY"],
                      token_budget=p["budgets"]["daily_sweep_token_target"] * 3)
    conn = connect(ROOT / "data" / "controversy.db", check_same_thread=False)
    stats = run_sweep(client, conn, sweep_kw, tax["issues"],
                      batch_size=args.batch_size,
                      batch_start=args.batch_start,
                      batch_end=args.batch_end or None)
    print(f"sweep done: {stats}")
    if stats["tokens_used"] > p["budgets"]["daily_sweep_token_target"]:
        print(f"WARNING: token burn {stats['tokens_used']} above daily target "
              f"{p['budgets']['daily_sweep_token_target']}")


if __name__ == "__main__":
    main()

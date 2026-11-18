#!/usr/bin/env python3
"""Daily shadow-mode sweep.
Usage: python scripts/daily_sweep.py
Cron example: 0 6 * * * cd /path/to/controversy && python scripts/daily_sweep.py
"""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_env, params, taxonomy, ROOT
from src.er_client import ERClient
from src.store import connect
from src.sweep import run_sweep


def main():
    load_env()
    p = params()
    client = ERClient(os.environ["NEWSAPI_AI_KEY"],
                      token_budget=p["budgets"]["daily_sweep_token_target"] * 2)
    conn = connect(ROOT / "data" / "controversy.db")
    stats = run_sweep(client, conn, p, taxonomy())
    print(f"sweep done: {stats}")
    if stats["tokens"] > p["budgets"]["daily_sweep_token_target"]:
        print(f"WARNING: token burn {stats['tokens']} above daily target "
              f"{p['budgets']['daily_sweep_token_target']}")


if __name__ == "__main__":
    main()

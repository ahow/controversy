#!/usr/bin/env python3
"""Historical replay harness — generates the labelled training set for the
escalation scorecard.

Replays the daily sweep over an archive date range (run_sweep's target_date),
with the theme roll-up running automatically after each day. State is
checkpointed to data/replay_state.json so an interrupted replay resumes
without token double-spend. Token pacing honours the daily budget; the run
stops cleanly on BudgetExceeded and resumes the next day.

    python scripts/replay_themes.py --start 2024-01-01 --end 2024-03-31
    python scripts/replay_themes.py --resume
    python scripts/replay_themes.py --start ... --end ... --dry-run

NOTE: replaying history requires Event Registry ARCHIVE access on the API
plan and costs tokens (~80-120 per replayed day). The point of the harness is
that replay is what turns 'which emerging issues became high-profile' from
anecdote into a labelled dataset (see src/calibration.py).
"""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_env, params, taxonomy, ROOT
import yaml
from src.er_client import ERClient, BudgetExceeded
from src.store import connect
from src.sweep import run_sweep

STATE = ROOT / "data" / "replay_state.json"


def _load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def _save_state(s: dict) -> None:
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(s, indent=2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", help="YYYY-MM-DD first archive day")
    ap.add_argument("--end", help="YYYY-MM-DD last archive day")
    ap.add_argument("--resume", action="store_true",
                    help="continue from data/replay_state.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only: no API calls, no DB writes")
    ap.add_argument("--batch-size", type=int, default=30)
    args = ap.parse_args()

    state = _load_state() if args.resume else {}
    start = args.start or state.get("start")
    end = args.end or state.get("end")
    if not start or not end:
        sys.exit("provide --start/--end (or --resume with a saved state)")
    done_to = state.get("done_to")

    days = []
    d = date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        days.append(d.isoformat())
        d += timedelta(days=1)
    if done_to:
        days = [x for x in days if x > done_to]
    print(f"replay plan: {len(days)} days ({days[0] if days else '—'} .. "
          f"{days[-1] if days else '—'})")
    if args.dry_run:
        est = len(days) * params()["budgets"]["daily_sweep_token_target"]
        print(f"dry run: est. {est} tokens at daily target; no calls made")
        return

    load_env()
    import os
    p = params()
    tax = yaml.safe_load((ROOT / "config" / "taxonomy.yaml").read_text())
    client = ERClient(os.environ["NEWSAPI_AI_KEY"],
                      token_budget=p["budgets"]["daily_sweep_token_target"] * 3)
    conn = connect(ROOT / "data" / "controversy.db", check_same_thread=False)

    state.update({"start": start, "end": end})
    for day in days:
        try:
            stats = run_sweep(client, conn, tax["sweep_keywords"], tax["issues"],
                              batch_size=args.batch_size, target_date=day)
        except BudgetExceeded as e:
            print(f"budget exhausted at {day}: {e} — resume tomorrow with --resume")
            break
        state["done_to"] = day
        _save_state(state)
        print(f"replayed {day}: articles={stats['articles']} "
              f"themes={stats['themes']}", flush=True)
    else:
        print("replay complete — run scripts/calibrate_scorecard.py")


if __name__ == "__main__":
    main()

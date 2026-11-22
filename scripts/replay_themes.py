#!/usr/bin/env python3
"""Historical replay harness — populates the DB with archive sweep data to
generate the labelled training set for the escalation scorecard AND the
controversy level/direction parameter fits (per-issue half-lives, diffusion).

Replays the daily sweep over an archive date range, oldest day first (the
theme chain and trailing baselines are order-sensitive). Checkpoints live in
the DB table replay_days — NOT a file — so they travel with the Actions
cache and a rerun of the same range resumes exactly where it stopped, at
batch granularity, with zero token double-spend (article inserts are
idempotent and completed batches are never re-fetched).

    python scripts/replay_themes.py --start 2025-11-22 --end 2026-11-21
    python scripts/replay_themes.py --start ... --end ... --dry-run
    python scripts/replay_themes.py --start 2025-11-22 --probe   # 1 call, 5 tokens
    python scripts/replay_themes.py --start ... --end ... --reset

COST: archive article searches cost 5 tokens per call (per searched year).
A replayed day costs ~31 batches x ~1.4 pages x 5 = ~210 tokens (observed
recent-window page rate; controversy-heavy days paginate deeper). Use
--token-budget to cap a run; the driver stops cleanly on BudgetExceeded or
--max-minutes and resumes on the next invocation.

NOTE: replaying history requires Event Registry ARCHIVE access on the API
plan; --probe verifies it for the start date with a single 5-token call.

--reset wipes ALL derived tables (articles onward) so the replay chain is
chronologically consistent from day zero. Companies, states and transitions
are preserved. Required before the first replay into a DB that already has
swept days (they were processed out of chain order).
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_env, params, taxonomy, ROOT
import yaml
from src.er_client import ERClient, BudgetExceeded
from src.store import connect
from src.sweep import run_sweep

DERIVED_TABLES = ["articles", "article_companies", "incidents",
                  "company_daily", "company_scores", "event_signatures",
                  "themes", "theme_events", "theme_daily", "replay_days"]

CKPT_DDL = """
CREATE TABLE IF NOT EXISTS replay_days (
    day TEXT PRIMARY KEY,
    next_batch INTEGER DEFAULT 0,   -- first batch not yet fetched
    done INTEGER DEFAULT 0,
    articles INTEGER DEFAULT 0,
    tokens INTEGER DEFAULT 0,
    ts TEXT
);
"""


def _days(start: str, end: str) -> list[str]:
    out, d = [], date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _reset(conn) -> None:
    conn.executescript(CKPT_DDL)
    for t in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {t}")
    conn.commit()


def _n_batches(conn, batch_size: int) -> int:
    n = conn.execute("SELECT COUNT(*) FROM companies "
                     "WHERE map_status='auto_mapped'").fetchone()[0]
    return (n + batch_size - 1) // batch_size


def _probe(client, day: str, sweep_kw) -> None:
    """Single archive call: does the plan return articles for this date?"""
    q = {"$and": [{"$or": [{"keyword": kw} for kw in sweep_kw]},
                  {"dateStart": day, "dateEnd": day}]}
    js = client.search_articles(q, count=10, page=1, cost=5)
    total = (js.get("articles") or {}).get("totalResults", 0)
    print(f"probe {day}: totalResults={total} — archive access OK "
          f"(5 tokens spent)")
    if total == 0:
        print("WARNING: zero results — either no controversy news that day "
              "(implausible) or the plan does not reach this far back. "
              "Probe a more recent date to distinguish.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYY-MM-DD first archive day")
    ap.add_argument("--end", help="YYYY-MM-DD last archive day")
    ap.add_argument("--token-budget", type=int, default=0,
                    help="cap for THIS run (default: budgets.replay_run_token_budget)")
    ap.add_argument("--batch-size", type=int, default=0,
                    help="default: budgets.replay_batch_size")
    ap.add_argument("--page-cost", type=int, default=0,
                    help="tokens per search call (default: budgets.replay_page_cost)")
    ap.add_argument("--max-pages", type=int, default=3)
    ap.add_argument("--max-minutes", type=int, default=100,
                    help="wall-clock self-limit; checked between days")
    ap.add_argument("--reset", action="store_true",
                    help="wipe all derived tables before replaying")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only: no API calls, no DB writes")
    ap.add_argument("--probe", action="store_true",
                    help="single 5-token archive call for --start, then exit")
    args = ap.parse_args()

    load_env()
    p = params()
    b = p["budgets"]
    token_budget = args.token_budget or b.get("replay_run_token_budget", 25000)
    batch_size = args.batch_size or b.get("replay_batch_size", 39)
    page_cost = args.page_cost or b.get("replay_page_cost", 5)
    tax = yaml.safe_load((ROOT / "config" / "taxonomy.yaml").read_text())
    sweep_kw = tax["sweep_keywords"]

    client = ERClient(os.environ["NEWSAPI_AI_KEY"], token_budget=token_budget)
    conn = connect(ROOT / "data" / "controversy.db", check_same_thread=False)
    conn.executescript(CKPT_DDL)
    conn.commit()

    if args.probe:
        _probe(client, args.start, sweep_kw)
        return

    end = args.end or (date.today() - timedelta(days=1)).isoformat()
    days = _days(args.start, end)
    done = {r["day"] for r in conn.execute(
        "SELECT day FROM replay_days WHERE done=1")}
    todo = [d for d in days if d not in done]
    n_batches = _n_batches(conn, batch_size)
    est_calls_day = n_batches * 1.4  # observed recent-window page rate
    print(f"replay plan: {len(todo)}/{len(days)} days remaining "
          f"({todo[0] if todo else '—'} .. {todo[-1] if todo else '—'}), "
          f"{n_batches} batches/day, ~{int(est_calls_day * page_cost)} "
          f"tokens/day est., run cap {token_budget}")
    if args.dry_run:
        print(f"dry run: est. {int(len(todo) * est_calls_day * page_cost)} "
              f"tokens total; no calls made")
        return

    if args.reset:
        _reset(conn)
        print("reset: all derived tables wiped (companies/states/transitions "
              "preserved)")

    t0 = time.time()
    for day in todo:
        if (time.time() - t0) / 60 > args.max_minutes:
            print(f"wall-clock limit ({args.max_minutes} min) — stopping; "
                  f"rerun to resume at {day}")
            break
        row = conn.execute("SELECT next_batch FROM replay_days WHERE day=?",
                           (day,)).fetchone()
        batch_start = row["next_batch"] if row else 0
        try:
            stats = run_sweep(client, conn, sweep_kw, tax["issues"],
                              batch_size=batch_size, batch_start=batch_start,
                              max_pages=args.max_pages, target_date=day,
                              page_cost=page_cost)
        except BudgetExceeded as e:
            # Budget died before the first batch of this day — nothing to
            # checkpoint beyond what prior calls saved.
            print(f"budget exhausted before {day}: {e} — rerun to resume")
            break
        batches_done = batch_start + stats["batches_done"]
        if stats["budget_exceeded"]:
            conn.execute(
                "INSERT INTO replay_days (day, next_batch, done, articles, ts)"
                " VALUES (?,?,0,?,datetime('now'))"
                " ON CONFLICT(day) DO UPDATE SET"
                " next_batch=excluded.next_batch,"
                " articles=articles+excluded.articles, ts=datetime('now')",
                (day, batches_done, stats["articles"]))
            conn.commit()
            print(f"budget exhausted mid-{day} at batch {batches_done}/"
                  f"{n_batches} — rerun to resume", flush=True)
            break
        # Completed day: run_sweep already ran the roll-ups (they run at the
        # end of every call that finishes without BudgetExceeded, including
        # resumed batch ranges).
        conn.execute(
            "INSERT INTO replay_days (day, next_batch, done, articles,"
            " tokens, ts) VALUES (?,0,1,?,?,datetime('now'))"
            " ON CONFLICT(day) DO UPDATE SET done=1, next_batch=0,"
            " articles=articles+excluded.articles,"
            " tokens=tokens+excluded.tokens, ts=datetime('now')",
            (day, stats["articles"], stats["tokens_used"]))
        conn.commit()
        print(f"replayed {day}: articles={stats['articles']} "
              f"tokens={stats['tokens_used']} (run total {client.spent})",
              flush=True)
    else:
        print("replay range complete")

    print(f"run finished: tokens spent {client.spent}/{token_budget}")


if __name__ == "__main__":
    main()

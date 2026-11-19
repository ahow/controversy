#!/usr/bin/env python3
"""Taxonomy promotion workflow — close the Channel B loop.

A theme that repeatedly escalates AND attaches to investable companies should
stop being emergent and become a first-class taxonomy issue, so the NEXT time
the issue appears it is caught by the confirmed/indicative layers on day one.

    python scripts/promote_themes.py                      # list candidates
    python scripts/promote_themes.py --apply th:2026-11-18:slurry-spill \
        --issue-id MINING_WASTE --label "Mining waste & slurry spills" \
        --keywords "slurry spill,tailings leak,mine waste" \
        --severity 3 --attention 3

The gate (parameters.yaml themes.promotion) is evaluated in
src/themes.promotion_candidates. --apply appends the issue to
config/taxonomy.yaml (status: promoted, with provenance + evidence), bumps
the taxonomy version, and marks the theme promoted. Promotion is ALWAYS a
human decision — nothing here runs unattended.
"""
import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import ROOT, params
from src.store import connect
from src.themes import promotion_candidates

TAX = ROOT / "config" / "taxonomy.yaml"


def _bump_version(text: str) -> tuple:
    m = re.search(r'^version: "(\d+)\.(\d+)\.(\d+)"', text, re.M)
    if not m:
        return text, None
    v = f'{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}'
    return text[:m.start()] + f'version: "{v}"' + text[m.end():], v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", metavar="THEME_ID")
    ap.add_argument("--issue-id")
    ap.add_argument("--label")
    ap.add_argument("--keywords", help="comma-separated English keyword cues")
    ap.add_argument("--severity", type=int, default=3, choices=[1, 2, 3, 4])
    ap.add_argument("--attention", type=int, default=3, choices=[1, 2, 3, 4])
    ap.add_argument("--hard-trigger", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="apply even if the promotion gate is not met (logged)")
    args = ap.parse_args()

    conn = connect(ROOT / "data" / "controversy.db")
    gate = params()["themes"]["promotion"]

    if not args.apply:
        cands = promotion_candidates(conn, gate)
        if not cands:
            print("no live themes")
            return
        for c in cands:
            t = c["theme"]
            flag = "QUALIFIES" if c["qualifies"] else "         "
            print(f"{flag} {t['id']}")
            print(f"          label={t['label']}  status={t['status']}  "
                  f"score={t['escalation_score']}  events={t['n_events']}  "
                  f"companies={t['n_companies']}  alive={c['days_alive']}d")
        return

    for req in ("issue_id", "label", "keywords"):
        if not getattr(args, req):
            sys.exit(f"--apply requires --{req.replace('_', '-')}")
    cands = {c["theme"]["id"]: c for c in promotion_candidates(conn, gate)}
    c = cands.get(args.apply)
    if not c:
        sys.exit(f"theme not found or not promotable state: {args.apply}")
    if not c["qualifies"] and not args.force:
        sys.exit("promotion gate not met (see themes.promotion); "
                 "re-run with --force to override (will be logged)")

    kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text, version = _bump_version(TAX.read_text())
    t = c["theme"]
    block = f"""
  - id: {args.issue_id}
    label: {args.label}
    keywords_en: [{', '.join(kws)}]
    hard_trigger: {'true' if args.hard_trigger else 'false'}
    prior_severity: {args.severity}
    prior_attention: {args.attention}
    status: promoted
    promoted_from_theme: {args.apply}
    promoted_at: "{now}"
    promotion_evidence: "events={t['n_events']} companies={t['n_companies']} score={t['escalation_score']} alive={c['days_alive']}d{' FORCED' if not c['qualifies'] else ''}"
"""
    TAX.write_text(text.rstrip("\n") + "\n" + block)
    conn.execute(
        "UPDATE themes SET status='promoted', promoted_issue_id=? WHERE id=?",
        (args.issue_id, args.apply))
    conn.commit()
    print(f"promoted {args.apply} -> {args.issue_id} "
          f"(taxonomy version {version or 'unchanged'})")
    print("note: keyword cues are English seeds; refine from theme evidence "
          "before the next calibration cycle")


if __name__ == "__main__":
    main()

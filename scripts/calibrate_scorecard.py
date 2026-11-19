#!/usr/bin/env python3
"""Fit the escalation scorecard from replay-generated theme histories and
write a PROPOSAL to data/scorecard_proposal.json.

The proposal never edits parameters.yaml itself. If the evidence base is too
thin (single outcome class or <10 themes) the proposal is marked
insufficient_data and the prior weights stay in force — silence is not
calibration. Human review merges accepted weights into
config/parameters.yaml (themes.weights) with a version bump.

    python scripts/calibrate_scorecard.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.calibration import calibrate
from src.config import ROOT, params
from src.store import connect


def main():
    conn = connect(ROOT / "data" / "controversy.db")
    tp = params()["themes"]
    result = calibrate(conn, tp)
    out = ROOT / "data" / "scorecard_proposal.json"
    out.write_text(json.dumps(result, indent=2, default=str))

    print(f"themes: {result['n_themes']}  escalated: {result['n_escalated']}  "
          f"base rate: {result['base_rate']}")
    if result["status"] == "insufficient_data":
        print(f"NOT FIT: {result['note']}")
        print(f"proposal written to {out} (diagnostics only)")
        return
    m = result["model"]
    print(f"AUC: {result['auc']}")
    print("proposed weights (standardised):")
    for k, v in m["weights"].items():
        print(f"  {k:16s} {v:+.4f}")
    print(f"  {'intercept':16s} {m['intercept']:+.4f}")
    print(f"proposal written to {out} — merge into parameters.yaml "
          "themes.weights with a version bump if accepted")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Materiality calibration proposal: event study -> data/materiality_proposal.json.

Runs the market-adjusted CAR event study over all confirmed incidents with
price coverage and writes the proposal JSON. NOTHING is merged into
parameters.yaml here — merging impact.materiality.expected_car_bps is a
human decision with a version bump (same discipline as the theme scorecard).

    python scripts/build_materiality.py

Requires prices populated by scripts/fetch_prices.py. Cells with
n < min_cell_n are written with low_evidence: true and never smoothed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import ROOT, params  # noqa: E402
from src.event_study import build_materiality_table  # noqa: E402
from src.store import connect  # noqa: E402


def main():
    p = params()["impact"]["materiality"]
    conn = connect(ROOT / "data" / "controversy.db")
    res = build_materiality_table(conn, p["benchmark"], p["windows"],
                                  p["min_cell_n"])
    res["param_version"] = params()["version"]
    res["generated_for"] = "impact.materiality.expected_car_bps"
    res["merge_contract"] = (
        "human merge only: copy expected_car_bps into config/parameters.yaml, "
        "set impact.materiality.status=calibrated, bump version, changelog")
    out = ROOT / "data" / "materiality_proposal.json"
    out.write_text(json.dumps(res, indent=2, sort_keys=True))
    n_low = sum(1 for c in res["cells"].values() if c["low_evidence"])
    print(f"status={res['status']} incidents_used={res['n_incidents_used']} "
          f"skipped={res['n_incidents_skipped']} cells={len(res['cells'])} "
          f"low_evidence={n_low}")
    print(f"proposal written: {out} — human merge only, never auto-applied")
    if res["status"] == "insufficient_data":
        sys.exit(2)


if __name__ == "__main__":
    main()

"""Unit tests for reversal math and transition legality — the rules that must
never silently change. Backtest and production import the same functions.
"""
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from src.states import (ReversalParams, check_transition, probation_window_months,
                        reversal_eligible_date, exit_criteria_met, escalate_tier)

P = ReversalParams(base_months={"S1": 3, "S2": 6, "S3": 12, "S4": 24})


def test_transition_legality():
    check_transition("NORMAL", "WATCH")
    check_transition("PROBATION", "NORMAL")
    with pytest.raises(ValueError):
        check_transition("NORMAL", "EXCLUDED")   # must pass through FLAGGED
    with pytest.raises(ValueError):
        check_transition("WATCH", "EXCLUDED")    # no skipping the review gate


def test_probation_windows():
    assert probation_window_months("S1", "exemplary", 0, P) == pytest.approx(2.25)
    assert probation_window_months("S3", "adequate", 0, P) == 12.0
    assert probation_window_months("S2", None, 0, P) == 6.0          # unassessed = adequate
    assert probation_window_months("S2", "poor", 1, P) == 13.5       # 6 x 1.5 x 1.5
    assert probation_window_months("S1", "poor", 3, P) == 9.0        # recurrence capped at 2.0


def test_reversal_date():
    # 13.5 months = 13 whole months + round(0.5 x 30.44) = 15 days
    d = reversal_eligible_date(date(2026, 1, 1), "S2", "poor", 1, P)
    assert d == date(2027, 2, 16)


def test_exit_criteria_all_required():
    ok, unmet = exit_criteria_met(0.4, 0.5, 0, "S1", has_resolution_evidence=False)
    assert ok and not unmet                       # S1 needs no evidence
    ok, unmet = exit_criteria_met(0.4, 0.5, 0, "S3", has_resolution_evidence=False)
    assert not ok and any("resolution" in u for u in unmet)
    ok, unmet = exit_criteria_met(0.6, 0.5, 1, "S3", has_resolution_evidence=True)
    assert not ok and len(unmet) == 2             # salience + open trigger


def test_tier_escalation():
    assert escalate_tier("S2") == "S3"
    assert escalate_tier("S4") == "S4"

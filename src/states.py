"""State machine + severity-dependent reversal logic.

States: NORMAL -> WATCH -> FLAGGED -> EXCLUDED -> PROBATION -> NORMAL

Reversal eligibility (binary exclusion, rule-based):
    eligible_date = exclusion_date + base_months[tier] x M_response x M_recurrence
Exit criteria (ALL required; time is necessary, never sufficient):
    - salience z < reentry_z_ceiling, sustained for final third of probation
    - no open Confirmed-layer triggers
    - resolution evidence for tiers S2-S4
    - human sign-off (recorded outside this module)

Reversal path is recorded as DECAY (rule-based) or FALSE_POSITIVE (fast human
override with mandatory written rationale) — tracked separately for tuning.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from dateutil.relativedelta import relativedelta  # pip install python-dateutil

STATES = ["NORMAL", "WATCH", "FLAGGED", "EXCLUDED", "PROBATION"]

ALLOWED = {
    "NORMAL":    {"WATCH"},
    "WATCH":     {"NORMAL", "FLAGGED"},
    "FLAGGED":   {"WATCH", "EXCLUDED"},
    "EXCLUDED":  {"PROBATION"},          # automatic once exclusion executed
    "PROBATION": {"NORMAL", "EXCLUDED"}, # NORMAL=re-entry; EXCLUDED=recurrence re-exclusion
}

RESPONSE_MULT = {"exemplary": 0.75, "adequate": 1.0, "poor": 1.5}


@dataclass
class ReversalParams:
    base_months: dict          # {"S1": 3, "S2": 6, "S3": 12, "S4": 24}
    recurrence_multiplier: float = 1.5
    recurrence_cap: float = 2.0


def check_transition(frm: str, to: str):
    if to not in ALLOWED.get(frm, set()):
        raise ValueError(f"illegal transition {frm} -> {to}")


def probation_window_months(tier: str, response_grade: str | None,
                            n_recurrences: int, p: ReversalParams) -> float:
    """Full probation window in months (float; round up when scheduling).

    - response_grade None (not yet assessed at day 90) -> treat as 'adequate' (1.0);
      probation cannot end before response assessment anyway for S2-S4.
    - recurrence: each recurrence multiplies by recurrence_multiplier (capped) —
      same-issue recurrence also escalates the tier by one level upstream.
    """
    base = p.base_months[tier]
    m_resp = RESPONSE_MULT.get(response_grade or "adequate")
    m_rec = min(p.recurrence_multiplier ** n_recurrences, p.recurrence_cap) if n_recurrences else 1.0
    return base * m_resp * m_rec


def reversal_eligible_date(exclusion_date: date, tier: str, response_grade: str | None,
                           n_recurrences: int, p: ReversalParams) -> date:
    months = probation_window_months(tier, response_grade, n_recurrences, p)
    whole = int(months)
    extra_days = round((months - whole) * 30.44)
    return exclusion_date + relativedelta(months=whole, days=extra_days)


def exit_criteria_met(salience_z_final_third_max: float, reentry_z_ceiling: float,
                      open_confirmed_triggers: int, tier: str,
                      has_resolution_evidence: bool,
                      requires_evidence_tiers=("S2", "S3", "S4")) -> tuple[bool, list[str]]:
    """Returns (all_met, list_of_unmet_criteria). Human sign-off handled by caller."""
    unmet = []
    if salience_z_final_third_max >= reentry_z_ceiling:
        unmet.append(f"salience z {salience_z_final_third_max:.2f} >= ceiling {reentry_z_ceiling}")
    if open_confirmed_triggers > 0:
        unmet.append(f"{open_confirmed_triggers} open confirmed trigger(s)")
    if tier in requires_evidence_tiers and not has_resolution_evidence:
        unmet.append(f"tier {tier} requires documented resolution evidence")
    return (not unmet), unmet


def escalate_tier(tier: str) -> str:
    order = ["S1", "S2", "S3", "S4"]
    return order[min(order.index(tier) + 1, 3)]

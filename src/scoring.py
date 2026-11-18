"""Scoring: per-company salience baselines, indicative-layer composite,
confirmed-layer trigger detection. The two layers are NEVER summed.
"""
from __future__ import annotations
import statistics
from collections import defaultdict

HARD_TRIGGER_KEYWORDS = {
    "regulator_action": ["investigation opened", "fined", "sanctioned", "charged by", "probe launched", "cease and desist"],
    "litigation_filed": ["lawsuit filed", "sued", "class action", "indicted", "criminal charges"],
    "company_acknowledgment": ["recall", "we apologise", "we apologize", "admitted", "settles", "profit warning"],
    "credible_ngo_campaign": ["greenpeace", "amnesty international", "oxfam", "human rights watch", "rainforest action network"],
}


class Baseline:
    """Rolling per-company daily mention baseline (median + MAD for robustness)."""

    def __init__(self, min_history_days: int = 30):
        self.counts: dict[int, list[int]] = defaultdict(list)
        self.min_history = min_history_days

    def update(self, company_id: int, daily_count: int):
        self.counts[company_id].append(daily_count)

    def z(self, company_id: int, today_count: int) -> float | None:
        hist = self.counts.get(company_id, [])
        if len(hist) < self.min_history:
            return None  # THIN_DATA — caller must tag, never treat as clean
        med = statistics.median(hist)
        mad = statistics.median([abs(x - med) for x in hist]) or 1.0
        return (today_count - med) / (1.4826 * mad)

    def history_days(self, company_id: int) -> int:
        return len(self.counts.get(company_id, []))


def severity_weight(issue_prior: int) -> float:
    """Map taxonomy prior_severity (1-4) to a salience multiplier."""
    return {1: 0.5, 2: 0.75, 3: 1.0, 4: 1.5}[issue_prior]


def indicative_score(z: float | None, n_sources: int, issue_prior_severity: int) -> float | None:
    """Indicative-layer composite: salience z x breadth factor x severity weight.
    Returns None when baseline is too thin (THIN_DATA)."""
    if z is None:
        return None
    breadth = 1.0 + 0.1 * min(max(n_sources - 1, 0), 9)   # 1.0 at 1 source, 1.9 at 10+
    return z * breadth * severity_weight(issue_prior_severity)


def detect_confirmed_triggers(title: str, body: str) -> list[str]:
    """Keyword-level hard-trigger detection. v1: deliberately high-precision;
    v2: LLM classification of candidate incidents (small volume, human-reviewed)."""
    text = f"{title} {body[:2000]}".lower()
    return [k for k, kws in HARD_TRIGGER_KEYWORDS.items() if any(kw in text for kw in kws)]


def assign_issue(text: str, taxonomy: list[dict]) -> str | None:
    """First-match issue classification by keyword cues. Unmatched incidents are
    logged as ORPHAN — the feed for taxonomy evolution (see docs/TAXONOMY.md)."""
    t = text.lower()
    for issue in taxonomy:
        if any(kw.lower() in t for kw in issue["keywords_en"]):
            return issue["id"]
    return None

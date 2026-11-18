# Parameter Register v0.1.0 (2026-11-18)

Machine-readable source: `config/parameters.yaml`. This document carries the
rationale; the YAML carries the values. They must never diverge.
Change control: version bump + backtest re-run + changelog entry here.

## Thresholds (indicative layer)
| Param | Value | Rationale |
|---|---|---|
| z_watch | 2.0 | Cheap to flag; watchlist costs monitoring only. High recall by design. |
| z_flag | 3.0, sustained 3d + ≥3 outlets | Confirmation requirement filters one-source noise. |
| z_exclude_extreme | 4.0, sustained 5d | ~99th percentile; still requires human sign-off — exclusions are never fully automated in v1. |
| salience baseline | 90d, median/MAD | Robust to weekend/holiday structure and one-off spikes. |
| THIN_DATA | <30d history | Coverage bias is structural; silence ≠ cleanliness. |

## Severity tiers (locked at exclusion; evidence stored)
| Tier | Base probation | Anchor examples |
|---|---|---|
| S1 minor | 3m | Isolated incident, no regulator, contained geography |
| S2 moderate | 6m | Litigation filed / formal investigation; national coverage |
| S3 severe | 12m | Regulator sanction, major harm, multi-country coverage |
| S4 critical | 24m | Fraud, systemic safety failure, fatalities, criminal proceedings |

## Response multipliers (assessed once at day 90, locked)
| Grade | × | Evidence standard |
|---|---|---|
| exemplary | 0.75 | Prompt acknowledgment + independent investigation + verified remediation + accountability change |
| adequate | 1.00 | Acknowledgment + credible remediation plan in progress |
| poor | 1.50 | Denial contradicted by evidence / no remediation / obstruction |

Positive-sentiment coverage is EXCLUDED from response grading — PR tone-laundering
is precisely what the system exists to see through.

## Worked examples (normative — tests encode these)
1. **S1, exemplary, no recurrence**: 3 × 0.75 = 2.25m → eligible ~10 weeks post-exclusion, if salience < 0.5σ for the final third. Fast and defensibly so.
2. **S3 (sanction), adequate**: 12 × 1.0 = 12m; exit blocked until the regulatory process formally closes even if coverage goes quiet at month 4. Quiet ≠ resolution.
3. **S2, poor, recurrence month 5**: clock resets; 6 × 1.5 × 1.5 = 13.5m from recurrence; tier escalates to S3. Reoffenders stay out longer.
4. **S1, poor, 3 recurrences**: recurrence multiplier capped at 2.0 → 3 × 1.5 × 2.0 = 9.0m. The cap prevents runaway exclusions on noisy low-severity names.

## Reversal paths
- **DECAY**: rule-based, slow. All exit criteria required.
- **FALSE_POSITIVE**: fast human override with mandatory written rationale, stored.
  A high FALSE_POSITIVE rate ⇒ entry thresholds wrong; high DECAY recidivism ⇒
  probation windows too short. Reviewed annually.

## Token budgets
| Param | Value | Note |
|---|---|---|
| daily sweep target | 100 tokens/day | ~50 batched queries + margin |
| alert | 7-day avg > 150 | Investigate pagination blowups (mega-caps) |

## Changelog
- v0.1.0 (2026-11-18): initial register. All values are priors pending shadow-mode
  measurement and historical replay (Wirecard, Vale, Boohoo, Adani).

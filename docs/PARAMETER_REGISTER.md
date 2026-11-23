# Parameter Register v0.3.0 (2026-11-20)

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
| replay_page_cost | 5 tokens/call | ER archive pricing (per searched year); recent window is 1 |
| replay_batch_size | 39 | 39 concepts + 40 keywords = 79 ≤ 80-term cap; 31 vs 40 batches/day (−22% burn) |
| replay_run_token_budget | 25,000/run | Per-dispatch cap; batch-granular checkpointing in `replay_days` (DB, cache-persisted) |

## Channel B: emergent themes (`themes:` block)
| Param | Value | Rationale |
|---|---|---|
| top_k_concepts | 8 | Signature size; enough to characterise an event, small enough to discriminate. |
| min_concept_score | 3 | Drops ER's low-confidence tail concepts. |
| max_doc_freq | 0.08 | Concepts in >8% of orphan events are non-discriminative (day-1 evidence: "Artificial intelligence" 16.8%, "USD" 9.0% chained a 151-event mega-cluster). Grid-tested 2026-11-19 on real signatures: (0.15,2)→123-event blob; (0.05,3)→fragments coherent chains; (0.08,3)→19 coherent themes. |
| merge_shared | 3 | Union-find merge threshold; 2 over-merges via one generic + one specific concept. |
| assign_overlap | 2 | Looser threshold for attaching leftovers to an established theme — deliberate asymmetry: creation strict, attachment lenient. |
| fixed-point loop | ≤10 iterations | Attach+merge repeated until no change; guarantees run-count independence (verified: run 2 is a no-op). |
| snapshot_days | 3 | Features snapshotted at first_seen+2d for calibration — only information available at decision time. |
| emerging_score | 40 | candidate → emerging status threshold (prior). |
| dead_after_days | 7 | Theme with no new events for 7d → dead (kept for outcome labelling). |

## Escalation outcome contract ("high profile")
| Param | Value | Rationale |
|---|---|---|
| window_days | 7 | Rolling window — high-profile means sustained, not one-day spike. |
| within_days | 90 | Escalation must begin within 90d of first_seen to count as predicted. |
| min_articles | 100 | ~14/day sustained — clears routine coverage. |
| min_sources | 25 | Independent outlets; kills single-source amplification. |

## Escalation scorecard (weights are PRIORS until replay calibration)
| Feature | Cap | Prior weight | Rationale |
|---|---|---|---|
| accel | 3× | 0.8 | Rising volume vs own recent average — the core "emerging" signal. |
| spread_sources | 50 | 0.6 | Breadth of independent coverage. |
| spread_langs | 5 | 0.7 | Cross-language spread ⇒ escaped local containment. |
| authority | 3 | 0.7 | Tier-1 outlets (Reuters/AP/AFP/FT/WSJ/NYT/BBC/Guardian/Bloomberg/CNBC) confer agenda-setting. |
| actors | 5 | 0.9 | Institutional actors (courts, regulators, prosecutors, NGOs) are the strongest escalation driver — highest prior. |
| attach | 10 | 0.8 | Universe-company attachment — relevance to the portfolio. |
| persist | 30 | 0.4 | Survival past the news cycle. |
| intercept | — | −2.0 | Encodes low prior base rate of escalation. |

Feature caps normalise to [0,1]; full decomposition persisted per theme in
`themes.score_json`. Scores are INDICATIVE-only (rank attention; never drive
exclusion). Calibration: L2 logistic regression on 72h snapshots vs the outcome
contract above, rank-based AUC; insufficient_data guard (<10 themes or single
class ⇒ priors stand). Promotion gate: status escalated OR score ≥60, ≥1 company
attached, ≥7 days alive; human-only apply with taxonomy provenance.

## Company impact scores (`impact:` block) — three scores, NEVER summed
Each score answers a different question for a different decision. All weights
are PRIORS until calibrated by evidence; full per-term decomposition is
persisted in `company_scores.scores_json`.

### Shared
| Param | Value | Rationale |
|---|---|---|
| active_window_days | 7 | An incident is "active" for scoring while seen within the trailing 7 days. |
| z_cap | 4.0 | Caps the size-adjusted salience input so one extreme day cannot dominate severity. |

### SEVERITY — issue substance, size-adjusted (raw volume is never an input)
| Feature | Weight | Rationale |
|---|---|---|
| trigger_strength | 1.6 | Hardest evidence wins: regulator_action 1.0 > litigation_filed 0.75 > credible_ngo_campaign 0.7 > company_acknowledgment 0.6. Confirmed layer only — indicative coverage is not substance. |
| issue_severity | 0.9 | Taxonomy prior_severity / 4, worst active confirmed issue. |
| recurrence | 0.8 | Prior same-issue incidents before the active window, capped at 3. |
| z_excess | 0.7 | Salience z vs the company's OWN 90-day baseline, capped at z_cap — the size adjustment: a Meta-scale pile-on of a minor story must not read as severe. THIN ⇒ excluded (contribution 0), NEVER imputed. |
| actors | 0.6 | Institutional-actor pattern hits in the day's titles, cap 5. |
| intercept | −2.2 | Baseline ≈ 10/100 for a company with coverage but no confirmed incident. |

### OUTRAGE — raw visibility × moral charge (large-company skew is legitimate here)
| Feature | Cap | Weight | Rationale |
|---|---|---|---|
| articles | 50 (log1p) | 0.5 | Raw reach, log-scaled. |
| sources | 50 | 0.7 | Independent-outlet breadth, raw. |
| tier1 | 3 | 0.8 | Tier-1 outlets (themes.authority_tier1 list). |
| langs | 5 | 0.6 | Cross-language spread. |
| theme_boost | — | 0.7 | Strongest live (non-dead) emergent theme attached to the company, escalation_score/100. |
| charge | — | 1.0 | Moral-charge prior per worst active issue (SAFETY_FATALITIES 1.0 … ANTITRUST 0.4, _default 0.35); a credible_ngo_campaign trigger forces the 0.8 floor. |
| intercept | — | −2.4 | Low base rate of sustainable-fund headline risk. |

### MATERIALITY — expected share-price response (empirical only)
| Param | Value | Rationale |
|---|---|---|
| status | uncalibrated | Flips to calibrated only when a human merges the event-study proposal. |
| benchmark | ^GSPC | Market-adjusted model (company return − benchmark return); no estimation window — with thin per-issue samples, extra model complexity is false precision. |
| windows | [0,5], [0,20] | Trading days from day 0 = first trading day ≥ incident first_seen. |
| min_cell_n | 5 | Cells below n=5 are FLAGGED low_evidence, never smoothed. |
| cap_bps | 2000 | Caps the 0–100 magnitude rank (|bps| / cap × 100). |
| p_escalation_cap_sources | 25 | Escalation-probability proxy = max(theme_boost, min(n_sources/25, 1)); 25 anchors to the theme outcome contract. |
| expected_car_bps | {} | EMPTY ⇒ materiality is NULL (shown UNCAL, never zero-filled). Keys are ISSUE\|TIER from the PRIMARY window (windows[0]); per-window cells stay in the proposal JSON for audit. |

Calibration path: `scripts/fetch_prices.py` (FMP EOD, company + benchmark) →
`src/event_study.py::build_materiality_table` (confirmed incidents only;
tier falls back to S2 for ungraded shadow-mode incidents) →
`data/materiality_proposal.json` → human merge with version bump.

## Subject-salience gate (`attach:` block) — v0.4.0
| Param | Value | Rationale |
|---|---|---|
| min_concept_score | 4 | Minimum Event Registry concept salience for a company-article link to enter the precision layer (incidents, daily counts, impact scores). Title mention overrides. Evidence (2026-11-18 corpus, 6,004 links): score<=2 without title mention is ~100% bystander noise; confirmed incidents whose best link scores 3 are ~98% noise; every verified true-subject incident scores 5 (Deutsche Bank submarine leak: 42/42 articles). Raw links are never deleted — the recall layer stays complete and the threshold can be flexed without re-fetching. Known recall risk: real stories whose best link scores 3 with no title mention are dropped (one observed: CN derailment); the LLM subject/bystander pass is the proper fix for the residual (person-name collisions like 'Pat Cummins' cricket vs Cummins Inc.). |

## Changelog
- ops (2026-11-22, no parameter change): quota-stop resilience. ER rejects
  with HTTP 403 when the monthly token allowance is exhausted; replay now
  treats 401/402/403 as a graceful stop (checkpoint, no partial-day roll-up,
  exit 0 so the cache saves) via `is_quota_error` +
  `run_sweep(graceful_quota=True)`. The daily sweep keeps `graceful_quota=
  False` — a blind monitor must fail loudly. Cache-save steps in both
  workflows are now `if: always()`. Trigger: 2026-11-22 replay run lost
  ~21 replayed days when a 403 crashed the run before cache save.
- v0.4.1 (2026-11-22): archive-replay tooling — no scoring-logic change.
  `run_sweep` gains `page_cost` (archive = 5/call) and skips roll-ups on
  BudgetExceeded (never roll up a partial day); roll-ups extracted to
  `rollup_day`. Replay driver rewritten: checkpoints in DB table
  `replay_days` (survives CI cache), batch-granular resume with zero token
  double-spend, wall-clock self-limit, `--reset` (derived tables only),
  `--probe` (5-token archive-access check). New `replay.yml` dispatch
  workflow; `controversy-db` concurrency group on both workflows.
- v0.4.0 (2026-11-20): subject-salience gate (`attach:` block) between the
  recall layer (all company-article links, kept raw with score/title_match
  evidence) and the precision layer (incidents, daily counts, impact scores).
  Fixes cross-company pollution (e.g. Nvidia Jensen Huang stories attaching
  to Tesla at concept score 1-2). Incidents/daily/scores rebuild is now full
  per-day rebuild so tightening the filter never leaves stale rows.
- v0.3.0 (2026-11-20): company impact scores — SEVERITY (size-adjusted
  substance), OUTRAGE (raw visibility × moral charge), MATERIALITY (expected
  CAR × escalation proxy; UNCAL until event-study calibration). Event-study
  machinery (prices table, market-adjusted CAR, per-window cells with
  low-evidence flags, primary-window lookup contract) and FMP price ingestion.
  All weights are priors.
- v0.2.0 (2026-11-19): Channel B emergent-theme layer — theme signatures,
  df-cut + fixed-point clustering, escalation features/scorecard (priors),
  outcome contract, replay/calibration/promotion harness. All weights are
  priors pending replay-produced labelled outcomes.
- v0.1.0 (2026-11-18): initial register. All values are priors pending shadow-mode
  measurement and historical replay (Wirecard, Vale, Boohoo, Adani).

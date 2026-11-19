# Architecture

## Objective
Reputational-hygiene overlay for low-fee, low-risk portfolios (~2,500-company
universe, binary exclusion). High recall at flagging, high precision at trading.
This is NOT an alpha signal; it is judged on lead time vs. commercial providers
and on false-positive cost (foregone return on wrongly excluded names).

## Signal channels
- **Subject-salience gate (v0.4.0).** Every company-article link is stored raw
  in `article_companies` with its ER concept salience score and a title-match
  flag (the recall layer). Only links scoring >= `attach.min_concept_score` (4)
  or naming the company in the title form incidents, daily counts and impact
  scores (the precision layer). Rationale: ER's low scores (1-2) are passing
  mentions — in the day-1 corpus they were ~100% bystander noise (e.g. Nvidia
  scandal stories attaching to Tesla) — while verified true-subject incidents
  score 5 throughout. Residual known gap: person-name collisions (a cricketer
  named Cummins) pass the title match; the LLM subject/bystander pass handles
  those. Incidents/daily/scores are full per-day rebuilds, so tightening the
  threshold never leaves stale rows.
- **Channel A — company incidents.** Articles on universe companies matched to the
  issue taxonomy (`config/taxonomy.yaml`); clustered into incidents via the API's
  native `eventUri` (one story amplified across 200 outlets is ONE incident).
- **Channel B — emergent themes.** Orphan incidents (no taxonomy issue match) are
  clustered into themes by shared concept signatures: top-8 ER concepts per event
  (weight = max article score; universe-company concepts excluded — attachment is
  tracked separately, not treated as theme content); concepts appearing in >8% of
  orphan events are dropped at comparison time as non-discriminative (cached
  signatures stay unfiltered for audit). Events sharing ≥3 concepts merge via
  union-find; leftover events attach to live themes at ≥2 overlap inside a bounded
  (10-iteration) fixed-point loop, so the result is deterministic and independent
  of run count. Lone events stay in the raw orphan feed.

## Escalation scoring (Channel B, INDICATIVE-only)
Objective: flag which already-surfaced emergent issues are on a trajectory to
become high-profile, early enough to act. News cannot predict incidents; it can
predict salience trajectory — that is the honest reframe this layer implements.
- Seven features, each normalised to [0,1] by documented caps: acceleration
  (articles/day vs prior-day average, capped 3×), source spread (cumulative, 50),
  language spread (5), authority (tier-1 outlets, 3), institutional actors
  (court/regulator/NGO pattern hits on concept labels, 5), company attachment
  (10), persistence (days alive, 30). THIN flag until a theme has prior-day
  history — acceleration is then undefined and never imputed.
- Score = sigmoid(intercept + Σ wᵢfᵢ) × 100; the full decomposition (feature
  values, weights, contributions) is persisted in `themes.score_json` for audit.
- Weights are PRIORS until calibrated: `scripts/replay_themes.py` replays the
  archive day-by-day through the same production roll-up (checkpointed, budget
  capped), then `scripts/calibrate_scorecard.py` labels outcomes and fits an
  L2 logistic regression (standardised features, deterministic zero-init) with
  rank-based AUC. The fitted proposal is written to `data/scorecard_proposal.json`
  — merging into `parameters.yaml` is a human decision with a version bump.
  <10 labelled themes or a single class ⇒ insufficient_data, priors stand.
- Outcome contract ("high profile"): a rolling 7-day window starting within
  90 days of first_seen containing ≥100 articles AND ≥25 distinct sources,
  computed from exact per-article rows — never from aggregates.
- Scores rank attention only; they never drive exclusion. Themes that repeatedly
  escalate AND attach to universe companies become taxonomy-promotion candidates
  (`scripts/promote_themes.py`; gate: status escalated OR score ≥60, ≥1 company,
  ≥7 days alive). Promotion is human-only (`--apply`) and appends to
  `taxonomy.yaml` with provenance (source theme, date, evidence).

## Company impact scores (three scores — never summed)
Every company with coverage on a day gets three separate scores
(`src/impact.py`, table `company_scores`, full decomposition in
`scores_json`). They answer different questions and feed different decisions;
they are stored in separate columns and never combined into a composite.
- **SEVERITY** — how bad is the underlying conduct, size-adjusted. Inputs:
  confirmed-trigger hierarchy, issue prior severity, recurrence (cap 3),
  institutional actors, and — the size adjustment — salience z vs the
  company's OWN 90-day baseline. Raw volume is never an input, so Meta-scale
  coverage of a minor story cannot read as severe; THIN companies have the z
  term excluded (never imputed). Feeds exclusion/probation policy (human-signed).
- **OUTRAGE** — how visible and morally charged the story is for a
  sustainable-fund audience. Raw reach (articles, sources, tier-1 outlets,
  languages), strongest attached emergent theme, × moral-charge priors per
  issue (NGO campaign forces a 0.8 floor). The large-company skew is
  legitimate here: outrage risk IS partly visibility. Feeds review priority
  and client-comms risk only.
- **MATERIALITY** — expected share-price response = expected CAR bps for the
  issue × severity tier (event-study table) × escalation-probability proxy
  (max of theme boost and sources/25). UNCALIBRATED by construction until
  `src/event_study.py` + `scripts/fetch_prices.py` (FMP EOD prices) produce
  labelled price responses and a human merges the proposal; while the table
  is empty the score is NULL (dashboard: UNCAL), never zero-filled.
  Event-study method: market-adjusted CAR vs ^GSPC over [0,5] and [0,20]
  trading days; day 0 = first trading day ≥ incident first_seen; company
  windows must exactly match benchmark trading days (gaps fail loudly);
  cells with n<5 are flagged low_evidence and never smoothed.

## Two layers — never summed
- **Confirmed layer**: hard triggers only (regulator action, litigation filed,
  company acknowledgment, credible NGO campaign). May drive EXCLUDED with human
  sign-off.
- **Indicative layer**: composite media signal — salience z-score (vs. own 90-day
  baseline, median/MAD) × breadth (independent sources) × issue severity weight.
  Drives WATCH/FLAGGED only. A loud pile-on can never masquerade as a regulator.

THIN_DATA rule: companies with <30 days of baseline history are tagged; silence
is never treated as cleanliness (small-cap/EM coverage bias).

## State machine
`NORMAL → WATCH → FLAGGED → EXCLUDED → PROBATION → NORMAL`
- Transitions only via `src/states.py::check_transition` (unit-tested; backtest and
  production import the same code — no drift permitted).
- Every transition logged to `transitions` with evidence JSON + parameter version.

## Reversal (severity-dependent; binary exclusion)
`eligible = exclusion_date + base[tier] × M_response × M_recurrence`
- Base: S1 3m, S2 6m, S3 12m, S4 24m. Response (assessed once, day 90, evidence
  mandatory, locked): exemplary 0.75 / adequate 1.0 / poor 1.5.
- Recurrence resets the clock, ×1.5 each (cap 2.0); same-issue recurrence
  escalates tier +1.
- Exit criteria (ALL): salience below ceiling for final third of window; no open
  confirmed triggers; resolution evidence for S2–S4; human sign-off.
- Two paths tracked separately: DECAY (rule-based) vs FALSE_POSITIVE (fast
  documented override). Re-offense-within-12m tracked as the tuning statistic.
- Market price recovery is NOT a reversal input.

## Data layer
- newsapi.ai (Event Registry): primary feed. Sequential requests; 1 token/article
  search, 5/event search; budget enforced in `src/er_client.py`.
- FMP: EOD share prices (universe tickers + ^GSPC benchmark) for the
  materiality event study (`scripts/fetch_prices.py` → table `prices`).
- Free GDELT 2.0: planned redundancy/cross-validation layer (v2).
- SQLite: `data/controversy.db` (git-ignored). Schema in `src/store.py`.

## Token budget (production target)
~50 batched sweep queries/day ≈ 1,500 tokens/month; watchlist deep-dives extra.
Budget guardrails in `config/parameters.yaml`.

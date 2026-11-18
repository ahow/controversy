# Architecture

## Objective
Reputational-hygiene overlay for low-fee, low-risk portfolios (~2,500-company
universe, binary exclusion). High recall at flagging, high precision at trading.
This is NOT an alpha signal; it is judged on lead time vs. commercial providers
and on false-positive cost (foregone return on wrongly excluded names).

## Signal channels
- **Channel A — company incidents.** Articles on universe companies matched to the
  issue taxonomy (`config/taxonomy.yaml`); clustered into incidents via the API's
  native `eventUri` (one story amplified across 200 outlets is ONE incident).
- **Channel B — theme exposure.** Theme registry (`config/themes.yaml`) monitored
  in aggregate; a theme salience spike flags holdings with exposure ≥ trigger even
  with zero company-specific coverage. Exposure scores are placeholders until
  segment/supply-chain data is wired in.

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
- FMP: share-price history for taxonomy calibration event studies.
- Free GDELT 2.0: planned redundancy/cross-validation layer (v2).
- SQLite: `data/controversy.db` (git-ignored). Schema in `src/store.py`.

## Token budget (production target)
~50 batched sweep queries/day ≈ 1,500 tokens/month; watchlist deep-dives extra.
Budget guardrails in `config/parameters.yaml`.

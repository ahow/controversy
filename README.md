# controversy

Early-warning system for corporate controversy risk, designed for low-fee,
low-risk portfolios where sustained reputational damage must be mitigated.

**Core loop**: monitor a defined universe (~2,500 companies) via the
newsapi.ai (Event Registry) API → detect incidents and theme exposure →
score salience/severity → drive a per-company state machine
(`NORMAL → WATCH → FLAGGED → EXCLUDED → PROBATION → NORMAL`) →
emit binary exclusion decisions with severity-dependent, rule-based reversal.

Design principles:
- **Reputational hygiene, not alpha.** High recall at flagging, high precision at trading.
- **Two signal layers, never summed.** Confirmed (hard triggers: regulator, litigation,
  company acknowledgment) drives exclusions; Indicative (media signal) drives watch/flag only.
- **Auditability by design.** Every transition stores evidence, score decomposition and
  parameter version. Backtest and production run identical transition logic.
- **Living taxonomy.** Issue weights are calibrated against observed attention and
  share-price impact (event studies via FMP), versioned with evidence.

## Layout
- `config/` — parameter register, taxonomy, theme registry (all versioned, human-editable)
- `docs/` — architecture, parameter register with rationale + worked examples, taxonomy methodology
- `src/` — pipeline modules (client, entity mapping, sweep, scoring, states, store)
- `scripts/` — runnable entry points (`map_entities.py`, `daily_sweep.py`)
- `data/` — local SQLite + ingested payloads (git-ignored)

## Quickstart
```bash
cp .env.example .env   # add keys
pip install requests pyyaml pandas pytest
python scripts/map_entities.py --universe data/universe.csv   # one-time entity mapping
python scripts/daily_sweep.py                                  # daily shadow-mode sweep
```

## Dashboard (GitHub Pages)

`docs/index.html` is a self-contained static dashboard (review queue, issue mix,
coverage trend, emerging-themes orphan feed, per-company daily table) generated
from the database:

```bash
python scripts/build_dashboard.py
```

The `.github/workflows/daily.yml` workflow runs the sweep daily at 06:00 UTC,
rebuilds the dashboard and commits it. Two one-time setup steps:

1. Add repository secret `NEWSAPI_AI_KEY` (Settings → Secrets and variables → Actions).
2. Enable Pages: Settings → Pages → Source: *Deploy from a branch* → `main` / `/docs`.
   The dashboard is then served at `https://<owner>.github.io/controversy/`.

The database itself is git-ignored (too large for git); the workflow persists it
via the Actions cache and re-seeds the universe from `universe/entity_map.csv`
on cache miss. Note: GitHub Pages is public — incident details and company
names on the dashboard are visible to anyone with the URL.

## Status

Shadow mode (v1): sweeps capture and score, but state transitions to
EXCLUDED are human decisions recorded with evidence — never automated.

# Living Taxonomy — Methodology

The taxonomy (`config/taxonomy.yaml`) is a versioned, evidence-calibrated artefact,
not a static list. It exists to classify incidents by issues that *typically attract
significant attention and/or share-price impact*.

## Sources of the seed (v0)
Category structure adapted from the UNGC principles, OECD MNE Guidelines and ILO
conventions — the same normative base used by commercial controversy providers —
which gives every category a defensible basis for client reporting. 16 seed
categories with prior severity (1–4) and attention (1–4) weights. Priors are
explicitly labelled `status: seed` — honest placeholders awaiting evidence.

## Calibration loop (quarterly, from v1 + 3 months of data)
1. **Attention calibration.** For each category: distribution of incident spread
   (outlets, countries, languages), social-share counts, and time-to-peak.
   Categories whose incidents reliably spread widely get attention weight raised;
   chronically ignored categories get lowered.
2. **Price-impact calibration.** Event study per category using FMP daily prices:
   abnormal returns (market model, est. −250…−30d, window −1…+5d) around first-seen
   dates of Confirmed-layer incidents. Categories with reliably negative CARs get
   severity weight raised. Results stored per category as evidence records
   (n, mean/median CAR, CI) — small-n categories are tagged, not smoothed.
3. **Weight updates** land as a version bump with the evidence cited. Weights are
   never edited without an evidence record.

## Evolution rules
- **Orphan incidents** (matching no category) are logged. >20 orphan incidents of a
  recognisable cluster per quarter ⇒ propose a new category with evidence.
- **Mergers/splits** require evidence that two categories' incident populations are
  indistinguishable on spread and CAR (merge) or bimodal within one (split).
- Every change is a commit to `config/taxonomy.yaml` with rationale in this file's
  changelog. Client-facing reporting always cites the taxonomy version in force
  for the period being reported.

## Known structural limits (stated, not managed away)
- Media attention is biased toward large Anglophone names; THIN_DATA tags apply.
- Category priors embed the normative judgment of the seed frameworks; calibration
  corrects toward observed attention/impact but cannot remove coverage bias.
- Share-price impact is context-dependent (market cap, index membership); CAR
  evidence is one input to severity weights, not the definition of severity.

## Changelog
- v0.1.0 (2026-11-18): 16 seed categories, all `status: seed`.

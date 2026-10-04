# Inspection route v5: held-out synthetic result (primary target missed)

The preregistered 3-step beam planner `cb400_route_beam3` did **not** beat the frozen v3 incumbent
`cb400_route_full` by the required margin. Over 100 fresh paired test lots at **360 CU**, `candidate_only`,
same frozen `cb400/identity` probabilities (latent `frozen_p`, not yield-adjusted), it confirmed
**29.60 vs 29.37** true DOI per lot: relative gain **+0.78%**, paired mean difference **+0.23**, 95%
stratified paired-lot bootstrap CI **[-0.050, +0.530]** (10,000 replicates, seed 2026100407). The rule
needed both gain >= +5% and CI lower bound > 0; **both failed**. No candidate is promoted; the v3 incumbent
stays. This is authored numeric synthetic evidence, not SEM-image accuracy, factory throughput or yield.

Freeze receipt `7fa2e9dd91ab399efd814237eb707ca09baf44cfa282903dd126132d62eac1c1`, source commit
`f748064b40ef452fd5cb17a85ff982bdc731108f`. The test stage was run once by the root coordinator.

| Scope | Beam3 | Incumbent | Paired diff (95% CI) | Relative | Lots beam better / worse / tied |
|---|---:|---:|---:|---:|---:|
| Test, all (primary) | 29.60 | 29.37 | +0.23 [-0.050, 0.530] | +0.78% (CI as share of incumbent mean: -0.17% to +1.80%) | 35 / 23 / 42 |
| stationary | 32.10 | 31.65 | +0.45 | | 8 / 4 / 8 |
| novel_cluster | 31.85 | 31.70 | +0.15 | | 7 / 4 / 9 |
| low_contrast | 23.90 | 24.05 | -0.15 | | 5 / 6 / 9 |
| nuisance_heavy | 30.85 | 30.70 | +0.15 | | 8 / 6 / 6 |
| process_shift | 29.30 | 28.75 | +0.55 | | 7 / 3 / 10 |
| Development, 40 lots (not evidence) | 29.625 | 29.10 | +0.525 [0.05, 0.975] | +1.80% | 20 / 7 / 13 |

Development already missed the +5% target; its numbers are not held-out proof, and the test gain is smaller.
Mean spend was 352.1 vs 352.2 CU; mean loading cost 12.8 vs 12.0 CU (beam switched wafers slightly more),
stage cost 54.0 vs 54.5 CU.

## Secondary: software timing only

Mean cumulative policy time per 360-CU run: **1.414 s** beam3 vs **0.0273 s** incumbent wall clock, about
**52x** (CPU about 50x). These are software seconds on one workstation, measured by the campaign and only
checked for internal consistency by the audit. They are not factory time and not equipment CU.

## Independent audit

`scripts/audit_inspection_route_v5.py` re-derived the result read-only (it never writes into the campaign
root; the sanitized receipt is `evidence/inspection-route-v5/audit.json`). It does not import the route v5
planner/harness/campaign, the v3 policies/harness or `inspection_review.reporting`. It checked:

- freeze digest and pinned identity, all 34 frozen sources in the working tree **and** in the recorded commit's
  git blobs, the 23 reused v1-v3 sources equal to the v3 receipt's hashes (incumbent policy unchanged), study
  files, environment (python 3.12.10, numpy 2.3.3, catboost 1.2.10), v3 receipt/config/model hashes, study
  config = frozen v3 config + `route_v5.beam` only, and the freeze -> test-started -> test lots -> held-out order;
- all 40 development and 100 test lots byte-checked and regenerated from seed; development bytes equal the freeze;
- all **200** held-out ledgers (and all 80 development ledgers) replayed row by row: 6,932 paid test rows,
  7,344 sensor attempts, 412 retries. Per row: allowed/unvisited candidate site, full-retry admission,
  load/stage/movement/dwell/outside/retry bills, charged <= reserved, cumulative spend <= 360, frozen
  probability and reward equal to the recomputed `frozen_p`, no online update, no hidden key, sensor outcome
  from the authored table, labels and stop reason;
- every beam row's projected plan: unique IDs, indices/IDs consistent, allowed, rewards = `frozen_p`, each step's
  reservation with full retry, wafer reload and same-wafer movement, summed score, total <= remaining budget;
  the protocol beam replay reproduced every logged plan and beam count (4,735 unique best, 113 inside the 1e-12
  protocol tie band, resolved identically); all 4,861 incumbent decisions reproduced exactly;
- per-run metrics from the hidden oracle posthoc, and the summary's primary statistics and condition means
  recomputed independently (same preregistered resampling scheme, own code);
- the eight original protected files in `evidence/inspection-research/protected-before-v1.json` unchanged.

Audit status: **passed** (certifies integrity and statistics, not the hypothesis). Wall time about 253 s.

Trusted helpers and limits: the frozen generator/sensor table `inspection_review.data` (`generate_lot`,
`review_observation`) is the outcome oracle; `frozen_p` comes from `inspection_v3.inference.compile_predictor`,
cross-checked against `inspection_v3.model.predict` (<= 1e-12) and the ledger's `frozen_p_sha256`; the model
is checked by hash, never refitted. A defect shared by that frozen generator/sensor and the campaign would not
be detected. The bootstrap reproduces the preregistered scheme, so its independence is in code only.

## Relation to v3

The v3 primary (`cb400_route_full` vs logistic learned, +7.12%, CI [1.46, 2.43]) is a different, earlier
comparison on different lots and is unchanged by this study. v5 only asked whether deeper routing lookahead
adds to the v3 incumbent; on this synthetic benchmark it does not reach the preregistered margin.

## Evidence

`evidence/inspection-route-v5/`: `audit.json` (sanitized audit receipt), `summary.json` (aggregate held-out
and development summaries), `freeze.json`, `protocol.json`. Local absolute paths are replaced by
`<campaign_root>` / `<v3_receipt_root>`; each redacted file records the original file sha256. Per-run
ledgers, lot files, oracle arrays and the model are not committed.

Reproduce: `python scripts/audit_inspection_route_v5.py <campaign-root> --replay-development --out <file>`
with the project inspection virtual environment.

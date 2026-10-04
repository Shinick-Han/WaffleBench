# Inspection route v5 protocol (preregistered before any test lot)

Authored synthetic tabular study. Not SEM-image, factory or yield evidence.

## Question

Does a bounded 3-step receding-horizon beam planner (`route_beam3`) confirm more true DOI
than the frozen v3 incumbent `cb400_route_full` (`route_full_gamma`, gamma 1) at 360 CU in
`candidate_only` mode on fresh lots?

## Frozen inputs reused unchanged

- v3 receipt `C:/Users/user/hacknation7th/output/inspection-v3-build/development-20261004a`
  (`freeze.json` receipt `faadbe49…f3fb`, `frozen-config.json`, `models.json`), verified by
  digest at every stage. Model `cb400/identity` (hash `58aef92c…064b`), predicted through
  `inspection_v3.inference.compile_predictor`.
- The frozen v3 config is used verbatim (cost, sensors, selection, `v2_selection`,
  `v3_selection`); v5 only adds a `route_v5.beam` block read by the new planner.
- Environment must equal the receipt's (python 3.12.10, numpy 2.3.3, catboost 1.2.10).

## Policies

- Incumbent `cb400_route_full`: `inspection_v3.policies.make_policy('route_full_gamma')`, same
  model, same reward (`frozen_p` of cb400/identity, `latent_doi_probability`), unchanged.
- Candidate `cb400_route_beam3`: identical reward source (`inspection_v3.policies.selection_reward`),
  input checks and cost contract. Beam: level 1 = top 16 affordable by `reward/max_cost` plus
  top 2 per wafer; level 2 = from each level-1 site top 16 by `reward/step_cost` plus top 2 per
  wafer; level 3 = the same from each level-2 path. Only allowed, unvisited, not-yet-planned
  sites; each step reserves the full retry; wafer load on every wafer change (including a
  return); movement only on the same wafer; cumulative reserved cost <= remaining budget.
  Every feasible 1/2/3-step path is scored by summed reward / summed reserved cost. Ties:
  value (relative 1e-12), then first-step single value, then lexicographic planned site IDs.
  The first site is executed; the plan is recomputed after every observation. Each row logs
  planned IDs, per-step reserved costs, rewards, total and beam counts.
- No oracle, seed, scenario or `online_p` input; no model update.

## Selection loop

`inspection_route_v5.harness.run_selection` is a scoped copy of the v3 loop with an injected
policy factory (no monkeypatching). Its rows equal the v3 loop's for the incumbent (tested).

## Lots

- Development: 40 lots, seed `11000 + scenario_index*100 + i`, i < 8.
- Test: 100 lots, seed `12000 + scenario_index*100 + i`, i < 20.
- Scenario order: stationary, novel_cluster, low_contrast, nuisance_heavy, process_shift.
- Seeds are checked disjoint from every v1-v4 tabular protocol and the v3 receipt. The
  sensor is the deterministic per-(site, attempt) review table, so both policies see the
  same outcome at the same site (paired).

## Development

Fixed beam settings; no hyperparameter scan and no candidate selection. Development
numbers are not evidence.

## Primary endpoint (test only)

Paired mean `true_doi_confirmed` at 360 CU, `candidate_only`, `cb400_route_beam3` vs
`cb400_route_full` over 100 test lots. Success requires BOTH relative mean gain >= +5% AND
the 95% percentile bootstrap CI of the paired mean difference (lots resampled within
scenario, 10000 replicates, seed 2026100407, `inspection_review.reporting.paired_comparison`)
having lower bound > 0. Policy timing (software CPU/wall) is secondary only.

## Stages

`python scripts/run_inspection_route_v5.py build|dev|freeze|test --root DIR`

- `build` (new dir): verify receipt/env/seeds, generate development lots.
- `dev`: run both policies, write `development.json`.
- `freeze`: requires development, a clean committed tree for all hashed sources and no test
  lots; hashes new and reused sources, this protocol, study files, receipt files, model,
  environment and every development lot's bytes.
- `test`: refused unless the freeze verifies; one-shot `test-started.json` marker refuses
  any rerun or resume. Run by the root coordinator only.

Outputs: aggregated summaries and per-run ledgers. Hidden truth stays in lot `oracle.npz`
files and is read only after a run finishes, for evaluation.

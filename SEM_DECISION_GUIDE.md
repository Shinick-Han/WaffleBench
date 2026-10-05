# SEM decision tools guide

Post-hackathon development, excluded from judging. Package: `sem_decisions/`. Tests:
`tests/test_sem_decisions.py`. Nothing here produces primary or real scientific evidence,
calls a paid or network API, reads credentials, or connects to hardware.

## Commands

```sh
uv run python -m sem_decisions.cli self-check
uv run python -m sem_decisions.cli demo [--seed 2026100502]
uv run python -m sem_decisions.cli evaluate-fixture [--manifest PATH] [--verify-files]
uv run python -m unittest tests.test_sem_decisions
```

`self-check` and `demo` use small synthetic fixtures and print
`SYNTHETIC FIXTURE - NOT SCIENTIFIC EVIDENCE`. The demo is a fresh development simulation
(default seed 2026100502); it does not reuse any earlier held-out data. `evaluate-fixture`
with `--manifest` validates a manifest in the common schema and reports split and class
counts. It does not run a model and has no results to claim.

## Data interface

`manifest.validate` enforces the shared schema_version 1 manifest: `dataset_id='carinthia-s'`,
`data_mode='real'`, an absolute `root`, unique ids, relative image and mask paths under
root, positive sizes, 64-hex digests, splits `train`/`calibration`/`test`, duplicate
groups that never cross splits, and no synthetic items. Test fixtures must say
`data_mode='synthetic_fixture'` and pass only with `allow_fixture=True`. File digests are
checked only with `verify_files=True`, and a missing root is reported as unverifiable.

Class values are kept exactly as recorded. Numeric Carinthia-S classes are not given
semantic names. The summary lists per-split class counts, scarce classes (fewer than 10
items) and classes missing from a split. According to the dataset description, one class
has no visible defect and may reflect SEM misalignment. That is not confirmed physically
good material, so an empty mask is never treated as a verified physical negative. The
description also notes framing artefacts such as a black right border and center bias.
These are potential shortcuts for image models. Per the coordinator, the strict dataset excludes every class-2 item. That exclusion belongs to the data worker's manifest, and this validator does not enforce it. `prospective_view` drops masks, mask
digests and class labels. Class and mask data are for evaluation only and are never policy
features.

## Budget-aware audit policy

`SemBudgetAuditPolicy` implements `select(state, max_cost, affordable) -> Choice` from
`inspection_review.policies` and is not registered in any existing factory.

- **Inputs (read-only):** public view, frozen probabilities, threshold, selected, visited and labeled history, optional `state.embedding` and optional `state.remaining_budget`. The existing state also exposes public site metadata (ids, wafer, xy, layer, candidate flag). The policy uses it only for costs, ids and the trace.
- **Guard:** a name-based tripwire rejects state fields whose names look like oracle, truth, mask, seed, scenario, hidden or future data. It also rejects labels for sites that were never selected. The guard is not a guarantee, because truth stored under an innocuous name goes undetected.
- **Costs:** the harness's `max_cost` and `affordable` are authoritative, and the choice is always affordable. With `config['cost']`, `max_cost` must match `inspection_review.harness.cost_vectors`, including wafer load, movement on the loaded wafer and the retry reserve. The trace itemizes each component.
- **Objectives:** expected DOI per cost (`yield_objective`) and the future-learning score per cost (`learning_objective`: uncertainty, novelty and embedding diversity) are reported separately. They are never summed.
- **Audit allocation:** the fraction of audit decisions is bounded by `audit_min`, `audit_target` and `audit_max`. Audits draw from affordable sites with frozen probability below the threshold. There is no RNG, and ties go to the lowest site index.
- **Backends:** the policy runs pure NumPy (`numpy_deterministic`). modAL and apricot entry points in `learning_backends.py` target pinned commits. The coordinator installed and executed these optional wrappers in the isolated `decisions-env` on synthetic inputs; see `SEM_IMPLEMENTATION_STATUS.md`. This does not change the policy's default backend. `available_backends()` reports `configured`, `installed` and `executed` separately, and fallback results carry `upstream_executed: false`.

## Reliability, shift and acquisition

- **`conformal`:** split-conformal sets need `check_independent` evidence. Calibration ids and duplicate groups must be disjoint from fit and test data. An ambiguous or empty set means "abstain and request review". The evidence's `n_calibration` must match the number of calibration scores. Probabilities must be finite and in [0, 1] and are never clipped, and labels must be observed 0/1 aligned with them. `coverage_statement` always reports `empirical_guarantee: "unverified"`. It gives only a conditional theoretical statement ("coverage ≥ 1 − alpha IF exchangeable"), and only when the caller passes `exchangeability_assumed=True`, the independence evidence matches the model, and a shift diagnostic ran without alerting. A diagnostic that does not alert never establishes exchangeability by itself. The optional MAPIE wrapper was executed on synthetic compatibility inputs in `decisions-env`; it has no measured SEM coverage and gives no guarantee under drift.
- **`shift`:** a max-feature KS test with a permutation p-value that uses measured inputs only. `calibrate_false_alert` estimates false alerts on null reference splits. `label_shift` runs only on observed labels and otherwise reports `unavailable_labels_not_observed`. No Alibi Detect code was copied or integrated.
- **`acquisition`:** the repeat-image protocol uses measured quality features and flags only, and truth or mask inputs are rejected. Costs, time and the retry cap are explicit. Non-finite or boolean parameters are rejected. Outcomes are `observed_positive`, `observed_negative`, `invalid_acquisition` and `unknown`, and only a valid capture can be negative. Thresholds are development defaults, not instrument-calibrated, and every output says so. `hardware_connected` is always false.
- **`offline_tools`:** the NIST-style FN/FP-versus-quality curve is offline-evaluation only. The ARTIMAGEN launch config lists the pinned prerequisites (CMake, libtiff, fftw3, lua5.3). The tool is not built or run, and it is not instrument-calibrated.
- **`jev_payload`:** validates one structured prospective action. It accepts only permitted actions and targets, evidence ids that were actually observed, and a finite cost within the budget. Hidden-truth or credential-like keys are rejected anywhere in the payload. It makes no API calls.

## Limitations

Decision behavior checks use synthetic fixtures. The coordinator subsequently validated
the real Carinthia-S manifest and executed modAL, apricot and MAPIE adapters on synthetic
inputs in a separate environment. MAPIE execution verifies compatibility, not empirical
coverage on SEM data. No real-data decision-performance claim is made. The policy uses
frozen probabilities only and does not update a model. Permutation tests assume
exchangeable rows, which correlated tiles from one image violate.

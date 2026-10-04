# Inspection v3 boundary

This separate authored-synthetic study retains v1/v2 sensor and CU assumptions. No prior frozen source, input, evidence or UI is modified. It uses fresh training, calibration, development and held-out seeds. Previous held-out evidence only motivates hypotheses; it never supplies fitting examples or a selection score.

Three worker owners: `inspection_v3/model.py` + its tests (rank-preserving calibration); `inspection_v3/policies.py` + its tests (vectorized routing); `inspection_v3/sensor_yield.py` + its tests (reported-review-positive operational probability). Coordinator owns namespace init, CLI, protocol, harness, integration tests and reports.

The harness preserves immutable DOI `frozen_p` for classifier and false-negative metrics. A separate immutable optional `selection_reward` is a probability of successful reported positive review, with explicit semantics. A policy never receives oracle, generator seed/scenario, latent kind/size or unpaid future observations. Reservation includes full retry cost before sensor access. A hypothetical second action includes its own switching, stage, dwell, outside-rescan and retry costs and must fit the remaining budget.

Sensor-yield fitting uses only explicitly admitted historical training lots. Its target is an observable review result under the same retry rule; it must not be called latent DOI probability. Training measurements and their costs are reported separately. Logistic and CatBoost DOI fitting receive exactly the same training lots.

Before held-out generation, choose the candidate on development mean confirmed DOI at 360 CU candidate-only, then lower mean spent, then variant ID. Primary comparator is fresh logistic learned; same-model comparator is recorded separately. Primary relative gain target is 5%, with paired lot bootstrap 95% interval. Classification and runtime are separate secondary outcomes. No posthoc replacement of the selected candidate. Negative results and all ablations remain available.

No primary campaign until coordinator M5 checks, source/config/model freeze and an explicit receipt. User authorized autonomous local implementation and validation through the three-hour window. Workers may commit locally; coordinator integrates and releases. External API use, genuine Jev notes and real wafer accuracy remain distinct from this numerical experiment.

# Inspection Performance Improvements v2: Run Guide

A synthetic study based on numerical observations, separate from the v1 study and results. It compares model strengthening, independent probability calibration, audit budget allocation and two-step route planning. It does not imply real SEM image performance or tool throughput.

## Installation and running

Uses Python 3.12, numpy 2.3.3 and CatBoost 1.2.10. CatBoost is not forced into the app's default dependencies; it was added as an optional group.

```powershell
uv sync --extra inspection
uv run --extra inspection python -m inspection_v2.cli develop --root 'C:\path\to\new-development'
uv run --extra inspection python -m inspection_v2.cli freeze --root 'C:\path\to\new-development'
uv run --extra inspection python -m inspection_v2.cli campaign --root 'C:\path\to\new-development'
uv run --extra inspection python -m inspection_v2.cli report --root 'C:\path\to\new-development'
```

`develop` requires a new directory. It generates 12 training, 6 calibration and 20 development lots and compares 8 variants × 2 modes × 3 budgets. Candidate selection is fixed by mean DOI at 360 CU candidate-only on the development data, then mean cost, then ID order. Comparison results on these data are not evidence of final performance.

`freeze` records the selected candidate, the model, source, protocol and data hashes, and package versions. After all sources are verified, `campaign` generates a new set of 60 test lots. Existing runs are never resumed or overwritten. `report` writes documents only from stored results.

## Implementation and roles

- [model.py](inspection_v2/model.py): train-only scaler, frozen CatBoost, the logistic baseline with the existing settings, isotonic calibration on separate lots, candidate-scope AP/Brier/ECE/risk-coverage.
- [policies.py](inspection_v2/policies.py): the existing policies plus adaptive audit and route policies. Wafer loading, move, dwell and retry costs are reserved for the second action as well. Only reported successful observations update the audit utility.
- [harness.py](inspection_v2/harness.py): provides the policy with the actual remaining budget every time. Failure costs, immutable initial probabilities and the paid-observation boundary are the same as v1. No fake online updates are made to CatBoost.
- [jev.py](inspection_v2/jev.py): genuine note whitelist, explicit enablement, typed Choice/Noul, caching, abstention and shadow audit. The default HTTP path allows only the official endpoint and does not follow redirects.

The Jev module requires real inspection notes, which the current numerical harness does not have. `integration_status([])` returns unavailable. The API key is read from the process environment variable `TYPESAFE_API_KEY`. Credentials are not put into logs, payloads, caches or model files. Network requests occur only when `JevClient(enabled=True)` is set explicitly, and real service latency and accuracy require a separate genuine-note evaluation.

The propensity recorded for audit samples is **the conditional probability of drawing a candidate within the decided audit branch**. It does not justify off-policy evaluation of the whole policy.

## Verification and interpreting results

The improvement effect including the model replacement is compared with the original logistic learned baseline, and the effect of changing only inspection selection with the same model is compared with the same-model learned policy. The difference from the existing Falsify is also reported as a secondary comparison. Total discoveries, high-confidence misses, out-of-candidate discoveries, cost and actual CPU latency are recorded together. Unfavorable ablation and calibration results are preserved too.

Even a finalized model and policy do not imply field deployment performance. Defects outside the optical candidates, sensor limits, image/noise differences and real stage costs must be validated with separate data. v2 numbers are not written over the existing v1 results or the public UI.

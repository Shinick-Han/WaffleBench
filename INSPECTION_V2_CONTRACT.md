# Inspection v2 parallel implementation contract

This is a new, explicitly synthetic development study. Preserve every existing v1 inspection file/result and old PVT/UI file. No test-seed tuning. New namespace: `inspection_v2/`.

## Scope ownership

- Model worker: `inspection_v2/model.py`, `tests/test_inspection_v2_model.py` only.
- Selection worker: `inspection_v2/policies.py`, `tests/test_inspection_v2_policies.py` only.
- Jev worker: `inspection_v2/jev.py`, `tests/test_inspection_v2_jev.py` only.
- Coordinator: v2 protocol, harness, experiment CLI, docs, dependencies/lock, acceptance and measured development comparisons.

Workers use independent worktrees from this committed contract. Claude Code Opus 5.5 medium, nested Agent disabled. Local commits and focused tests allowed; no pushes, publications or primary campaigns. Report base/candidate, files, commands/results and residual limitations through herdr and stop.

## Model API

`train_model(lots, config) -> dict` fits only seeds in `config['splits']['train_seeds']`. Each lot is `{public,oracle}` with restored privileged `public.seed/scenario` metadata. Reject duplicates and non-train seeds. Nonstationary TRAIN examples are allowed only when that explicit seed/scenario pair is in `config['v2_splits']['train']`.

`predict(model, features) -> np.ndarray` accepts n x 7 public numeric features, imputes using train-only mean. `hash_model(model) -> str` canonical JSON SHA-256. Models must be JSON serializable with `family`, `mean`, `scale`, `train_ids`, `supports_online_update` and provenance. `config['v2_model']` selects `family` (`logistic` / `catboost`), iterations/depth/lr/seed/thread_count. CatBoost 1.2.10 is coordinator-installed; no silent fallback. Keep CatBoost model frozen during selection and set supports_online_update=False. Logistic may delegate to the frozen v1 model in stationary-only training and may set supports_online_update=True.

`fit_calibration(model, lots, config, method='isotonic') -> dict` returns a NEW model calibrated only with independent seeds in `v2_splits.calibration`; reject overlap/duplicate/unapproved lots. Methods identity/isotonic (NumPy PAV is sufficient), record IDs/raw vs calibrated provenance. No calibration on development/test or per-online observation. Preserve train scaler and raw base model. `update_model` may delegate for uncalibrated logistic; reject unsupported update rather than pretend CatBoost has incremental updates. `evaluate_model` reports candidate-only metrics, PR-AUC/AP, Brier, ECE and risk-coverage with explicit sample scope, not all-site physical recall.

## Policy API

`make_policy(name, seed, config) -> Policy` returns objects using the frozen v1 `SelectionState` / `Choice` interface: `select(state,max_cost,affordable)->Choice`. Never edit v1 policies or access oracle/seed/scenario. Support unchanged v1 baselines by delegating to their factory; new names `adaptive_audit`, `route_aware`, `adaptive_route`. New policies keep model frozen (`updates_model=False`); may read successful selected labels from SelectionState. No registry monkeypatching.

Adaptive audit uses ONLY paid reported labels and current public scores. Configured hard lower/upper audit fractions and rescan frequency; deterministic local random audit sampling, log conditional selection propensity and pool size for random branches, never claim full-policy unbiased OPE. No audit branch outside original candidates; outside rescan separately in with_rescan. Record observable posterior/utility/budget decision reason, support config audit_min=0 ablation.

Route-aware selection uses existing CU first/retry reservation semantics and public wafer/xy. Bounded shortlist and 2-step lookahead comparing per-total-cost reward; no simulated review outcomes in lookahead. Costs for a possible second step must include a wafer revisit/load, same-wafer movement, outside rescan and retry reserve. Do not mutate state to score future actions. Replan after each paid observation. Deterministic tie handling, affordability masking always.

## Jev API

Server-side `JevClient` with injectable transport and explicit TYPESAFE_API_KEY process env; no secret in payload/log/cache. Pin jev-1.13.0, official v1 systemone typed API. Input genuine inspection note records only; whitelist note/id/source/observed_at, never arbitrary oracle dictionaries. Batched Choice/Noul evidence-routing rubric, insufficient-information option, schema and finite-probability validation. Cache by model+rubric+relevant-state hash, in-memory and optional caller-owned local directory. Timeout/HTTP failure returns explicit abstention, never successful mock. API invocation opt-in; offline mode returns unavailable. No images/fine-tuning claims and no numeric equipment control. Audit shadow judgement against expert labels with coverage/errors/tokens/latency. No live API calls during worker tests or inspection campaign. Current simulator has no genuine notes; mark integration unavailable there.

## Common invariants

Budget charged/reserved before observing. Failed/missing attempts billed and never labels. Frozen probability definition cannot change during a run. New data use disjoint seed ranges above 3000. Sensor generation and true outcomes unchanged; no oracle-derived text. Model/policy runtime and equipment CU recorded separately. Meaningful tests must cover split/label leakage, immutable frozen model, impossible actions, cost reservation and failures. External accounts and protected user terminals are outside scope.

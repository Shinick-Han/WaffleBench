# WaffleBench optical-candidate to SEM-review PoC

Created 2026-10-05. Post-hackathon development, excluded from judging.
This is a new isolated development study. No previous primary protocol, consumed test
set, submitted source, deployment or external submission is changed.

## Question and evidence boundary

At a fixed review-time allowance, can movement-aware selection confirm more important
defects than risk-only ordering or a risk-per-time greedy scheduler without increasing
escapes? The first execution uses synthetic fixtures, never a commercial-equipment claim.
Complete historical replay can assess a modeled schedule, not certify actual equipment
throughput. Partial historical logs cannot reveal outcomes of unmeasured actions.
Real instrument comparison requires a prospectively frozen policy, actual incumbent
outputs, measured end-to-end time and independent ground truth including unselected sites.

## Shared interface (new package `routing_poc`)

All interfaces use JSON-compatible dictionaries. Runtime requires only Python stdlib.
Policy inputs are strict whitelists, not arbitrary caller objects.

`job` keys: `schema_version` (1), `job_id`, `data_mode` (`synthetic` or `log_replay`),
`cutoff_utc` (timezone-aware timestamp), `cost`, `candidates`, `provenance`.
`cost`: nonnegative `wafer_load_s`, `settle_s`, positive `move_um_per_s`, integer
`retry_limit` (0 or 1). All numeric values must be finite and not booleans.
Each candidate has `site_id`, `wafer_id`, finite `x_um`, `y_um`, `prior_p` in [0,1],
`optical_observed_at` <= cutoff, `recipe_id`, positive `capture_bound_s`, nonnegative
`inference_bound_s`. These are declared action-time bounds, not future outcome times.
No SEM image, image path, embedding, label, mask, truth, scenario or seed is a candidate
feature. Authentic coordinates/recipes are required for real logs; never invent them
from Carinthia-S image identifiers.

`archive` keys: `job_id`, `observations`, optional `reference`.
`observations` maps site id to a list of at most 2 attempts. Each has `status`
(`ok`, `failed`, `missing`), `reported_doi` (bool only for ok, otherwise null),
`capture_s`, `inference_s`, `observed_at` (> cutoff), and optional `image_path` and
`image_sha256`. If present, image hashes must be verified only after action admission.
No observation occurs without a paid attempted action. Absent observations remain
unknown. An `ok` negative is a detector report, not physical absence of defects.
`reference` is offline evaluator-only: `complete` bool, `source` string and `doi_by_site`
mapping ids to bool/null. Null or absent truth makes escapes/recall unidentifiable.
Reported labels are never silently promoted to independent reference truth.

Functions owned by adapter worker:
- `routing_poc.contracts.validate_job(job) -> sanitized job` (deep copy).
- `routing_poc.contracts.validate_archive(job, archive) -> sanitized archive`.
- `routing_poc.contracts.estimate_cost(candidate, current, cost) -> dict` containing
  `load_s`, `move_s`, `settle_s`, `first_s`, `reserved_s`.
  Current is null or `{wafer_id,x_um,y_um}`; move is charged only on the same wafer.
  First is load+move+settle+capture_bound+inference_bound; reserved additionally
  includes retry_limit*(capture_bound+inference_bound). No future times used.
- `routing_poc.replay.run_loop(job, archive, selector, budget_s, seed=0, observer=None)`.
  `selector(state, rng)` returns a choice or null. State contains `job_id`, `candidates`
  (remaining public candidates, each extended with `cost` above), `remaining_s`,
  `current`, `history` (selected paid outcomes only). Selector gets a copy.
  `rng` is `random.Random(seed)`; it is policy randomness, never latent fixture randomness.
  Charge actual archived capture/inference times, movement/load/settle, including failed
  attempts. Admission reserves full public bounds. An archive time exceeding bounds is
  invalid input, never clipped. Unknown/missing is paid and cannot become negative.
  Use a retry only after failed/missing. `observer(attempt, candidate)` is optional and
  called after admission for an archived image; its returned dict is logged as auxiliary
  perception evidence, never overwrites the detector report or supplies pre-acquisition
  features. Its measured wall time is reported separately from replay resource time.
  No network or hardware commands. Record decision runtime separately: replay resource
  seconds are modeled/recorded action charges, NOT full physical elapsed time.
  Output: `job_id`, `data_mode`, `budget_s`, `spent_s`, `remaining_s`, `rows`,
  `decision_wall_s`, `observer_wall_s`, `stop_reason`, `commercial_validated:false`.
  Rows contain selected id, choice, attempts, final `reported_doi` bool/null,
  `charged_s`, `reserved_s`, `cumulative_s`, `resource_parts`.

Functions owned by policy worker:
- `routing_poc.policies.select(state, rng, policy='beam_route', audit_epsilon=0.1)`.
  Choices contain `site_id`, `reason`, `propensity` (actual conditional selection
  probability), `audit` bool, and `components`. Null if none affordable.
  Policies: `risk_only`, `risk_per_second`, `beam_route`. All share the same frozen priors
  and uniform exploration mixture. No online training or oracle. Unselected sites have
  positive sampling support when affordable and epsilon>0, with propensity epsilon/n
  plus (1-epsilon) for the deterministic best site. Deterministic tie breaks by id.
  Beam depth 3, bounded shortlist 12 plus per-wafer candidates, recompute after each
  observation. Score paths by sum(prior_p)/sum(projected reserved seconds), using public
  cost parameters inferred from candidate cost/current metadata or explicit state cost.
  `state['cost']` is allowed to contain the public job cost for accurate projections.
  Do not claim the heuristic is globally optimal. Pure-Python dependency-free.

Functions owned by experiment worker:
- `routing_poc.fixtures.make_job(seed, regime='clustered') -> (job, archive)`.
  Two preregistered regimes: clustered positions with expensive wafer/movement overhead,
  and low-overhead diffuse positions as a stress/control regime. 120 candidates across
  3 synthetic wafers, probabilistic DOI sampled from public prior_p; complete independent
  synthetic reference and imperfect paid reports; occasional failed/missing attempts.
  No choice-policy-specific fixture construction or edits after held-out inspection.
- `routing_poc.metrics.evaluate(job, archive, run) -> dict`: confirmed reference DOI,
  detector reports, false confirmations, total reference DOI, escapes and candidate recall
  separately. Invalid/unknown never negative. Incomplete reference => recall/escapes null.
  Historical partial observations => counterfactual support explicitly unavailable.
- `routing_poc.campaign.run_campaign(out, split='development', protocol=None)`.
  Refuse existing output, preserve failure artifacts. Fixed budgets 120 and 240 seconds,
  epsilon 0.1, policies above. Development seeds 12000..12007; reserved independent
  synthetic test seeds 22000..22039. Both regimes, paired same archive per policy;
  policy RNG derived from public job id + policy, not latent seed. Write frozen protocol
  hash, source hashes, environment, raw jobs/runs and summary with paired bootstrap CI
  resampling entire lots (2000 replicates, seed 2026100511). No test by default.
  Explicit `--confirm-synthetic-test` freezes source/protocol before running reserved
  test once in fresh output. Never tune on test. Source digests verified after execution.
  `commercial_validated:false` always, regardless of synthetic gain. Candidate-space
  recall is never wafer-wide recall. Include planner/observer wall time separately.
- CLI `python -m routing_poc.cli demo --out PATH` for development, `benchmark --out PATH
  --confirm-synthetic-test` for the preregistered synthetic evaluation, and
  `replay --job PATH --archive PATH --out PATH --budget-s N --policy NAME` for input logs.
  Replay truth metrics computed only post-loop. All reports visibly label evidence mode.

## Ownership and acceptance

Adapter: new `routing_poc/__init__.py`, `contracts.py`, `replay.py`, tests/test_routing_poc_adapter.py,
ROUTING_POC_ADAPTER.md. Policy: new `routing_poc/policies.py`, tests/test_routing_poc_policies.py,
ROUTING_POC_POLICIES.md. Experiment: new fixtures.py, metrics.py, campaign.py, cli.py,
tests/test_routing_poc_campaign.py, ROUTING_POC_STUDY.md. Coordinator owns this contract,
integration repairs, optional observed-image adapter, local report and preview only.
Workers may not edit any existing scientific/UI/module file or each other's files.

Acceptance covers timestamp boundary, unknown/negative distinction, full retry reserve,
cost accounting, post-admission images, no reference leakage, exact propensity,
movement projection, partial-log nonidentifiability and reproducible paired metrics.
Live GEM/SECS, proprietary KLARF support, commercial comparison and fab qualification are
not represented as implemented. Start with JSON exchange and offline replay/shadow plans.

The first synthetic benchmark is authorized as isolated post-hackathon research only.
Its result may be positive, neutral or negative. No commercial ROI or superiority claim
will be derived from invented physical-layer parameters.

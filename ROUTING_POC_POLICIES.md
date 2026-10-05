# Routing PoC choice policies

Created 2026-10-05. Post-hackathon development, excluded from judging.
Policy-worker scope of `ROUTING_POC_CONTRACT.md`: `routing_poc/policies.py`.
Pure Python standard library. No network, hardware, API key, paid call or training.

## API

- `select(state, rng, policy='beam_route', audit_epsilon=0.1)` returns
  `{site_id, reason, propensity, audit, components}` or `None` when no remaining
  candidate's full public reserve (`cost.reserved_s`) fits `remaining_s`.
- `make_selector(policy, audit_epsilon)` returns a `selector(state, rng)` closure for
  `routing_poc.replay.run_loop`.
- Helpers: `sanitize_state`, `project_cost`, `resolve_cost_params`, `beam_plan`,
  `history_counts`, `POLICIES`.

## Input whitelist

`state` may contain only `job_id`, `candidates`, `remaining_s`, `current`, `history`
and optional `cost` (the public job cost). Candidates may contain only the public
contract fields plus the adapter's `cost` (`load_s`, `move_s`, `settle_s`, `first_s`,
`reserved_s`). Any other key (label, truth, reference, image path, embedding, seed,
scenario, …) raises `ValueError` rather than being silently dropped. Numbers must be
finite and not booleans. The caller's state is never mutated.

`history` rows are projected to `site_id` (or `selected_id`) and `reported_doi` only;
nothing else in a row is read. `reported_doi` must be `true`, `false` or null/absent.
Null/absent is counted as `unknown`, never as negative. History is reported in
`components.history` but does not change any choice (no online training). A site that
already appears in history but is offered again raises `ValueError`.

## Policies

All three share the same frozen `prior_p` and the same exploration mixture.

- `risk_only`: highest `prior_p`.
- `risk_per_second`: highest `prior_p / cost.reserved_s` (current-relative reserve).
- `beam_route`: depth-3 beam (width 16) over a bounded shortlist: the top 12 affordable
  sites by risk-per-second plus up to 4 per wafer by movement-independent
  `prior_p / (settle + (1+retry_limit)*(capture+inference bound))`. Step 1 uses the
  declared candidate cost; later steps project load (wafer change), Euclidean
  same-wafer movement, settle, bounds and the full retry reserve from the previous site.
  Paths must fit `remaining_s`; score is `sum(prior_p) / sum(projected reserved_s)`.
  Every feasible prefix of depth 1, 2 or 3 stays eligible as the best final path, even
  when it could be extended (a cheap high-prior single site is not displaced by
  low-prior expensive extensions); only the set expanded at the next depth is pruned to
  the beam width. Exact score ties use the site-id sequence, as in the frozen contract.
  Only the first site is used; the plan is recomputed on every call, i.e. after each observation. This is a
  heuristic, not a globally optimal schedule.

Cost parameters for projection come from `state['cost']` when present; every
candidate's declared cost is then checked against the projection and a disagreement
raises (e.g. a different distance metric in the adapter). Without `state['cost']` they
are inferred from candidate costs and `current`. When no same-wafer movement is
observable (e.g. no wafer mounted yet) the move rate is unknown
(`resolve_cost_params` source `inferred_move_rate_unavailable`) and `beam_route`
raises `ValueError` instead of planning; `risk_only` and `risk_per_second` still work
from the declared current-relative cost. `project_cost` never projects an unknown
same-wafer move between different coordinates as zero; it raises. Passing
`state['cost']` is therefore recommended.

## Exploration mixture and propensity

With `n` affordable sites (sorted by `site_id`) and deterministic best `b`:
one `rng.random()` draw per call; if it is `< epsilon` (`audit: true`,
`reason: audit_uniform`) the site is `affordable[rng.randrange(n)]`, otherwise `b`
(`reason: <policy>_best`). Reported propensity is exact:
`epsilon/n + (1-epsilon)` for `b`, `epsilon/n` otherwise, `1.0` when `n == 1`.
Every affordable site has positive support whenever `epsilon > 0`. Because each policy
consumes the RNG identically, a shared seed yields identical audit decisions across
policies. Exact ties break by ascending `site_id` (beam: site-id
sequence).

`components` includes `policy`, `audit_epsilon`, `n_affordable`,
`deterministic_site_id`, the chosen site's `prior_p`/`first_s`/`reserved_s`,
`remaining_s`, `history`, `score`, and for `beam_route` `path`, `path_prior`,
`path_reserved_s`, `shortlist_size`, `cost_source`.

## Evidence boundary

Seconds are replay resource charges and declared bounds, not physical elapsed time.
No commercial validation is implied. Checks: `python -m unittest
tests.test_routing_poc_policies` (development fixtures only; the reserved synthetic
test is not run here).

# Routing PoC adapter: contracts and offline replay

Created 2026-10-05. Post-hackathon development, excluded from judging. Implements the
adapter scope of `ROUTING_POC_CONTRACT.md` (which remains the authoritative API):
`routing_poc/contracts.py` and `routing_poc/replay.py`. Stdlib only. No network,
hardware, API key or paid call. `commercial_validated` is always `false`.

## Validation (`routing_poc.contracts`)

- `validate_job(job)` and `validate_archive(job, archive)` return sanitized deep copies
  and raise `ContractError` (a `ValueError`) on any violation. Nothing is clipped or
  repaired silently.
- Strict whitelists: unknown job, cost, candidate, archive, attempt or reference keys are
  rejected, so SEM images/paths, embeddings, labels, masks, truth, scenarios or seeds
  cannot enter as candidate features.
- Ids (`job_id`, `site_id`, `wafer_id`, `recipe_id`, observation/reference keys) must
  match `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`; site ids are unique; archive and reference
  keys must be job candidates; `archive.job_id` must equal `job.job_id`.
- Timestamps are ISO-8601 and timezone-aware, compared as instants:
  `optical_observed_at <= cutoff_utc < observed_at`; a retry must be strictly later than
  its first attempt.
- Numbers must be finite and not booleans; `retry_limit` is integer 0 or 1.
- Archive bounds: at most 2 attempts and at most `1 + retry_limit`; no record after an
  `ok` attempt; archived `capture_s`/`inference_s` above the declared bounds is invalid.
- `reported_doi` is a bool only for `ok`, otherwise null (unknown is never negative).
  `image_sha256` (64 lowercase hex) requires `image_path`; `missing` cannot carry an image.
- `reference.complete=true` requires a bool for every candidate; partial references are
  allowed with `complete=false`. The reference is never shown to the selector.

`estimate_cost(candidate, current, cost)` uses only public fields. Load is charged when no
wafer is mounted or the wafer changes; movement is Euclidean distance / `move_um_per_s`
only on the same wafer; settle is always charged. `first_s` = load+move+settle+capture
bound+inference bound; `reserved_s` adds `retry_limit * (capture bound + inference bound)`.
It tolerates extra keys (e.g. the `cost` attached to state candidates) so policies can
reuse it.

## Replay (`routing_poc.replay.run_loop`)

Signature per contract, plus an optional keyword-only `image_root` extension that resolves
relative image paths and refuses paths escaping it (default: working directory).

Each step builds `state = {job_id, candidates, remaining_s, current, history, cost}`.
Candidates are remaining public candidates (sorted by id) each with an `estimate_cost`
result; `cost` is the public job cost; `history` holds only paid selected outcomes
(`site_id`, coordinates, `final_status`, `reported_doi`, `attempt_statuses`,
`charged_s`). The selector receives a deep copy and `random.Random(seed)`.

- `None` stops with `selector_declined`, or `no_affordable_candidate` when nothing fits;
  `candidates_exhausted` when all were visited.
- A choice must be a JSON-compatible dict naming a remaining site, and its full
  `reserved_s` must fit `remaining_s`; otherwise `ContractError` (fail closed, a policy bug).
- Only after admission is the archive read for that site. Actual archived capture and
  inference seconds are charged with load/move/settle, including failed and missing
  attempts. A retry is used only after `failed`/`missing` when `retry_limit` is 1.
- An action with no archive record (common in partial historical logs) is charged at its
  declared bounds and logged `status: "unavailable"`, `reported_doi: null`. It never
  becomes negative and no retry outcome is invented.
- Images: after admission, the hash (if given) is verified. Statuses are `verified`,
  `unverified_no_hash`, `hash_mismatch`, `unreadable`, `outside_image_root`,
  `not_provided`. The observer runs only for `verified`/`unverified_no_hash`, receives
  the attempt without `reported_doi` plus the public candidate, and its dict output is
  logged under `observer`. Exceptions and non-dict outputs are logged as
  `observer_error`. It never overwrites the detector report and never enters `history`.
- `decision_wall_s` (selector) and `observer_wall_s` are measured wall time, reported
  separately from replay resource seconds. `spent_s`/`charged_s` are modeled or recorded
  action charges, NOT physical elapsed time (`time_semantics` says so in every output).

Output: `job_id, data_mode, budget_s, spent_s, remaining_s, rows, decision_wall_s,
observer_wall_s, stop_reason, seed, time_semantics, commercial_validated:false`. Rows:
`site_id, choice, attempts, final_status, reported_doi, charged_s, reserved_s,
cumulative_s, resource_parts{load_s,move_s,settle_s,capture_s,inference_s}`.

## Checks

`python -m unittest tests.test_routing_poc_adapter` covers timestamp cutoff, rejection of
pre-measurement image/truth features, cost reserve vs. charge, missing/failed/negative
handling, unavailable archive records, observer-after-admission and hash mismatch,
unselected images never read, archive bounds, malformed ids and input immutability.
Hand-written development fixtures only; the reserved synthetic test is not run here.

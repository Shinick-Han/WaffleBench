# Falsify Lab integration contract v1

The frozen scientific inputs are RESEARCH_PROTOCOL.md and research-manifest.json. Do not change them during implementation. The existing mockup is preserved. All times and counts in a scientific view must come from stored evidence; an empty export shows an empty state.

## CLI boundary

Implement `python -m falsify_lab.cli <command> --root <campaign-or-demo-directory>`.

- `calibrate`: run the nine calibration observations and freeze the model. Development validation is labeled development, never benchmark results.
- `demo-prepare`: numerical preflight, nine calibration observations, and the fixed seed 1001 initial three search observations. Saves a prepared live run and snapshot, no adaptive updates yet.
- `benchmark`: preflight if needed, forty fixed-policy runs, then only after all search sequences close, held-out/full-grid reference, postflight, report, snapshot. Respect one campaign's global attempt/time caps including preflight, calibration, evaluation and live run when in that campaign.
- `export`: export the existing stored evidence to `<root>/snapshot.json`, without performing experiments.
- `reproduce`: an explicitly labeled short reproduction check, isolated from the primary campaign, with recorded usage.

The CLI prints JSON result/status/path summaries and returns nonzero on failure. Artifacts remain on failure. No full primary campaign until coordinator authorizes M5. Do not silently resume partial primary runs or replace failed selected queries. A controller may start only `demo-prepare` and `reproduce`, one fixed job at a time; it never exposes arbitrary command text or unrestricted benchmark requests. Primary benchmark is coordinator-operated.

## Snapshot JSON v1

`schema_version: 1`, `data_mode: "real"`, `generated_at: ISO timestamp`, `project`, `protocol_hash`, `limitations: string[]`.

- `run`: `{run_id, kind, policy, seed, status, model_hash, budget: {limit, used, remaining}, termination_reason}` or null.
- `model`: `{model_hash, coefficients, calibration_result_ids, form}` or null.
- `observations`: array `{result_id, point_id, pvt: {corner,vdd,temp_c}, phase, tphl_s, tplh_s, tpd_s, cache_hit, wall_time_s, provenance: {netlist_sha256, simulator, raw_meas_lines}}`.
- `evaluations`: array `{result_id, point_id, pvt, model_hash, predicted_tpd_s, simulated_tpd_s, relative_error, abs_relative_error, clear_counterexample, secondary_counterexample}`. Thresholds are strict >0.11 and >0.10; predicted values are never mislabeled as observed.
- `decisions`: array `{sequence, evidence_result_ids, candidates: [{point_id,pvt,role,score,idw_predicted_abs_error,min_normalized_distance,cost_queries}], selected_point_id, remaining_budget, rank_before, rank_after, selection_changed, observed_result_id}`. At least two distinct candidate tests when possible; roles exploitation/exploration. Candidates' true outcomes are absent until revealed.
- `trace`: array `{sequence, actor, action, result_ids, detail, timestamp}`. Only genuine tool/handoff records may be labeled Omnigent; deterministic CLI events are labeled deterministic runner.
- `coverage`: array `{point_id,pvt,state,predicted_tpd_s,observed_tpd_s,abs_relative_error,result_id}`. state is unobserved/observed/calibration/held_out/failed. Hidden truth fields remain null before posthoc evaluation.
- `benchmark`: null before campaign, else `{status, policies: [{id,label,runs: [{seed,status,clear_count,secondary_count,cumulative_clear,first_clear_query,fifth_clear_query,selection_point_ids,cost}],mean_cumulative_clear}], primary: {complete_pairs,paired_differences,mean_difference,ci95,success,reason}, secondary_comparisons, held_out, reference, cost, limitations}`. Report incomplete data honestly; unavailable means/intervals are null. Auxiliary fields may be added, not substitute mock values.
- `cost`: `{logical_queries,attempts,successes,failures,cache_hits,simulation_seconds,policy_seconds,wall_seconds}` or null.

Arrays can be empty. The UI handles absent optional fields and nulls. Static mode fetches `./data/snapshot.json`; local mode GET `/api/snapshot`. Public mode explicitly says recorded-run replay, not remote execution. Local GET `/api/job`; POST `/api/jobs` takes `{kind:"demo-prepare"|"reproduce"}`; POST `/api/jobs/cancel` cancels the owned job. Bind 127.0.0.1, same-origin requests, bounded body, no user-supplied paths, atomic job admission. Export/download uses the same snapshot as the display.

## Ownership and acceptance

Core worker: experiment/model/storage/policies/benchmark/reporting/cli modules, core tests, pyproject.toml and uv.lock. UI worker: web/, scripts/serve_app.py, UI/controller tests. Coordinator: integration contract, milestone state, receipts, live Omnigent execution, evidence publication and release. A fresh later worker owns MCP/agent/launcher updates after the core API is accepted. No overlap without coordinator direction.

Workers use separate Git worktrees and exact base commits. Commit local changes on the worker branch; do not push, publish, start a primary campaign, or submit. Report exact base/candidate, focused checks and untested behavior through herdr; stop after completion. Permission is already provided for folder trust within the task worktrees and routine implementation/checks. Do not enable worker subagents.

# WaffleBench core API (M1–M2)

Research-path modules under `falsify_lab/`. The legacy `experiment.py` surrogate is
connection-test only and is never used here.

| Module | Role |
|---|---|
| `protocol.py` | Loads and verifies `RESEARCH_PROTOCOL.md` + `research-manifest.json` (hash, partitions, SHA256 split/orders, thresholds, budget, policy parameters). `Point`, normalized `coords`, `full_grid()`. |
| `simulator.py` | `NgspiceSimulator` (binary from `FALSIFY_NGSPICE`, never copied), numerical settings `basic` (1 ps, identical to the connection netlist), `half` (0.5 ps), `tight` (0.5 ps + RELTOL 1e-4, VNTOL 1e-7, ABSTOL 1e-13). `FixtureSimulator` = NON-SCIENTIFIC test stand-in. |
| `model.py` | Frozen 5-coefficient log OLS model; rank-5 check; `FrozenModel` with `model_hash`; `evaluate()` with strict `> 0.11` / `> 0.10`. |
| `storage.py` | `Campaign`: hash-chained append-only ledger, thread + cross-process lock, attempt admission against caps, fingerprinted write-once evidence cache, runs, run-scoped access, restricted (preflight/posthoc/postflight) store. |
| `policies.py` | `rank()` and `build_decision()` for the four frozen policies. |
| `benchmark.py` | `preflight`, `calibrate`, `open_and_seed`, `step`, `preview_decision`, `run_policy`, `posthoc`, `postflight`, `run_benchmark`, `demo_prepare`, `reproduce`. |
| `reporting.py` | `run_summary`, `paired_comparison`, `paired_bootstrap`, `benchmark_report`, `build_snapshot`, `export_snapshot` (snapshot JSON v1). |
| `cli.py` | `python -m falsify_lab.cli <calibrate|demo-prepare|benchmark|export|reproduce> --root DIR` |

## CLI

```
python -m falsify_lab.cli calibrate    --root runs/development/<name>      # kind development (default)
python -m falsify_lab.cli demo-prepare --root <demo-root>                  # kind demo: preflight, calibration, seed 1001 initial 3
python -m falsify_lab.cli reproduce    --root <reproduce-root>             # kind reproduce: calibration + adaptive/random seed 1001
python -m falsify_lab.cli benchmark    --root <dev-root> --seeds 1001 1002 # development subset only
python -m falsify_lab.cli benchmark    --root <campaign> --kind primary --confirm-primary   # coordinator only, M5
python -m falsify_lab.cli export       --root <root> [--focus-run RUN_ID]  # no experiments; empty root -> empty snapshot
```

Output is one JSON object (`ok`, result fields, `usage`, `snapshot` path). Exit code is
nonzero on failure or an incomplete benchmark. Every command writes `<root>/snapshot.json`.
A root is bound to one campaign kind; `benchmark` never resumes or extends a started
benchmark, and a run id is never reopened.

## Information-flow API for a later MCP worker

```python
from falsify_lab.storage import Campaign, AccessDenied
from falsify_lab.simulator import NgspiceSimulator
from falsify_lab import benchmark as bm

c = Campaign(root, NgspiceSimulator())              # or Campaign(root, None) read-only
rid = "live-adaptive_idw_plus_distance-1001"
bm.preview_decision(c, rid)    # candidates (exploitation/exploration, score parts), nothing recorded
bm.step(c, rid)                # record fixed-rule decision, then spend one query on it (runner path)
c.revealed_result_ids(rid)     # ids revealed in this run only
c.get_observation(rid, result_id)   # AccessDenied unless revealed in rid
c.close_run(rid, "reason")
```

Lower level: `c.record_decision(rid, decision)` then `c.query(rid, point_id, "search", decision_sequence)`.
A search query must execute a recorded decision for the same point. Held-out,
prior-excluded and calibration points are denied in the search phase without
consuming budget. Failed queries consume budget, mark the run incomplete and are never
replaced (a failed point cannot be re-queried). A repeat of a revealed point consumes
budget, is served from the run's own evidence and adds no distinct point.

## Live Omnigent loop (MCP, M3)

`falsify_lab/mcp_server.py` serves the prepared demo root named by `FALSIFY_RUNS_DIR`
and only the fixed run `live-adaptive_idw_plus_distance-1001`. Tools take no root, run
or path argument; there are no shell/filesystem tools. `FALSIFY_MCP_ROLE` (written into
each sidecar by `launch.ps1 -Setup`) gates tools server-side and sets the ledger actor
`Omnigent <role>`; without it every tool is refused.

| Role | Tool | Effect |
|---|---|---|
| analyst | `analyze_and_plan(finalize=false)` | appends one `analysis_update` for the newest search result (if not yet recorded), then records the fixed rule's next decision (≥ 2 distinct candidates, evidence ids, score parts, budget). With a pending decision it returns that decision and appends nothing. |
| analyst | `analyze_and_plan(finalize=true)` | appends the last `analysis_update`, returns the next preview **unrecorded**, closes the run with `live_finalized`. |
| experimenter | `simulate_pvt_point(point_id, decision_sequence)` | executes exactly the pending recorded selection; anything else (held-out, other point, wrong sequence, malformed) is denied without spending budget. A failed query is spent, never replaced, and closes the run `query_failed`. |
| both | `read_result(result_id)` | one observation revealed in the live run; other ids are denied. |

`analysis_update` payload: `update_index, observed_result_id, observed_point_id, query_index,
decision_sequence, observed_abs_relative_error, observed_clear_counterexample,
remaining_candidates, rank_before, rank_after, selection_changed, next_candidates,
next_selected_point_id, next_decision (recorded | not_recorded_finalized |
not_recorded_search_quota_reached), evidence_result_ids, actor`. `rank_before`/`rank_after`
rank the same remaining set without/with that observation. The initial preparation is not an
update. Each state-changing tool call also appends `mcp_tool_call {tool, actor, status,
decision_sequence, query_index, result_ids}`.

Live budget: 9 calibration + 3 initial + at most 4 search = 16 logical queries
(`live_demo_max`, `adaptive_updates_max`); a 5th decision raises `BudgetExhausted`.
`close_run` of a live run is `complete` only with `termination_reason == "live_finalized"`,
2..4 successful search queries, no failure, no pending query or decision; the payload adds
`search_queries`, `pending_decisions`, `remaining_budget`. Every other stop is `incomplete`.
Benchmark and reproduce runs keep 24 queries and their completion rule.

Supervisor workflow (agent/config.yaml): analyst plan → experimenter execute → analyst plan →
experimenter execute → analyst finalize/interpret, stop (5 delegations; caps 12/6/6).
`scripts/run_live.py` (coordinator, Omnigent tool interpreter) runs that one session through
`omnigent_client` (`GET /v1/agents`, `GET /v1/hosts`, JSON `POST /v1/sessions`,
`SessionsChat.send`) and writes the sanitized `<demo-root>/omnigent/session-proof.json`;
raw stream events and items stay in `.omnigent-runtime/live-proof/`. It exits 0 only if
every observed supervisor response completed, the workflow settled and the ledger shows the
live run complete with ≥ 2 updates made by `Omnigent` sidecar calls.

The supervisor delegates asynchronously: `SessionsChat.send` returns at its FIRST
`response.completed` (typically "waiting for the analyst") while sub-agents and later wake
turns keep running. The driver therefore never judges on that stream. After it, it tails
`sessions.stream` (reattaching on errors) and polls with GETs only (`sessions.get`,
`subtree_busy`, the ledger) every `--poll-interval` s until the root is `idle` and no
sub-agent is busy continuously for `--settle-grace` s (default 60), or the global
`--timeout` (whole workflow). Settled with a closed ledger → judged; settled without one →
failed; root `failed`, a root `last_task_error`, or any observed `failed/incomplete/cancelled`
response → that outcome. The proof's `responses` lists every terminal supervisor response
observed (with its usage and source), `workflow_follow` the poll/stream record, and
`attempts` the truthful attempt history. Nothing is ever posted after the one prompt.

`--recover-session ID` re-attaches to an existing session without a prompt, session create,
interrupt or experiment. It requires this root's proof to name `ID`, the same live run and
agent, the session to be bound to the proof's `agent_id`, and the ledger to extend the
proof's recorded live run (existing decisions/closure are allowed only here); otherwise it
exits 2 and writes nothing. The raw prior proof is copied to the local live-proof directory
before the proof is rewritten; prior responses are carried over marked `from_prior_attempt`.

Snapshot additions: `updates` (focus run's `analysis_update` payloads);
`decisions[].post_result_update = {observed_result_id, rank_before, rank_after,
selection_changed, next_candidates, update_index, next_decision, actor}` when the real update
for that decision's result exists (`rank_before`/`rank_after` on the decision itself keep the
recorded meaning: the previous observation's impact on this choice); `trace` includes
`analysis_update` and `mcp_tool_call`, `detail.decision_sequence`/`detail.query_index` are
run-local (`trace.sequence` is the global ledger number), and a search `query_result` executed
by a tool call carries that call's actor and `detail.via_tool`; `omnigent_session` is the
runner's sanitized proof when present for the focus run, else null. Deterministic CLI events
stay `deterministic runner`.

## Ledger and budget semantics

- Campaign caps: attempts 1200 and wall 60 min from campaign creation, covering preflight,
  calibration, runs, posthoc and postflight. Primary campaigns cannot override caps;
  fixture/development campaigns may (tests only).
- Every physical attempt is admitted (`attempt_admitted`) under the lock before ngspice
  starts; success, failure, timeout and crashes stay counted.
- Logical (run) queries vs physical attempts are counted separately (`cost`).
  Cache reuse is limited to the same campaign and identical fingerprint
  (measurement schema, setting, netlist sha256, simulator version + binary sha256) and
  the record hash bound in the ledger; anything else raises `CacheIntegrityError`.
- Preflight/posthoc/postflight evidence lives in `restricted/` and is never served to a run.
  Posthoc starts only after every run in the campaign is closed and blocks new runs.

## Policy details

- `min_normalized_distance` is measured to every location queried in the run
  (calibration + all search queries, including failed ones); IDW uses the calibration
  points and the run's successful revealed points.
- Exact score ties break by corner string, VDD, temperature.
- Decision candidates: exploitation = best IDW; exploration = best maximin point distinct
  from exploitation (next maximin point if the optima coincide); the policy's own choice is
  added as `policy_choice` if it is neither. Selection is never changed by this.
- `rank_before` uses the evidence without the latest outcome (same queried locations);
  `selection_changed` compares the top choice before/after that outcome.

## Statistics

Primary: adaptive − random unique clear counterexamples over the 15 search queries,
only with 10 complete seed pairs; otherwise `mean_difference`, `ci95`, `success` are null
with a reason. Bootstrap: numpy `Generator(PCG64(20261004))`, 10,000 resamples of seed
pairs, `np.quantile(..., [0.025, 0.975], method="linear")`. Success = mean ≥ 2 and lower > 0.
`first_clear_query`/`fifth_clear_query` index the 15 search queries; null when not reached.

## Known limitations

- The 60-minute wall cap of a `demo` root counts from `demo-prepare`; run the live loop
  within that window or prepare a fresh demo root.
- The coordinator preserves the original frozen manifest bytes through the
  `research-manifest.json -text` Git attribute. Early development worktrees used LF
  before this correction; their byte hashes differ, while canonical JSON and scientific
  inputs agree. Primary campaigns and new clean checkouts preserve the original bytes.
  The ledger records byte and canonical-JSON hashes separately.
- `wall_seconds` is first-to-last ledger event; idle time inside one root is included.

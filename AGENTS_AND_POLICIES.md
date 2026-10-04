# WaffleBench agents and policies

This page tells judges which agent roles exist in the repository, what each role decides, what it can call and what it is denied. Every row cites the configuration or source file it comes from. Python packages, agent names and paths keep their original `falsify_lab` / `falsify_supervisor` / `inspection_*` identifiers; WaffleBench is the presentation name.

Labels used below:

- **Human-authored**: the original benchmark hypotheses, protocol, diagnostic catalog, budgets and role allowlists. The separate discovery analyst authors its diagnostic hypotheses and interpretations.
- **Tool-computed**: produced by deterministic code or a simulator/sensor through an MCP tool and stored with a result ID. Agents copy these values; they do not compute them.
- **Unknown**: not established by evidence in this tree. Absence of a recorded failure is not treated as proof of correctness.

## Who decides what

| Decision | Owner | Source |
|---|---|---|
| Research question and hypotheses H1/H2 | Human-authored, frozen | [RESEARCH_PROTOCOL.en.md](RESEARCH_PROTOCOL.en.md) (lines 12–18; authoritative Korean original [RESEARCH_PROTOCOL.md](RESEARCH_PROTOCOL.md)), [research-manifest.json](research-manifest.json) |
| Inspection objective and policy comparison | Human-authored, frozen | [INSPECTION_PROTOCOL.en.md](INSPECTION_PROTOCOL.en.md), [INSPECTION_LIVE_CONTRACT.md](INSPECTION_LIVE_CONTRACT.md) |
| Execution authorization (through M6) | Human / coordinator, recorded | [milestones.json](milestones.json) `execution_authorization`; [DATA_CONTRACT.md](DATA_CONTRACT.md) (no primary campaign before coordinator authorizes M5) |
| Budgets and caps (live circuit 16 queries; inspection 120 CU, 4 reviews) | Human-authored constants, enforced in code | [research-manifest.json](research-manifest.json) `budget.live_demo_max`, `live_demo.adaptive_updates_max`; [inspection_live/session.py](inspection_live/session.py) `prepare(..., budget=120, max_reviews=4)` |
| Which point / site is tested next | **Frozen deterministic planner** (numerical) | Circuit: `preview_decision` in [falsify_lab/benchmark.py](falsify_lab/benchmark.py) (line 137) with scoring in [falsify_lab/policies.py](falsify_lab/policies.py). Inspection: frozen `cb400_route_full` preview in [inspection_live/session.py](inspection_live/session.py) (line 320) |
| Delegation order and carrying IDs between specialists | **Omnigent coordination** (LLM supervisor) | [agent/config.yaml](agent/config.yaml), [inspection_agent/config.yaml](inspection_agent/config.yaml) |
| Measured values (delays, sensor reports, CU charged) | Tool-computed (ngspice / authored synthetic sensor) | [falsify_lab/mcp_server.py](falsify_lab/mcp_server.py), [inspection_live/mcp_server.py](inspection_live/mcp_server.py) |

Omnigent agents are not credited with the numerical selection algorithm. Both supervisor prompts forbid re-ranking or replacing the planner's choice ([agent/config.yaml](agent/config.yaml) line 58; [inspection_agent/config.yaml](inspection_agent/config.yaml) lines 26–27), and both MCP servers deny any experiment other than the recorded pending selection ([falsify_lab/mcp_server.py](falsify_lab/mcp_server.py) line 274; [inspection_live/mcp_server.py](inspection_live/mcp_server.py) → `review_site` in [inspection_live/session.py](inspection_live/session.py) lines 469–471).

## Role table

The six original agents run on Omnigent with `harness: claude-sdk`, `model: claude-opus-5-5`, `reasoning_effort: medium`, and `skills: none`. No agent declares `os_env`.

### Circuit loop (seed 1001 live run)

| Role | Decision owned | Tools | Inputs | Outputs | Budget / call caps | Denied capabilities | Source |
|---|---|---|---|---|---|---|---|
| `falsify_supervisor` | Delegation order (5 fixed steps) and verbatim hand-off of `point_id` / `decision_sequence`; stop on error | Agents only: `analyst`, `experimenter` | Prepared live run (9 calibration + 3 initial observations already stored) | Chain question → hypothesis → two candidate tests → choice and budget → result_id → interpretation → updated ranking, quoted from specialists | `max_iterations: 12`, `timeout: 900`; guardrail `max_tool_calls_per_session` limit 12 | No shell/filesystem (no `os_env`); may not override or re-rank the frozen choice, invent numbers, retry, replace failed points or start other runs (prompt rules) | [agent/config.yaml](agent/config.yaml) |
| `analyst` | None numerical; records the planner's decision and analysis update by calling the tool | MCP `analyze_and_plan`, `read_result` | `finalize` flag; result IDs named by supervisor | Decision with ≥2 candidates, score parts, evidence IDs, budget; update `rank_before` / `rank_after` / `selection_changed` (copied) | `max_iterations: 6`, `timeout: 600`; tool-call cap 6 | `simulate_pvt_point` (server-side role allowlist); no root/path/run-id arguments; refuses planning with fewer than two distinct candidates | [agent/agents/analyst/config.yaml](agent/agents/analyst/config.yaml); `ROLE_TOOLS` in [falsify_lab/mcp_server.py](falsify_lab/mcp_server.py) lines 50–53, 202 |
| `experimenter` | None; executes exactly the recorded selection | MCP `simulate_pvt_point`, `read_result` | `point_id`, `decision_sequence` copied verbatim | Stored `result_id`, measured delays, evaluation, provenance, budget (copied) | `max_iterations: 6`, `timeout: 600`; tool-call cap 6; live run budget 16 logical queries | `analyze_and_plan`; any point other than the pending recorded selection (denied, no budget spent); retry or replacement of failed points | [agent/agents/experimenter/config.yaml](agent/agents/experimenter/config.yaml); [falsify_lab/mcp_server.py](falsify_lab/mcp_server.py) lines 50–53, 274 |

### Inspection loop (authored synthetic lot)

| Role | Decision owned | Tools | Inputs | Outputs | Budget / call caps | Denied capabilities | Source |
|---|---|---|---|---|---|---|---|
| `inspection_supervisor` | Delegation order (exactly 5 via `sys_session_send` / `sys_read_inbox`) and verbatim hand-off of `site_id` / `decision_sequence` / `result_id`; stop on error or denial | Agents only: `inspection_analyst`, `inspection_experimenter` | One prepared paid live session on a fresh synthetic lot | Per-decision chain and closed status / final spend, quoted from specialists | `max_iterations: 16`, `timeout: 1200`; tool-call cap 16; `spawn_bounds` `max_dispatches_per_turn: 5` | No shell/filesystem (no `os_env`); may not choose, rank or replace sites, send a sixth delegation, retry, or treat a negative report as proof of no defect (prompt rules) | [inspection_agent/config.yaml](inspection_agent/config.yaml) |
| `inspection_analyst` | None numerical; records the frozen planner's decision, updates and finalization | MCP `analyze_and_plan`, `read_result` | `finalize` flag; result IDs named by supervisor | Decision with candidates and score parts, evidence IDs, budget (limit/spent/remaining), analysis updates, closed status (copied) | `max_iterations: 6`, `timeout: 600`; tool-call cap 6 | `review_site` (server-side role allowlist); unknown arguments refused; no root/path/shell/seed/scenario arguments | [inspection_agent/agents/inspection_analyst/config.yaml](inspection_agent/agents/inspection_analyst/config.yaml); `ROLE_TOOLS`, `_check_args` in [inspection_live/mcp_server.py](inspection_live/mcp_server.py) lines 49, 73–91 |
| `inspection_experimenter` | None; pays for exactly the pending recorded site | MCP `review_site`, `read_result` | `site_id`, `decision_sequence` copied verbatim | `result_id`, reported DOI/kind, status, attempts, charged CU, cumulative spend, frozen DOI probability, selection reward and its meaning (copied) | `max_iterations: 6`, `timeout: 600`; tool-call cap 6; session cap 120 CU and 4 admitted reviews | `analyze_and_plan`; any site other than the pending selection; reviews past the cap or the CU budget (denied, nothing charged) | [inspection_agent/agents/inspection_experimenter/config.yaml](inspection_agent/agents/inspection_experimenter/config.yaml); [inspection_live/mcp_server.py](inspection_live/mcp_server.py) line 49; [inspection_live/session.py](inspection_live/session.py) lines 452–479 |

## Enforcement layers, and what each one is

| Layer | Kind | Where |
|---|---|---|
| Role tool allowlists | Code, server-side; a role sees and may call only its listed tools; unset role exposes nothing | `ROLE_TOOLS` in [falsify_lab/mcp_server.py](falsify_lab/mcp_server.py) and [inspection_live/mcp_server.py](inspection_live/mcp_server.py) |
| Pending-selection check, caps, budget | Code, in the stored ledger; denials spend nothing | [falsify_lab/mcp_server.py](falsify_lab/mcp_server.py), [inspection_live/session.py](inspection_live/session.py) |
| Tool-call and dispatch caps | Omnigent guardrail policies | `guardrails` blocks of every config listed above |
| "Never invent numbers", "never retry" | Prompt rules only; not an OS sandbox | Agent `prompt` fields |
| No unrestricted shell | No agent declares `os_env`; MCP servers offer no shell or path tools | Configs; module docstrings of both MCP servers; [README.md](README.md) "Security boundary" |

The README states the limits of this boundary: on native Windows Omnigent runs in degraded mode with no filesystem or network isolation, and the MCP server runs as the user.

## What is not in place

- **No runtime human approval gate.** Human approval exists before a run (frozen protocol, budgets, role allowlists, execution authorization). Nothing pauses a running loop for a person to approve the next experiment.
- **No demonstrated parallel scientific experiments by Omnigent.** Both workflows are strictly sequential: one delegation at a time, analyst → experimenter → analyst → experimenter → analyst.
- **No literature agent.** No role searches or reads literature.
- The original circuit and inspection hypotheses are human-authored and frozen. The separate diagnostic analyst now authors and revises hypotheses over a fixed catalog; it does not change a primary benchmark or execute its proposed prospective study.

## Attribution and its evidence

| Claim | Status | Evidence |
|---|---|---|
| Inspection loop: real Omnigent 0.16.0 supervisor completed five ordered delegations, two paid reviews (`ir_71b91846930cb176`, `ir_6573c8a47872df63`), two analysis updates, 26.066613859 of 120 CU charged, session finalized | Verified by proof | [evidence/inspection-live/session-proof.json](evidence/inspection-live/session-proof.json), [evidence/inspection-live/verification.json](evidence/inspection-live/verification.json) (`status: passed`), [INSPECTION_LIVE_RESULTS.md](INSPECTION_LIVE_RESULTS.md) |
| Inspection: the two child conversations were reused across delegations; raw SDK records (6 `sys_session_send`, 3 `review_site`, 4 `analyze_and_plan`) normalize to 5 / 2 / 3 genuine calls | Documented normalization | Same proof (`raw_dispatch_records`, `collapsed_record_artifacts`); [INSPECTION_LIVE_RESULTS.md](INSPECTION_LIVE_RESULTS.md) |
| Circuit loop: Omnigent seed-1001 demonstration | **Not verified in this tree.** [RESULTS.md](RESULTS.md) describes it and links `evidence/live/omnigent/session-proof.json`, which is not present at this base commit | — |
| Ledger `actor` strings (`Omnigent <role>` in the circuit server, `mcp-sidecar:<role>` in the inspection server) | Role labels written by the sidecar from its environment; not proof of orchestration by themselves | [falsify_lab/mcp_server.py](falsify_lab/mcp_server.py) line 341; [inspection_live/mcp_server.py](inspection_live/mcp_server.py) docstring |
| Benefit of orchestration over a deterministic planner without agents | **Unknown.** Not measured | [RESEARCH_PROTOCOL.en.md](RESEARCH_PROTOCOL.en.md) line 124; [INSPECTION_LIVE_RESULTS.md](INSPECTION_LIVE_RESULTS.md) |
| Classifier accuracy, factory throughput, equipment performance from the live inspection run | **Unknown.** The run is an authored synthetic demonstration outside the primary benchmark | [INSPECTION_LIVE_RESULTS.md](INSPECTION_LIVE_RESULTS.md) |
| A negative sensor report means a site has no defect | Not claimed. Unmeasured and negative sites remain unknown | [inspection_agent/config.yaml](inspection_agent/config.yaml) line 59 |

## Verified bounded diagnostic discovery cycle

See [DISCOVERY_CYCLE_RESULTS.md](DISCOVERY_CYCLE_RESULTS.md) and its linked SDK proof. The session completed five sequential delegations, two computations, two updates and finalization. This is posthoc analysis, not fresh held-out performance evidence.

| Role | Decision and tools | Enforced limits | Config |
|---|---|---|---|
| `discovery_supervisor` | Coordinates five delegations; `read_context`, `finalize` | Tool cap 20, iteration cap 20, timeout 900 s, dispatch bound 5 | [config](discovery_agent/config.yaml) |
| `discovery_analyst` | Authors hypotheses; compares at least two available tests; `read_context`, `record_plan`, `record_update` | Tool cap 8, iteration cap 8, timeout 600 s; each later plan names all earlier result IDs; update before next plan | [config](discovery_agent/agents/discovery_analyst/config.yaml) |
| `discovery_experimenter` | Runs the exact pending catalog selection with `run_experiment` | Tool cap 4, iteration cap 4, timeout 600 s; total cycle cap two executions; no retry after failed admission | [config](discovery_agent/agents/discovery_experimenter/config.yaml) |

[Core](discovery_cycle/core.py) enforces catalog IDs, role allowlists, immutable input hashes and a hash-chained ledger; [MCP server](discovery_cycle/mcp_server.py) exposes no free-form code, shell or path argument. Configs use Claude Opus 5.5 medium with the Claude SDK harness and no `os_env`. Native Windows still has no OS isolation.

The human approved a read-only catalog and cap before the run. An outside-catalog request is refused and would require separate authorization; this is not a runtime interactive approval gate. The next prospective study remains proposed and unexecuted. Literature agents and Omnigent parallel scientific runs remain undemonstrated.

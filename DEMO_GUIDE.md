# Try WaffleBench

The public demo needs only a browser. The evidence pages replay actual recorded runs. The separate [fresh discovery page](https://shinick-han.github.io/WaffleBench/discovery-run.html) starts a new Claude CLI-backed Omnigent diagnostic session when you press **Start new research loop**. No account or API key is required from judges; the owner supplies the authenticated runtime.

The fresh cycle runs two read-only diagnostic computations over existing authored synthetic evidence. Follow the actual ledger phase and counts, then read the verified hypotheses, competing studies, results, revisions and final proposed experiment. The final proposal is unexecuted. This is not new physical inspection or a new held-out accuracy gain. It takes several minutes. One active run, a cooldown and a limited shared quota apply. If the PC or temporary tunnel is offline, use the recorded cycle below.

## A three-minute judging path

1. Open [inspection evidence](https://shinick-han.github.io/WaffleBench/inspection-evidence.html). Read the equal-budget result and confidence interval. Step through the paid inspection replay, switch wafer and result filters, and click a recorded inspection to inspect its observation. Unmeasured sites stay unknown. This is authored synthetic wafer data, not physical inspection.
2. Open the [diagnostic discovery cycle](https://shinick-han.github.io/WaffleBench/discovery-cycle.html). Read the analyst's initial hypothesis and competing tests. Follow `miss_partition` to its measured values and scenario-specific revision, then `capacity_bound` to the next prospective proposal. Expand the raw result groups. Check the result IDs, role limits and verification receipt. These two computations diagnose existing evidence; they do not establish a new performance gain.
3. Open the [inspection agent replay](https://shinick-han.github.io/WaffleBench/inspection-live.html). Press Next to see each delegation, paid review, update and remaining budget. Play advances recorded stages; it does not run an LLM. This shows actual Omnigent coordination of a frozen numerical planner.
4. If time allows, inspect [real PCB photographs](https://shinick-han.github.io/WaffleBench/inspection-images.html) and [misses and capacity](https://shinick-han.github.io/WaffleBench/inspection-quality.html). Read the negative result, fixed replication, remaining misses and CPU cost. PCB evidence does not prove SEM or fab performance.

The evidence pages display published records; the fresh discovery page displays only its newly completed and verified session. Exact JSON downloads preserve the received bytes. Both modes use genuine SDK and MCP calls; the evidence-page Play and Next controls advance recorded stages.

## Reproduce without a Claude account

Clone this public repository, install its Python dependencies using the README, then run:

```powershell
python -m unittest tests.test_discovery_cycle tests.test_discovery_cycle_proof -v
python scripts/run_discovery_cycle.py --root evidence/discovery-cycle --verify
```

These checks recompute the existing diagnostics and compare SDK arguments, outputs, delegated turns, result IDs and ledger hashes. They send no prompts and execute no new diagnostic campaign. The second command refreshes a local verification receipt and export.

## Start a fresh Omnigent diagnostic cycle locally

This requires Windows, Omnigent 0.16.0, the repository's Python environment and an authenticated Claude SDK account. Model coordination can consume account usage. The repository does not contain credentials.

```powershell
./scripts/launch_discovery.ps1 -Setup -Validate -Server -Live -CycleRoot runs/discovery-cycle/judge-new-cycle
```

Use a new root. The launcher uses isolated local port 6773 and refuses an occupied port. It prepares the same frozen diagnostic input and lets the analyst choose two tests from a preauthorized catalog. The result can differ in reasoning and test order; this is still analysis of existing synthetic evidence, not a new physical or held-out study. The separate public runtime uses isolated port 6775 behind its bounded API; see [runtime setup](PUBLIC_DISCOVERY_RUNTIME.md).

## What Omnigent actually decides

The inspection replay uses a **frozen numerical planner** to choose sites; Omnigent owns delegation and result hand-off. In the separate diagnostic cycle, the **analyst owns the diagnostic hypothesis, choice among competing catalog tests, interpretation and next proposal**. Deterministic tools own the numeric results. The final prospective proposal remains unexecuted.

See [agents and policies](AGENTS_AND_POLICIES.md), [diagnostic results and proof](DISCOVERY_CYCLE_RESULTS.md), and [English submission summary](SUBMISSION_EN.md). No new performance improvement caused by Omnigent orchestration, parallel scientific experiment, literature-search agent, commercial-superiority result or 10× discovery-speed gain is claimed.

# Try WaffleBench

The public demo needs only a browser. It is a read-only workbench for inspecting actual recorded runs and their evidence. It does not start a new Omnigent session, operate equipment or charge an API account.

## A three-minute judging path

1. Open [inspection evidence](https://shinick-han.github.io/WaffleBench/inspection-evidence.html). Read the equal-budget result and confidence interval. Step through the paid inspection replay, switch wafer and result filters, and click a recorded inspection to inspect its observation. Unmeasured sites stay unknown. This is authored synthetic wafer data, not physical inspection.
2. Open the [diagnostic discovery cycle](https://shinick-han.github.io/WaffleBench/discovery-cycle.html). Read the analyst's initial hypothesis and competing tests. Follow `miss_partition` to its measured values and scenario-specific revision, then `capacity_bound` to the next prospective proposal. Expand the raw result groups. Check the result IDs, role limits and verification receipt. These two computations diagnose existing evidence; they do not establish a new performance gain.
3. Open the [inspection agent replay](https://shinick-han.github.io/WaffleBench/inspection-live.html). Press Next to see each delegation, paid review, update and remaining budget. Play advances recorded stages; it does not run an LLM. This shows actual Omnigent coordination of a frozen numerical planner.
4. If time allows, inspect [real PCB photographs](https://shinick-han.github.io/WaffleBench/inspection-images.html) and [misses and capacity](https://shinick-han.github.io/WaffleBench/inspection-quality.html). Read the negative result, fixed replication, remaining misses and CPU cost. PCB evidence does not prove SEM or fab performance.

All displayed data comes from published records. Exact JSON downloads preserve the original bytes. The recorded runs happened through genuine SDK and MCP calls; the public pages are replays of those runs.

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

Use a new root. The launcher uses isolated local port 6773 and refuses an occupied port. It prepares the same frozen diagnostic input and lets the analyst choose two tests from a preauthorized catalog. The result can differ in reasoning and test order; this is still analysis of existing synthetic evidence, not a new physical or held-out study. No public hosted live-run endpoint is provided.

## What Omnigent actually decides

The inspection replay uses a **frozen numerical planner** to choose sites; Omnigent owns delegation and result hand-off. In the separate diagnostic cycle, the **analyst owns the diagnostic hypothesis, choice among competing catalog tests, interpretation and next proposal**. Deterministic tools own the numeric results. The final prospective proposal remains unexecuted.

See [agents and policies](AGENTS_AND_POLICIES.md), [diagnostic results and proof](DISCOVERY_CYCLE_RESULTS.md), and [English submission summary](SUBMISSION_EN.md). No new performance improvement caused by Omnigent orchestration, parallel scientific experiment, literature-search agent, commercial-superiority result or 10× discovery-speed gain is claimed.

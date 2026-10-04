# WaffleBench — recorded diagnostic discovery cycle

This is one genuine Omnigent 0.16.0 session, separate from the frozen performance benchmarks. Claude Opus 5.5, medium effort, with the Claude SDK harness, coordinated an analyst and experimenter through five ordered delegations. The analyst authored the hypotheses, selected diagnostic studies from a human-authorized catalog, and interpreted each result. Deterministic tools computed the numbers. The two diagnostics used privileged posthoc labels from existing synthetic evidence: they did not run a new held-out campaign or inspect physical wafers.

## What the agent chose and learned

| Stage | Actual record | Learning |
|---|---|---|
| First hypothesis | Most latent defects are lost among admitted but unselected candidates | Compared `miss_partition`, `capacity_bound` and `calibration_shift`; chose `miss_partition` to locate losses before choosing the follow-up |
| First result | `res_aed06de352d5a8af` | Per lot: 162.48 admitted but unselected DOI, 77.83 never optically admitted, 32.29 selected; 29.33 confirmed, 2.80 sensor-missed and 0.16 unresolved |
| Revision | Overall selection loss dominates, but the hypothesis fails for low-contrast lots | Low contrast: 177.80 unadmitted versus 49.75 unselected DOI per lot; optical recall ceiling 29.88%. Process shift has a mixed bottleneck |
| Second selection | Compared `capacity_bound` and `calibration_shift`; chose `capacity_bound` | Asked whether the review budget was close to its capacity limit or being underused, with a stated 0.8 support / 0.6 falsification threshold |
| Second result | `res_13e4a5ad1338e30f` | 34.65 mean reviews per lot versus an optimistic bound of 38 at 360 CU: 91.18% of that bound. The bound ignores movement and failures and is not achievable equipment throughput |
| Proposed next study | Contrast-adapted optical admission on fresh paired lots | Hold the review policy and budget fixed; test low-contrast confirmed DOI with a paired 95% interval and a preregistered non-inferiority margin on other scenarios. **Not executed** |

The agent interpreted the review budget as binding and prioritized admission for low contrast. This is an agent interpretation, not a proven causal intervention: the diagnostic does not measure what a new admission policy would improve, and calibration was not tested. The observation supports scenario-specific bottleneck analysis rather than a universal optical-bottleneck claim.

## Verification and reproduction

- Session: `93ce9d9f48e9453d88898c13ebe8f83d`; completed and finalized.
- [Session proof](evidence/discovery-cycle/omnigent/session-proof.json), [sanitized SDK records](evidence/discovery-cycle/omnigent/sdk-records.json), [verification](evidence/discovery-cycle/verification.json), [hash-chained ledger](evidence/discovery-cycle/events.jsonl).
- The verifier matched five actual delegations, two specialist executions, two result-bound updates and one supervisor finalization against the core ledger. It recomputed diagnostic values and checked the input hash, prior-result dependencies, role tools and exact SDK arguments/outputs. Adapter mirrors are collapsed only with positive call-ID evidence; genuinely repeated calls remain failures.
- Input: existing [quality diagnostics](evidence/inspection-improvements-v3/quality-diagnostics.json), SHA-256 `ffae6077a5e96e237ad1ad4c5887cffa6e382529bfdace50cf3ae2da44c366ee`.
- Recheck the published record without an API call: `python scripts/run_discovery_cycle.py --root evidence/discovery-cycle --verify`. This writes a fresh local receipt and export; it does not send a prompt or execute a diagnostic.
- Run the focused checks: `python -m unittest tests.test_discovery_cycle tests.test_discovery_cycle_proof -v`.
- A new Windows live run requires installed Omnigent and the Claude SDK account: `./scripts/launch_discovery.ps1 -Setup -Validate -Server -Live -CycleRoot runs/discovery-cycle/your-new-root`. The launcher renders ignored machine-specific MCP configs. Each prepared root permits one SDK session; recovery collects that session without sending another prompt.

The runtime is native Windows with no OS filesystem/network sandbox. Role allowlists, a two-execution cap and the catalog are enforced in code; outside-catalog actions are denied. That denial is not an interactive human approval screen. The proposed prospective experiment requires a new preregistration and authorization. No parallel scientific experiment or literature-search agent is demonstrated.

Zero inspection CU for these arithmetic diagnostics does not mean zero LLM cost or wall-clock time. No new inspection-accuracy, discovery-speed, factory-throughput, commercial-superiority or 10× improvement is claimed. The primary result remains 29.33 versus 27.38 confirmed DOI per lot, +7.12%, on 100 fresh authored synthetic lots at the same 360 CU budget.

The current recorded verification passed 63 checks. The frontend shows only a completed, verified export and downloads its exact bytes.

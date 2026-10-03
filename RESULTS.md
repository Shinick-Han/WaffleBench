# Frozen research result — 4 October 2026

The adaptive IDW + distance policy discovered **5.1 unique clear counterexamples on average**, compared with **1.4 for random selection**. The paired difference is **+3.7**, with a prespecified paired-bootstrap 95% interval of **[3.2, 4.1]**. All ten seed pairs completed. This satisfies the frozen primary criterion: mean gain at least two, with interval lower bound above zero.

This is a comparison of deterministic selection rules in a single computational world. It does not establish an LLM speed-up, a silicon result, or a general advantage across circuits.

## What was executed

The protocol and manifest were frozen before implementation and remain unchanged. Each of four policies ran from the same three initial search points for each of ten seeds. Every run spent 24 logical queries: nine calibration queries, three shared initial search queries and twelve subsequent choices. The five-coefficient log-delay surrogate stayed frozen throughout. A clear counterexample requires absolute relative error strictly greater than 11%; the secondary threshold is strictly greater than 10%.

The experiment uses a generic Level-1 CMOS inverter with a 20 fF load, five synthetic process conditions, seven supplies and five temperatures. Process labels do not identify a foundry PDK. The 175-point grid contains nine training points, 125 search points, forty held-out points and one previously observed, excluded point.

All forty runs completed successfully. Only after every search run closed did the tools evaluate the full grid, including all forty held-out points. No selected failure was retried or replaced; no failures occurred. The independent audit checked the ledger chain, per-run budgets and initial points, decision evidence available at choice time, positive delay measurements, threshold flags, every cumulative count, bootstrap calculation, numerical checks and caps.

## Policy comparison

| Policy | Complete runs | Mean clear counterexamples | Adaptive minus policy | Paired 95% interval |
|---|---:|---:|---:|---:|
| IDW + distance | 10/10 | 5.1 | — | — |
| Random | 10/10 | 1.4 | +3.7 | [3.2, 4.1] |
| Space filling | 10/10 | 3.5 | +1.6 | [1.0, 2.2] |
| IDW without distance | 10/10 | 3.1 | +2.0 | [1.0, 3.0] |

The ten primary paired gains, in seed order 1001–1010, are `2, 4, 5, 4, 4, 4, 4, 3, 4, 3`. The bootstrap jointly resamples these seed pairs 10,000 times using NumPy PCG64 seed 20261004 and linear percentile quantiles. Seeds vary starting conditions within the same fixed simulation world; they are not ten independent physical experiments. The secondary comparisons are descriptive and have no multiplicity adjustment or separate confirmatory success claim. Adaptive selection did not win every individual secondary comparison: for seed 1008, IDW without distance found seven clear counterexamples versus adaptive's six.

## Where the frozen model fails

The posthoc reference found thirteen clear counterexamples among the 125 search points and four among the forty held-out points. The prior-excluded point was also a clear counterexample; it never entered search or policy evaluation. There were eighteen clear counterexamples over the full 175-point grid.

All eighteen were in the predefined low-supply group (VDD ≤ 1.8 V; 75 points). The high-supply group (VDD ≥ 2.9 V; 50 points) had none. Mean absolute relative error was 7.68% in the low-supply group and 1.53% in the high-supply group. This is the frozen exploratory H2 comparison, not a new confirmatory test or evidence about intermediate voltages omitted from those groups.

The forty held-out points had mean absolute relative error 4.93%, maximum 28.22%, four clear and five secondary counterexamples. Held-out accuracy here describes the frozen delay model and an IDW error map; no retraining occurred after observing held-out truth.

## Actual Omnigent demonstration

A separate seed-1001 live run used the same frozen model. Omnigent's supervisor delegated to an analyst and an experimenter. The analyst recorded two distinct candidate tests per decision; the experimenter executed the recorded selection; the analyst recorded the new result's effect on the next ranking. Two search experiments and two analysis updates completed, using 14 of the maximum sixteen live logical queries, with two remaining and no pending decision.

| Live observation | Predicted delay | Observed delay | Absolute relative error |
|---|---:|---:|---:|
| FF, 1.2 V, 125°C | 224.24 ps | 162.20 ps | 38.25% |
| FF, 1.5 V, 125°C | 131.83 ps | 112.77 ps | 16.90% |

The first observation changed the next selection to FF, 1.5 V, 125°C. The second changed parts of the ranking but preserved the next preview's top selection, FF, 1.2 V, 85°C. That preview was not executed. These two observations substantiate failure at those particular points, rather than all FF or all hot conditions.

The stored metric uses observed delay as its denominator, `|prediction − observation| / observation`. Natural-language agent explanations may paraphrase this imprecisely; the UI and statistics use the independently checked tool values. The SDK driver's first attempt to collect proof ended at a waiting response while the same session continued asynchronously. The original report is retained privately and recovery collects that session's complete workflow without issuing another experiment.

## Numerical stability and cost

All fifteen preflight measurements passed positivity and the 0.5% relative-difference bound across the frozen basic, half-step and tightened-tolerance settings. The three largest adaptive-seed-1001 search errors were checked with both stronger settings after search; all six postflight measurements remained within the same bound. The largest postflight deviation was approximately 0.00425%, far smaller than the observed model errors.

| Scope | Logical queries | Physical attempts | Failures | Measured simulation time | Measured wall time |
|---|---:|---:|---:|---:|---:|
| Primary campaign, including setup and reference | 969 | 194 | 0 | 12.144 s | 46.738 s |
| Separate live demonstration, including setup | 23 | 29 | 0 | 1.613 s | 101.540 s |

Each search run still pays its full logical budget even when an identical physical simulation is cached. The primary campaign's nine separate model-fitting queries explain `969 = 9 + 40 × 24`; the live root similarly includes nine model-fitting queries in addition to its fourteen-query run. Physical primary attempts comprise fifteen preflight, nine calibration, 112 benchmark, 52 posthoc and six postflight attempts. The full reference evaluates 175 points; existing identical, hash-verified records supply the remainder, producing 123 restricted cache hits. Posthoc reference cost is included and disclosed, not hidden as a saving. The primary had 848 run cache hits. It stayed within 1,200 attempts and sixty minutes.

Development tests and short reproductions are separate usage scopes. They are not included in the table or presented as primary evidence. Claude subscription usage is reported as recorded token metadata where available, without converting it to an invented monetary cost.

Across both published research roots, the total was 223 physical attempts and 468.714 seconds from the first live admission to the last primary event. Thus the combined scientific execution also stayed below 1,200 attempts and sixty minutes. The later read-only proof recovery does not run simulations. Development, isolated reproduction and packaging are outside that scientific execution interval.

## Evidence and practical limits

The public package contains [the frozen protocol](RESEARCH_PROTOCOL.md), [manifest](research-manifest.json), the primary [report](evidence/primary/benchmark_report.json), [snapshot](evidence/primary/snapshot.json), raw scientific ledger/netlists/measurements, the [independent audit](evidence/primary/independent-audit.json), and the sanitized [live session proof](evidence/live/omnigent/session-proof.json). The public workbench replays the live campaign; the comparison tab reads the separate primary campaign. The snapshot explicitly records both provenance and cost scopes.

This is a compact, reproducible demonstration of falsification-driven experiment selection, with real simulation and actual agent handoffs. Generalizing it requires independent circuits, richer device models or validated PDKs, genuinely expensive evaluations, and a comparison that isolates the contribution and cost of agent orchestration. The error heuristic is not a calibrated uncertainty estimate. No production design sign-off, manufacturing applicability or human-researcher acceleration is claimed.

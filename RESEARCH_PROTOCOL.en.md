# Falsify Lab: Falsification Experiment Design for a Circuit Model

> **English companion translation.** The frozen, authoritative protocol is the Korean original
> [`RESEARCH_PROTOCOL.md`](RESEARCH_PROTOCOL.md) (SHA-256
> `45e04ba3feea0d0fea1ccf236530ee7168f061753d82c1bc8f9d0fc3dd473ff9`, recorded in
> [`research-manifest.json`](research-manifest.json) and verified by the code). This file is a
> faithful translation for English readers. It is not hashed or loaded by any tool and does not
> change the protocol. If the two ever differ, the Korean original governs.

This is an internal research design finalized on October 4, 2026. It fixes the comparison method and evaluation criteria before the research is run. It does not mean an external preregistration has been completed. The current implementation is for connectivity verification; the model calibration, comparison experiment and evaluation separation that match the design below have not yet been implemented.

The research question is: **Can the failure conditions of a circuit delay model that reflects process, voltage and temperature but is calibrated with little data be found more efficiently by choosing the next experiment based on results?** We compare the number of counterexamples found under the same simulation budget. The study draws on the user's experience in semiconductor simulation and ML research, and demonstrates Falsify's themes of falsification and experimental design through real computation.

## Research hypotheses and scope of interpretation

The primary hypothesis H1 is that adaptive selection finds more clear counterexamples than random selection within 15 search steps. The comparison with geometric space-filling is a predefined strong-control evaluation, and the comparison with the variant that removes the exploration bonus is a component analysis. All results are published together.

The secondary hypothesis H2 is that errors may be larger at low voltage because of PVT interactions the simple model cannot represent. Low voltage is fixed as VDD ≤ 1.8 V and high voltage as VDD ≥ 2.9 V. This comparison is an exploratory analysis over the full grid and was set with reference to the existing connectivity tests. It is not treated as a hypothesis that proves a new physical law or a phenomenon of a real process.

The outcome of this study is a reproducible method for checking the valid range of a circuit model under a limited budget. Results obtained from the current generic Level 1 device model are evidence within that computational model. Generalizing to real manufacturing processes, FinFET, TCAD or silicon requires validation with independent device models, circuits and measurements.

## Circuit and variables

The existing ngspice 47 CMOS inverter is kept: 20 fF load, 50 ps input rise/fall time, NMOS W/L 1 µm/0.35 µm, PMOS 2.5 µm/0.35 µm. The output rise and fall delays are each measured at the 50% crossing, and the mean tpd is used. The device parameters are the generic public Level 1 model in the current repository, not a foundry PDK.

| Axis | Fixed values |
|---|---|
| Synthetic process condition | TT, FF, SS, FS, SF |
| VDD | 1.2, 1.5, 1.8, 2.2, 2.5, 2.9, 3.3 V |
| Temperature | −40, 0, 27, 85, 125 °C |

This gives 175 conditions in total. The process-condition names do not denote real foundry corners. The current code's definition of NMOS/PMOS threshold-voltage shifts of ±50 mV and KP changes of ±10% is used as is. The hashes of the conditions, netlist, model and simulator binary are linked to the results.

## Delay model under comparison

The current voltage-only 1-point calibration model is replaced in the main study. Every experimental method uses the same model below.

`log(tpd / 1 ps) = b0 + bv * g(VDD) + bt * ((T - 27) / 100) + bn * n + bp * p`

`g(V) = log[(V / (V - 0.55)^1.3) / (2.5 / (2.5 - 0.55)^1.3)]`

`n` and `p` are the NMOS and PMOS threshold-voltage shifts of the synthetic process condition, each divided by 50 mV. The five coefficients are fitted by least squares in log space to the actual ngspice results at the following 9 points. Regularization, regularization strength and functional form are not tuned to the results. Before running, the matrix is checked to have column rank 5.

- TT / 1.2 V / 27 °C
- TT / 2.5 V / 27 °C
- TT / 3.3 V / 27 °C
- TT / 2.5 V / −40 °C
- TT / 2.5 V / 125 °C
- FF / 2.5 V / 27 °C
- SS / 2.5 V / 27 °C
- FS / 2.5 V / 27 °C
- SF / 2.5 V / 27 °C

After calibration the model is frozen. During search, the **error map is updated** and the next experiment changes. Retraining the delay model itself would change the very definition of a counterexample, so it is not done in this comparison. This function is a research approximation inspired by the alpha power law; it does not claim to implement the full delay equation of the original paper.

## Definition of a counterexample and numerical verification

The error is `e = abs(predicted_tpd - simulated_tpd) / simulated_tpd`. The study's error tolerance is set at 10%. To leave a margin at the numerical boundary, **a clear counterexample for the primary metric is defined as e > 11%**. Results with `e > 10%` are also published as a secondary metric. 10% and 11% are design choices of this study and do not denote an industry standard.

Before the main run, the following 5 points are compared under the default time step of 1 ps, 0.5 ps, and 0.5 ps with stricter convergence-tolerance settings. The strict settings are RELTOL=1e-4, VNTOL=1e-7 V, ABSTOL=1e-13 A.

- TT / 2.5 V / 27 °C
- TT / 1.2 V / −40 °C
- SS / 1.2 V / 125 °C
- FF / 3.3 V / −40 °C
- FS / 1.8 V / 85 °C

We check that the tpd difference at each point is at most 0.5% relative, and that both directional delays are positive and were measured successfully. If any check fails, the main run does not start. The measurement method is fixed and the reason and time of the design revision are recorded. The numerical verification results are not provided as input to the search policies. After the main run, the top 3 highest-error points of adaptive seed 1001 are also checked with the two strengthened settings, and that verification is presented separately from the primary metric.

## Data split and prior observations

The earlier connectivity verification already observed the delays at TT / 2.5 V / 27 °C and SS / 1.2 V / 125 °C. The former is a common calibration point for all methods. The latter is excluded from the main search and held-out evaluation and kept only for connectivity verification. The earlier −58.7% error belongs to the previous delay model and is not reused as a result of the new model.

Of the 175 points, excluding the 9 calibration points and the 1 already-seen SS point leaves 165 points; from these, 8 points per process condition, 40 points in total, are fixed for held-out evaluation. The remaining 125 points form the search candidate set. The coordinates are specified in `research-manifest.json`.

The split is determined without using result values, by SHA256 ordering of the coordinates and a fixed string. The ground truth of the 40 held-out points is queried only in the evaluation process after all search orders have been finalized. Raw results are kept in a separate path the agents cannot read, and MCP only permits result IDs published in that run. The current configuration, which grants no file access, supports this information flow, but it is not a security boundary that isolates the whole of Windows.

## Budget and controls

Every method has a **logical simulation budget of 24 runs**: 9 calibration and 15 search. Of the 15 search steps, the first 3 points are shared by all methods for the same seed. Each method chooses the remaining 12 points. Re-querying the same point or reading from the cache does not grant a new selection opportunity. Calibration and initial-search costs are not excluded from the improvement factor.

There are 10 seeds, 1001 through 1010. Each seed's initial 3 points and random order are determined by SHA256 of a fixed string, the seed and the coordinates. Comparisons are made within the same seed to reduce the effect of the starting point. These are repetitions that measure the variation of the initial selection in the same computational world, not 10 independent semiconductor device experiments.

| Method | Rule for choosing the next point |
|---|---|
| Adaptive | The point maximizing the IDW prediction of observed absolute error + an unexplored-distance bonus |
| Random | The next unqueried point in a random order fixed from the start |
| Space-filling | The point whose minimum distance to already-queried points is largest. Does not look at result values |
| No exploration bonus | The point maximizing only the IDW-predicted error |

Distance coordinates normalize each of the four axes, NMOS shift, PMOS shift, VDD and temperature, to 0–1. The IDW weight is `1 / (distance^2 + 1e-9)`. Only the actual absolute errors of the 9 calibration points and the search points published in that run are used. The distance bonus is fixed at `0.05 * minimum_distance / 2`. This value is an untuned design choice. Ties are broken by taking the first point when sorted by corner string, VDD, then temperature.

Because space-filling is independent of results, once the initial 3 points are fixed the remaining order can be computed in advance. It is a control that checks whether simple advance planning can achieve a similar effect. The IDW score is a heuristic of learning quantity and is not presented as a probability, information gain or calibrated uncertainty.

## Evaluation and success criteria

The primary metric is the number of distinct clear counterexamples found within 15 search steps. Calibration points are not counted as discoveries. The primary comparison is the per-seed difference between adaptive and random.

The design's practical success criterion is a mean difference of **2 or more** and a lower bound above 0 of the 95% interval from a 10,000-resample percentile bootstrap that resamples seed pairs together. The bootstrap random seed is fixed at 20261004, and linear interpolation of the 2.5% and 97.5% quantiles is used. This is the result of a small study conditional on one grid and 10 starting points, and is not to be interpreted as proof of general superiority.

The space-filling and no-exploration-bonus comparisons are secondary analyses that each present the mean difference, the same interval and the raw data for the 10 seeds. If adaptive does not beat space-filling, we state that the added value of a complex selection method was not confirmed. We do not promote whichever of several secondary analyses is favorable to the primary result.

The following secondary metrics are also published.

- The number of simulations until the first and fifth counterexamples. If not reached within 15 runs, it is recorded as not reached and is not replaced with 0 or a success value.
- The cumulative number of discoveries per search step and the area under that curve.
- The mean absolute error of the IDW error map at the 40 held-out points. This is not retraining performance of the delay model.
- The number of process conditions covered by the counterexamples found and their distribution over voltage and temperature ranges.
- Logical query count, actual ngspice run count, cache use, simulation time, policy computation time and total elapsed time.
- The agent call count, token information and total time of the live Omnigent demonstration. Subscription usage is not arbitrarily converted into money.

The post-hoc evaluation cost of obtaining the full ground truth for held-out points and search candidates is stated separately from the research budget. This verification is possible because the simulator is small enough for an exhaustive post-hoc check; it is not hidden to claim a reduction in total computation. Even if there are too many or too few failures for the comparison to discriminate, the thresholds, model and split are not changed and that result is reported.

## Omnigent collaboration and next decisions

The supervisor agent manages the fixed goal, budget and selection rules. The experiment agent simulates only the specified conditions and passes on stored result IDs. The analysis agent computes the error of the frozen model and updates the error map. It returns two candidates: one that follows the error prediction to find counterexamples, and one that widens the unexplored region. Each candidate carries observation-evidence IDs, score components and the same cost of 1 query. The supervisor selects using the fixed score and leaves the following record.

`question → hypothesis → two candidate tests → choice and budget → result_id → interpretation → updated ranking and next test`

The actual time required by the two unobserved candidates is assumed to be equal and treated as 1 query. The observed actual run time is recorded separately. Numbers invented after seeing results are not put into the decision rationale. Whether the ranking and choice actually changed before and after a result is compared mechanically, and cases where they did not change are shown as they are.

The live demonstration is fixed in advance to seed 1001. After the same 9 calibration points and 3 initial points, at least 2 and at most 4 Omnigent discovery loops are run with real ngspice. The seed is not switched to one whose results look good. The tool-call limit is kept and the demonstration is shortened if necessary. The 40 runs of the policy comparison are performed by a runner that automates the same decision rules.

What the quantitative comparison evaluates is the **experiment selection policy**. The live record shows **Omnigent's ability to pass evidence between specialist agents and run the loop**. This design does not separately measure an intelligence effect of the LLM itself or acceleration relative to a human researcher. Improvements produced by a deterministic policy are not described as an intrinsic effect of the LLM.

## Execution limits and when there are no results

When simulations are newly run, the limit for the primary comparison is 4 methods × 10 starting points × 24 runs = 960 runs. With at most 175 for the separate exhaustive evaluation, 15 for pre-run numerical verification, at most 16 for the live demonstration and 6 for post-run numerical verification, the total is at most 1172 successful runs. The limit on all actual attempts is 1200, and failed attempts are also counted in a separate ledger and are not hidden by retrying. The elapsed-time limit for the whole run is 60 minutes. The current per-ngspice limit of 60 seconds is kept.

A failed query consumes 1 unit of that run's budget and is not counted as a counterexample discovery. It is not automatically replaced with the next point. A run with a numerical error is marked incomplete, and the number excluded from the paired comparison and the reason are published. The primary hypothesis success criterion is judged only when 10 complete seed pairs exist. When a limit is reached, execution stops and the unrun and incomplete items are stated.

If H1 is not supported, it is not dressed up as adaptive always winning. We report in which process, voltage and temperature regions the simple model is valid or fails, why space-filling or random was competitive, and the next experiment. If the data are missing or too simple, we distinguish that only the tool-collaboration demo has been demonstrated.

## Items to implement

1. 9-point calibration and frozen model storage, linking protocol, coefficient and model hashes.
2. Per-run result publication based on the manifest, blocking access to held-out points, and a budget ledger that counts even failed attempts.
3. Comparison runs of the 4 policies and 10 starting points, post-hoc evaluation and raw-data export.
4. MCP output of the two candidates, the selection rationale and the before/after ranking, plus Omnigent handoff records.
5. A demo showing the cumulative counterexample discovery curve, the PVT error map, held-out error and actual cost.

The current `propose_next_point` emits a single candidate from the previous voltage-only model and overwrites the last decision file. This function is not taken to mean the research is already implemented. A calibrated model, multiple candidates, an append-only decision record and comparison runs are needed. The earlier description in the preparatory README will be updated after this protocol is implemented.

## Supporting literature and challenge conditions

- [Databricks challenge original](https://app.hack-nation.ai/api/documents/c0bdd796-5a59-4550-b36a-f4f223ce1409): requires real Omnigent collaboration, at least two experiment candidates, next decisions based on results, and reproducible evidence. A 10× improvement does not have to be proven.
- [Sakurai and Newton's alpha power law paper](https://courses.ece.ucsb.edu/ECE125/125_W11Banerjee/Lectures/SAK90a.pdf): research background for a simple CMOS delay equation using voltage and a device model. It does not guarantee the accuracy of this approximate model.
- [Garnett et al. 2012](https://arxiv.org/abs/1206.6406): distinguishes active search, which finds conditions of interest under a limited query budget, from surveying, which estimates the overall proportion. Background for separating the primary discovery metric from the held-out evaluation.
- [Jiang et al. 2017](https://proceedings.mlr.press/v70/jiang17d.html): studies exploration and exploitation in active search and budget-dependent selection. This repository's simple IDW policy does not implement that paper's algorithm.
- [ngspice official documentation](https://ngspice.sourceforge.io/docs.html): the basis for the simulator and its measurement and numerical settings.

If the protocol or manifest is changed, the reason for the change and the new hash are recorded. A change after seeing the main experiment results is marked as a new exploratory study and the existing results are preserved.

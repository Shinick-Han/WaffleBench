# Wafer Precision Review Selection Study v1: Results

**Cost-weighted ranking by the frozen model confirmed more DOIs than Falsify. The 20% discovery-increase and 30% cost-saving goals were not met.** No parameters or test seeds were modified after seeing the results.

## Implementation and evaluation scope

A synthetic tabular-data harness was implemented that selects precision reviews after optical screening. A logistic model was trained on 12 past lots, with 4 validation lots and 60 test lots kept separate. The test set is 12 independent lots for each of 5 conditions, each lot with 3 wafers and 3915 care sites. The full test set is 180 wafers and 234,900 sites; including training and validation, 228 wafers and 297,540 sites. There is 1 care site per die, which does not mean every structure inside the die was exhaustively inspected.

2,880 runs were completed: 8 policies × 2 modes (candidates only / out-of-candidate rescan allowed) × independent budgets of 120, 360 and 720. The cost unit is a synthetic tool cost unit set by the researcher, not real seconds, prices or tool throughput. Sensor sensitivities are not field-calibrated values either.

## Primary comparison

Within candidates at a cost limit of 360, the mean number of true-positive confirmed DOIs was **23.67** for Falsify and **27.77** for the frozen learned model. The paired difference is **-4.10**, the independent-lot 95% bootstrap interval is **[-4.65, -3.55]**, and the relative discovery yield is **-14.8%**. The primary hypothesis result points toward rejection. This does not mean our prototype performs worse than commercial inspection tools; it is a comparison between fixed selection policies under these synthetic conditions.

| Policy | Confirmed DOI/lot | Confirmed misses of the initial model/lot | Confirmed high-confidence negative misjudgments/lot |
| --- | ---: | ---: | ---: |
| Frozen learned model | 27.77 | 0.00 | 0.00 |
| Uncertainty/diversity | 26.08 | 0.38 | 0.00 |
| Falsify | 23.67 | 1.27 | 0.15 |
| Recipe-like score | 22.17 | 0.98 | 0.02 |
| Random | 5.13 | 1.35 | 0.03 |
| No audit | 27.55 | 0.47 | 0.00 |
| No spatial term | 24.27 | 0.83 | 0.12 |
| No model update | 23.23 | 1.13 | 0.10 |

Falsify confirmed an average of 1.27 misses of the initial model, but that is not more than random's 1.35. High-confidence negative (p<0.1) misjudgments totaled 9 for Falsify and 2 for random. No significance or industrial value is claimed for these secondary results. They reveal a trade-off between discovery yield and audit performance.

## Why the primary goal was not met

Removing the audit raised the mean confirmed DOIs to 27.55 but reduced confirmed misses to 0.47. This ablation suggests that the opportunity cost of the fixed negative-audit slots accounts for a large part. The no-model-update and no-spatial-term variants were also measured, and we do not, after seeing these results, switch only the well-performing ablation to be the post-hoc primary policy.

The fixed learned ranking secures many DOIs by exploiting strong existing candidate features. The falsification-search objective alone did not automatically raise production inspection discovery yield at the same cost. Inspecting low-score and out-of-candidate regions can find classifier errors or screening misses, but it has to be paid for.

## Per-condition results and the sensor ceiling

| Synthetic condition | Learned ranking DOI | Falsify DOI | Falsify confirmed misses | Share of true DOIs included in initial candidates |
| --- | ---: | ---: | ---: | ---: |
| Normal distribution | 30.83 | 26.33 | 1.17 | 90.1% |
| New defect cluster | 30.92 | 26.67 | 2.33 | 90.1% |
| Low signal contrast | 20.00 | 17.58 | 1.58 | 33.2% |
| Increased nuisance | 31.33 | 25.50 | 0.25 | 91.4% |
| Process feature shift | 25.75 | 22.25 | 1.00 | 56.4% |

Under low signal contrast, the initial screening put only about 33.2% of true DOIs into the candidates. No matter how the selection order within candidates is changed, out-of-candidate DOIs cannot be recovered in this mode. This is a consequence of the generation assumptions, not the detection rate of a real tool. The cost and discovery yield of the out-of-candidate rescan mode were also reported separately.

## Secondary results on identifying new types

On the 12 new-cluster lots, candidates only, budget 360, we compared the cost at which the simulated review first identified a true new DOI as `novel`. The cost of runs that never identified one is not entered as 0, and the mean over identified lots only is not used as the overall mean.

| Policy | Lots with new type identified | Mean cost of first identification: identified lots only |
| --- | ---: | ---: |
| Frozen learned model | 12/12 | 176.40 |
| Uncertainty/diversity | 12/12 | 140.71 |
| Falsify | 12/12 | 111.11 |
| Recipe-like score | 11/12 | 119.61 |
| Random | 8/12 | 152.60 |
| No audit | 11/12 | 152.35 |
| No spatial term | 12/12 | 74.66 |
| No model update | 12/12 | 119.39 |

On the same 12 lots where both identified the new type, Falsify's mean cost of first identification was about 37.0% lower than the learned ranking. However, the primary goal on total DOI discovery yield failed, and the no-spatial-term variant had an even lower identification cost. This is a result on **early type identification in a synthetic new cluster**, which was set in advance as a secondary metric, and is not extended to a field detection rate or a general 37% cost saving. No additional statistical significance is claimed for an exploratory comparison of 12 lots.

## Cost and classification error rate

On the 60/60 lots where both policies reached 5 true-positive DOIs, the mean cost was 72.79 for Falsify and 66.10 for the learned ranking. Falsify spent **10.1% more cost**. The 30% saving goal also failed.

On 2,825 development validation candidates, model precision was 86.41%, recall 81.32%, balanced accuracy 87.15% and Brier 0.0860. These are development numbers and differ from the sensor detection rate and whole-wafer recall.

After all selection experiments had ended, the frozen model was evaluated post hoc on all 47,464 test candidates: precision 78.45%, recall 77.50%, balanced accuracy 85.25%, Brier 0.0822. The threshold is the frozen 0.5 and the model was not retrained. Detailed per-condition errors are in model_test_classification.json.

## Verification and records

The 56 tests in the new namespace passed. The stored 2,880 runs and 107,794 sensor attempts were cross-checked with an independent audit script. It verified fixed per-location observations, selection rationales that use only prior evidence, frozen predictions, cost reservation, loading, moves and retries, and post-hoc true-positive/miss counting. Budget overruns and hidden ground-truth field leaks were 0, and the SHA256 of the 8 existing frozen input files is unchanged. Observation totals across repeated policies and budgets are not counted as mutually independent defect counts.

On the real preparation path, an error occurred in which the training loader failed to restore lot metadata. The failed preparation folder was preserved before test data were generated, and the bug was fixed in e975110. After the fix, the freeze and campaign were run in a new folder. No policy, probability or seed was modified after seeing held-out results.

Frozen source commit: `e9751106665b0724b1d287ad1c909b1183e3353d`. Frozen receipt: `0427e4141f3dee5c8d2ca7ed30d8ce160e2b1a728b9bad6ef5c32eb77e9395a9`.

Full local evidence: `../output/inspection-review-v1/campaign-20261004b/`. The program design, sources and the boundary with the original PVT experiment are in INSPECTION_PROTOCOL.md (frozen Korean original; English translation INSPECTION_PROTOCOL.en.md) / INSPECTION_RESEARCH.md / INSPECTION_GUIDE.md.

## Direction for application

In the current experiment, if discovery yield is the goal, the frozen learned ranking should be the reference. Rather than claiming Falsify as a performance-improving product that replaces a production inspection selector, it is positioned as a tool for independent audit of existing judgments and for tracking counterexamples. Even so, the current evidence does not support the claim that it found more total misses than random auditing.

The question to test in the next version is whether the audit budget can be decided by error risk, audit obligations and additional cost instead of a fixed audit ratio. Rather than changing weights on these data to produce a success, separate development conditions and not-yet-used test lots must be preregistered. Until real optical/SEM logs, independent audit ground truth and field costs are available, error-rate and tool-throughput improvement rates are withheld.

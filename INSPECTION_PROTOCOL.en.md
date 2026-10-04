# Precision Review Selection Experiment v1

> **English companion translation.** The frozen, authoritative protocol is the Korean original
> [`INSPECTION_PROTOCOL.md`](INSPECTION_PROTOCOL.md) (SHA-256
> `86eb2486c63b0824c7fbf0af87c9037d112375066412e8bba6943535f4008768`). It is one of the
> `SOURCE_FILES` hashed by `inspection_review/cli.py` and recorded in
> `evidence/inspection-review-v1/freeze.json`, so it must stay byte-for-byte unchanged. This file is
> a faithful translation for English readers. It is not hashed or loaded by any tool and does not
> change the protocol. If the two ever differ, the Korean original governs.

This protocol is fixed before evaluation together with `inspection_review/protocol.json`. The existing PVT protocol, model and experimental results, and the 3-wafer UI data, are preserved unchanged. This study is a **tabular-feature-based inspection scheduling simulation** without real SEM images or tool logs. Generation probabilities, tool costs and process changes are assumptions set by the researcher and are not to be interpreted as real fab performance.

## What is compared

After optical screening inspection is complete, locations for precision review are chosen under a limited cost. The primary evaluation value is **the number of locations that are true DOIs and were also confirmed positive by the simulated review**, within 360 synthetic tool cost units. A DOI is defined as a morphological defect worth investigating and is distinguished from electrical failure, yield impact and confirmed root cause. Harmless physical defects are also generated separately. Precision review is not a perfect ground-truth instrument, so false positives among confirmed positives, review misses of true DOIs, and dropouts/failures are counted separately.

The first comparison is cost-weighted importance selection within candidates: Falsify versus a frozen learned model. In addition, it is compared with an active uncertainty/diversity policy, with random and recipe-like scores as secondary baselines. No claim is made of reproducing or beating a vendor product. If auditing reduces the defect count, that result is also published. Finding negative misjudgments of the initial model is a goal separate from the number of defects found.

## Boundary between observation and ground truth

Each lot has three 300mm wafers, each with 1305 dies placed under real geometric conditions. Each die has 1 care site with a layer and internal coordinates. The 3915 sites of a lot therefore do not represent every possible defect location in the whole die. No whole-wafer recall is claimed for structures outside this scope.

The information a policy first receives is candidate membership, site/wafer/layer identifiers, coordinates and process context. Only inside the candidates does it receive optical signal, size, texture and design-difference features. Features outside the candidates are masked as missing and, for the learned model, imputed with the training-data mean. No policy is given precision review results, latent DOI, defect type, electrical impact, generating cause, or the observation state of unselected locations. The process context is a noisy sensor context rather than the true cause.

Training uses the initial candidates and separate ground-truth annotations of 12 independent past lots. The frozen standardized L2 logistic model has its classification performance checked on 4 separate validation lots. The threshold of 0.5 and the high-confidence-negative cutoff of 0.1 are not changed after seeing results. The model is a genuinely trained numerical model and is not called an image classifier. Validation is used for reporting and development checks, not for selecting test parameters.

Online updates are allowed only at locations whose precision review result has been revealed. Failures and dropouts are not labels, and unconfirmed locations are not treated as normal. The predictions of the initial frozen model are kept to the end so that the definition of a negative misjudgment does not shift. The hashes of the updated model and the initial model are recorded separately. The ground-truth object is not placed in the policy input object. The evaluator reads the ground truth after all policies have finished selecting.

## Sensors and cost

Each sensor type's sensitivity, false positives, failures and dropouts are simulated assumptions fixed in the JSON. The same lot, site, action and attempt number always return the same observation, so a policy cannot obtain favorable sensor randomness by changing its selection order. Nuisance/harmless physical defects are separated from DOIs. On review failure or dropout, at most one retry is made, and the actual attempt and the retry cost are charged.

Tool cost includes loading for the first wafer access and wafer changes, location moves, dwell, retries after failure/dropout, and rescreening outside the candidates. The unit is the **synthetic tool cost unit** and is not converted to seconds, won or real throughput. A policy must not read the next observation first and then estimate the cost. Only actions whose maximum cost including retry can be reserved are allowed, and actual consumption is computed after they finish. Actions that do not fit the budget are skipped and the limit is never exceeded. All policies follow the same rules. Inference/training CPU and wall time are recorded separately from tool cost.

`candidate_only` reviews only the initially screened candidates. Both the DOI capture of the candidate population and the end-to-end recall over all care sites are reported, and the ceiling due to candidate misses is shown. `with_rescan` allows every policy a bundled action that optically rescreens and then precision-reviews care sites outside the candidates in the same site space, at an additional cost of 12. Rescreening is not treated as a complete exhaustive inspection. Locations outside the candidates are also confirmed under the same imperfect review sensor conditions.

## Policies and ablations

| Policy | Selection criterion |
| --- | --- |
| random | Random order of allowed, budget-feasible locations |
| recipe | Score from a fixed combination of optical signal, size and design difference, divided by the current cost including moves |
| learned | Frozen logistic DOI probability / current maximum cost including moves |
| uncertainty_diversity | Same initial model, online updates from paid labels, DOI probability, uncertainty and spatial diversity / cost |
| falsify | Same online model and spatial-neighbor DOI rate of paid observations, an audit of an initial negative candidate every 5th pick, an out-of-candidate probe every 10th pick in rescan mode |
| falsify_no_audit | Fixed negative-audit schedule removed; the out-of-candidate schedule is kept in common |
| falsify_no_spatial | Neighbor-label and spatial-diversity terms removed; audit kept |
| falsify_no_update | Initial numerical model frozen; spatial evidence from paid observations kept |

Audits are chosen among candidates the frozen model judged negative, based on feature-distribution deviation and the spatial context of paid observations. High-confidence-negative audit results are also counted separately. If there are no candidates or all are exhausted, selection returns to the usual choice. Scheduled out-of-candidate probes are common to the Falsify family, and other policies may also freely choose the same action. The policies are deterministic numerical selectors and are not presented as Omnigent/LLM execution.

## Experimental unit and metrics

**60 independent test lots** are used that do not overlap with the 12 training lots and 4 validation lots. There are 12 lots for each of the conditions normal, new cluster, low signal contrast, increased nuisance and process feature shift, and the seeds of each condition differ. Policies within the same lot are compared pairwise, but the 3 wafers or the thousands of sites are not counted as if they were independent lots. Because the conditions do not represent real-world occurrence frequencies, the overall mean is a synthetic stress evaluation that weights each condition equally.

Each lot/policy/mode is run **independently** for each budget of 120, 360 and 720. Because selection can differ with the retry-reservation limit and the remaining budget, the prefix of a larger-budget run is not used in place of a smaller-budget run. The same budget-reservation rules apply at the same comparison point.

Reported: the candidate capture ceiling, the number of true DOIs and the number per type, the number of simulated sensor positives, the number of true-positive confirmed DOIs, confirmed false positives, candidate capture, overall care-site recall, the number of confirmed negative misjudgments of the frozen model and its high-confidence subset, the cost of the first confirmation of a new type, post-hoc evaluation of electrical impact, and the counts and costs of failures, dropouts and retries. The number of still-unconfirmed locations and the number of true DOIs are distinguished in the post-hoc evaluation. Runs that fail early or do not reach the goal are not hidden either.

A 2000-resample paired bootstrap 95% interval is used for policy differences at the independent-lot level. The primary comparison is fixed to exactly one: Falsify versus learned, candidate_only, number of true-positive confirmed DOIs at cost 360. Strong active comparisons and per-condition and per-budget results are exploratory secondary analyses and are not used for multiple-comparison statistical significance claims. Pooled metrics and per-condition differences are published together. If the denominator is 0 the ratio is null, and failing to reach the baseline goal is not treated as a cost-saving success.

The goal is a 20% relative increase in mean discoveries at the same budget and a lower bound >0 of the paired-difference interval. The 30% cost saving is computed as a secondary analysis from the 720-budget run ledgers of lots in which both policies reached **5 true-positive DOIs**, and the share of lots reached by each of the two policies is reported together. A saving rate over only some reached lots is not interpreted as an overall cost saving. The goals are not a customer SLA or an industry-mandated standard. If a goal is not met, it is stated explicitly as failed or pending.

## Execution and revision rules

Before running, the protocol SHA, source commit, generator/model/policy SHAs, Python/numpy versions and the seed list are frozen. After the policy implementation passes the leakage, budget, ground-truth invariance and reproducibility checks, the coordinator runs this separate synthetic campaign. Weights, seeds and sensor probabilities are not changed after seeing the first held-out results. Fixes for implementation errors publish the error, its impact, the fix commit and the invalidated runs, and are replaced by new results. The authorization and experiments of the original PVT campaign are not repurposed.

The separated ground-truth files, selection ledgers and the combined JSON and Markdown reports are stored. Public deployment or replacement of the existing UI is not an automatic side task of this implementation. The next step of field validation is an external-lot evaluation that obtains optical candidates + paid review + independent audit/electrical test logs for the same tool/layer and calibrates actual costs. The background research remains in [INSPECTION_RESEARCH.md](INSPECTION_RESEARCH.md) and the source ledger.

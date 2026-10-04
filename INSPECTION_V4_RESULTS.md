# Inspection v4: the mixture candidate did not improve the selected method

The preregistered Gaussian-mixture candidate confirmed **29.17 DOI per lot versus 29.45** for a freshly fitted CB400 two-step route comparator on **120 fresh paired test lots**. The relative change was **−0.96%**, paired mean difference **−0.283**, 95% bootstrap CI **[−0.567, 0.008]**. The +5% primary target failed. The selected product remains the independently validated v3 CB400 route method.

This is authored numerical synthetic evidence at the same **360 CU** inspection budget, original optical candidates only. It is not factory yield, SEM-image accuracy or equipment throughput. The separate v3 and v4 campaigns use different training and test lots; their absolute means must not be treated as a paired comparison across campaigns.

## Design and attribution

Training used 60 lots and calibration 20, balanced across five scenarios. Forty development lots selected `gmm_diag8x6_platt_route_full` from eligible mixture candidates before any test outcome. The fresh CB400 and logistic models used the same training and calibration lots. Eleven model/calibration combinations and two policies produced 880 development and **2,640 test runs**. Test contained 24 lots per scenario.

The candidate fits class-conditional Gaussian mixtures to seven public optical/process features, with train-only scaling, seeded EM and calibration-only Platt scaling. It uses no authored DOI probability coefficients or defect-kind features. Probabilities stay frozen during inspection. It is a tabular mixture model, not an image model or an LLM-driven policy.

| Scenario | Mixture route DOI | CB400 route DOI |
|---|---:|---:|
| Stationary | 31.17 | 31.54 |
| Novel cluster | 31.67 | 31.38 |
| Low contrast | 23.67 | 24.38 |
| Nuisance heavy | 30.79 | 30.96 |
| Process shift | 28.54 | 29.00 |

The candidate was lower in four of five scenarios. Its +3.34% secondary gain over the weaker logistic one-step comparator does not reverse the failed primary comparison. Exploratory cost to the first five DOI was 61.63 versus 61.96 CU, a 0.54% saving of means; all 120 pairs reached the target. No cost-saving success claim was declared.

## Residual classification errors

On 92,214 original optical candidates, including 22,649 latent DOI, the mixture had precision **85.75%**, candidate recall **71.75%**, AP **0.8850**, Brier **0.07196**, and ECE **0.00761**. The corresponding fresh CB400 had precision **84.65%**, candidate recall **75.24%**, AP **0.8940**, Brier **0.06887**, and ECE **0.00313**. Higher mixture precision came with more false negatives: **6,398 versus 5,609** at the fixed 0.5 classification threshold. These classification metrics are separate from budgeted ranking and from physically missed sites outside the optical candidate set.

The selected mixture's positive EM fit stopped at iteration 199 of 200. A smaller mixture's positive fit did not converge within 200; its diagnostics and results remain recorded. No test-driven refit, calibration change or model replacement followed the result.

## Independent audit and preservation

The independent auditor regenerated all 240 train/calibration/development/test lots, replayed development selection and all 2,640 test runs, recomputed **118,014 paid choices** and **125,309 sensor attempts**, and recalculated classification and paired primary statistics. It found zero budget violations, hidden-truth keys, duplicate/missing runs or online probability updates. Eight original protected files remained byte-identical. Audit PASS means integrity passed; the primary hypothesis still failed.

The audit trusts the frozen generator/sensor implementation and frozen model prediction API. It does not independently verify software timing, risk-coverage curves or the recorded git commit. The auditor's 21 focused tests passed. The model and harness had 24 focused tests; these fixture checks are separate from the full campaign audit.

Source freeze: `8a8c70859a88d10b4c3074999cc077463b6f470a`. Receipt: `bd4ba4f70e793d44ac51210c8fe95b9d943cc32ab949986d1316ef008e8458ae`. Reproduce the audit with `scripts/audit_inspection_v4.py <v4-root>` in the inspection environment. Aggregates and the immutable receipt are preserved in `evidence/inspection-improvements-v4/`; full paid ledgers remain in the local output root.

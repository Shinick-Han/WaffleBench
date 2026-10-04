# Inspection v3: independent synthetic result

The preselected `cb400_route_full` confirmed **29.33 DOI per lot versus 27.38** for a freshly fitted logistic probability-per-cost baseline: **+7.12%**, paired mean difference **+1.95**, 95% bootstrap CI **[1.46, 2.43]**. The preregistered +5% target passed. This is a model-plus-routing comparison at equal **360 CU**, original optical candidates only, across **100 fresh paired lots**. It is authored numerical synthetic evidence, not SEM-image accuracy, factory throughput or wafer-yield improvement.

Development chose this candidate before the test campaign. Training used 36 lots, calibration 12, development 30; test used 20 lots in each of five scenarios. The test's 300 wafers contain 391,500 care sites. Twelve variants, two candidate/rescan modes and three budgets produced 7,200 runs. The immutable receipt is `faadbe498c1dec1f83926c733e21b386e2aa49cf24219b8ccd7fdd1327e4f3fb`, source commit `a67f72798729ee0e935167ca0a92ba6435c1f5b0`. Full ledgers remain under the local v3 output root.

## Attribution and failed goals

| Comparison | Mean confirmed DOI | Paired difference (95% CI) | Interpretation |
|---|---:|---:|---|
| Selected model + route vs logistic learned | 29.33 vs 27.38 | +1.95 [1.46, 2.43] | Primary +7.12%; +5% target passed |
| Route vs same CB400 model with one-step selection | 29.33 vs 28.20 | +1.13 [0.68, 1.61] | Secondary routing gain +4.01% |
| Mean cost to first 5 DOI, both attained | 61.34 vs 66.16 CU | Saving of means 7.29% | Supplementary; 30% target failed |

All 100 paired lots attained five DOI in the supplementary 720-CU ledgers. Mean per-lot paired fractional saving was 4.02%; the 7.29% saving of means is a different estimator. Selection is replanned after each paid review and does not update the frozen classifier. No LLM generated these campaign choices. Omnigent coordinates a separately logged live demonstration; its provenance must be verified separately.

Platt calibration, temperature calibration, isotonic calibration, a larger classifier and reported-review-yield objectives did not replace the frozen candidate after test. Their complete results are preserved in `evidence/inspection-improvements-v3/summary.json`.

## Accuracy and compute remain separate

On 78,323 optical candidate sites (19,477 latent DOI), the selected CB400 classifier had precision **81.84%**, candidate recall **75.96%**, AP **0.8825**, Brier **0.07480**, ECE **0.01910**. Its 3,284 FP and 4,682 FN remain visible. The logistic baseline had precision 80.08%, recall 75.01%, AP 0.8659. Candidate recall excludes sites never admitted by the optical sensor; a paid negative report also does not prove a physically good die.

Reusing a compiled immutable predictor reduced the same CB400 model's warm 3,915-site median from about **9.28 ms to 1.00 ms** in a local sequential microbenchmark. Predictions matched exactly. Compilation took about 429 ms including cold native initialization. This is cache/CPU optimization, separate from equipment CU and DOI quality. Across a complete 360-CU run, the two-step route policy's selection calls averaged **27.0 ms total per lot** versus 0.34 ms total for the simpler logistic policy; this is cumulative selection time, not time per decision. The method trades more local computation for better paid inspection choices.

## Audit and reproducibility

The independent audit recomputed 274,728 selected-site rows and 292,314 sensor attempts across all 7,200 runs. It found zero budget violations, hidden-truth fields in policy components, frozen-probability/reward mismatches or hypothetical two-step budget violations. All eight original protected files remained byte-identical. Model and core focused suites passed; the new core's fixture tests are not live Omnigent evidence.

Run `scripts/audit_inspection_v3.py <v3-root>` and then `scripts/export_inspection_v3.py <v3-root>` with the project inspection virtual environment. Export verifies the matching receipt and passed audit, displays only public inputs and paid observations, and chooses the first protocol test lot for replay without selecting by outcome. The source campaign refuses silent reruns or changed inputs. Prior failed v1/v2 results remain preserved.

Historical model fitting uses candidate labels from separate training lots. The reported-review-yield model records historical dwell and retry cost but omits historical load/stage cost; those offline training costs are not charged to the online search budget. Deployment requires real labeled inspection data, retraining/calibration and equipment-specific cost validation. Jev was unavailable in this campaign because no genuine free-text inspection notes existed.

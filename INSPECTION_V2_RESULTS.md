# Inspection Improvements v2: Results

The frozen models were compared on 60 new synthetic lots (180 wafers, 234,900 sites) with 2,880 policy runs. These are not measurements of real SEM images or factory performance.

## Confirmed results

The candidate selected first on development data is `catboost_learned`. At 360 CU within optical candidates, its mean confirmed DOI count was 28.30, effectively the same as the strong logistic learned baseline at 28.33. The paired mean difference is −0.033, with a lot-bootstrap 95% interval of [−0.583, 0.550]. The prespecified 20% discovery-improvement goal was not met. Cost to 5 DOIs also increased by 2.63%, so the 30% cost-saving goal was not met.

Classification results on 47,406 candidates are as follows. The threshold is 0.5, and defects outside the optical candidates are not included in this classification table.

| Model | Precision | Recall | AP | Brier ↓ | ECE ↓ |
|---|---:|---:|---:|---:|---:|
| Logistic | 81.51% | 75.34% | 0.8723 | 0.07970 | 0.04577 |
| CatBoost | 81.56% | 76.89% | 0.8843 | 0.07461 | 0.01774 |
| CatBoost + isotonic | 79.53% | 79.10% | 0.8730 | 0.07507 | 0.01794 |

CatBoost's probability estimates and recall improved. Isotonic calibration raised recall further but lowered precision and AP, so not every metric improved together.

## Exploratory results and the next bottleneck

The route-planning policy found 29.07, 0.45 more than the 28.62 of learned with the same calibrated model. The 95% interval [−0.133, 1.017] includes 0. The +2.59% versus the logistic baseline is a post-hoc exploratory result and is not presented as a confirmed winner replacing the development-stage selection. The selected candidate's +16.62% versus the existing fixed-audit Falsify is also a secondary comparison.

CatBoost inference over 3,915 sites took 438.9 ms on the first call and a warm median of 6.68 ms (p95 9.23 ms). The logistic warm median is 0.091 ms. Classification improvement and compute speed improvement must be distinguished. The CPU cost of the route policy, about 109 ms per lot, is also larger than simple learned. Tool CU were not converted to real seconds.

The next experiment separately investigates the detectability of real sensor observations, route planning, and rank-preserving probability calibration. It uses new development and validation seeds, and the v2 test results are not used again for candidate selection.

## Verification and reproduction

145 tests passed. The independent audit recomputed 106,667 selections, 114,048 sensor attempts and 3,091 audit decisions across the 2,880 runs. Budget violations, hidden-ground-truth exposure to policy components and frozen-probability mismatches were all 0, and the 8 existing protected files were preserved.

- Frozen source: `7e64e95db102c71fbee57c2726616093b1f6f64f`
- Frozen receipt: `ac99238f00af278cd56ce79fdfed66ab0061ff2c4cbe3ba6492bb72f43b4d374`
- [Structured results](evidence/inspection-improvements-v2/results.json), [audit](evidence/inspection-improvements-v2/audit.json), [inference timing](evidence/inspection-improvements-v2/inference-timing.json), [graph](evidence/inspection-improvements-v2/summary.png)
- The full run paths and file SHAs are preserved in the [artifact manifest](evidence/inspection-improvements-v2/artifact-manifest.json). They can be re-verified with the [run guide](INSPECTION_V2_GUIDE.md) and `scripts/audit_inspection_v2.py`.

Jev was unavailable in this numerical experiment because there were no genuine inspection notes. No claims are made about real API performance or semiconductor inspection accuracy.

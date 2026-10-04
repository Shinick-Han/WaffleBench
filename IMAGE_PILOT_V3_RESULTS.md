# VisA PCB3: the fixed v2 composite pipeline met all three preregistered conditions, narrowly

The separately preregistered PCB3 replication (`IMAGE_PILOT_V3_PROTOCOL.md`,
`inspection_images_v3/protocol.json`) scored the untouched official VisA PCB3 test split once:
**100 anomalous and 101 normal PCB photographs**. At each method's own calibration-only threshold,
the primary composite pipeline `primary448` found **55 of 100 defects with 3 of 101 normal false
alarms (2.97%)**. The baseline `baseline224` found **44 of 100 with 8 of 101 false alarms (7.92%)**.
The defect-recall gain is **+11 percentage points**, paired stratified bootstrap 95% CI
**[+1, +21] pp**. All three conditions pass, but the gain clears the 10 pp bar by one image and the
CI lower bound sits one image above zero. The primary pipeline still **misses 45 of 100 defects**.
This is one PCB category of real photographs, one seed and one test set. It is not wafer, SEM or
factory accuracy and says nothing about throughput.

| Fixed method | Role | TP / FN | FP / TN | Defect recall | Normal false alarms | Image AUROC | Image AP |
|---|---|---:|---:|---:|---:|---:|---:|
| `baseline224` (224², 14×14, coreset 1024, max) | baseline | 44 / 56 | 8 / 93 | 44% | 7.92% | 0.7616 | 0.8002 |
| `primary448` (448², 28×28, coreset 2048, mean of top 3) | **primary** | 55 / 45 | 3 / 98 | **55%** | **2.97%** | 0.9200 | 0.9244 |
| `highres448max` (same r448 memory, max) | descriptive only | 57 / 43 | 4 / 97 | 57% | 3.96% | 0.9078 | 0.9154 |

Rates are counts over 100 defect / 101 normal images; no extra precision is claimed. AUROC
(pairwise, ties half) and AP (tied-score groups) are secondary ranking statistics, not accuracy.
Thresholds (internal distance units) were fixed in the freeze before any test image was scored,
each at the linear 95th percentile of that method's 90 held-out normal-train calibration scores:
baseline **18.753552**, primary **22.827364**, highres448max **24.128162**. `score > threshold`
predicts a defect.

## Preregistered primary endpoint

| Condition (all required) | Observed | Pass |
|---|---|---|
| Recall gain ≥ 10 pp, integer rule `10*(TP_p − TP_b) ≥ n_anomaly` | 10 × (55 − 44) = 110 ≥ 100 | yes |
| Paired image bootstrap 95% CI lower bound > 0 (10,000 resamples within label stratum, seed stream `[2026100408, 7]`) | gain 0.11, CI [0.01, 0.21] | yes |
| `primary448` normal FAR ≤ 10%, `10*FP_p ≤ n_normal` | 10 × 3 = 30 ≤ 101 | yes |

Decision: **success** (all three true). The descriptive FAR difference (primary − baseline) is
−4.95 pp, CI [−9.90, 0.00] pp.

**`highres448max` cannot be swapped in.** Its 57% recall is higher than the primary's 55%, and
its descriptive gain over baseline is +13 pp, CI [+2, +24] (FAR −3.96 pp, CI [−9.90, +0.99]).
The primary endpoint was fixed as `primary448 minus baseline224` before scoring. The secondary
number is reported, not promoted. The auditor fails if the endpoint comparison differs between
protocol, freeze and evaluation.

## What the comparison can and cannot claim

- **Selection disclosure.** The three methods, feature sets, coreset sizes, aggregation,
  calibration fraction and 95th percentile were all **chosen on PCB2 (v2)** and then fixed for PCB3.
  The PCB2 result is the reason this pipeline was replicated, and PCB3 data chose nothing. PCB1 (v1) and
  PCB2 (v2) test sets were consumed earlier and are not rerun or tuned on.
- **Composite change, not resolution.** `primary448` differs from `baseline224` in input
  resolution, patch grid, memory coreset size and image-score aggregation at once. The gain belongs
  to that composite pipeline and **cannot be attributed to resolution alone**.
- **Replication strength.** PCB2 showed +21 pp, CI [+12, +30]. PCB3 shows +11 pp, CI [+1, +21].
  The direction replicated; the size was roughly halved and the margin over the preregistered bar is
  one image.
- **Scope.** Real VisA PCB photographs only, category `pcb3`, one seed (`2026100408`), CPU
  ResNet18 ImageNet weights. Images are resized whole with aspect distortion and no crop. Nothing here
  measures wafer, SEM or production-line inspection.

## Drawbacks: 45 missed defects and 3 false alarms

The defect types below come from the dataset's `pcb3/image_anno.csv`. It was read **only after
evaluation, for this descriptive breakdown**. It played no part in scoring or the decision.

| Annotated defect type | Test defects | Missed by `primary448` |
|---|---:|---:|
| melt | 38 | 17 |
| scratch | 20 | 13 |
| missing | 16 | 8 |
| bent | 19 | 2 |
| melt,scratch | 2 | 2 |
| scratch,missing | 2 | 1 |
| melt,missing | 1 | 1 |
| melt,scratch,missing | 1 | 1 |
| bent,melt | 1 | 0 |
| **total** | **100** | **45** |

- Most misses are melt and scratch (30 of 45). Bent leads were nearly all caught.
- Margins below threshold range from −4.51 to −0.04. Of the 45 misses, 9 were within 0.5 units of
  the threshold and 19 were more than 2 units below it. The near-misses are threshold-sensitive.
  The far misses show defects the composite score does not separate from normal at all.
- Paired with baseline: 20 defects were caught only by `primary448` and 9 only by `baseline224`.
  The pipeline is not a strict improvement: 9 of the primary's misses were flagged by the baseline,
  and 3 by `highres448max`.
- False alarms, all normal boards: `Normal/0363.JPG` (+3.12 above threshold; also flagged by baseline
  and highres), `Normal/0800.JPG` (+1.77; also flagged by both) and `Normal/0804.JPG` (+1.14;
  flagged by highres, not baseline). On normals, 1 board was flagged only by primary and 6 only by baseline.

The full per-image lists with margins are in `evidence/inspection-images-v3/audit.json` (`errors`).

## Exact data, sources and run

| Item | Value |
|---|---|
| Dataset | VisA (CC-BY-4.0), `VisA_20220922.tar`, archive sha256 `2eb8690c…0f362` (protocol pin) |
| Split | `split_csv/1cls.csv` from amazon-science/spot-diff `2a692ab5…`, sha256 `a48557e6033318cb90556f706196bc9d247a776a23ea51aecee5a80dd0332995`, 1,106 `pcb3` rows |
| Normal train | 905 = memory 815 + calibration 90 (disjoint, no test overlap) |
| Test | 201 = 100 defect + 101 normal; class counts read from labels only after scoring |
| Images | all 1,106 `pcb3` image byte hashes re-verified against the freeze, 0 mismatches |
| Backbone | torchvision ResNet18 `resnet18-f37072fd.pth`, sha256 `f37072fd47e89c5e827621c5baffa7500819f7896bbacec160b1a16c560e07ec`, layers 2+3 |
| Environment | Python 3.12.10, numpy 2.5.2, torch 2.14.1+cpu, torchvision 0.29.1+cpu, Windows 11, 4 threads |
| Memory artifacts | `memory_r224.npz` `33ea713f…40b63e3`, `memory_r448.npz` `1387b8bd…98e77a7a` (re-hashed in run root) |
| Freeze digest | `6bda4bd8bdae26f27366d0edc17612b683133f9d44b55b15527f6444af9334a1` |
| Sources | 16 LF-normalized source hashes in the freeze, re-hashed against this commit, all match; frozen v1/v2 dependencies match their protocol pins |

Evidence in `evidence/inspection-images-v3/`. The three run files are **exact byte copies** of the
run root (`runs/train-reviewed-candidate`). They contain no absolute local paths, so no sanitization
or redaction was needed:

| File | sha256 |
|---|---|
| `freeze.json` | `1f866a03d20ead68ef12a6307c5ba0d6b0609deef875bc8e9f9ed397f2c45ac3` |
| `test-scores.json` | `e0715009ed994b00dec0c9fd5d81900f5bb85776a22d30d399d26576422566f7` (= `evaluation.json` `test_scores_sha256`) |
| `evaluation.json` | `0aa206238cab868b71b7f0543bce5f4ddec59fcfedcd3e070b051d423e259022` |
| `audit.json` | independent audit receipt (paths reduced to file names) |

No images, model weights, memory `.npz` files or environment details beyond package versions are committed.

## Independent audit

`scripts/audit_inspection_images_v3.py` imports none of `inspection_images`,
`inspection_images_v2` or `inspection_images_v3`. It reads only the captured JSON files and
recomputes the following from them: the freeze digest, the source hashes and dependency pins, the
environment and model pins, the identity splits, the 95th-percentile thresholds, the strict-`>`
confusion counts, pairwise AUROC, tied AP, and both paired bootstrap replays (`default_rng` stream
`[2026100408, 7]`, defect stratum first). It then recomputes the integer decision. The stated headline is compared
in a **separate guard** after the recomputation. It is never used in place of it. Run read-only:

```
tools/inspection-image-venv/Scripts/python.exe scripts/audit_inspection_images_v3.py evidence/inspection-images-v3 \
  --split-csv <build>/sources/1cls.csv --data-dir <build>/visa --run-root <build>/runs/train-reviewed-candidate \
  --weights <v1-build>/torch-weights/resnet18-f37072fd.pth --out evidence/inspection-images-v3/audit.json
```

Result: `status: pass`. All three decision conditions are true and every headline guard is true.
`tests/test_inspection_images_v3_audit.py` contains 39 tests: metric reimplementations, integer-rule
boundaries, and tampering with thresholds, calibration and test scores, labels, counts, AUROC/AP,
the bootstrap, the decision, a secondary-for-primary swap, the test-usage guard, freeze source hashes,
a repository source edited after the freeze, v2 dependency pins, environment, model weights, absolute
paths, split leakage, image bytes and run-root copies.

**Audit limits.** The audit does not rescore. It extracts no features, rebuilds no memory bank and
does not recompute any image score from pixels. Score correctness rests on the pipeline, the
freeze/score/evaluation hash chain, the image and artifact hashes, and the root's separate review
of the v3 source. The audit cannot detect a scoring bug that was present at freeze time and
recorded consistently.

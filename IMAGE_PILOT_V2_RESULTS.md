# VisA PCB2: the preregistered composite patch pipeline met all three success criteria

The separately preregistered PCB2 study (`IMAGE_PILOT_V2_PROTOCOL.md`) scored the official VisA
PCB2 test split once: **100 normal and 100 anomalous PCB photographs**. At each method's own
calibration-only threshold, the primary composite pipeline `primary448` found **72 of 100 defects
with 5 of 100 normal false alarms**. The baseline `baseline224` found **51 of 100 with 11 of 100
false alarms**. The defect-recall gain is **+21 percentage points**, with a paired stratified bootstrap 95% CI of
**[+12, +30] pp**. All three preregistered conditions pass. The result is limited to one PCB
category and one test set. It is not wafer, SEM or factory accuracy and says nothing about throughput.

| Fixed method | Role | TP / FN | FP / TN | Defect recall | Normal false alarms | Image AUROC | Image AP |
|---|---|---:|---:|---:|---:|---:|---:|
| `baseline224` (224², 14×14, coreset 1024, max) | baseline | 51 / 49 | 11 / 89 | 51% | 11% | 0.8005 | 0.7772 |
| `primary448` (448², 28×28, coreset 2048, mean of top 3) | **primary** | 72 / 28 | 5 / 95 | **72%** | **5%** | 0.9060 | 0.9180 |
| `highres448max` (same r448 memory, max) | descriptive only | 69 / 31 | 6 / 94 | 69% | 6% | 0.8942 | 0.9080 |

Rates are counts over 100 images per class; no extra precision is claimed. AUROC and AP are
secondary ranking statistics, not accuracy. Thresholds (internal distance units) were fixed
before any test image was scored, each at the linear 95th percentile of that method's 90
held-out normal-train calibration scores: baseline **17.880889**, primary **22.227259**,
highres448max **23.472733**. `score > threshold` predicts a defect.

## Preregistered primary endpoint

| Condition (all required) | Observed | Pass |
|---|---|---|
| Recall gain ≥ 10 pp, integer rule `10*(TP_p − TP_b) ≥ n_anomaly` | 10 × (72 − 51) = 210 ≥ 100 | yes |
| Paired image bootstrap 95% CI lower bound > 0 (10,000 resamples within label stratum, seed stream `[2026100406, 7]`) | gain 0.21, CI [0.12, 0.30] | yes |
| `primary448` normal FAR ≤ 10%, `10*FP_p ≤ n_normal` | 10 × 5 = 50 ≤ 100 | yes |

Decision: **success**. The endpoint, methods, thresholds and split were not changed after
scoring. The descriptive FAR difference (primary − baseline) is −6 pp, CI [−12, −1] pp.
`highres448max` − baseline is descriptive only: recall +18 pp, CI [+8, +28]; FAR −5 pp, CI [−12, +1].

## What the comparison can and cannot claim

`primary448` differs from `baseline224` in **input resolution, patch grid, memory coreset size and image-score aggregation at once**.
The gain belongs to that composite pipeline. It **cannot be attributed to resolution alone**.
`highres448max` shares the r448 memory and changes only the aggregation back to max. It reaches
69 rather than 72 defects at its own threshold. That suggests the r448 feature set, not top-3 averaging, carries most of the change.
This is still descriptive: r448 itself changes resolution, grid and coreset size together, and
no ablation was preregistered. Nothing here measures factory or wafer accuracy, inspection throughput, latency or cost.

## Predecessor outcome: the PCB1 negative stands

The v1 pilot on VisA **PCB1** (`IMAGE_PILOT_RESULTS.md`) remains a **negative threshold result**. Its patch method
improved AUROC (.7881 → .8098). At its frozen threshold, though, recall fell from 42% to 33% while
false alarms rose from 4% to 6%. That test set is consumed. It was not rerun, rescored or used to
choose anything in v2. PCB2 is a different category with its own preregistration. Its success does not
overturn the PCB1 outcome, and the two must not be pooled into one "PCB accuracy".

## Errors that remain (posthoc, descriptive)

Defect types come from the dataset's `pcb2/image_anno.csv`. Labels were read only after
evaluation and only to describe errors (sha256 `b165a50f…25560`). Nothing was refit.

**28 missed defects** (primary FN). By annotated type (missed / test images): *missing* 9 / 17,
*melt* 12 / 48, *bent* 3 / 12, *scratch* 1 / 14, *melt,scratch* 2 / 4, *bent,melt* 1 / 3,
*scratch,missing* 0 / 2. Missing-component defects are the weakest group: about half are still
missed. Global image resizing (aspect distorted, no crop) and a single image score may not separate
a missing part from normal layout variation.
Ten of the 28 sit within 1.5 distance units below the threshold. The worst miss is 3.69 below (`Anomaly/038`, melt+scratch).
Twenty-five of the 28 are also missed by the baseline. Three (`Anomaly/014`, `049`, `083`) are caught by the
baseline but not by the primary. Paired against the baseline, the primary catches 24 defects the baseline misses.

**5 false alarms** (primary FP): `Normal/0011`, `0024`, `0394`, `0333`, `0804`. The first three exceed
the threshold by 2.4–4.5 units and are flagged by all three methods. They are hard normals for this
feature space, not a primary-specific artifact. `0804` is a near-threshold case (+0.14) that the
baseline also flags. `0333` is the only normal that the primary flags and the baseline does not. The
baseline flags 7 normals that the primary does not.

The per-image list with margins and cross-method flags is in `evidence/inspection-images-v2/audit.json` (`errors`).

## Limits

- **One category, one sample.** One PCB category (PCB2), one seed and one official test split of 100 + 100 images.
  The CI reflects image resampling only, not variation across categories, boards, cameras, seeds or memory draws.
- **Scope.** This is real industrial PCB photography, not wafers or SEM. It is separate from the numerical inspection-budget
  studies and gives no evidence about tabular policies.
- **Threshold portability.** Thresholds come from 90 normal calibration images; a real line would need its own calibration.
- **Preprocessing.** Images are resized whole with aspect-ratio distortion and no crop. No pixel-level localization metric is claimed.
- **Not an official PatchCore reproduction.** The method is *inspired by* PatchCore (Roth et al. 2021,
  [arXiv:2106.08265](https://arxiv.org/abs/2106.08265); reference code
  [amazon-science/patchcore-inspection](https://github.com/amazon-science/patchcore-inspection) @ `fcaa92f1`, Apache-2.0).
  No code was copied, and the backbone (ResNet18), coreset and scoring differ from the published
  configuration. The numbers are not comparable to published PatchCore VisA results.

## Data and licenses

- VisA dataset (Zou et al. 2022), official archive
  `https://amazon-visual-anomaly.s3.us-west-2.amazonaws.com/VisA_20220922.tar`
  (1,929,840,640 bytes, sha256 `2eb8690c…0f362`), licensed **CC BY 4.0**
  ([registry entry @ `db4a7728`](https://github.com/awslabs/open-data-registry/blob/db4a77281596a5951a812c4cbc19a8723db2103b/datasets/visa.yaml)).
- Official split: [amazon-science/spot-diff](https://github.com/amazon-science/spot-diff) `split_csv/1cls.csv` @ `2a692ab5`
  (sha256 `a48557e6…32995`), code Apache-2.0.
- Backbone: torchvision `resnet18-f37072fd.pth` (ImageNet-1K V1 weights), torchvision BSD-3-Clause.
- No photograph, mask, weight file or memory bank is committed. They stay in the local build output.

## Verification and evidence

`evidence/inspection-images-v2/` holds byte-exact copies of the run's `freeze.json`,
`test-scores.json` and `evaluation.json` (no redaction was needed: they contain no local paths, credentials or account data).
It also holds `audit.json` (independent audit receipt) and `frozen-verify.json` (read-only frozen verification receipt, paths redacted to `<build>`).

| Item | Value |
|---|---|
| Source commit (frozen pipeline) | `c1ba83a` tree; sources hashed into the freeze match LF-normalized |
| Freeze digest | `a15a37ae78b7234aefedd4f69b89c9fec2148a7a86e1c1d1d2000b94094a6fed` |
| `freeze.json` sha256 | `3ba3ce8f9faec4637de2d39eab6551f8f1c90dda3036fe09e65ce9698816706d` |
| `test-scores.json` sha256 (also recorded in evaluation) | `eac3dee802bffe882252b54df0006017108519d71d19887f23500c6aba0f2de4` |
| `evaluation.json` sha256 | `a2ed03a68e82da855e3339c922a0fcb7c80bcadd2d164fc591d3269017352ed5` |
| `memory_r224.npz` / `memory_r448.npz` sha256 | `97ec871f…13ca6` / `1cdb758e…68f1b` (local only) |
| Environment | Python 3.12.10, torch 2.14.1+cpu, torchvision 0.29.1+cpu, numpy 2.5.2, pillow 12.3.0 (full list in freeze) |

1. **Frozen verify (read-only).** After evaluation, `inspection_images_v2.cli verify` passed against the run root.
   It re-checked the digest, protocol, sources, full package environment, memory artifacts, model identity,
   split CSV, identities and the byte hashes of all 1,101 images.
   The run root's sha256 and mtimes were identical before and after.
2. **Independent audit.** `scripts/audit_inspection_images_v2.py` imports neither `inspection_images` nor
   `inspection_images_v2`. It recomputes the canonical freeze digest, the LF-normalized source hashes against
   this tree, the calibration-only linear 95th-percentile thresholds and the strict-`>` confusion counts. It
   also recomputes pairwise AUROC (ties count half), AP over tied-score groups, a replay of the preregistered
   paired stratified bootstrap (10,000 resamples) and the integer decision. It checks labels three ways
   (evaluation rows, image paths, pinned split CSV), rehashes all 1,101 images against the freeze, and
   confirms the evidence copies are byte-identical to the run root and the memory artifacts match. Every
   value agreed. AP matched to within 4e-16 (summation order).
3. **Tests.** `tests.test_inspection_images_v2_audit` has 29 tests. They cover the metric reimplementations,
   a read-only audit of the committed evidence, and adversarial tampering. The tampering cases include
   modified thresholds (raw and re-sealed), calibration scores, test scores (raw and re-sealed), counts,
   rates, labels, AUROC/AP, bootstrap CI and decision. They also cover a broken or truncated freeze, a
   missing section, a test-usage guard, a source hash, the digest chain, a test-identity leak, a missing input
   and a run-root mismatch. Each tamper must produce a specific finding and no receipt. The frozen
   `tests.test_inspection_images_v2` (24 tests) still passes.

```
$py = "C:\Users\user\hacknation7th\tools\inspection-image-venv\Scripts\python.exe"
$build = "C:\Users\user\hacknation7th\output\inspection-image-v2-build"
& $py -m unittest tests.test_inspection_images_v2 tests.test_inspection_images_v2_audit -v
& $py -m inspection_images_v2.cli verify --build-dir $build --root $build\runs\train-reviewed-candidate
& $py scripts\audit_inspection_images_v2.py evidence\inspection-images-v2 --split-csv $build\sources\1cls.csv `
    --data-dir $build\visa --anno-csv $build\visa\pcb2\image_anno.csv --run-root $build\runs\train-reviewed-candidate
```

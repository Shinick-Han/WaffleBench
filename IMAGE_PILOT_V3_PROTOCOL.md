# PCB3 external-category replication protocol (inspection-image-replication-v3-pcb3)

Preregistered before any PCB3 test image is scored or any PCB3 test label is read. Question:
does the fixed composite `primary448` improvement seen on PCB2 (v2) generalize to the untouched
VisA PCB3 category? This is an external-category replication, not hyperparameter optimization.
It is not wafer, SEM or factory accuracy and not tabular policy evidence. Machine-readable
constants live in `inspection_images_v3/protocol.json`; both files, the v3 sources and every
reused read-only v1/v2 file are hashed into every freeze.

## Selection disclosure

Architecture, methods, feature sets, coreset sizes, score aggregation, calibration fraction and
the 95th-percentile calibration quantile are copied unchanged from the v2 PCB2 protocol
(`IMAGE_PILOT_V2_PROTOCOL.md`). They were selected for this replication because of the prior
PCB2 result. Nothing was chosen from PCB3 data. Only the category (`pcb3`), the seed
(`2026100408`), the bootstrap seed stream (`[2026100408, 7]`), the study name and the
expected identity counts differ from v2.

## Predecessors (preserved, not revisited)

PCB1 (v1) and PCB2 (v2) test sets are consumed. Their reported outcomes stand. They are not
rerun, rescored or used to choose anything here. The v1/v2 code, protocol documents and v2
`protocol.json` are not edited; v3 pins their LF-normalized sha256 in
`frozen_dependency_sha256` and refuses train/verify/evaluate if any differs.

## Sources (pins carried from v1/v2)

| Item | Source | Pin | License |
|---|---|---|---|
| VisA archive | `https://amazon-visual-anomaly.s3.us-west-2.amazonaws.com/VisA_20220922.tar` | 1,929,840,640 bytes, sha256 `2eb8690c803ab37de0324772964100169ec8ba1fa3f7e94291c9ca673f40f362`; read-only from `output\inspection-image-build\downloads`, not refetched | CC BY 4.0 (`LICENSE-DATASET`) |
| Split | amazon-science/spot-diff `split_csv/1cls.csv` @ `2a692ab575001cbde74d402d897a7286086c6199` | sha256 `a48557e6033318cb90556f706196bc9d247a776a23ea51aecee5a80dd0332995`; copied from the v1 pinned copy with hash check | spot-diff code Apache-2.0 |
| Method reference | amazon-science/patchcore-inspection @ `fcaa92f124fb1ad74a7acf56726decd4b27cbcad`, arXiv:2106.08265 | method only; no code copied | Apache-2.0 |
| Backbone | torchvision `resnet18-f37072fd.pth` (IMAGENET1K_V1), shared read-only v1 weights dir | sha256 recorded in freeze | torchvision BSD-3-Clause |

Official PCB3 1cls split identities (read with `split_identities`, which never returns test
labels): 905 normal train, 201 test. Test class counts are not read before evaluation; the
decision rule uses the class counts read from labels after scoring. Only `pcb3/`,
`split_csv/1cls.csv` and `LICENSE-DATASET` are extracted into the new
`output\inspection-image-v3-build\visa`, using the v1 `safe_extract` (`tarfile` `filter="data"`,
regular files/directories only, resolved-path containment, size bound). The CLI refuses a
build dir equal to, inside, or containing the v1 build, and a run root outside the v3 build.

## Shared design (identical to v2)

- Seed 2026100408. floor(10%) of the 905 official normal-train images = 90 calibration images;
  the other 815 are memory-train. Memory/calibration/test identities are shared by every
  method and disjoint. Calibration and test images never enter memory or normalization.
- Whole-image resize (PIL bilinear, ImageNet mean/std), aspect ratio distorted, no crop.
- CPU ResNet18 (ImageNet weights), the v1 extractor read-only: layer2+layer3, 3x3 local
  average, layer2 pooled onto the layer3 grid, 384-d patch descriptors.
- Per feature set: memory-train-only per-dimension mean/std; seeded reservoir of at most 50,000
  descriptors; farthest-point greedy coreset in a seeded 16-d projection (v1 `core`); exact
  chunked nearest-neighbour distances (v2 `scoring`, read-only).
- Each method's threshold is the linear 95th percentile of its own calibration scores;
  `score > threshold` (strict) predicts a defect.

## Methods

| Method | Role | Resize / grid | Coreset | Image score |
|---|---|---|---|---|
| `baseline224` | baseline | 224x224 / 14x14 | 1024 | max patch nearest distance |
| `primary448` | primary | 448x448 / 28x28 | 2048 | mean of the 3 largest patch nearest distances |
| `highres448max` | secondary, descriptive only | same r448 memory | 2048 | max patch nearest distance |

`primary448` is a composite change (resolution, grid, coreset size and aggregation at once).
A difference cannot be attributed to resolution alone.

## Primary endpoint (fixed, never swapped after test)

Defect recall at each method's own frozen threshold, `primary448` minus `baseline224`, on the
official PCB3 anomaly test images. Success requires all three:

1. recall gain >= 10 percentage points (integer counts: `10*(TP_primary - TP_baseline) >= n_anomaly`);
2. paired stratified image bootstrap 95% percentile CI lower bound of the recall gain > 0
   (10,000 resamples, seed stream `[2026100408, 7]`);
3. `primary448` normal false alarm rate <= 10% (`10*FP_primary <= n_normal`).

Anything else is reported as not meeting the preregistered endpoint. A failure is kept as is:
no threshold, method, candidate, split or parameter is changed after PCB3 test outcomes are
seen. Image AUROC/AP, the FAR-difference CI and all `highres448max` numbers are descriptive.

## Freeze, verification and evaluation order

`train` refuses a non-empty root, any frozen-dependency hash differing from its pin, or an
environment other than Python 3.12.10 / numpy 2.5.2 / torch 2.14.1+cpu. It reads only official
normal-train images and writes `memory_r224.npz`, `memory_r448.npz` and `freeze.json`. The
freeze records the protocol; LF-normalized hashes of the v3 sources, this document and every
reused v1/v2 file; the full installed package list; model identities; split CSV hash; per-image
sha256 of train and test images (test images are only byte-hashed); identities (test without
labels); a test-usage guard; artifact hashes; calibration scores; thresholds; the endpoint and a
canonical digest.

`verify` and `evaluate` fail on any mismatch of the above. `evaluate` is single-shot and run by
the root coordinator only: it refuses when `evaluation.started`, `test-scores.json` or
`evaluation.json` exists, writes `evaluation.started` after verification, scores the frozen test
identities, writes `test-scores.json`, and only then reads the labels.

## Commands (separate CPU environment, no install)

```
$py = "C:\Users\user\hacknation7th\tools\inspection-image-venv\Scripts\python.exe"
$build = "C:\Users\user\hacknation7th\output\inspection-image-v3-build"
& $py -m unittest tests.test_inspection_images_v3 -v
& $py -m inspection_images_v3.cli prepare-data --build-dir $build
& $py -m inspection_images_v3.cli train --build-dir $build --root $build\runs\train-reviewed-candidate
& $py -m inspection_images_v3.cli verify --build-dir $build --root $build\runs\train-reviewed-candidate
& $py -m inspection_images_v3.cli evaluate --build-dir $build --root $build\runs\train-reviewed-candidate   # root coordinator only
```

Archive, extracted images, weights and run outputs stay outside Git.

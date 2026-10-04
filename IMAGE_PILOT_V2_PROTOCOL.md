# PCB2 image study protocol (inspection-image-pilot-v2-pcb2)

Preregistered before any PCB2 test image is scored or any PCB2 test label is read. This is a
separate feasibility study on real PCB photographs from the VisA PCB2 category. It is not
wafer, SEM or factory accuracy and not tabular policy evidence. Machine-readable constants
live in `inspection_images_v2/protocol.json`; both files, the v2 sources and the read-only
v1 modules they import are hashed into every freeze.

## Predecessor outcome (preserved, not revisited)

The v1 pilot (`IMAGE_PILOT_PROTOCOL.md`, VisA PCB1, 200 official test images) reported global
baseline AUROC .7881 / AP .827 and patch224 AUROC .8098 / AP .804. At the frozen train-normal
95th-percentile threshold, patch recall was .33 at FAR .06 versus global recall .42 at FAR .04:
a negative threshold outcome for the patch method. The PCB1 test set is consumed. It is not
rerun, rescored or used to choose anything here. Every v2 choice below was fixed from method
reasoning and normal-train data only.

## Sources (pins carried from the v1 protocol)

| Item | Source | Pin | License |
|---|---|---|---|
| VisA archive | `https://amazon-visual-anomaly.s3.us-west-2.amazonaws.com/VisA_20220922.tar` | 1,929,840,640 bytes, ETag `"05c830591a1172938cb714895c9e0cfb-113"`, sha256 `2eb8690c803ab37de0324772964100169ec8ba1fa3f7e94291c9ca673f40f362`; reused from the v1 download, not refetched | CC BY 4.0 (`LICENSE-DATASET`; awslabs/open-data-registry `datasets/visa.yaml` @ `db4a77281596a5951a812c4cbc19a8723db2103b`) |
| Split | amazon-science/spot-diff `split_csv/1cls.csv` @ `2a692ab575001cbde74d402d897a7286086c6199` | sha256 `a48557e6033318cb90556f706196bc9d247a776a23ea51aecee5a80dd0332995`, git blob `b92269792db47fe3f7de6a67e29c7bb04acee0f2`; copied from the v1 pinned copy with hash check | spot-diff code Apache-2.0 |
| Method reference | amazon-science/patchcore-inspection @ `fcaa92f124fb1ad74a7acf56726decd4b27cbcad`, arXiv:2106.08265 | method only; no code copied | Apache-2.0 |
| Backbone | torchvision `resnet18-f37072fd.pth` (IMAGENET1K_V1), shared read-only v1 weights dir, hash-checked by torch hub | sha256 recorded in freeze | torchvision BSD-3-Clause; ImageNet-pretrained weights |

Official PCB2 1cls split: 901 normal train, 100 normal test, 100 anomaly test. Only `pcb2/`,
`split_csv/1cls.csv` and `LICENSE-DATASET` are extracted into a new
`output\inspection-image-v2-build\visa`, using the v1 `safe_extract` (`tarfile` `filter="data"`,
regular files/directories only, resolved-path containment, size bound).

## Shared design

- Seed 2026100406. floor(10%) of the 901 official normal-train images = 90 calibration images;
  the other 811 are memory-train. The same memory/calibration/test identities are used by every
  method. Calibration and test images never enter memory or normalization.
- Each image is resized whole (PIL bilinear, ImageNet mean/std). Aspect ratio is distorted and
  nothing is cropped.
- CPU ResNet18 (ImageNet weights), the v1 extractor read-only: layer2 and layer3 each get 3x3
  stride-1 average local aggregation, layer2 is 2x2 average-pooled onto the layer3 grid, and
  the two are concatenated into 384-d patch descriptors.
- Per feature set, per-dimension mean/std from memory-train descriptors only.
- Per feature set, a sorted seeded reservoir of at most 50,000 memory-train descriptors, then a
  farthest-point greedy coreset in a seeded 16-d Gaussian projection (v1 `core`), exact chunked
  float64 nearest-neighbour distances.
- Each method's threshold is the linear 95th percentile of its own calibration scores;
  `score > threshold` predicts a defect.

## Methods

| Method | Role | Resize / grid | Coreset | Image score |
|---|---|---|---|---|
| `baseline224` | baseline | 224x224 / 14x14 | 1024 | max patch nearest distance (v1 patch recipe) |
| `primary448` | primary | 448x448 / 28x28 | 2048 | mean of the 3 largest patch nearest distances |
| `highres448max` | secondary, descriptive only | same r448 memory as primary | 2048 | max patch nearest distance |

`primary448` changes resolution, grid, coreset size and score aggregation at once. It is a
composite pipeline change. A difference cannot be attributed to resolution alone.
`highres448max` only describes the aggregation part and never replaces the primary.

## Primary endpoint (fixed, never swapped after test)

Defect recall at each method's own frozen threshold, `primary448` minus `baseline224`, on the
100 official PCB2 anomaly test images. Success requires all three:

1. recall gain >= 10 percentage points (checked with integer counts: `10*(TP_primary - TP_baseline) >= n_anomaly`);
2. paired image bootstrap 95% percentile CI lower bound of the recall gain > 0 (10,000
   resamples, test images resampled with replacement within label stratum and shared by both
   methods, seed stream `[2026100406, 7]`);
3. `primary448` normal false alarm rate <= 10% (`10*FP_primary <= n_normal`).

Anything else is reported as not meeting the preregistered endpoint, including a negative
result. Image AUROC/AP are secondary ranking statistics, never accuracy. The FAR difference CI
and all `highres448max` numbers are descriptive. Rates are counts over 100 images per class;
no extra precision is claimed. No method, threshold, split or parameter is refit after PCB2
test outcomes are seen.

## Freeze, verification and evaluation order

`train` refuses a non-empty root (no resumption). It reads only official normal-train images,
fits each feature set's memory, scores calibration images, sets thresholds and writes
`memory_r224.npz`, `memory_r448.npz` and `freeze.json`. The freeze records: protocol; LF-normalized
hashes of the v2 sources, this document and the imported v1 modules
(`inspection_images/{__init__,core,data,features}.py`, `requirements-cpu.txt`); the full
installed package list; model identities (weights sha256, threads, batch, resize); split CSV
hash; per-image sha256 of train (memory and calibration) and test images; memory/calibration/test
identities (test without labels) and a test-usage guard stating test images were not scored and
entered no memory, normalization, calibration or threshold; artifact hashes; calibration scores;
thresholds; the primary endpoint; and a canonical digest.

`verify` and `evaluate` fail on a missing freeze, missing source or dependency file, digest,
protocol, source, environment, artifact, model, split CSV, identity or image-byte mismatch, or
thresholds that are not the calibration-only percentile. `evaluate` is single-shot: it refuses
when `evaluation.started`, `test-scores.json` or `evaluation.json` exists, writes
`evaluation.started` after verification, scores the frozen test identities, writes
`test-scores.json`, and only then reads the labels.

## Commands (separate CPU environment, no install needed)

```
$py = "C:\Users\user\hacknation7th\tools\inspection-image-venv\Scripts\python.exe"
$build = "C:\Users\user\hacknation7th\output\inspection-image-v2-build"
& $py -m unittest tests.test_inspection_images_v2 -v
& $py -m inspection_images_v2.cli prepare-data --build-dir $build
& $py -m inspection_images_v2.cli train --build-dir $build --root $build\runs\train-reviewed-candidate
& $py -m inspection_images_v2.cli verify --build-dir $build --root $build\runs\train-reviewed-candidate
& $py -m inspection_images_v2.cli evaluate --build-dir $build --root $build\runs\train-reviewed-candidate   # coordinator only
```

Archive, extracted images, weights and run outputs stay outside Git.

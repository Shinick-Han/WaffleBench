# PCB1 image pilot protocol (inspection-image-pilot-v1)

This is a separate feasibility study on real PCB photographs from the VisA PCB1 category.
It is not wafer or SEM accuracy and not tabular policy evidence. It does not replace the
selected main product or any frozen numeric study, and its results say nothing about real
wafer inspection performance. Machine-readable constants live in
`inspection_images/protocol.json`. Both files are hashed into every freeze.

## Sources (fetched 2026-10-04, read before use)

| Item | Source | Pin | License |
|---|---|---|---|
| VisA archive | `https://amazon-visual-anomaly.s3.us-west-2.amazonaws.com/VisA_20220922.tar` (link in spot-diff README) | 1,929,840,640 bytes, ETag `"05c830591a1172938cb714895c9e0cfb-113"`, observed sha256 `2eb8690c803ab37de0324772964100169ec8ba1fa3f7e94291c9ca673f40f362` | CC BY 4.0 (`LICENSE-DATASET` in archive; awslabs/open-data-registry `datasets/visa.yaml` @ `db4a7728`) |
| Split | amazon-science/spot-diff `split_csv/1cls.csv` @ `2a692ab575001cbde74d402d897a7286086c6199` | sha256 `a48557e6…dd0332995`, git blob `b9226979…`; identical to the archive's own `split_csv/1cls.csv` | spot-diff code Apache-2.0 |
| Method reference | amazon-science/patchcore-inspection @ `fcaa92f124fb1ad74a7acf56726decd4b27cbcad`, arXiv:2106.08265 | method read from `src/patchcore/{patchcore,common,sampler}.py`; no code copied | Apache-2.0 |
| Backbone | torchvision `resnet18-f37072fd.pth` (IMAGENET1K_V1), hash-checked by torch hub | sha256 recorded in freeze | torchvision BSD-3-Clause; ImageNet-pretrained weights |

Official PCB1 1cls split: 904 normal train, 100 normal test, 100 anomaly test.
Only `pcb1/`, `split_csv/1cls.csv` and `LICENSE-DATASET` are extracted, with
`tarfile` `filter="data"`, regular files/directories only, and resolved-path containment.

## Method

1. Each image is resized to 224x224 as a whole (PIL bilinear; aspect ratio not kept, no
   crop), ImageNet mean/std normalized.
2. CPU ResNet18 (ImageNet weights). layer2 (128x28x28) and layer3 (256x14x14) each get 3x3
   stride-1 average local aggregation; layer2 is 2x2 average-pooled to 14x14; the two are
   concatenated into 196 descriptors of 384 dims per image.
3. Official normal-train images are split with seed 2026100405: floor(10%) = 90 calibration
   images, 814 memory-train images. Calibration images never enter memory or normalization.
4. Per-dimension mean/std from memory-train descriptors only standardize all descriptors.
5. Patch memory: a sorted seeded reservoir of at most 50,000 memory-train descriptors, then a
   1024-point farthest-point greedy coreset computed in a seeded 16-d Gaussian projection,
   starting at the projected point farthest from the projected mean. Stored descriptors are
   the full 384-d ones.
6. Scores, both on the same extracted features:
   - global baseline: L2 distance from the image's mean-pooled descriptor to the nearest
     memory-train image mean descriptor;
   - patch method: max over 196 patches of L2 distance to the nearest coreset descriptor.
7. Each method's threshold is the 95th percentile (linear) of its calibration scores;
   `score > threshold` predicts a defect.

### Adaptations from published PatchCore

ResNet18 not WideResNet50; layer2 downsampled to the layer3 grid instead of layer3
upsampled; 3x3 average aggregation instead of patch unfolding plus adaptive average
projection to 1024 dims; fixed 1024-point coreset from a bounded 50,000-descriptor
reservoir instead of a percentage of all descriptors; 16-d projection instead of 128;
deterministic farthest-from-mean start instead of random start points; exact numpy nearest
neighbour instead of FAISS; no pixel-level maps or anomaly-map smoothing; resize to 224 with
no center crop.

## Freeze and evaluation order

`train` refuses a non-empty output root. It reads only official normal-train images, fits
memory, scores calibration images, sets thresholds and writes `memory.npz` and
`freeze.json`. The freeze records protocol and source file hashes, the environment's
package list, model weights hash, split CSV hash, per-image sha256 for train and test
images, memory/calibration/test identities (test without labels), the memory artifact
hash, calibration scores, thresholds and a canonical digest. No test image is scored and
no test label is read before the freeze exists.

`evaluate` is single-shot. It refuses when `evaluation.json` or `evaluation.started` exists,
or when the digest, protocol, source files, memory artifact, split CSV, train/test image
bytes, train/calibration/test identities or model identity differ from the freeze. It then
scores the frozen test identities and reads the labels last.

Reported per method: image AUROC and average precision, which are ranking statistics and
never accuracy; the confusion counts at the frozen threshold; defect recall; normal false
alarm rate. No pixel AUROC. One category, one seed, 200 test images; no intervals are
claimed.

## Commands (separate environment)

```
uv venv --python 3.12 C:\Users\user\hacknation7th\tools\inspection-image-venv
uv pip install --python C:\Users\user\hacknation7th\tools\inspection-image-venv\Scripts\python.exe --index-url https://download.pytorch.org/whl/cpu torch==2.14.1+cpu torchvision==0.29.1+cpu
uv pip install --python C:\Users\user\hacknation7th\tools\inspection-image-venv\Scripts\python.exe -r inspection_images/requirements-cpu.txt
$py = "C:\Users\user\hacknation7th\tools\inspection-image-venv\Scripts\python.exe"
$build = "C:\Users\user\hacknation7th\output\inspection-image-build"
& $py -m inspection_images.cli prepare-data --build-dir $build
& $py -m inspection_images.cli train --build-dir $build --root <new-run-root>
& $py -m inspection_images.cli verify --build-dir $build --root <run-root>
& $py -m inspection_images.cli evaluate --build-dir $build --root <run-root>   # coordinator only
```

Archive, extracted images, weights and run outputs stay outside Git.

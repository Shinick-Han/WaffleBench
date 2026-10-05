# SEM image segmentation guide (post-hackathon development)

All work in `sem_images/` is post-hackathon development and is excluded from hackathon judging.
Development/calibration outputs are not benchmark results, and nothing here claims superiority over
inspection equipment. Physical pixel size (nm/pixel) is unknown; every size is in pixels.

## Environment

The isolated env is `output/post-hackathon/sem-build-20261005/images-env` (Python 3.12.10, created by uv).
It reuses torch 2.14.1+cpu, torchvision, numpy and pillow read-only through
`Lib/site-packages/zz_reuse_inspection_image_venv.pth`, which points at
`tools/inspection-image-venv/Lib/site-packages`. Nothing was installed into that existing env.
Only the dependencies listed in `sem_images/requirements-sem.txt` were added, with `--no-deps`.
segmentation_models.pytorch (MIT) comes from the pinned commit `d2f65c5e3c9a34a02d90e19ad798c5f8f99c21ae`.

torch and smp are imported lazily, so `import sem_images.*` works without them. A missing backend
raises `BackendUnavailable` with install instructions. Encoder weights default to none. Pretrained
weights are loaded only from an explicit local file passed with `--encoder-weights`, and the file's
SHA-256 is recorded. Loading is strict: only the torchvision classifier keys `fc.weight` and `fc.bias`
are dropped. The RGB stem is summed into one grey channel. `HF_HUB_OFFLINE=1` is set, so nothing is
ever downloaded.

Legacy checkpoints such as the official cached torchvision `resnet18-f37072fd.pth` predate the
BatchNorm `*.num_batches_tracked` buffers (20 keys for ResNet-18). These are non-learned step counters,
so the loader normalizes them deterministically: absent counters are filled with integer zero matching
the encoder buffer's dtype, device and shape. No other key may be missing; every learned weight and
running statistic must be present with the right shape, and any unexpected key other than `fc.*` is
rejected. The input state dict and the weights file are not modified. The model config records this
policy under `encoder_weights_compat` next to `encoder_weights_sha256`.

## Manifest interface

The code consumes the shared `schema_version: 1` Carinthia-S manifest. It requires `split` metadata
(`splits` and `split_metadata` are also accepted). Items with a normal or synthetic class are rejected,
as are duplicate ids, duplicate groups that cross splits, and paths that escape the root.
`data_mode` must be `real`. Fixture manifests (`data_mode: "fixture"`) are accepted only with
`--allow-fixture-manifest` or `allow_fixture=True`, for tests.

Masks are read with strict binary checks by default: values must be in {0,255} or {0,1}, and grey-level
masks raise `MaskFormatError` without being silently thresholded. A threshold conversion is honoured
only when the manifest declares it explicitly:
- the data worker's `mask_policy: {mode: "threshold-soft", soft_threshold: N, development_only: true}`, or
- `mask_convention: {type: "threshold", threshold: N, development_only: true, provenance: ...}`.

An unknown policy mode is rejected. The mask convention is part of each split identity hash.

Validation reports per-split class counts. It warns when a class is absent from a split or when no
source groups exist. Defect classes keep the source's numeric codes.

## CLI

```
PY=output/post-hackathon/sem-build-20261005/images-env/Scripts/python.exe
$PY -m sem_images.cli validate --manifest M
$PY -m sem_images.cli train    --manifest M --root RUN --epochs 1 --max-train-images 24 --size 256 --threads 2 --model unet
$PY -m sem_images.cli predict  --manifest M --root RUN --split calibration --max-images 24
$PY -m sem_images.cli evaluate --manifest M --root RUN --split calibration --max-images 24
$PY -m sem_images.cli export-backends --out DIR
```

- **train** reads only train-split images and masks through a train-only reader. It samples by
  seeded round-robin over train classes, so minority classes such as `5` are included, and it records
  the chosen identities and per-class counts. Training uses native-resolution 256 px tiles with 64 px
  overlap. Padding is excluded from the loss. After training it saves `model.pt` and freezes the model
  SHA-256, config, tiling, the fixed 0.5 threshold and every split identity into `frozen.json`. A run
  directory that is already frozen is never retrained. The torch seed is set before the model is
  built. `--augment-copies K` adds deterministic train-only scratch/particle copies with generated
  masks, each recorded with `synthetic: true` provenance. No recall gain from augmentation is claimed
  without a matched comparison.
- **predict** never enables mask reads. It checks the model hash and the train/target split identities
  against `frozen.json`. It writes float16 probability maps at the original image size and
  `predictions-<split>.json` with file hashes. It writes once per split.
- **evaluate** runs predict first if needed. It verifies the receipt, model hash, split identity and
  every prediction file hash. Only after that does it read masks. It reports:
  - pixel TP/FP/TN/FN with Dice and IoU;
  - connected-component recall at IoU 0.25 (each ground-truth component against the union of the
    predicted components overlapping it), by class and by pixel-area bin, with denominators;
  - a count of predicted components with no ground-truth overlap;
  - a geometric resize-bias reference: native tiles versus a 448 square or aspect-preserving resize.
  Empty-mask frames (Carinthia-S class `6`, "no visible defect", possibly SEM misalignment) are
  reported separately and are never treated as clean negatives. No image-level false-alarm rate is
  computed (`image_level_false_alarm_rate.value: null`). The class label is never used at inference.
- **Test split**: the default split is `calibration`. `--split test` requires `--confirm-test`. After
  `test-receipt.json` exists, any further test predict/evaluate is refused. Training never touches
  calibration or test data.

## Tiling

`sem_images.tiling` plans tiles of size T with stride T−overlap. The transform is
`image_xy = tile_xy + (x0, y0)` at scale 1. Edge tiles are padded only at the right and bottom
(symmetric by default), and each tile records its valid region. `stitch` blends overlaps with
separable ramp weights over valid pixels only, and returns exactly the original H×W. A 480×480
Carinthia-S frame becomes 3×3 tiles at 256/64.

## Backends (configuration only)

`export-backends` writes launch configs with `status: "not_run"` and `outcome: null` for:
- official PatchCore (`fcaa92f1…`, Apache-2.0);
- Anomalib EfficientAD (`335a6be1…`, Apache-2.0);
- author DRAEM (`2dbf6739…`, MIT);
- a SAHI slicing reference (`80ebdb69…`, MIT).

The configs include licence hashes and prerequisites. All three anomaly methods need real normal
training images, which defect-only Carinthia-S does not supply, so that prerequisite is marked unmet.
Commands mirror the upstream READMEs and must be checked against the root-owned forks before any run.
`stitch_patchcore_tile_maps` is this package's own stitching code; SAHI is only cited.

## Other helpers

- `sem_images.cascade` logs the fast score, gate skip and per-stage latency for each item. `summarize`
  refuses to run until gate misses have been evaluated against labels.
- `sem_images.registration` is an optional OpenCV ORB + RANSAC API. It raises `RegistrationFailed`
  with a reason and never returns a silent identity transform. For Carinthia-S it reports
  `not_applicable`, because no reference pairs exist.

## Known limitations

- Dataset caveats:
  - Splits are image-level, with no lot or acquisition groups.
  - Class `2` is fully excluded by the strict mask audit.
  - Classes `1` and `5` are scarce, and class `5` has no test items.
  - The 3 px black right border, centred defects and framing squares are potential shortcuts. They
    are not removed.
- Evaluation subset: `--max-images` takes the first N items by sorted id. The ids are content hashes,
  so this is not a stratified calibration sample.
- Threshold: the threshold is fixed at 0.5 and is not calibrated.

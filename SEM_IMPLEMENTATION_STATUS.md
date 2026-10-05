# WaffleBench SEM implementation status

2026-10-05. Post-hackathon development, excluded from the submitted version and judging.

## Integrated implementation

Three parallel Claude Code Opus 5.5 medium workers delivered isolated data, image and
decision modules. The coordinator reviewed scope, reran focused checks, integrated
accepted candidates and executed a real-data development pipeline. The integrated code
before this documentation commit is `daf1f7e03ca32cf9d3565667f22bdaea6b4a41a3` on
`post-hackathon/roi-20261005`, in `.worktrees/wafflebench-post-hackathon-20261005`.
This checkout has no remote. Existing submitted code, UI, scientific protocols and results
were not edited. There is no new public deployment.

| Area | Implemented and verified | Boundary |
|---|---|---|
| Real SEM data | Safe ZIP extraction; image/mask/hash audit; deterministic duplicate-group-disjoint splits; strict mask validation | Missing acquisition/lot identifiers prevent verification of lot-level independence |
| Supervised localization | Pinned SMP U-Net/ResNet-18; native 256 px overlapping tiles; padding exclusion; original-coordinate stitching; model and prediction receipts; component and pixel metrics | Development training and calibration subset only; no test inference |
| Active review | Budget-feasible uncertainty/diversity audit policy; separate current-yield and future-learning objectives; modAL/apricot wrappers | Real library calls tested on synthetic inputs; real decision-loop advantage not measured |
| Reliability | Independent split-conformal interface; MAPIE wrapper; abstention; permutation drift diagnostics | Synthetic compatibility checked; no SEM coverage certification or guarantee under drift |
| Registration | OpenCV ORB/RANSAC API with explicit failure | Synthetic translation recovered; no authentic reference pairs in Carinthia-S |
| Efficiency and acquisition | Fast/slow cascade accounting including gate misses; paid quality-triggered repeat-capture contract | Helpers tested; hardware disconnected and thresholds not instrument-calibrated |
| Rare defects and simulation | Train-only scratch/particle augmentation; NIST-style quality curves; ARTIMAGEN launch requirements | No controlled augmentation comparison; NIST upstream runtime not executed; ARTIMAGEN not built |
| Additional anomaly methods | Local pinned PatchCore, Anomalib/EfficientAD and DRAEM sources plus explicit launch configs | Not trained or evaluated: representative normal images are missing |
| Jev/Omnigent | Prospective-action payload validation with observed evidence and budget checks | No paid API call, credential read or new production wiring |

Worker candidate commits: data `d8b6d4b`; images `864ccf8`, followed by a fresh-session
checkpoint-compatibility repair `c46bc89`; decisions `b816822`, followed by reliability
repair `64a3d25`. Integration receipt and upstream provenance are retained separately.

## Real data findings

Carinthia-S was retrieved from [Zenodo record 16895427](https://zenodo.org/records/16895427)
under CC BY 4.0. All 4,591 source image/mask pairs were audited. The strict binary-mask
policy accepts 4,196 pairs and excludes 395 masks containing intermediate gray values.
The exclusion is visible in the audit, not silently converted into background. An optional
explicit threshold policy exists for development and requires separate evaluation.

| Class code | Accepted pairs | Train | Calibration | Test |
|---|---:|---:|---:|---:|
| 1 | 11 | 8 | 2 | 1 |
| 2 | 0 | 0 | 0 | 0 |
| 3 | 3,691 | 2,584 | 554 | 553 |
| 4 | 263 | 184 | 40 | 39 |
| 5 | 4 | 3 | 1 | 0 |
| 6 | 227 | 159 | 34 | 34 |
| Total | 4,196 | 2,938 | 631 | 627 |

Class 6 means no visible defect, possibly SEM misalignment; it is not verified physically
good material. It cannot establish a normal-material false-alarm rate. No exact duplicate
images were found; near duplicates and acquisition-level correlation remain unresolved.
The source has centered defects, a black border and framing artifacts. Physical nm/pixel
is unknown. These are unstructured-layer SEM frames, not three complete wafer scans.

## Executed development result

Output root: `output/post-hackathon/sem-build-20261005/` in the workspace.
The complete run is `integrated-pretrained64-e3-r2/integration-receipt.json`.
It trained on 64 class-balanced images (576 native tiles) for three CPU epochs using an
explicit cached ResNet-18 encoder checkpoint. Training took 176.94 seconds. The fixed
probability threshold was 0.5. Predictions were saved and hashed before mask evaluation.
Only the first 24 calibration items by sorted identity were evaluated; this sample is
not stratified. No test images were used for inference, evaluation or model selection.

| Development metric | Observed result |
|---|---:|
| Pixel Dice | 0.839080 |
| Pixel IoU | 0.722772 |
| Pixel recall, TP / (TP + FN) | 0.849820 |
| Component recall at IoU >= 0.25 | 22 / 22 |
| Class 3 / class 4 evaluated components | 21 / 1 |
| Predicted components without ground-truth overlap | 5 |
| Empty-mask class-6 frames with any predicted component | 0 / 2 |
| False-alarm rate on verified normal material | Unavailable |

All evaluated components have area at least 1,024 pixels. This result does not demonstrate
small-defect sensitivity, six-class generalization, full-wafer recall or equipment
superiority. Component matching uses each ground-truth component against the union of
overlapping predicted components; it is not a one-to-one detection AP metric. The 22/22
component result must be read alongside pixel misses and five extra predicted components.

Model SHA-256: `4377d3354b06c449734a38a04a48949ca7955667d4c65c92ccfc5813347d99d9`.
Original encoder SHA-256:
`f37072fd47e89c5e827621c5baffa7500819f7896bbacec160b1a16c560e07ec`.
The legacy loader fills only missing non-learned BatchNorm counters with zero; missing
learned parameters or running statistics still fail. The original weights are unchanged.

Two earlier outcomes are preserved: `images-runs/smoke-train24-e1` (from scratch,
24 images, one epoch, Dice approximately 0.0015 and 0/22 component recall), and
`integrated-pretrained64-e3` (checkpoint-loader failure). The later successful run changes
pretraining, training sample size and epoch count together. Their difference is not a
controlled estimate of any individual method's benefit.

## Upstream and environment evidence

Ten pinned local source checkouts and their licenses are recorded in
`upstream-receipt.json`. These are local forks/checkouts, not public GitHub forks.
`upstream-adapter-check.json` confirms actual calls into pinned modAL, apricot and MAPIE
on synthetic inputs. `registration-check.json` records OpenCV 5.0.0 recovering a known
(12, 7) px translation within one pixel and rejecting a blank input explicitly.
These fixture outcomes establish compatibility only.

The isolated `images-env` and `decisions-env` reuse the existing torch installation
read-only; nothing was installed into the submitted runtime environment. The SMP pin and
direct image dependencies are in `sem_images/requirements-sem.txt`. Decision package
versions are retained in `decision-environment-lock.txt`; source pins and licenses are in
the receipts. Decision environment dependency check: 29 packages, compatible.

## Validation and reproducibility

The final focused suite passed **154 tests**, covering the three new modules and existing
policy/harness regressions. Real manifest validation and the full audit/split/train/
predict/evaluate development path passed. The submission freeze check passed at
2026-10-05T04:04:14Z: 2,787 local files checked and public repository main still at
`70eaf5a99da400d6efeef22f8fdf920606e4816f`. The guard detects changes; it does not replace
organizer rules or verify external submission-platform records.

Run from this development checkout in PowerShell. Use a fresh output directory so earlier
evidence is preserved:

```powershell
$semPython = 'C:\Users\user\hacknation7th\output\post-hackathon\sem-build-20261005\images-env\Scripts\python.exe'
& $semPython scripts/run_sem_development.py `
  --python $semPython `
  --data-root 'C:\Users\user\hacknation7th\output\post-hackathon\sem-build-20261005\data' `
  --output 'C:\Users\user\hacknation7th\output\post-hackathon\sem-build-20261005\next-development-run' `
  --train-images 64 --epochs 3 --eval-images 24 `
  --encoder-weights 'C:\Users\user\hacknation7th\output\inspection-image-build\torch-weights\resnet18-f37072fd.pth'
```

## Highest-value next comparisons

1. Resolve soft-mask semantics and obtain genuinely normal SEM frames with authentic
   acquisition/lot groups and scale. These unblock missing classes, anomaly methods and
   normal false-alarm measurement.
2. Evaluate stratified development subsets and targeted small-defect examples; compare
   resize versus native tiles with the same data, weights and training budget. Address
   border/center shortcuts before interpreting recall.
3. Compare real-only versus rare-defect augmentation, then PatchCore/EfficientAD/DRAEM once
   normal-data prerequisites are met. Freeze complete pipelines before confirmatory tests.
4. Integrate observed-image scores with the budget policy and measure yield versus
   learning objectives separately. Compare cascades at matched recall and false alarms,
   including gate misses, inference, movement, review and acquisition latency.
5. For an equipment claim, secure matched capture recipes, ground truth, physical defect
   sizes and instrument outputs. Report detection-versus-size, false alarms and complete
   throughput on the same specimens. This cannot be inferred from this development run.

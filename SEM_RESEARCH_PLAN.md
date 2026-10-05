# WaffleBench SEM research implementation

Created 2026-10-05. Post-hackathon development — not part of the submitted version.

## Scope and ownership

This development campaign adds independently testable real-SEM data, image and decision modules. Submitted studies and their consumed holdouts remain unchanged. No external publication, deployment or primary campaign is part of this implementation session.

The implementation baseline is `c006a2b0a34735abcc4d17b275ed07fc3fd387f3`. Claude Code Opus 5.5 workers use medium effort, separate worktrees and no subagents:

| Worker | New files owned | Acceptance |
|---|---|---|
| sem-data | `sem_data/`, dedicated tests, `SEM_DATA_GUIDE.md` | Safe archive extraction, real image/mask audit, deterministic duplicate-group-disjoint splits |
| sem-images | `sem_images/`, dedicated tests, `SEM_IMAGE_GUIDE.md` | Geometry-preserving tiles, valid-region stitching, supervised segmentation train/evaluate boundary, optional anomaly-model adapters |
| sem-decisions | `sem_decisions/`, dedicated tests, `SEM_DECISION_GUIDE.md` | Budget-feasible audit policy, abstention/drift diagnostics, acquisition action contract, optional upstream integration adapters |
| Coordinator | This plan, source/data retrieval receipts, integration runner and integrated validation | Review candidate identity/scope, verify real data, run bounded development experiments, preserve submission freeze |

## Data contract

Manifest schema version 1 contains `dataset_id`, `data_mode`, `created_at`, dataset `root`, `license`, `limitations`, `split` metadata and `items`. Each item records `id`, relative `image` and `mask`, `defect_class`, original `width` and `height`, content hashes, `duplicate_group`, available `source_group` and assigned `split`.

Carinthia-S is an unstructured-layer SEM defect dataset, not a full-wafer inspection record. Do not manufacture clean negatives, physical pixel size, wafer identities or acquisition settings. Exact duplicate groups cannot cross train, calibration and test. Group by authentic wafer/session identifiers if supplied; otherwise explicitly report the weaker image-level split. The initial split uses seed 2026100501 and approximate fractions 70/15/15. Duplicate annotation conflicts must be resolved before training.

## Development run boundary

Initial supervised execution is a bounded pipeline smoke run: at most 64 training images, 3 epochs, 2 CPU threads, and at most 24 calibration images. Parameters may be made smaller for compatibility/debugging. Every run records the actual parameters, image identities, model hash, data-manifest hash, timestamps and runtime. Use a new output directory for every experiment. This run evaluates software and the training/inference path; it is not a confirmatory benchmark.

Only training items supply training labels. Persist calibration predictions before reading their masks for development metrics. Test items remain untouched; they are not used for selecting architecture, thresholds, tiling or augmentation. A separate preregistration with an immutable model/threshold and fresh test receipt is required for a primary comparison.

## Next hypothesis comparisons

1. Whole-image resize versus geometry-preserving overlapping tiles, keeping other relevant model choices fixed and separately calibrating complete pipelines.
2. Custom patch-memory baseline versus pinned reference PatchCore, conditional on verified representative normal training and test frames.
3. Mask-supervised localization versus patch anomaly scoring, with defect-component recall, pixel overlap and per-class/size denominators reported separately.
4. Yield-only review selection versus a capped uncertainty/diversity audit allocation, counting audit cost and separating current-lot yield from future learning.
5. All-slow versus fast/slow cascade at matched detection and false-alarm requirements, including gate misses and complete pipeline latency.
6. Single capture versus paid quality-triggered repeat capture at equal acquisition resources; simulate first and verify physically only when actual paired captures are available.
7. Real-only training versus controlled rare-defect augmentation on a real independent holdout.

Pattern registration requires repeated-pattern reference pairs. Defect-only frames cannot establish clean-frame false-alarm rate. Normal-only anomaly training cannot be claimed when representative normal images are missing. Hardware acquisition, proprietary study data and restricted code are recorded as dependencies, not simulated as completed integrations.

## Upstream reuse and provenance

Pinned source checkouts, original licenses and retrieval receipts live in the separate post-hackathon output directory. These are local source forks/checkouts; no public GitHub forks or pushes are created. Preserve attribution when code is adapted. Current WaferDC and Alibi Detect sources are excluded from product integration because of the reviewed license limitations. Optional backends must distinguish missing dependency, configured adapter, successful import, successful inference and measured evaluation.

## Acceptance and reporting

Focused unit checks cover archive safety, split leakage, geometry, padding, inference/evaluation separation, deterministic budget decisions and invalid-acquisition state. Independently rerun accepted checks in the integrated checkout. Test old modules only as needed to verify imports and frozen dependencies were preserved. Do not substitute fixture outcomes for real image results.

The integration report records completed features, upstream availability, real runs, unexecuted experiments, blockers and exact candidate commits. Any superiority claim requires a matched observation/recipe/truth/false-alarm/time comparison against an actual instrument. Run the submission freeze check after integration.

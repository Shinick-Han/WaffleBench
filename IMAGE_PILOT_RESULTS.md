# VisA PCB1: ranking improved, threshold detection did not

The separately frozen real-image feasibility pilot evaluated the official VisA PCB1 test split: **100 normal and 100 anomalous PCB photographs**. A PatchCore-inspired patch-memory adaptation increased image AUROC from **0.7881 to 0.8098**, but reduced AP from **0.8270 to 0.8038**. At each method's calibration-only threshold, defect recall fell from **42% to 33%**, while normal false alarms rose from **4% to 6%**. This is not a successful detection improvement and neither method is ready for deployment from these results.

| Fixed method | Image AUROC | Image AP | TP / FN | FP / TN | Defect recall | Normal false alarms |
|---|---:|---:|---:|---:|---:|---:|
| Global pooled nearest normal | 0.7881 | 0.8270 | 42 / 58 | 4 / 96 | 42% | 4% |
| Patch nearest memory, maximum | 0.8098 | 0.8038 | 33 / 67 | 6 / 94 | 33% | 6% |

AUROC and AP describe ranking, not accuracy. Thresholds were fixed at the 95th percentile of normal calibration scores before any test score or label was used: global **2.229484**, patch **19.022955** in their respective internal distance units. They were not adjusted after this result.

## What was actually trained

The official 904 normal training images were divided deterministically into **814 memory images and 90 calibration images**. Both methods used the same CPU ResNet18 ImageNet features, local aggregation and train-only normalization. The global method uses a mean descriptor per image. The patch method stores a 1,024-point coreset of local descriptors and scores an image by its largest nearest-memory distance. Whole photographs are resized to 224×224; this discards fine detail and distorts aspect ratio. These limitations motivate another preregistered study on a different PCB category; they do not license tuning on this consumed test set.

This is an explicitly documented adaptation of [PatchCore](https://github.com/amazon-science/patchcore-inspection), using the official [VisA data and split](https://github.com/amazon-science/spot-diff). VisA photographs are real industrial PCB images, licensed [CC BY 4.0](https://github.com/awslabs/open-data-registry/blob/main/datasets/visa.yaml). They are **not wafers or SEM images**, and this pilot is separate from the numerical inspection-budget studies. No pixel accuracy, wafer yield, equipment throughput, or confidence interval is claimed.

## Reproducibility

Reviewed source `f143adf` includes full Python-environment verification before scoring. The separate CPU image environment leaves the inspection research environment unchanged. Twenty-one focused tests passed, including train/test separation, calibration-only thresholds, tamper refusal, source and environment checks, scoring before test-label use, and single-shot evaluation. Two worker training runs and the coordinator's reviewed training run produced the same memory artifact and thresholds.

Freeze digest: `9e4b5d94b79090844c2ed8adf70fa4bd8491615ffa67e33c30c11e043014b3ab`. Memory SHA-256: `303e238ace60fecef451ca04bc79a948ef7a8c6e0f22af09aaba01db708e7d1b`. The coordinator verified that receipt before the only test evaluation. Aggregates, scores and the source/model/data freeze are preserved in `evidence/inspection-images-v1/`; raw photographs, weights and model artifacts stay in the local build output. This does not constitute an independent reproduction of the published PatchCore configuration.

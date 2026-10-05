# WaffleBench inspection augmentation engine

Created 2026-10-05. **Post-hackathon development — not part of the submitted version.**

This local engine augments acquired images and incumbent pixel-coordinate candidates.
It is a working software exchange/decision layer, not a physical instrument or a
validated commercial inspection product. It never sends stage or acquisition commands.

## What now runs end to end

1. Verify an acquired grayscale image and a frozen model, run native-resolution inference.
2. Revisit every incumbent candidate using that actual image evidence. Emit supported,
   nuisance-review or reacquisition states. Every incumbent remains retained.
3. Scan all usable acquired pixels, including pixels outside incumbent boxes. Generate
   strong-seeded connected regions with explicit source-pixel coordinates. A new region
   must have zero intersection with incumbent boxes; otherwise it is associated with
   incumbent detections and can suggest a refined localization box.
4. Feed image evidence into the actual score-per-review-bound queue. Export admitted and
   deferred candidates, resource reservations and unknown outcomes.
5. Import human or independent-lab review outcomes only for admitted actions. Record
   explicit charges and evidence ids. Failed, missing and deferred actions remain unknown.

The existing `routing_poc` archival replay contract is unchanged. Its observer remains
auxiliary. `inspection_engine` is a new explicit workflow in which image scores affect
the queue; it does not relabel historical detector reports.

## Local environment

Use the separate Python environment already created for data-efficient development:

```powershell
$enginePython = 'C:\Users\user\hacknation7th\output\post-hackathon\data-efficient-20261005\env\Scripts\python.exe'
```

Run commands from this development worktree. No original environment was upgraded and
no model weight is downloaded. NumPy/Pillow are used for the exchange layer; direct
image inference additionally uses the existing CPU Torch/SMP backend.

## Local execution screen

The executable upload screen is `web/post-hackathon-engine/inspection-engine.html`,
served by `scripts/serve_inspection_engine.py` on loopback port 8877. It fixes the local
model and accepts only an acquired image, candidate boxes, source, quality and review
allowance. There is one admitted analysis at a time. Color images and EXIF rotations
are refused instead of silently changing the image/coordinate contract.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_inspection_engine.ps1
```

Open `http://127.0.0.1:8877/inspection-engine.html`. Click **Load real SEM example**,
then **Run local analysis**. This executes the actual frozen CPU model, not a recorded
result. The example incumbent boxes remain clearly labeled software proxies. Users
can upload their own grayscale PNG/JPEG and original-image pixel boxes. Runs are
saved separately under `output/post-hackathon/inspection-engine-20261005/live-runs`.

The separate standalone comparison HTML remains a recorded development evidence
review. Its budget slider changes admissions only and never changes archived outcomes.

## Prospective input contract

`frame.json` must contain exactly these fields. Boxes are `[x0,y0,x1,y1]` with exclusive
right/bottom edges at original image scale. The list may explicitly be empty for an
AI-only scan, which must not be described as recovery against an actual equipment list.

```json
{
  "schema_version": 1,
  "frame_id": "your-acquired-frame-id",
  "width": 480,
  "height": 480,
  "image_sha256": "<64 lowercase hexadecimal characters>",
  "model_sha256": "<64 lowercase hexadecimal characters>",
  "data_mode": "instrument_shadow",
  "baseline_source": "equipment export identifier and acquisition recipe",
  "quality": "valid",
  "candidates": [
    {"candidate_id": "equipment-001", "bbox_px": [20,30,60,70], "review_bound_s": 10.0}
  ]
}
```

`quality` is caller-declared `valid`, `unknown` or `invalid`, not an automatic instrument
qualification. Unknown/invalid quality yields no defect-absence decision. Optional
coverage is a native-size Boolean `.npy`, loaded without pickle and with an explicit
hash. Pixels excluded by it are unmeasured, even if a score map has values there.
Runtime frames and candidates reject masks, class labels, truth and future fields.

```powershell
& $enginePython -m inspection_engine.cli analyze --frame frame.json --image acquired.png --model-root FROZEN_MODEL_DIRECTORY --budget-s 120 --out NEW_OUTPUT_DIRECTORY
```

For a verified cached score map, replace `--image`/`--model-root` with
`--probability map.npy --probability-sha256 SHA256`. In this mode, the engine validates
the map's bytes and caller-declared metadata; it does not independently establish
which model generated it. `--config config.json` records an explicit frozen Config
and its hash. It never enables automatic suppression.

Outputs: `analysis.json`, `plan.json`, `probability.npy`, `inference-receipt.json`,
`status.json`. Existing output directories are refused, and failures are preserved.

## Independent review exchange

```json
[
  {
    "frame_id": "your-acquired-frame-id",
    "candidate_id": "equipment-001",
    "status": "ok",
    "label": false,
    "charged_s": 5.0,
    "evidence_id": "human-review-001",
    "source": "human_review"
  }
]
```

`source`: `human_review` or `independent_lab`. Failed/missing events require `label:null`.
Another score from the same model is not accepted as independent confirmation.
Review labels are caller-supplied observed reports, not engine-certified physical
truth. The engine checks admission, duplicate consumption, pending states and costs.
Frame ids must match the plan, which also preserves the acquired-image and model hashes.

```powershell
& $enginePython -m inspection_engine.cli reviews --plan RUN_DIRECTORY\plan.json --events reviews.json --out NEW_REVIEW_DIRECTORY
```

Bounds concern independent review of acquired image evidence. They exclude physical
wafer loading, movement and capture. Refined boxes are suggestions, not authorized
changes to stage targets or actual acquisition-cost estimates. Coordinate conversion
requires image origin, scan orientation and a qualified pixel-to-stage transform.

## Development study and evidence review

`inspection_engine.study` uses 32 previously consumed tuning images and 64 previously
consumed development images from original calibration, never the original 627-image
test. It validates receipt/map hashes, disjoint known groups and original split roles.
All prospective results are persisted before development evaluation masks are read.
Threshold candidates and both comparisons are recorded before evaluation.

The primary incumbent is the same model at 0.50; a 0.95 proxy is a fragmentation stress
case. Neither is commercial equipment. One-to-one bounding-box IoU >=0.25 counts each
reference defect and candidate once. Nuisance filtering and ROI refinement are offline
shadow ablations, never silently applied to live detections. Localization recovery is
reported separately from discovering a baseline-absent physical defect.

The standalone `inspection_engine.report` HTML embeds verified source images and actual
evidence only. It supports image navigation, proxy selection, reference-box toggles,
review allowance and candidate selection. The allowance changes admission only; it
cannot reveal future outcomes or improve the reported retrospective metrics.

## Product qualification milestones

| Milestone | Current state | Exit evidence |
| --- | --- | --- |
| Image-to-decision coupling | Implemented locally | Native model/byte receipts and queue response checks |
| Nuisance triage + outside-list candidate generation | Implemented locally | Known fixture boundaries and real image-coordinate development analysis |
| Paired incumbent comparison | Software proxy only | Actual equipment exports plus independent truth of flagged and unflagged regions |
| Recall-safe nuisance suppression | Disabled | Fresh lot-grouped study with acceptable uncertainty and qualified review outcomes |
| Added physical DOI | Unproven | Confirmed defects absent from actual incumbent output; extra nuisance and review costs reported |
| Equipment-facing operation | JSON shadow exchange only | Qualified transform, adapter, measured time bounds and prospective site/lot comparison |

The qualification report distinguishes engineering pass from scientific sufficiency.
Zero observed missed positives in a small image sample does not establish negligible
miss risk. Its binomial bound is explicitly conditional on independent observations,
which the available acquisition metadata cannot establish.

Primary next data packet: raw scans including unflagged regions, original equipment
candidate boxes/scores/recipe, image registration metadata, independent ROI labels,
lot/acquisition ids and end-to-end review times. Training or RL alone cannot supply
missing physical observations or validate commercial superiority.

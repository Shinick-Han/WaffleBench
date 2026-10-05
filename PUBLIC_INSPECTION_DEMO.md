# WaffleBench live inspection development demo

**Post-hackathon development — not part of the submitted version.**

Live URL: <https://wafflebench-lab.shinick.dev>

Click **Load real SEM example**, then **Run analysis**. The service runs the fixed
segmentation model on the selected image, constructs original-pixel review regions,
and reserves a bounded independent-review allowance. This is actual new inference,
not a replay of an archived result. Download the returned evidence JSON to keep it.

The public service accepts 8-bit grayscale PNG/JPEG images (8 MiB and 1,048,576
pixels maximum), up to 200 candidate boxes, caller-declared quality and a review
allowance. It rejects arbitrary paths, commands, model choices and truth fields.
One analysis runs at a time, with shared rolling limits of 12 attempts/minute and
1,000/day. These limits include invalid analysis requests and reset on restart.
Uploads and intermediate inference artifacts use a per-request temporary directory
and are deleted before a response returns, including failed runs. The normal local
runner still retains its own evidence; public uploads are never routed to it.

## Deployment and availability

A dedicated Cloudflare named tunnel forwards HTTPS traffic to the owner-operated
CPU server on `127.0.0.1:8878`. **The owner's PC, Python service and tunnel must
remain running.** This is a live development deployment with no availability SLA.
No inference API key is needed by visitors. No instrument is connected.

Only the development branch is published. The submitted `main` commit
`70eaf5a99da400d6efeef22f8fdf920606e4816f`, submission pages and videos remain
unchanged. This branch snapshots additive post-submission implementation from
local commit `9dc8902de86e822d353195afbf72fa4639867aa6`, with a separate public runner.

## Running the service elsewhere

This source branch does **not** include acquired-image collections, trained model
weights, private runtime state or tunnel credentials. A fresh clone alone cannot
run the model-backed service. Supply a verified frozen model directory containing
`frozen.json` and its checkpoint, plus an image and matching development frame.
See [INSPECTION_ENGINE_GUIDE.md](INSPECTION_ENGINE_GUIDE.md) and
[SEM_IMAGE_GUIDE.md](SEM_IMAGE_GUIDE.md) for model training and image contracts.

The deployed model checkpoint SHA-256 is
`4377d3354b06c449734a38a04a48949ca7955667d4c65c92ccfc5813347d99d9`.
The existing isolated CPU environment uses Torch, NumPy, Pillow, and the pinned
segmentation-models-pytorch backend. No dependencies in the submitted environment
are upgraded by this deployment.

```powershell
python scripts/serve_public_inspection_engine.py `
  --model-root FROZEN_MODEL_DIRECTORY `
  --out DEDICATED_SCRATCH_DIRECTORY `
  --example-frame VERIFIED_DEVELOPMENT_FRAME_JSON `
  --example-image VERIFIED_DEVELOPMENT_IMAGE `
  --public-origin https://YOUR_DEDICATED_HOSTNAME `
  --port 8878
```

For a locally managed Cloudflare tunnel, use a new tunnel and a dedicated config;
never reuse or overwrite submitted-demo routes. Keep its credentials outside Git.
The matching ingress entry must set `originRequest.httpHostHeader` to
`127.0.0.1:8878` and `originRequest.disableChunkedEncoding` to `true`. The service
allows POSTs only with the exact configured HTTPS `Origin`. No CORS bypass is added.

## Scientific limits

Example incumbent boxes are **same-model software proxies**, not commercial
equipment logs. Image scores are uncalibrated; candidates require independent
confirmation. Incumbent detections are retained, and unknown acquisition quality
requests reacquisition rather than inferring defect absence. Review bounds exclude
physical loading, stage movement and capture costs.

The current study uses previously consumed development data, not the original
627-image holdout. It does not establish superiority to commercial instruments or
automatic nuisance-suppression safety. Improved proxy localization is distinct
from finding a physical defect absent from an equipment detection list.

## Attribution

The real example is from Carinthia-S, Corinna Kofler (KAI GmbH) and Vahidin Hasić
(University of Sarajevo), 2025, [Zenodo DOI 10.5281/zenodo.16895427](https://doi.org/10.5281/zenodo.16895427),
licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
The original image is used for inference; generated boxes and overlays are software
annotations. The underlying Carinthia dataset is
[DOI 10.5281/zenodo.10696644](https://doi.org/10.5281/zenodo.10696644).

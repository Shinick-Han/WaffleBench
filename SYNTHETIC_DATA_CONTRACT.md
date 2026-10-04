# Separate synthetic inspection dataset v1

This extension never replaces `web/data/snapshot.json`, the frozen protocol,
the primary experiment or its reported findings. Source material supplies
taxonomy, not measured distributions. Numerical limits and probabilities are
authored examples for an illustrative logic-die wafer-sort scenario.

## Reproduction

`python scripts/generate_synthetic_wafers.py` writes the data with seed 7001305.
The generator uses only the standard library. Three wafers share one synthetic
lot. Each has 1305 full dies within an illustrative 300mm wafer, 8x6mm die,
0.08mm streets and 3mm edge exclusion. Forty attempted tests per die give
156600 measurement records. A seeded shared spatial field produces correlation;
independent die noise, multiple defects, inspection misses, false positives and
missing test values prevent a perfect-observation toy scenario.

## Files and boundaries

| File | Meaning |
| --- | --- |
| `synthetic_wafers/measurements.csv` | 40 raw attempted tests per die; blank value and `status=missing` for unavailable values |
| `synthetic_wafers/observed-only.json` | Blinded agent/model input, without generator mechanism labels or wafer scenarios |
| `synthetic_wafers/ground-truth.json` | Separate oracle labels; never input to blinded selection |
| `synthetic_wafers/synthetic-wafers.json` | UI exploration joins observations and clearly labeled generator truth |
| `synthetic_wafers/manifest.json` | Seed, counts and SHA-256 hashes |
| `web/data/synthetic-wafers.zip` | Deterministic archive of those five data files |

The JSON schema includes geometry, sources, 34 researched catalog entries,
19 modeled entries, 10 pattern descriptors, 40 test definitions and 3 wafer
records. Mechanisms, electrical outcomes and spatial patterns are distinct.
The three scenarios exercise random/local, edge-ring/center and scratch/field
repetition. Donut and near-full are documented patterns, not generated cases.
Reliability stress and downstream bonding/dicing are researched but unmodeled.

## Interpretation

The 36 electrical tests are delay, leakage, line/contact resistance and Vth at
3 voltages x 3 temperatures. Four additional records measure CD, overlay X/Y
and film thickness. Limits are in each test record and are not fab standards.
Displayed electrical metrics are observed 1.0V/25°C values; displayed overlay
is the X component. Missing nominal metrics remain null.

`fail` means at least one observed test is outside its authored inclusive limits.
`inconclusive` means no observed failure but at least one required test is
missing. `pass` requires all 40 observed tests within limits. The bin does not
read generator truth. A detected particle may be electrically benign; a missed
physical defect may still cause an electrical failure.

The initial viewer's 35 tiles are PVT conditions, not 35 sampled manufacturing
dies. This separate dataset inspects every generated die. Real production
inspection sampling depends on modality, time, cost and required sensitivity.
Three wafers support demonstration and data plumbing, not independent-lot
generalization or proof of real yield improvement. For any learning experiment,
split by wafer/lot and keep oracle labels out of features and agent prompts.

## Data sourcing investigation (2026-10-04)

Bright Data's public catalog and domain searches returned no verified wafer/die
defect product. Its custom data offering is public-web collection; this does not
establish availability of private fab measurements. Aside's content filter
blocked the website, so no dashboard inventory, purchase or vendor contact was
performed. The official catalog and public-data restriction are linked in JSON.

WM-811K is an established real wafer-bin-map research source; the original lab
endpoint could not be reached during this check and its license/download were
not verified. MixedWM38's author describes fabrication maps augmented using GAN
generation, so it must not be labeled entirely real. UCI SECOM supplies 1567
production entities and 591 columns according to its current page, with pass/fail
labels and missing values; it does not supply die coordinates. None of these
datasets was copied into this synthetic release. See JSON `sources` and
`procurement` for the primary reference links.

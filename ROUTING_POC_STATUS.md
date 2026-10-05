# WaffleBench routing PoC status

Post-hackathon development, excluded from the submitted version. 2026-10-05.

## Decision

The optical-candidate to SEM-review routing scaffold is implemented and independently
checked. A time-aware policy improves on a risk-only baseline in the fixed synthetic
fixtures. The bounded beam policy **does not demonstrate a reliable improvement over
the stronger risk-per-second baseline**. Commercial superiority is unproven.

The highest-ROI next step is paired real instrument data and an incumbent-policy
comparison, starting with the simple time-aware policy as a strong baseline. More
lookahead or another decision model is not justified by this experiment alone.

## Implemented behavior

`routing_poc/contracts.py` separates preselection optical candidates from private
archived detector outcomes and independent reference. The policy receives only
public candidates, cost estimates and already-paid outcome history. It receives no
SEM image paths, masks, embeddings, reference labels, fixture seeds or provenance.

`routing_poc/replay.py` admits actions against a full retry reserve before reading
their archived results/images. Loading, movement, settling, capture, inference and
failed retries are charged. Unavailable/failed/missing outcomes stay unknown.
An optional observer runs only after admission and cannot overwrite detector reports
or inject image scores into the policy history.

Three policies share frozen priors and a 10% uniform exploration mixture with exact
conditional selection probabilities: `risk_only`, `risk_per_second`, `beam_route`.
Beam planning is a bounded depth-three heuristic; feasible short prefixes remain
eligible, and unidentifiable movement speed is refused rather than assumed free.
There is no online model update, live instrument control or KLARF/GEM connector.

Metrics separate selected detector reports, independent-reference confirmed DOI,
false confirmations and escapes. Partial historical logs cannot establish unseen
counterfactual outcomes or full recall. Recall is over optical candidates only;
wafer-wide recall remains unidentified.

## Frozen synthetic comparison

Protocol hash: `669646cd2a6761dbb1a1daccee37034058d03cbcef1cb5760cd3d162accf0dd6`.
Source frozen at integration commit `4948463`, before the reserved campaign.
Development seeds 12000–12007; reserved synthetic seeds 22000–22039, run once by
the coordinator after integration and checks. No source tuning followed test results.

Two regimes, two resource budgets, three policies, 40 independent generated lots per
regime: **480 runs**, each job containing 120 candidates on three synthetic wafers.
Every policy uses the same archived outcomes/reference within a lot. Selection RNG
is derived from public job ID and policy; exploration draws differ between policies.
Intervals are paired lot-level percentile bootstraps, 2,000 replicates. They are
exploratory intervals without adjustment across multiple comparisons/budgets.

Mean independently confirmed DOI per synthetic lot:

| Regime / budget | Risk only | Risk per second | Beam route | Beam − risk/second, 95% CI | Beam − risk only, 95% CI |
|---|---:|---:|---:|---|---|
| Clustered / 120 s | 3.100 | 4.900 | 5.000 | +0.100 [−0.625, +0.875] | +1.900 [+1.275, +2.525] |
| Clustered / 240 s | 5.525 | 9.475 | 10.025 | +0.550 [−0.425, +1.500] | +4.500 [+3.550, +5.425] |
| Diffuse / 120 s | 10.475 | 11.475 | 11.350 | −0.125 [−0.550, +0.325] | +0.875 [+0.200, +1.550] |
| Diffuse / 240 s | 17.550 | 19.500 | 19.400 | −0.100 [−0.575, +0.375] | +1.850 [+1.325, +2.350] |

The clustered beam versus risk-only mean gains are 61.3% and 81.4%; these are ratios
of aggregate means, not paired percentage-gain estimates. Against risk-per-second,
the corresponding ratios are 2.0% and 5.8%, with difference intervals containing zero.
The diffuse regime slightly favors risk-per-second. There is no universal beam win.

Safety and coverage context (mean per lot):

| Regime / budget | Beam escapes | Risk/second escapes | Beam false confirmations | Risk/second false confirmations | Beam candidate recall | Risk/second candidate recall |
|---|---:|---:|---:|---:|---:|---:|
| Clustered / 120 s | 25.875 | 25.975 | 0.450 | 0.350 | 16.47% | 16.33% |
| Clustered / 240 s | 20.850 | 21.400 | 0.825 | 0.875 | 33.28% | 31.05% |
| Diffuse / 120 s | 18.700 | 18.575 | 0.825 | 0.775 | 37.75% | 38.19% |
| Diffuse / 240 s | 10.650 | 10.550 | 1.775 | 1.925 | 64.77% | 65.07% |

Budgeted selection leaves many reference defects unconfirmed. This is not a high
wafer-detection-rate claim. Mean spent charges range from 113.44–114.68 s for the
120 s budget and 233.46–234.78 s for the 240 s budget; all 480 runs obey admission
and total-resource limits. These charges are modeled seconds, not measured physical
throughput. Policy decision wall time is recorded separately and excludes replay
state construction; a real comparison must include complete orchestration latency.

Priors are calibrated by construction and cost/position/detector parameters are
invented. Detector sensitivity 0.9 and false-positive probability 0.05 are generator
settings, not measured SEM or commercial equipment capabilities. No vendor policy
or acquired wafer was evaluated. All outputs have `commercial_validated: false`.

## Actual SEM inference compatibility

A frozen model ran on one real **calibration** SEM image only after synthetic action
admission. It produced a native 480×480 probability map; no reference mask or held-out
test image was read. Model hash:
`4377d3354b06c449734a38a04a48949ca7955667d4c65c92ccfc5813347d99d9`.

Observed callback wall time was 7.535 s, including cold dependency/model loading and
inference. The archived 0.2 s inference charge was an explicitly invented wiring
fixture and does not describe this actual callback. Thus the test proves wiring and
the post-admission access boundary, not latency feasibility. A physical implementation
must warm the runtime and measure/charge inference and queue overhead accurately.

Optical prior, wafer coordinates, recipe, archived detector report and timing in this
compatibility receipt are synthetic. Image localization is auxiliary: it does not
reclassify the archived report or demonstrate model-driven routing gains. Pixel
probability maxima are not calibrated site-level DOI probabilities.

## Checks and evidence locations

- **84 focused tests passed, no skips**, after integration and the final tie-order fix.
- The independent coordinator recomputed confirmed DOI, false confirmations, escape
  breakdowns and resource accounting from all 480 raw runs/references; 101 unknown
  final outcomes were checked for preservation. Current benchmark sources match the
  pre-run source hashes, and the campaign's post-run source verification passed.
- Development campaigns preserve both pre-final-tie and final-source outputs. The
  reserved campaign was not repeated. Existing output directories are never overwritten;
  the CLI does not enforce a global one-run test registry.
- Submitted artifact guard passed at `2026-10-05T04:57:54.316914Z`: 2,787 local files
  checked; public main remains `70eaf5a99da400d6efeef22f8fdf920606e4816f`.

Evidence under `C:/Users/user/hacknation7th/output/post-hackathon/routing-poc-20261005/`:

- `reserved-synthetic-test/summary.json`, `protocol.json`, `source_freeze.json`,
  `source_verification.json`, `jobs/`, `runs/` and `status.json`;
- `independent-result-check.json` and `independent_result_check.py`;
- `observed-sem-compatibility/receipt.json` and the probability map;
- `data-sourcing/ipi-retrieval.json` and the retrieved logistics CSV.

The package is stdlib-only for routing replay. The actual-image observer uses the
isolated `sem-build-20261005/images-env` Python environment. No public deployment,
submitted artifact update, supplier contact or purchase was performed.

## Run and next gate

From this isolated repository, for a fresh development-output directory:

```powershell
python -m routing_poc.cli demo --out C:\Users\user\hacknation7th\output\post-hackathon\routing-demo-new
python -m routing_poc.cli replay --job job.json --archive archive.json --out replay-new --budget-s 120 --policy risk_per_second
```

The delivered replay bridge accepts the contract JSON. Acquiring raw SEM images
alone will not close the evidence gap. [DATA_ACQUISITION_BRIEF.md](DATA_ACQUISITION_BRIEF.md)
lists verified repositories, commissioned-collection candidates and an unsent RFQ
for coordinates, real action times, selected/unselected reference and incumbent
outputs. NNFC is the most direct domestic linked-map/SEM collection lead; KANC is a
nearby feasibility lead; EAG describes the matching optical-coordinate workflow.

A commercial PoC gate requires a preregistered same-instrument comparison, held-out
lots/recipes, real elapsed-time accounting, independent unselected-site audits and
rights to use the data/models. Until that gate passes, the defensible result is a
working routing scaffold and a synthetic advantage over a weak baseline.

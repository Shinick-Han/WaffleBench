# WaffleBench data acquisition brief

Post-hackathon development; excluded from the submitted version. Investigated 2026-10-05.

## Decision

The best next acquisition is a small commissioned optical-to-SEM review experiment,
with paired candidate coordinates, images, instrument timing and independent reference.
No complete off-the-shelf package containing these fields and an incumbent routing
baseline was verified in the repositories and supplier catalogs checked. This is a
search finding, not proof that such a product cannot exist.

Use public SEM images for perception development now. Keep image segmentation,
workflow scheduling, historical replay and actual instrument comparison as separate
evidence types. Do not attach invented wafer coordinates or times to real images and
call the resulting hybrid an equipment benchmark.

## Verified public sources

| Source | What is available | Useful role | Missing for routing comparison / access |
|---|---|---|---|
| [Carinthia-S, Zenodo](https://zenodo.org/records/16895427) | 4,591 SEM image/mask pairs, six defect classes; one production layer of unstructured semiconductor wafers | Real-image localization training; already downloaded and audited locally | No matched optical candidate export, tool action timing or incumbent sequence in the inspected package. Physical scale and acquisition-group independence remain unknown. Record license verified locally as CC BY 4.0. |
| [Internal Physical Inspection process flows](https://zenodo.org/records/10069426) | FA laboratory job/task logistics: equipment ID, operator ID, task type, submission/finish/deadline, task working time in hours; CC BY 4.0 | Separate failure-analysis job scheduling research and workflow schema examples | No SEM images, wafer candidate coordinates or defect-reference labels. Task working time must not be treated as SEM dwell or move time. |
| [UCI SECOM](https://archive.ics.uci.edu/dataset/179/secom) | 1,567 production entities, process features, pass/fail outcomes and test timestamps; CC BY 4.0 | Process-risk and drift experiments | No paired SEM images, candidate-level review sequence or acquisition-duration log. Not downloaded for this task. |
| [Siemens AOI/MIS research data, Mendeley v2](https://data.mendeley.com/datasets/99jzmh9658/2), [paper](https://www.sciencedirect.com/science/article/pii/S2352340924000830) | 132 days of PCB solder inspection measurements and human review labels, according to the paper | Adjacent false-call triage research; closer to real downstream review than a segmentation-only collection | PCB, not wafer SEM. Selected AOI-positive cases; human label noise and drift; cannot establish full escape rate from uninspected negatives. CC BY-NC 3.0 plus a restrictive research-use notice; not imported into product training. File download availability was not verified. |

### Retrieved and inspected logistics data

`output/post-hackathon/routing-poc-20261005/data-sourcing/ipis_2019_2022.csv`
was actually retrieved through Aside and parsed locally: **29,918 task rows, 1,646
distinct job IDs, 14 columns**, 9,877,726 bytes. MD5 matches the repository:
`c8cacc2f0f1527c550c6ea411d774c6b`. SHA-256:
`fe7649d1bda05abf6f7b03640cad9b5724242a528ee6d9554accf4caa1a168a5`.
The adjacent `ipi-retrieval.json` preserves the retrieval/audit receipt. Counts are
local observations; source description and license come from the linked record.
The repository's prose has inconsistent year coverage, so no date-span claim is made.
This file was not inserted into the SEM routing benchmark.

## Paid acquisition routes, ranked

| Priority | Provider / path | Verified capability | What still needs a quote or agreement |
|---|---|---|---|
| 1 | [NNFC, Daejeon: photoresist defect evaluation](https://www.nnfc.re.kr/prog/srvc/kor/sub01_01_07/01/view.do?srvcNo=73) | Inspection recipe setup, defect-size wafer maps and SEM-linked particle review images are explicitly offered | Customer-sample eligibility, raw coordinates, matched action timestamps, negative/audit coverage, same-tool incumbent baseline and commercial data rights. This is a commissioned analysis route, not a verified archive for sale. |
| 2 | [KANC, Suwon: fab / analysis services](https://css.kanc.re.kr/gnb03/snb01_01.do) | Semiconductor process and physical/electrical analysis services; inquiry, specification/run-sheet agreement and quotation process | Optical-to-SEM candidate linkage and raw timing delivery are not advertised on the overview. Nearby feasibility discussion, not a confirmed dataset product. |
| 3 | [Eurofins EAG: full-wafer particle analysis](https://www.eag.com/app-note/full-wafer-particle-analysis-of-sub-50nm-defects-by-auger-electron-spectroscopy/), [technical note](https://www.eag.com/wp-content/uploads/2019/12/M-038619_Full-Wafer-Particle-Analysis-of-sub-50nm-Defects-by-Auger-Electron-Spectroscopy.pdf) | Optical particle-scanner coordinates guide SEM localization and compositional analysis; technical note describes compatible optical inspection coordinate formats and up to 300 mm wafers | Exportable raw logs, complete candidate coverage, timing granularity, reference agreement, international sample logistics, price and training/derived-model rights. No customer archive was confirmed for sale. |
| 4 | [Nexdata](https://www.nexdata.ai/) / [Datatang](https://www.datatang.com/) | General off-the-shelf AI datasets, custom collection and annotation services | No wafer optical-to-SEM routing-log SKU was verified. Treat as sourcing/annotation leads only; establish semiconductor access and provenance before paying. |

NNFC has a [published equipment fee sheet](https://ems.nnfc.re.kr/resources/files/fees_12_en.pdf),
but a complete paired-data pilot requires a custom quote. No total pilot price or
turnaround is verified. Its service webpage carries a noncommercial/no-derivatives
public-content notice; commissioned data rights must be agreed separately. Website
illustrations are not a substitute for licensable measured data.

Other screened leads: wafer-bin maps such as WM-811K address spatial failure patterns,
not SEM acquisition routing; SEM competition datasets such as
[ISSM 2020](https://www.kaggle.com/c/issm-AI-tech-contest-2020/data) carry competition
access/rule conditions and do not advertise timing logs. Do not assume either is
commercially reusable. General semiconductor market/billing data and synthetic
manufacturing tables do not answer this benchmark question.

## Draft technical request for quotation

Subject: Paired optical-to-SEM review dataset and instrument-time pilot for WaffleBench

We are developing a decision layer that prioritizes and orders SEM review of optical
inspection candidates. We seek a small paid pilot on customer-provided or mutually
agreed reference wafers. The objective is to compare review policies on the same
instrument under a fixed elapsed-time budget, with independently assessed escapes.

Please confirm which of the following can be delivered, with a sample export/schema,
an itemized quote, sample requirements and data-use terms:

1. **Preselection candidate table:** stable candidate IDs, lot/wafer/layer IDs,
   physical X/Y coordinates and units, orientation/notch transform, optical attributes
   and scores, acquisition timestamp, tool/recipe/model version. Specify which fields
   were available before any SEM review.
2. **SEM action records:** planned and realized review sequence; start/end timestamps;
   loading, stage travel, settling, autofocus, exposure/capture, inference, retries,
   queue/operator delays; failed/missing acquisitions with reasons. Retain raw
   timestamps as well as computed durations. Specify clock synchronization.
3. **Images and scale:** original SEM images, hashes, nm/pixel or calibrated scale,
   channels, beam/dose/recipe metadata and quality flags. Provide genuine negatives
   and failures, not just visually prominent defects.
4. **Independent reference:** adjudicated defect-of-interest definition and evidence
   (including composition/electrical significance where appropriate), labels for
   selected sites and a probability sample of unselected sites, with audit inclusion
   probabilities. Keep unknowns explicit. If full reference is infeasible, identify
   the statistical inference limits of the proposed audit.
5. **Comparator:** incumbent default/expert review policy and outputs on the same
   hardware and recipe. Agree on budget, counting rules and decision-compute latency.
   Randomize comparable lots/runs where appropriate; do not claim that repeating
   exposure on a single wafer guarantees equivalent conditions.
6. **Rights:** ownership/access to commissioned measurements, anonymization and
   retention requirements, commercial training and derived-model permissions,
   aggregate-metric publication and restrictions on redistribution.

Please distinguish existing data availability from new collection, and list missing
fields explicitly. We have no current instrument access and need assistance with
sample/reference-wafer supply as well as data collection. Please quote that separately.

This is an unsent draft. No supplier has been contacted or paid.

## Pilot staging and decision gate

Start with one narrowly defined particle-review recipe and a small engineering lot
to establish data quality and coordinate/timing linkage. The initial sample count is
a feasibility choice, not a powered superiority study. Use the pilot's variance and
reference-event rate to size a separate held-out comparative experiment before
freezing its policy, metric, margin and analysis.

Evaluate confirmed reference DOI per actual elapsed minute at an agreed escape-risk
limit, or elapsed time at a matched sensitivity target. Include decision latency,
load/move/settle/retry time and independent unselected-site audits. Confidence
intervals must resample independent lots/acquisitions, not correlated pixels.
Optical candidates alone cannot measure wafer-wide recall: defects never proposed
by the upstream instrument require separate wafer-wide reference coverage.

Proceed toward a commercial PoC only if the same-hardware comparison clears the
preregistered improvement and escape-risk gates on held-out data. Synthetic route
gains or SEM mask Dice alone do not clear this gate.

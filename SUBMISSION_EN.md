# WaffleBench — Hack Nation 07

**An agentic lab for smarter wafer inspection.**

WaffleBench is the submission name; the source repository, Python packages and public URLs keep the original `falsify-lab` names.

## Project summary

After optical screening, a fab can afford expensive precision review for only a small fraction of flagged sites. WaffleBench treats each review as a costly experiment: it states what it is trying to learn, compares competing next reviews under a fixed budget, records what each one returned, and uses the result in the next choice. Unmeasured sites stay unknown.

We do not claim superiority over commercial inspection equipment. AI-assisted review sampling already exists commercially ([Applied ExtractAI, 2021](https://ir.appliedmaterials.com/news-releases/news-release-details/applied-materials-introduces-new-playbook-process-control-based/); [KLA SPOT and Klarity](https://www.kla.com/products/software-solutions/semiconductor)). Our contribution is an open, auditable research loop for choosing the next costly inspection experiment and for preserving what each experiment taught us.

**Who does what, stated precisely:**

- A **frozen numerical planner** (a tool, not an LLM) scores candidate sites and chooses the next review inside a lot.
- **Omnigent** (0.16.0, Claude Opus 5.5 specialists) coordinates role-limited analyst and experimenter agents, carries result IDs between them and keeps the shared ledger. The earlier inspection session used frozen numerical choices. In a separate verified posthoc cycle, the analyst authored diagnostic hypotheses, chose studies and interpreted results.
- **Study-level revisions** (which model or planner to test next, which dataset to replicate on) were made by the human coordinator and Claude Code workers between preregistered studies. They were **not** made by Omnigent.

## The discovery loop, with actual records

### Inner loop: one lot, recorded Omnigent session

Session `ef847c8a38b549ce861f69998966858c` on synthetic lot `lot-799f9a90368feda0` (120 CU cap, at most four reviews; site IDs below drop the lot prefix) ([proof](evidence/inspection-live/session-proof.json), [verification](evidence/inspection-live/verification.json)).

| Step | Record |
|---|---|
| Question | Which flagged site should get the next paid review? |
| Competing tests | Decision 1: three candidate sites, budget 120 CU |
| Chosen and run | Site `w0:r2:c10` → result `ir_71b91846930cb176`, reported a defect (particle), charged 17.0 CU |
| Update | The analyst's update consumed that result; the planner's next choice was `w0:r1:c10` from three candidates, with 103 CU remaining |
| Chosen and run | Site `w0:r1:c10` → result `ir_6573c8a47872df63`, reported a defect (bridge), charged 9.07 CU |
| Updated decision | Next selected site `w0:r-4:c13`; the bounded demonstration closed after two reviews at 26.0666 of 120 CU |

Five delegations (analyst → experimenter → analyst → experimenter → analyst) were matched to tool calls and result IDs. The first verification attempt failed because Omnigent reused child conversations; recovery read the existing records without new prompts or inspections, and the failed attempt stays in the proof. A paid "defect" report is a sensor observation, not ground truth. This session is a coordination demonstration, separate from the benchmark below.

### Outer loop: study-level revisions (human and Claude Code workers)

| Step | Evidence | What changed next |
|---|---|---|
| Question | At an equal review budget, can a planner confirm more defects of interest (DOI) than a strong learned baseline? | — |
| Evidence → hypothesis | v2 (receipt `ac99238f…`): the new classifier tied the logistic baseline (−0.033 DOI, CI [−0.583, 0.550]); an exploratory route-aware policy was +0.45 with a CI that included zero ([v2](INSPECTION_V2_RESULTS.md)) | Hypothesis: accounting for wafer loading and stage movement, not just per-site probability, is the lever |
| Preregistered test | v3 `cb400_route_full` on 100 fresh held-out lots at 360 CU (receipt `faadbe49…`) | **29.33 vs 27.38 DOI; +1.95, paired 95% CI [1.46, 2.43]; +7.12%** ([v3](INSPECTION_V3_RESULTS.md)) |
| Competing follow-ups | v4: Gaussian-mixture model swap. v5: deeper 3-step beam planner (receipt `7fa2e9dd…`) | v4: −0.283, CI [−0.567, 0.008], failed ([v4](INSPECTION_V4_RESULTS.md)). v5: +0.23, CI [−0.050, 0.530], failed, at about 52× planning compute ([v5](INSPECTION_ROUTE_V5_RESULTS.md)) |
| Updated decision | Keep the v3 incumbent; stop adding planner depth | Next bottleneck is upstream: in the low-contrast scenario, only 29.9% of latent defects ever reach review ([diagnostics](INSPECTION_QUALITY_DIAGNOSTICS.md)) |

A parallel image branch followed the same pattern on real photographs:

| Step | Evidence | What changed next |
|---|---|---|
| Negative result | VisA PCB1: patch memory improved ranking but recall fell 42% → 33% and false alarms rose 4% → 6% ([v1](IMAGE_PILOT_RESULTS.md)) | Hypothesis: whole-image 224 px resizing discards fine defect detail |
| Preregistered test | VisA PCB2 composite pipeline (freeze `a15a37ae…`) | **Recall 51% → 72%, false alarms 11% → 5%**; gain +21 pp, CI [+12, +30] ([v2](IMAGE_PILOT_V2_RESULTS.md)) |
| Fixed replication | Same pipeline on untouched VisA PCB3 (freeze `6bda4bd8…`) | **Recall 44% → 55%, false alarms 8/101 → 3/101**; gain +11 pp, CI [+1, +21]; **still misses 45 of 100 defects** ([v3](IMAGE_PILOT_V3_RESULTS.md)) |
| Updated decision | The composite pipeline changed resolution, patch grid, coreset and aggregation at once, so the gain cannot be attributed to resolution alone; it costs about 3.08× the CPU batch time | Next: aspect-preserving or local-crop pipeline vs the fixed pipeline on a fresh category, with preregistered recall, false-alarm and compute endpoints |

These are VisA PCB photographs, not wafer or SEM images.

## What did not work

- The 30% cost-saving target failed: mean cost to the first five DOI fell by **7.29%** (saving of means; the mean per-lot paired saving was 4.02%).
- The v4 mixture model and the v5 beam planner both failed their improvement criteria and were not promoted.
- The PCB1 pilot was negative. PCB1 and PCB2 test sets were consumed and never rescored.
- The PCB3 gain cleared its preregistered bar by one image.

## Supporting evidence: reusable architecture

The same ledger, budget and planner architecture was first audited on a circuit-model study ([`RESULTS.md`](RESULTS.md)). Each run spent 24 logical queries (9 calibration, 3 shared initial and 12 policy choices) to find where a frozen inverter-delay model fails against ngspice. Adaptive selection found 5.1 clear counterexamples versus 1.4 for random: paired +3.7, 95% CI [3.2, 4.1]. Descriptively that is about 3.6× random and 1.46× space-filling (3.5); no interval is claimed for those ratios. This is secondary evidence about the architecture, not about inspection.

## Scope and next step

The wafer results are authored synthetic numerical evidence. The photo results use real PCB images, not semiconductor SEM images. Production use would need real wafer imagery and equipment logs, a time-split comparison on the same lots against a production sampling method, and measured escapes, false alarms and tool time.

**Agents and policies:** [`AGENTS_AND_POLICIES.md`](AGENTS_AND_POLICIES.md) documents each specialist's decision, tools, inputs, outputs, budget boundaries and denied capabilities. There is currently no runtime human-approval gate and no Omnigent-led parallel experiment.

**Verified diagnostic discovery cycle.** A separate actual Omnigent session compared competing tests, chose `miss_partition`, revised its overall bottleneck hypothesis for low-contrast lots, then chose `capacity_bound` and proposed a fresh admission experiment. Two read-only diagnostics and five delegations passed provenance verification. This is privileged posthoc analysis of existing synthetic evidence, **not a new performance gain**. See [`DISCOVERY_CYCLE_RESULTS.md`](DISCOVERY_CYCLE_RESULTS.md) and the [recorded cycle](https://shinick-han.github.io/WaffleBench/discovery-cycle.html).

## Demo and source

- [Agent diagnostic discovery cycle](https://shinick-han.github.io/WaffleBench/discovery-cycle.html)
- [Inspection evidence](https://shinick-han.github.io/WaffleBench/inspection-evidence.html)
- [Real-photo evaluation](https://shinick-han.github.io/WaffleBench/inspection-images.html)
- [Recorded agent session](https://shinick-han.github.io/WaffleBench/inspection-live.html)
- [Source repository](https://github.com/Shinick-Han/WaffleBench)
- Two-minute demo storyboard: [`CHALLENGE_DEMO_PLAN.md`](CHALLENGE_DEMO_PLAN.md)

## Sources

- Databricks × Hack-Nation Challenge 03 brief, "Agentic Scientific Discovery" ([original](https://app.hack-nation.ai/api/documents/c0bdd796-5a59-4550-b36a-f4f223ce1409); local copy `../output/award-research/databricks-challenge.pdf`). Paraphrased: a two-minute demo, agent specifications and policies, cited evidence, measured improvement and the next experiment are part of the submission.
- PatchCore, Roth et al. 2021 ([arXiv:2106.08265](https://arxiv.org/abs/2106.08265)); our image method is inspired by it, not a reproduction.
- VisA dataset, Zou et al. 2022 ([spot-diff split](https://github.com/amazon-science/spot-diff)), CC BY 4.0.
- Tools: Omnigent 0.16.0, ngspice 47 and Python dependencies are listed in [`THIRD_PARTY.md`](THIRD_PARTY.md).

## Submission videos

English narration and captions; 1920 × 1080, 30 fps, H.264/AAC. Local files are under `../output/pitch-delivery/`.

1. `participant-introduction.mp4` — 58.0 seconds.
2. `product-demo.mp4` — 59.6 seconds.
3. `technical-explanation.mp4` — 58.0 seconds.

The files are prepared for submission. This document does not assert that a HackOS submission has been made.

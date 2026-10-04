# Autonomous Optimization Run Log

- User delegation: decisions delegated for 3 hours; continue until explicitly stopped.
- Start: 2026-10-04 06:53:30 UTC / 15:53:30 KST.
- Optimization end: 2026-10-04 09:53:30 UTC / 18:53:30 KST.
- Afterwards: produce 3 videos per the submission spec, each 60 seconds or less: participant introduction, product demo and technical explanation.

## Boundaries

The v1 and v2 scientific sources, frozen results, the existing PVT work and the public UI are preserved. New experiments go in `inspection_v3/` and a new output directory. Candidates are selected in development and then evaluated on a new test set, and failed hypotheses are preserved too. Real tool results are distinguished from authored synthetic results.

## Progress

1. v2 results, audit and profile preserved. An improvement in discovery yield over the strong baseline was not demonstrated.
2. 3 new Claude Code Opus 5.5 medium work sessions: rank-preserving calibration, vectorized route planning, sensor detection-yield model.
3. coordinator: integrated harness, new split, development selection, independent verification, bottleneck re-evaluation.
4. Videos: HyperFrames, 16:9, English pitch + subtitles, 3 × 60 seconds. Because the user delegated routine decisions, the coordinator decides the brief, style and script. No final performance numbers go into the script before the final experiment results are confirmed.


## Verified interim results (18:29 KST, optimization window in progress)

- Numerical synthetic inspection: CB400 + 2-step move-cost route planning is kept as the default candidate. On 100 new held-out lots at the same 360 CU: 29.33 DOIs vs 27.38 for logistic, +7.12%, paired difference +1.95 [1.46, 2.43]. The 5% discovery-yield goal passed. The mean-ratio saving in cost to reach 5 DOIs was 7.29%, which did not meet the separate 30% goal. [Results](INSPECTION_V3_RESULTS.md).
- Accuracy and budget were separated. At low contrast the recall ceiling of the optical candidate stage is 29.9%, and of a lot mean of 272.6 DOIs, 77.83 are excluded at the candidate stage. About 35 precision inspections is what 360 CU can pay for; it does not mean all 3,915 locations were inspected. [Diagnostics](INSPECTION_QUALITY_DIAGNOSTICS.md).
- The warm batch time of rank-identical frozen-model inference was reduced from 9.28 ms to 1.00 ms. This is numerical inference over 3,915 locations; cold start time and tool processing time are separate. It is not called an improvement in detection accuracy. [Profile](evidence/inspection-improvements-v3/inference-profile.json).
- Real PCB2 photos: between the 224 baseline and the 448 composite, defect recall went 51%→72%, +21%p [12, 30]; false alarms on normal images 11%→5%. 28/100 defects were still missed. The 200 images and the threshold, freeze, scores and statistics were independently audited. Because resolution, grid, coreset and aggregation change together, this is not interpreted as an effect of resolution alone. [Results](IMAGE_PILOT_V2_RESULTS.md).
- Separate PCB3 validation of the same fixed design: root ran it exactly once. 44%→55%, +11%p [1, 21], false alarms 8/101→3/101. Testing and the independent audit are complete. It passed the independent audit and root recomputation as of 18:30 KST. This is an audit of the stored scores, thresholds, splits, image bytes, model and statistics, not an audit that re-inferred from pixels.
- Rejected new hypotheses are kept too. Mixture v4 was -0.96% versus the strong CB400 route baseline with a CI including 0, so it was not promoted. 3-step beam v5 reached only +0.78% with CI [-0.05025, 0.53], failed the 5% goal, and its cumulative route-planning time was about 52×. [v4](INSPECTION_V4_RESULTS.md), [v5](INSPECTION_ROUTE_V5_RESULTS.md).
- A real Omnigent run was completed: 5 role delegations, 2 paid sensor reviews, 2 analysis updates that carried over result IDs, 26.0666/120 CU. The LLM coordinates roles and the frozen numerical planner selects locations. The AI performance of the numerical benchmark is not to be confused with something Omnigent produced. [Evidence](INSPECTION_LIVE_RESULTS.md).
- UI: numerical results, the paid-measurement replay of the three wafers, budget and miss diagnostics, and the real PCB error distribution are each kept separate. Checked at 390/768/1440px; every unmeasured die is unknown. Step replay of the real Omnigent run is also complete, and root verified the 5-step budget and observation counts, error recovery, hiding of future observations, and browser file hashes.
- The 3 videos are participant introduction 50s, product demo 59.6s and technical explanation 58s. They use figures made from the original results, real recorded screens, a standard synthetic English voice and subtitles timed by real speech recognition. MP4 output and final checks proceed after the optimization end time.

The final adoption criterion is to show accuracy, discovery yield, cost and latency separately, and to adopt only improvements that pass on new data under rules fixed in advance. Jev semantic judgment is not promoted to the primary performance path until real inspection notes and expert annotations are available.


18:43 KST resource cost check: on 16 fixed normal calibration images, 1 warmup + 6 measured runs, CPU 4 threads, the 224→448 composite method went from a batch median of 262.25→806.98 ms (3.08×) and a descriptor bank of 1.50→3.00 MiB (2×). The per-image split mean of 16.39→50.44 ms is not single-request latency or tool throughput. The scores matched the fixed calibration scores exactly, and test images were not re-inferred. The detection-rate improvement is presented together with the increase in compute cost.


## Optimization end / transition to video output

- 2026-10-04 09:53:30 UTC / 18:53:30 KST: the user-specified 3-hour window for new experiments and optimization was closed. From then on the model, thresholds, primary experiment and route policy are not changed.
- The default path keeps v3 CB400 + 2-step move-cost planning. The real-image composite method was evaluated and audited separately on PCB2 and the fixed PCB3, and its detection rate and compute cost were published together.
- Public inspection release: `fab438f`, https://shinick-han.github.io/WaffleBench/inspection-evidence.html . The private development Git history was not pushed; only allowlisted files were applied on top of the existing public history.
- root independently recomputed all 200 held-out ledgers / 6,932 paid rows / 7,344 sensor attempts of v5, and re-checked the 8 protected original files. The PCB3 audit with 39 tests and the actual audit of 1,106 images and frozen scores passed.
- Completed Claude workers were shut down. The user's and other projects' panes and servers were excluded from shutdown.
- Video output started: 09:53:47 UTC / 18:53:47 KST. The 50s introduction, 59.6s product demo and 58s technical explanation all passed browser execution and layout checks. Remaining work is rendering, media verification and delivery.


MP4 output and verification of the 3 videos complete: introduction 50.0s/7,223,401 bytes, product demo 59.6s/21,223,300 bytes, technical explanation 58.0s/7,470,192 bytes. All 1080p, 30fps, H.264/AAC, 60 seconds or less and 1GB or less. The actually encoded frames and audio streams, loudness and SHA-256 were checked. The result files and verification scope are recorded in PITCH_DELIVERY.md.

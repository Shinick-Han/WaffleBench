# Falsify Lab — Hack Nation 07

## Project summary

Falsify Lab turns a limited inspection budget into an auditable sequence of tests. A numerical model ranks likely defects; a cost-aware planner selects the next review; an Omnigent supervisor coordinates specialist agents. The workbench shows what was selected, why it was selected, what was observed, and how much each action actually cost. Unmeasured sites remain unknown.

The central question is practical: after optical screening, which locations should receive expensive precision review? Our synthetic study models three 300 mm wafers with 1,305 dies each. It includes review failures, retries, wafer loading, movement, and reserved versus charged cost. These are authored assumptions, not calibrated factory equipment measurements.

## Evidence

- **Synthetic equal-budget study:** on 100 fresh held-out lots, the frozen route-aware policy confirmed an average of 29.33 defects of interest versus 27.38 for the logistic baseline at 360 cost units. The paired difference was +1.95, with a 95% bootstrap interval of [1.46, 2.43]: a 7.12% relative gain.
- **Real PCB photographs:** on VisA PCB2, a composite patch-based pipeline improved defect recall from 51% to 72%, while the normal-image false-alarm rate decreased from 11% to 5%. The architecture changes included resolution, patch grid, coreset size, and aggregation; the gain cannot be attributed to resolution alone.
- **Separate PCB3 replication:** with the architecture fixed after PCB2, recall improved from 44% to 55%, while false alarms decreased from 8/101 to 3/101. The gain interval was [1, 21] percentage points. The primary method still missed 45 of 100 defective images.
- **Actual agent coordination:** the recorded Omnigent SDK session completed five delegations, two paid synthetic reviews, and two analysis updates, spending 26.0666 of 120 cost units. This demonstrates coordination and traceability; it is separate from the primary benchmark.
- **Software cost:** warm wafer-model inference became 9.24 times faster. The real-photo primary pipeline required 3.08 times the baseline CPU batch time. Neither measurement establishes factory throughput.

## What did not work

The 30% cost-saving target was not met: measured savings were 7.29%. The deeper beam planner and mixture-policy proposal did not meet their improvement criteria and were not promoted. The first real-photo pilot was negative. These results remain available alongside successful results.

## Scope and next step

The wafer result is synthetic numerical evidence. The photo studies use real PCB images, not semiconductor SEM images. Production adoption would require real wafer imagery and equipment logs, representative cross-lot evaluation, and an explicit comparison of escaped defects, false alarms, and inspection time.

## Demo and source

- [Inspection evidence](https://shinick-han.github.io/falsify-lab-hacknation7/inspection-evidence.html)
- [Real-photo evaluation](https://shinick-han.github.io/falsify-lab-hacknation7/inspection-images.html)
- [Recorded agent session](https://shinick-han.github.io/falsify-lab-hacknation7/inspection-live.html)
- [Source repository](https://github.com/Shinick-Han/falsify-lab-hacknation7)

## Submission videos

English narration and captions; 1920 × 1080, 30 fps, H.264/AAC. Local files are under `../output/pitch-delivery/`.

1. `participant-introduction.mp4` — 50.0 seconds.
2. `product-demo.mp4` — 59.6 seconds.
3. `technical-explanation.mp4` — 58.0 seconds.

The files are prepared for submission. This document does not assert that a HackOS submission has been made.

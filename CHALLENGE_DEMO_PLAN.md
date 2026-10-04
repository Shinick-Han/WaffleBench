# WaffleBench — two-minute challenge demo plan

**An agentic lab for smarter wafer inspection.**

This is a storyboard for the brief's two-minute demo item. It is a plan only: nothing has been rendered, and it has not been uploaded. The three existing HackOS clips (`participant-introduction.mp4` 58.0 s, `product-demo.mp4` 59.6 s, `technical-explanation.mp4` 58.0 s, under `../output/pitch-delivery/`) are the submission set; the team introduction includes the participant's real profile photo and solo-team closing. Whether a separate two-minute field exists has not been confirmed. Every figure below comes from [`SUBMISSION_EN.md`](SUBMISSION_EN.md) and its linked reports. Nothing is taken from the mockup.

Target length: about 120 s, English narration and captions, 1920 × 1080.

| Time | Screen | Narration (paraphrase) | Evidence |
|---|---|---|---|
| 0–12 s | Title card: WaffleBench, tagline | After optical screening, only a few flagged sites can get an expensive precision review. Which one should be next? | Brief, page 3: choose between competing tests under a budget |
| 12–30 s | [Recorded session](https://shinick-han.github.io/WaffleBench/inspection-live.html) replay: lot map, three candidate sites, 120 CU budget | A frozen planner scores the three candidates. Omnigent sends the choice to the experimenter agent and the result back to the analyst. | [`INSPECTION_LIVE_RESULTS.md`](INSPECTION_LIVE_RESULTS.md) |
| 30–48 s | [Recorded session](https://shinick-han.github.io/WaffleBench/inspection-live.html): `ir_71b91846930cb176` (17.0 CU) → update → `ir_6573c8a47872df63` (9.07 CU) → next site `w0:r-4:c13` | Each paid result enters an analysis update before the next choice. Two reviews, five delegations, 26.07 of 120 CU. Unreviewed sites stay unknown. | Session `ef847c8a…`, [`verification.json`](evidence/inspection-live/verification.json) |
| 48–68 s | Outer-loop table: v2 tie → route hypothesis → v3 result | Between studies, our team, working with Claude Code, revised the hypothesis; Omnigent did not. On 100 fresh synthetic lots at equal budget: 29.33 vs 27.38 confirmed defects, +7.12%, paired CI [1.46, 2.43]. | [`INSPECTION_V3_RESULTS.md`](INSPECTION_V3_RESULTS.md) |
| 68–82 s | Red "did not work" panel | The 30% cost-saving target failed at 7.29%. A model swap and a deeper planner both failed and were not promoted, so we kept the simpler method. | v4, v5 reports |
| 82–100 s | [Real-photo evaluation](https://shinick-han.github.io/WaffleBench/inspection-images.html): PCB1 → PCB2 → PCB3 | A negative first pilot led to a new pipeline. PCB2 recall rose from 51% to 72% and false alarms fell from 11% to 5%. On a fixed PCB3 replication, recall rose from 44% to 55%, false alarms fell from 8 to 3 of 101, and 45 defects were still missed. These are PCB photos, not SEM. | Image pilot v1–v3 reports |
| 100–112 s | Bottleneck card: low-contrast optical ceiling 29.9% | Defects never flagged by the optical stage cannot be recovered by a better planner. The next experiment targets candidate loss and domain shift on fresh data. | [`INSPECTION_QUALITY_DIAGNOSTICS.md`](INSPECTION_QUALITY_DIAGNOSTICS.md) |
| 112–120 s | Closing: repository, agents-and-policies document, verified diagnostic cycle: hypothesis, competing tests, measured result, revision, proposed fresh study | Synthetic wafer evidence, real PCB photos, no commercial-superiority claim. Every number links to a receipt. | [`AGENTS_AND_POLICIES.md`](AGENTS_AND_POLICIES.md) |

## Guardrails for production

- The diagnostic cycle is completed and verified. Label its data as posthoc synthetic diagnostics; do not present it as a new held-out performance campaign.
- Do not credit Omnigent with the frozen planner's numerical choices or earlier benchmark revisions. Its new diagnostic hypothesis and study selection are evidenced in DISCOVERY_CYCLE_RESULTS.md; the new prospective study is unexecuted.
- Keep the failed 30% target, the failed v4/v5 follow-ups, the PCB1 negative and the 45 PCB3 misses on screen.
- The circuit study (5.1 vs 1.4, paired +3.7, CI [3.2, 4.1]) can appear only as a one-line note about the reusable architecture.
- Prefer screens from the existing public pages and the captures in `product-demo.mp4` and `technical-explanation.mp4`. Check which screens those clips actually contain before editing; this plan does not verify that.

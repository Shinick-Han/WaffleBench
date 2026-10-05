# WaffleBench: submitted version and subsequent development

Prepared on 5 October 2026. This is our operating policy, not organizer approval or a legal determination.

## Publication update — 5 October 2026

The owner subsequently authorized publishing the implemented state and a separate
live development demo. This authorization applies to the isolated development
branch and new `wafflebench-lab.shinick.dev` hostname only; it is not organizer
permission to change the judged artifact. The submitted `main`, Pages resources,
original service and videos remain frozen. The local-only plan below records the
earlier boundary; [PUBLIC_INSPECTION_DEMO.md](PUBLIC_INSPECTION_DEMO.md) documents
the separately disclosed development publication.

## Evidence and uncertainty

The currently published [Hack-Nation terms](https://app.hack-nation.ai/agb), dated 25 September 2026, say in section 6 that the workspace deadline governs and late submissions are not assessed unless the deadline is extended there. Section 8 refers judging to the event's published rules. The observed terms do not expressly authorize changing the contents behind a previously submitted repository or running-demo URL after the deadline. Terms applicable at admission and any event-specific instructions may impose additional conditions.

HackOS currently shows submissions closed, submitted, eligible, revision 6, and three videos received. Its deadline is 4 October 2026 at 22:00 Asia/Seoul; the displayed submission grace period ended at 22:15. Project edits and replacement-video controls are disabled.

Continuing development in an isolated private/local workspace is our chosen way to preserve the judged artifact. A Git branch or tag is a technical separation, not permission to replace the judged artifact. Organizer authorization for any judged update has not been obtained.

## Preserved submission baseline

- Public repository and submitted demo: `https://github.com/Shinick-Han/WaffleBench` and `https://shinick-han.github.io/WaffleBench/inspection-evidence.html`.
- Observed public `main`: `70eaf5a99da400d6efeef22f8fdf920606e4816f`.
- Original local runtime source: `482debe26d439cf4484fcf0c075b8bbff0cef303` in `falsify-lab`.
- Both repositories have the **local-only** annotated tag `submission-baseline-captured-20261005`. It records today's capture and is not a retroactive claim about the deadline.
- `submission-baseline.json` records source-file hashes, asset hashes, original receipt copies, and seven observed deployed resources. Git bundles and ZIP archives preserve both source baselines. The submitted three video files and photo are copied separately.
- The original submission receipt contains a timestamp earlier than the final source commit and the later live-runtime receipt. It is preserved verbatim; it is not used as proof of the exact final submission time.

These source/runtime baselines are distinct repositories with different histories. Their SHA values must not be represented as interchangeable versions.

## Development boundary

New branch: `post-hackathon/roi-20261005`.

New worktree: `C:\Users\user\hacknation7th\.worktrees\wafflebench-post-hackathon-20261005`.

All implementation, local preview, new dependencies and future experiment output belong in this worktree or separate post-hackathon output directories. Use a separate environment and available local port. Do not edit or upgrade the original runtime checkout/environment, change the submitted GitHub `main`, publish a tag/release there, modify Pages deployment settings, replace submitted videos, or resubmit either form while judging is pending. Do not add links from the frozen demo to the new preview.

The existing service may continue executing its submitted code. Runtime logs, quota counters and job records are mutable operational state and are explicitly excluded from the source-file freeze. This exclusion does not authorize new algorithms, changed tool behavior, replacement benchmark evidence or a new default result on the judged service. Diagnose an outage separately; obtain organizer guidance before a repair that changes judged behavior or an endpoint. Keeping a service available does not establish permission for substantive improvements.

Every new screen uses a visible English notice: **Post-hackathon development — not part of the submitted version.** New reports include creation/execution dates and separate version identifiers. Keep the original study evidence and claims intact. New experiments need a new preregistration and fresh holdout data; consumed test sets remain consumed.

## Highest-ROI local work, in order

| Step | Scope | Acceptance condition |
|---|---|---|
| 1 | A single overview of wafer, recorded measurement choice, cost and result | Uses authentic stored evidence; distinguish unmeasured, measured-negative and confirmed-positive sites; local preview only |
| 2 | A concise hypothesis → diagnostic → result → next-study summary | Every numerical claim links to stored evidence; proposals and completed experiments are visibly different |
| 3 | Reliable offline replay of verified completed runs | Works independently of the owner PC/tunnel; recorded replay and new execution remain explicit |
| 4 | Budget/cost comparison | Only calculated, reproducible policy outputs; no interpolated claim presented as an experiment |
| 5 | A prospective Omnigent research loop | Separate preregistration, fresh paired lots, recorded costs and independently verified results |

Start steps 1 and 2 in local-only development. They must not alter the original pages or scientific JSON. After organizer guidance or completion of judging, assess publication separately. If publication proceeds, preserve the submitted version and clearly date the new release; use a separate demo URL so the original judging URL remains reproducible.

## Check before and after a work session

From `C:\Users\user\hacknation7th`:

```powershell
python output\post-hackathon\freeze_submission.py check
```

This checks source heads/branches/tags, tracked file bytes, submitted assets, preservation archives, remote `main`, and the seven recorded Pages resources. It writes `last-verification.json`. It detects drift; it does not prevent an accidental deployment, inspect all third-party state, or establish contest eligibility. Stop integration/publication on a failure and investigate. Never recapture to hide drift.

## Organizer clarification draft — not sent

Subject: WaffleBench — preserving the submitted version during judging

Hello Hack-Nation team,

WaffleBench is submitted on HackOS (revision 6) and through the Google Form. We are keeping the submitted repository, demo, videos and benchmark results unchanged. We would like to continue development in a separate local branch, clearly identified as post-hackathon work and excluded from judging.

Could you confirm whether any changes to the submitted repository or running demo are permitted after the deadline, including availability-only repairs? If selected as a finalist, should the pitch demonstrate only the submitted version, or may separately disclosed subsequent work be shown?

Until clarified, we will leave the submitted artifacts unchanged and keep improvements local.

Thank you,
Shinick Han — WaffleBench

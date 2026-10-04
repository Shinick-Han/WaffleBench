# Performance update — 4 October 2026

Policy calculation and chart interactions are faster, with the scientific results
preserved. A separate kernel-based search proposal did not consistently outperform
the existing adaptive policy, so the default remains IDW plus distance bonus.
The original 40-run primary study and its +3.7 result remain in [RESULTS.md](RESULTS.md).

## Exact policy calculation

The implementation reuses normalized coordinates, minimum distances and IDW
predictions within each decision. It preserves scalar summation order and tie
breaking. It introduces no cross-run evidence cache.

An independent harness replayed **all 482 recorded decisions**: 480 from the
primary study and 2 from the actual Omnigent session. Selection, scores and
rankings matched the old scalar implementation and recorded decisions exactly.
The timing gate ran three alternating repetitions per implementation on the same
Windows 11 / Python 3.12.10 machine.

| Time for 482 `build_decision` calls | Previous median | Updated median | Ratio |
|---|---:|---:|---:|
| Fresh interpreter | 4.856 s | 0.467 s | 10.40× |
| Warm interpreter | 6.079 s | 0.646 s | 9.42× |

These measurements cover policy calculation only. ngspice, ledger I/O, LLM calls
and complete campaign execution were excluded. Original campaign cost records
retain their original measured times. [Timing and equality receipt](evidence/performance/core-timing.json).

## UI work reduction

The UI renders hidden views when they are opened, invalidates them when a new
snapshot arrives, and caches benchmark geometry and normalized record rows.
Toggling a policy redraws its chart and pressed state without rebuilding the
table, conclusion, details or legend.

The coordinator measured both versions independently in local Chrome with the
same real snapshot and three repetitions. Median legend-handler time fell from
**2.65 ms to 0.50 ms**. Across 24 toggles, benchmark DOM mutations fell from
240 to 120; table, conclusion, details and legend rebuilds fell from 24 each to
zero. Initial hidden benchmark/record mutations fell from 10/4 to zero.

View contents and policy-hidden chart hashes matched exactly. Replacement
snapshots refreshed the displayed view and lazily refreshed other views; record
filtering, replay cancellation and byte-identical local/static exports passed.
The candidate passed every performance assertion. The baseline intentionally
failed the three new work-reduction assertions; those failures document the
previous redundant rendering, rather than scientific or functional failures.

First-load and replay timing did **not** demonstrate improvement: initial-ready
medians were 96/99 ms and replay clicks 2.1/2.4 ms respectively. No claim is made
for those paths. Browser timing is machine-specific.
[Comparison](evidence/performance/ui-comparison.json),
[baseline](evidence/performance/ui-baseline.json),
[candidate](evidence/performance/ui-candidate.json).

## Additional search-policy comparison

After seeing the original 20 fF results, we specified a separate exploratory
plan before running new simulations. It fixes loads of 20, 8 and 35 fF, twenty
new seeds (2001–2020), three policies, and 24 logical queries per run. The initial
three search points are shared across policies within each seed. Each load
recalibrates the same five-coefficient delay-model form on the same nine points
once, then freezes it throughout search. Search never queries held-out points.
The frozen numerical preflight separately measures one held-out point,
FS / 1.8 V / 85 °C, in three settings per load. Those nine physical attempts
are stored as restricted numerical evidence and never enter selection evidence.

The proposal uses an RBF error interpolator with fixed normalized-coordinate
length scales (0.6, 0.6, 0.25, 0.5), ridge 0.0001, and a dispersion bonus of 0.05.
The dispersion is a heuristic, not calibrated uncertainty. We did not retune
parameters after these results.

| Load | Existing adaptive | Random | Proposed kernel | Kernel minus adaptive |
|---|---:|---:|---:|---:|
| 20 fF | 5.05 | 1.40 | 4.35 | −0.70 |
| 8 fF | 4.15 | 1.10 | 4.40 | +0.25 |
| 35 fF | 5.50 | 1.60 | 4.80 | −0.70 |

Each value is the mean number of clear counterexamples (`error > 11%`) among
15 search points, across 20 complete runs. All **180 runs** completed. The
proposal lost in two loads and gained only slightly in one; **it was rejected
as the default**. The existing adaptive policy exceeded random in these checks,
but they are descriptive sensitivity results, not a replacement primary test.

An independent read-only audit verified the hash chains, raw netlists and ngspice
measurements, frozen models, budgets, evidence order, and **all 2,160 decisions**.
Numerical preflight and postflight passed. The supplementary experiment used
**452 physical attempts**, **4,347 logical queries including separate model
fits**, **3,958 cache hits**, and **186.671 seconds** of execution wall time,
within its separately declared 1,000-attempt / 1,500-second cap.

The initial exporter counted only standard calibration query events in its cost
summary. We preserved [that original export](evidence/performance/extension/original-export.json)
and recomputed the [complete cost report](evidence/performance/extension/report.json)
from the immutable ledger, including the extension's query and decision events.
No observations or decisions were changed. The [plan](evidence/performance/extension/plan.json)
binds the exact [executed source](evidence/performance/extension/executed_source),
including the earlier exporter; today's runner fixes its cost accounting.
The original plan/export also said "no held-out truth queried" too broadly:
that holds for search, while restricted numerical preflight measures the one
point described above. The corrected report clarifies this scope without
rewriting the original plan or ledger.
[Independent audit](evidence/performance/extension/independent-audit.json).

All three loads belong to the same generic Level-1 inverter family. Twenty seeds
vary initial conditions; they are not twenty independent silicon experiments.
The additional method was designed after seeing primary outcomes. Neither this
study nor the calculation timing establishes LLM or human-research acceleration.

## Reproduce checks

From a checkout after `uv sync --locked --python 3.12`:

```powershell
# Stored evidence only: no simulator and no LLM.
.\.venv\Scripts\python.exe scripts\profile_policy_performance.py --root evidence\primary --root evidence\live --repeats 3 --output core-timing.json
.\.venv\Scripts\python.exe scripts\verify_performance_extension.py --root evidence\performance\extension --output extension-audit.json

# A new supplementary execution requires ngspice 47 and a fresh root.
# This remains separate from the frozen primary study.
.\.venv\Scripts\python.exe scripts\run_performance_extension.py --root runs\extension-new
```

Browser checks use `web/tests/ui_performance_check.mjs`; set `PLAYWRIGHT_MODULE`
to an installed Playwright module and run it against a read-only local server
with `docs/data/snapshot.json`. The actual screenshots/responsive functional
checks use `web/tests/ui_check.mjs`. These are development tools, not app dependencies.

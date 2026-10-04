# Inspection v3 quality diagnostics (posthoc, synthetic)

Why does a 3,915-die lot get only about 35 paid SEM reviews, and where do the remaining errors come from? This is a **posthoc diagnostic** of the frozen v3 `cb400_route_full` candidate-only 360-CU test ledgers (100 lots, 20 per scenario). It does not replace the preregistered primary result in `INSPECTION_V3_RESULTS.md`, claims no new gain, and tunes nothing. Authored synthetic tabular evidence only; not SEM-image accuracy, factory throughput or wafer yield.

`scripts/diagnose_inspection_v3.py <v3-root>` (or `diagnose(root)`) refuses unless the freeze receipt verifies against the current sources, `campaign-started.json` and `held-out.json` carry the same receipt, and `audit.json` passed for that receipt with the unchanged audit script. It then checks every lot and ledger byte receipt, recomputes the frozen probabilities (their SHA-256 must match each ledger) and recounts against the ledger metrics. The privileged oracle is read only to label outcomes; it never selects, scores or replays an action. The only file written is `<root>/quality-diagnostics.json`. On the actual root (receipt `faadbe49…f3fb`), the other 9,909 files hashed identically before and after.

## Where the 272.6 latent DOI per lot go

| Per lot (mean of 100) | Count |
|---|---:|
| Latent DOI (posthoc truth) | 272.6 |
| Never optically admitted (no candidate-only policy can reach them) | 77.8 |
| Optical candidate DOI not selected within 360 CU | 162.5 |
| Selected candidate DOI | 32.3 |
| … confirmed (reported positive, latent DOI) | **29.33** |
| … sensor reported negative | 2.80 |
| … unresolved (all attempts failure/missing; unknown, never called good) | 0.16 |
| Selected non-DOI | 2.36 (2.35 reported negative, 0.01 false positive) |

The optical recall ceiling (candidate DOI / latent DOI) is 0.897 for stationary, novel cluster and nuisance heavy but **0.541 for process shift and 0.299 for low contrast**, where most DOI are never offered to review.

## Why about 35 reviews, not 3,915

The 3,915 dies are the optically scanned care sites; only selected candidates receive a paid SEM review. A loose optimistic bound uses the frozen minimum fees: first review 8 load + 1 stage + 8 dwell = 17 CU, each later review 1 + 8 = 9 CU, and every admission must still hold the full 4-CU retry reserve, exactly as the harness reserves it. Assuming zero movement, one wafer, and no failure or retry, 17 + 9(n−1) + 4 ≤ 360 gives **at most 38 reviews** (350 CU charged). This is not an achievable oracle planner and not factory throughput. Reviewing every die would need at least 35,259 CU per lot, and every optical candidate (783 per lot) about 7,073 CU.

Observed visits were **34.65 per lot** (91% of the bound; stop reason `none_affordable` in all 100). The 3.35-visit gap is spent on stage movement above minimum (19.5 CU), extra wafer loads (4.1 CU), charged retries (8.7 CU) and an unspendable remainder below the next reservation (7.9 CU). By scenario: low contrast 32.7 (26.2 CU movement, 14.8 CU extra loads), process shift 34.3, nuisance heavy 35.3, stationary 35.4, novel cluster 35.7.

## Three separate error sources

**Threshold classification** (frozen 0.5 threshold over all 78,323 candidates; the route policy does not use it): 3,284 FP and 4,682 FN, matching the published v3 figures. Precision/recall is 0.88/0.80 for stationary but 0.60/0.58 for low contrast.

**Ranking**: only 2.36 of 34.65 selected sites per lot were non-DOI. Candidates with p ≥ 0.99 (50.5 per lot) outnumber the visits, and 31.7 latent DOI per lot with p ≥ 0.99 remained unselected. Visit count, not ranking, binds in the three high-ceiling scenarios (0.25–0.85 non-DOI picks). Ranking errors concentrate in low contrast (6.7) and process shift (3.3).

**Sensor**: 2.80 selected DOI per lot reported negative, 0.01 false positive, 0.16 unresolved; 2.17 reviews per lot needed a retry (0.91 failure and 1.42 missing attempts).

## Calibration against latent truth (candidates)

Fixed bins [0,.2), [.2,.5), [.5,.8), [.8,.95), [.95,1] plus p ≥ 0.99, observed latent DOI rate:

| Scenario | [0,.2) | [.2,.5) | [.5,.8) | [.8,.95) | [.95,1] | p ≥ .99 (n) |
|---|---:|---:|---:|---:|---:|---:|
| stationary | .050 | .372 | .665 | .877 | .984 | .995 (1,306) |
| novel cluster | .083 | .403 | .671 | .879 | .986 | .997 (1,553) |
| nuisance heavy | .017 | .207 | .499 | .803 | .983 | .998 (1,364) |
| process shift | .063 | .291 | .537 | .770 | .936 | .988 (573) |
| low contrast | .043 | .234 | .365 | .583 | .920 | .964 (251) |

The bin means of p are about .04, .33, .65, .89 and .99. The model is close to calibrated in stationary and novel cluster, but overconfident above 0.2 in low contrast, process shift and nuisance heavy. That overconfidence is the shifted-scenario ranking error above. Per-scenario tables for the selected sites are in the JSON.

## Scope

- Policy timing: 27.0 ms is the **total** selection-call wall time per lot run (18.9–39.5 ms by scenario), not per decision. The derived per-decision mean is stored separately and labeled.
- Historical training labels are assumed available, and offline learning CU is excluded from the online budget. No Jev or free-text notes were used or invented.
- Focused tests: `tests/test_inspection_quality_diagnostics.py` (fixture count conservation, missing-candidate ceiling, failure ≠ physical negative, retry-reserve visit bound, receipt/audit refusal without campaign mutation).

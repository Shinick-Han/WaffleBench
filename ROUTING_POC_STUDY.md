# Routing PoC — experiment scope (fixtures, metrics, campaign, CLI)

**Post-hackathon development — not part of the submitted version.**
Created 2026-10-05. Implements the experiment-worker part of `ROUTING_POC_CONTRACT.md`
(the contract is unchanged). Python stdlib only. No results are reported in this file;
every number must come from a stored campaign output.

## Modules

- `routing_poc/fixtures.py` — `make_job(seed, regime)` returns `(job, archive)`.
  120 candidates on 3 synthetic wafers. Regimes:
  - `clustered` (primary): 3–5 clusters per wafer, wafer load 20 s, settle 1 s,
    stage 10 000 µm/s;
  - `diffuse` (stress/control): uniform positions, load 2 s, settle 0.2 s, 200 000 µm/s.
  Recipes declare capture/inference bounds (3.0/0.5 s, 4.5/0.8 s); archived times never
  exceed them. Reference DOI is sampled from the public `prior_p` (calibrated priors).
  Paid reports are imperfect (sensitivity 0.9, false-positive rate 0.05); attempts fail
  (4 %) or go missing (3 %) and a retry attempt exists only after failed/missing.
  The job holds only whitelisted public fields; the job id is an opaque digest, so the
  latent seed is not readable from it. Truth/reports live only in the archive. The
  generator is policy-independent and frozen with the protocol.
- `routing_poc/metrics.py` — `evaluate(job, archive, run)` (post-loop only). Separates
  detector reports, confirmed reference DOI, false confirmations, total reference DOI,
  escapes (with breakdown: unselected / detector negative / unknown final) and
  candidate-space recall. Unknown finals are never negatives. Without a complete reference
  covering every candidate, total/escapes/recall are `null`; a positive report on a site
  with unknown reference makes confirmations `null` rather than promoting the report to
  truth. `log_replay` runs report counterfactual support as unavailable.
  `wafer_wide_recall` is always `null`; `commercial_validated` is always `false`.
- `routing_poc/campaign.py` — `run_campaign(out, split='development', protocol=None, *,
  confirm_synthetic_test=False)` with the frozen `PROTOCOL`: budgets 120/240 s, epsilon
  0.1, policies `risk_only`/`risk_per_second`/`beam_route`, development seeds
  12000..12007, reserved test seeds 22000..22039 (40 lots), both regimes, paired
  lots (every policy replays the same archive), policy RNG `sha256(job_id|policy)`,
  paired lot bootstrap (2000 replicates, seed 2026100511, percentile 95 %).
- `routing_poc/cli.py` — `demo --out`, `benchmark --out --confirm-synthetic-test`,
  `replay --job --archive --out --budget-s --policy [--audit-epsilon]`.

## Campaign artifacts (`out/` must not exist)

`status.json` (running → complete/failed), `protocol.json` (protocol + SHA-256),
`source_freeze.json` (SHA-256 of every `routing_poc` module and the contract, written
before the first job), `environment.json`, `jobs/<job_id>.json` (raw job, archive and
fixture seed — evaluator records, never policy inputs), `runs/<job>__<policy>__<budget>s.json`
(raw run + metrics), `source_verification.json` (re-hash after execution) and
`summary.json`. On any failure, `status.json` and `failure.json` (phase, traceback) are
written and all partial artifacts are kept; a source-digest drift fails the campaign
before a summary is written.

## Separation rules

- The reserved synthetic test needs `split='test'` plus `confirm_synthetic_test=True`
  (`--confirm-synthetic-test`), runs only the exact frozen `PROTOCOL`, and requires every
  source file to be hashed before execution. It is coordinator-operated after review and
  freeze; it was **not** run by this worker.
- A development protocol may be reduced for checks but may not touch or change the
  reserved test seeds, add regimes/policies or set `commercial_validated`.
- Replay resource seconds are modeled/recorded action charges, not physical elapsed
  time; decision and observer wall times are reported separately.
- A synthetic gain is not an equipment, throughput, ROI or superiority claim.

## Checks

`python -m unittest tests.test_routing_poc_campaign` — fixture provenance and whitelist,
timestamps/bounds, imperfect reports, complete-reference metrics, unknown ≠ negative,
partial-log unidentifiability, incomplete reference, lot bootstrap, artifact pairing,
policy-RNG independence from the latent seed, existing-output refusal,
development/test separation, source drift and mid-run failure preservation, CLI
confirmation and log-replay labeling. Campaign mechanics use labeled test doubles of the
adapter/policy modules; real-module integration tests run only when those modules are
importable (otherwise reported as skipped).

# Actual Omnigent inspection demonstration

A real Omnigent 0.16.0 supervisor and Claude Opus 5.5 medium specialists completed five ordered delegations: analyst → experimenter → analyst → experimenter → analyst. Two paid inspections returned distinct result IDs; both results reached the next analysis update and the ledger finalized. The deterministic frozen planner chooses sites; Omnigent coordinates role-limited tools and carries observations rather than receiving credit for the planner's numerical algorithm.

The separate authored numerical synthetic lot has 3,915 care sites, a 120 CU cap and a four-review maximum. The bounded demonstration executed two reviews and charged 26.066613859 CU. It is not part of the equal-budget primary experiment and provides no independent classifier accuracy, factory throughput or equipment performance estimate.

## Evidence and verification

The sanitized actual SDK/core record is [session-proof.json](evidence/inspection-live/session-proof.json); its byte hash and current verifier source hash are in [verification.json](evidence/inspection-live/verification.json). Every delegation was matched to its specialist's tool call, output, selected site/decision sequence and result ID. The core decision, observation, update and closure chains matched; the proof has no failure reasons. Root independently ran all 31 focused runner tests.

The initial verifier rejected three later delegations because Omnigent reused the same two child conversations. The contract requires five delegations, not five separate conversations. Recovery fetched existing SDK records and sent no new prompt, created no new session and performed no extra inspection. The original failed attempt remains in the proof's attempt history. Same-agent reuse now requires an exact dispatched user-message match and ordered turn boundaries; conflicting identity, missing/extra turns, foreign/nested children, duplicate genuine calls and mismatched result chains still fail.

Omnigent also persists SDK mirror records alongside bridge records. Normalization is limited to identical records or the documented 0.16 stale-FIFO pair within the same SDK response and delegated turn; collapsed records and raw counts remain visible. This depends on the installed SDK's persistence format and refuses ambiguous mappings.

Raw unsanitized events, runtime host identifiers and credentials remain local. The ordinary actor string alone does not prove orchestration; the actual SDK function-call chain is required. Software correctness tests and this two-review live demonstration do not establish benefit over a deterministic planner without orchestration.

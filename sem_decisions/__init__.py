"""SEM review decision tools (post-hackathon development, excluded from judging).

Modules:

- ``manifest``: validator/adapter for the common Carinthia-S manifest (schema_version 1)
  and the observed-vs-hidden item views.
- ``policy``: ``SemBudgetAuditPolicy``, a deterministic budget-aware audit policy with the
  frozen ``select(state, max_cost, affordable) -> Choice`` contract.
- ``learning_backends``: optional modAL/apricot entry points and a self-named pure NumPy
  deterministic fallback.
- ``conformal``: split-conformal prediction sets / abstention and an optional MAPIE wrapper.
- ``shift``: KS + permutation feature-shift diagnostic with false-alert calibration.
- ``acquisition``: measured-quality repeat-image protocol (no hardware is connected).
- ``offline_tools``: NIST detection_limits offline adapter and ARTIMAGEN launch config.
- ``jev_payload``: structured prospective action payload validator (no API calls).
- ``cli``: ``python -m sem_decisions.cli self-check|demo|evaluate-fixture``.

Nothing here reads credentials, calls a network API, or produces primary scientific
evidence. Fixture outputs are conspicuously labeled synthetic.
"""

SYNTHETIC_LABEL = "SYNTHETIC FIXTURE - NOT SCIENTIFIC EVIDENCE"
DEVELOPMENT_SEED = 2026100502

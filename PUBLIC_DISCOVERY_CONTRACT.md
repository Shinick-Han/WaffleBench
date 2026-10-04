# WaffleBench public diagnostic runs

The user has authorized judges to start fresh Claude CLI-backed Omnigent research loops from the public web demo. This scope extends the recorded demo; it does not alter the frozen research protocol, manifest, benchmarks, or existing evidence.

Each new run has its own ledger, input copy, isolated agent bundle, SDK session, and verification receipt. The analyst proposes hypotheses and chooses among at least two competing diagnostic studies. The experimenter executes two preauthorized read-only computations. The analyst interprets their results and proposes a prospective experiment. The supervisor finalizes the record. Results appear as verified only after the ledger and actual SDK delegation/tool records pass the existing verifier.

The diagnostic catalog is `miss_partition`, `capacity_bound`, and `calibration_shift`. Inputs are existing authored synthetic inspection evidence. There are no new physical measurements, training runs, held-out improvements, equipment comparisons, or discovery-speed claims. The final prospective experiment is a proposal, not an executed experiment.

The browser cannot supply prompts, models, commands, file paths, agent policies, credentials, or replacement data. The API exposes only fixed run creation, safe progress, and verified public result JSON. It binds to localhost; Cloudflare Tunnel publishes that bounded API, never the Omnigent server. The browser contains no provider credentials. One run executes at a time. Run reservations consume a persistent lifetime quota, including failures; cooldown and idempotency limit duplicate execution. Timeouts fail closed and are not retried automatically.

The submission website and recorded evidence remain available on GitHub Pages independently of this local execution service. The fresh-run service requires the owner's PC, Claude login, API process, and tunnel to remain available. A temporary tunnel provides no availability guarantee. If offline or quota exhausted, the interface says so and links to the verified recorded cycle.

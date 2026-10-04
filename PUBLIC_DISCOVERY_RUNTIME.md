# Public discovery runtime

`scripts/public_discovery_runtime.py` runs one fresh, isolated Omnigent diagnostic cycle for a public judge job. It is a new public mode. The frozen protocol, existing evidence and recorded replay stay unchanged. Omnigent's private data uses `.public-discovery-runtime/o/<full UUID without hyphens>` so artifact filenames fit Windows path limits; longer checkout paths use `%LOCALAPPDATA%/WaffleBench/o/<full UUID>` instead. Each job retains a distinct namespace. Ledgers, bundles and raw proof remain under the private job directory. Public JSON identifies its own logical `public-runs/<UUID>/cycle/events.jsonl` hash; this is a provenance label, not a file-serving route.

## Interface

The public API (private loopback port 8782, behind the coordinator-owned tunnel) launches the runtime with the main app venv Python, using a 1100 s timeout:

```
python scripts/public_discovery_runtime.py --job-root <private jobs dir>/<uuid> --output <private jobs dir>/<uuid>/public-result.json
```

- The job root name must be a canonical UUID. The output must be `<job-root>/public-result.json` and must not exist yet. `<job-root>/cycle` must not exist yet.
- The runtime prints one JSON line, `{"status": "completed"|"failed", "error": <code>|null}`, and exits 0 only on success. Exit 1 means a run failure; exit 2 means invalid arguments.
- Error codes are generic: `busy`, `port_occupied`, `runtime_unavailable`, `prepare_failed`, `bundle_invalid`, `server_unavailable`, `host_unavailable`, `setup_timeout`, `driver_timeout`, `driver_failed`, `verification_failed`, `export_invalid` and `internal_error`.
- The API can read partial progress from `<job-root>/cycle/events.jsonl` after the ledger is prepared.

## Sequence

1. Take a non-blocking global lock (`.discovery-live-runtime/public-runtime.lock`), or stop with `busy`. Refuse if 127.0.0.1:6775 is occupied. Ports 6767, 6771 and 6773 are never touched.
2. Prepare a new ledger at `<job-root>/cycle` from the fixed input `evidence/inspection-improvements-v3/quality-diagnostics.json`.
3. Copy `discovery_agent/` to `<job-root>/private/bundle/discovery_agent`. The three `config.yaml` files must hash-match the originals. Per-job MCP `discovery.yaml` files point at the main app venv Python and this job's cycle. The original bundle is never written to.
4. Validate the copied bundle under the Omni Python (`%APPDATA%\uv\tools\omnigent\Scripts\python.exe`). The check requires exactly three valid agents, each set to `claude-opus-5-5`, `medium` and `claude-sdk`.
5. Start our own `omni server --host 127.0.0.1 --port 6775 --agent <job bundle>` and then `omni host --server http://127.0.0.1:6775 --no-open --non-interactive`. Both run as hidden background processes (`CREATE_NO_WINDOW`) with stdin closed. The runtime waits for health (45 s), then for exactly one online local host (60 s), and checks that our own handles are still alive.
6. Run the existing verified driver once under the Omni Python: `run_discovery_cycle.py --root <job>/cycle --server http://127.0.0.1:6775 --timeout <=900 --proof-dir <job>/private/raw`. The internal overall budget is 1060 s, leaving a 40 s cleanup reserve inside the API's 1100 s limit. The runtime never retries and never sends prompt input. On a timeout it kills the driver's process tree, and the job fails closed.
7. Re-verify independently. The runtime checks the driver exit code, `verification.json` (passed, no failure reasons, every check passed), and a fresh in-process `verify()` that must match the receipt's check names and hashes. It also checks `session-proof.json` (completed/passed, same session) and `export.json` (exact SDK block, verification checks, 2 plans, 2 results, 2 updates). The export must contain no local paths.
8. Only then write `public-result.json` atomically, byte-identical to `cycle/export.json`.

Cleanup in `finally` stops only the process handles this job started, using `taskkill /T` on our own live PIDs. It records whether port 6775 was released.

## Environment

The runtime applies the same flags as `scripts/launch_discovery.ps1`. It sets `OMNIGENT_DATA_DIR` and `OMNIGENT_CONFIG_HOME` to `<job-root>/private/omnigent`, and sets the no-update, no-open, telemetry-off and `DO_NOT_TRACK` flags. Claude.ai connectors are disabled (`ENABLE_CLAUDEAI_MCP_SERVERS=false`, passed through to runners). `%APPDATA%\npm` is removed from `PATH`. `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `OPENAI_API_KEY` are removed, so the runtime uses the existing Claude CLI login.

## Evidence and privacy

- Sanitized proof stays per job: `cycle/events.jsonl`, `cycle/omnigent/session-proof.json`, `cycle/omnigent/sdk-records.json`, `cycle/verification.json` and `cycle/export.json`.
- Raw logs stay in `<job-root>/private/logs`, each bounded to 2 MiB (tail kept). The runtime passes `--proof-dir <job-root>/private/raw` to the driver, so raw SDK stream and capture files are written per job. The shared legacy `.discovery-live-runtime/proof` directory is never written, copied or moved. The driver's `--proof-dir` default is unchanged. Only this runtime sets it; the API cannot supply any path. `<job-root>/private/runtime-summary.json` records steps, exit codes and cleanup, with no paths or secrets.
- Only `public-result.json` is meant for the API to serve.

## Operational limits

- **Temporary PC uptime dependence.** The public button works only while this Windows PC is on, logged in with a valid Claude CLI login, and running the API and tunnel. Sleep, reboot, logout or an expired login makes runs fail closed (`server_unavailable`, `driver_failed` or `verification_failed`). This is a temporary hackathon deployment, not a hosted service.
- One job at a time (lock plus port check). Each run is a fixed two-experiment diagnostic of existing synthetic evidence. It is not a new held-out gain.
- The driver calls `ROOT/.venv/Scripts/python.exe` for its verify step and runs sessions with workspace `ROOT`. Deploy the runtime in the main app checkout, where that venv exists.
- `export.json` keeps the driver's fixed `sources` labels, which name the published-cycle path. They are labels, not job paths.

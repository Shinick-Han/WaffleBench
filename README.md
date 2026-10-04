# falsify-lab

Find the PVT conditions (synthetic process condition × VDD × temperature) where a small,
frozen CMOS inverter delay model fails against ngspice, and compare rules that choose the
next simulation under a fixed budget. An Omnigent supervisor coordinates two specialist
agents in a live workbench. Every number comes from a deterministic local tool that
stores and re-reads ngspice results. The LLMs never supply numeric evidence.

The research design is fixed in [`RESEARCH_PROTOCOL.md`](RESEARCH_PROTOCOL.md) (Korean) and
[`research-manifest.json`](research-manifest.json). The integration contract is
[`DATA_CONTRACT.md`](DATA_CONTRACT.md) and the core API is [`CORE_API.md`](CORE_API.md).

**Result.** Forty actual runs completed. Adaptive selection found an average of 5.1
clear counterexamples versus random's 1.4: paired gain **+3.7**, prespecified 95%
interval **[3.2, 4.1]**. The frozen primary criterion passed. See [`RESULTS.md`](RESULTS.md)
for the independent audit, secondary comparisons, held-out checks, cost and limits.
**Performance update.** Exact policy decisions now take about one tenth of the prior
ranking time. The UI avoids rebuilding hidden views and invariant chart details.
A separate 180-run load-sensitivity study rejected a proposed kernel policy as the
default. Measurements, raw evidence and reproduction steps are in
[`PERFORMANCE.md`](PERFORMANCE.md); the original primary result is unchanged.
**Usability update.** Policy comparison shows the conclusion first on narrow screens,
with readable summaries and expandable raw evidence. Records have labelled filters,
stable error sorting and filter reset. Replay progress, keyboard focus restoration
and larger touch targets improve navigation. Research values and snapshot bytes
remain unchanged. See `evidence/ui-polish/validation.json` for the acceptance checks.
The `mockup/` folder is the original UI mockup. Its numbers are invented placeholders and
are not results.

- Public repository: https://github.com/Shinick-Han/falsify-lab-hacknation7
- Recorded-run demo: https://shinick-han.github.io/falsify-lab-hacknation7/

## Research model

- **Circuit.** CMOS inverter, 20 fF load, 50 ps input edges, NMOS 1 µm/0.35 µm, PMOS
  2.5 µm/0.35 µm. ngspice measures tphl and tplh at the 50% crossings, and tpd is their mean.
  The devices use **generic SPICE Level-1 textbook models, not a foundry PDK**. The
  conditions TT/FF/SS/FS/SF are synthetic: Vt shifts by ±50 mV and KP scales by ±10%.
  They are not foundry corners.
- **Grid.** 5 conditions × VDD {1.2, 1.5, 1.8, 2.2, 2.5, 2.9, 3.3} V × T {−40, 0, 27, 85, 125} °C
  gives 175 points. The manifest fixes the 9 calibration points, 1 previously observed
  point that is excluded, 40 held-out points and 125 search candidates.
- **Frozen delay model.** `log(tpd/1ps) = b0 + bv·g(VDD) + bt·((T−27)/100) + bn·n + bp·p`
  with `g(V) = log[(V/(V−0.55)^1.3) / (2.5/(2.5−0.55)^1.3)]`. `n` and `p` are the NMOS and
  PMOS Vt shifts divided by 50 mV. The 5 coefficients come from unregularized log-space
  least squares on the 9 calibration points (rank-5 checked). The model is then frozen and
  never refit. Its `model_hash` binds the coefficients and the protocol and manifest hashes.
- **Counterexample.** `e = |predicted − simulated| / simulated`. The primary metric is a
  **clear** counterexample, defined as `e > 11%` (strict). `e > 10%` is reported as a
  secondary count. Both thresholds are design choices for this study, not industry standards.
- **Policies** (the same frozen model for all four). Adaptive picks the highest IDW-predicted
  error plus a distance bonus. Random follows a fixed manifest order. Space-filling
  maximizes the minimum distance and never looks at results. No-bonus uses IDW alone.
- **Budget per run.** 24 logical queries: 9 calibration, 3 shared initial points per seed,
  and 12 policy choices. Seeds 1001–1010, so the primary benchmark is 4 policies × 10 seeds
  = 40 runs. Failed queries consume budget and are never replaced.

The scientific scope is this computational model only. Results do not generalize to real
processes, FinFETs, TCAD or silicon without independent device models and measurements.

## Two separate evidence sources

1. **Live workbench (Omnigent).** It uses seed 1001 only. A prepared demo root
   (`demo-prepare`) contains the preflight, the 9 calibration points and the 3 shared
   initial points. This preparation is **not** an adaptive update. The supervisor then runs
   2 to 4 genuine discovery loops: the analyst plans, the experimenter simulates, and the
   analyst interprets and plans again. Each loop calls the real local MCP tools and ngspice.
   The cap is 9 + 3 + 4 = 16 logical queries. Every state-changing tool call and every
   `analysis_update` is written to the hash-chained ledger with an `Omnigent <role>` actor.
2. **Primary benchmark.** This is a separate campaign root. The coordinator operates it
   after M5 authorization. A deterministic runner executes the same fixed decision rules
   with no LLM. Statistics come only from this campaign.

The quantitative comparison evaluates **experiment-selection policies**. The live
workbench shows that Omnigent can pass evidence between specialist agents and run the
loop. This project makes **no claim that an LLM speeds up the research** and no claim of
acceleration over a human researcher.

## Requirements

| Component | Version | Notes |
|---|---|---|
| Python | 3.12 | project `.venv` created by `uv sync --locked` (`mcp==1.30.0`, `numpy==2.3.3`; see `uv.lock`) |
| uv | any recent release (validated with 0.12.20) | assumed to be installed already |
| ngspice | 47, Win64 console build | downloaded separately, never bundled |
| Omnigent | 0.16.0 | only for the live workbench; installed as a separate uv tool |
| Claude Code login | existing user subscription | only for the live workbench; no API key is needed or used |

### ngspice 47 (not bundled)

Official archive: `https://downloads.sourceforge.net/project/ngspice/ng-spice-rework/47/ngspice-47_64.7z`
(13,814,879 bytes, SHA256 `59225971BD68CDD1199443649AA4615A9E6D684933F205AB49006A3942518F5A`).

```powershell
New-Item -ItemType Directory -Force tools\downloads, tools\ngspice-47 | Out-Null
# download ngspice-47_64.7z from the URL above into tools\downloads, then:
(Get-FileHash tools\downloads\ngspice-47_64.7z -Algorithm SHA256).Hash
# must equal 59225971BD68CDD1199443649AA4615A9E6D684933F205AB49006A3942518F5A
tar -xf tools\downloads\ngspice-47_64.7z -C tools\ngspice-47     # Windows bsdtar reads 7z
$env:FALSIFY_NGSPICE = "$PWD\tools\ngspice-47\Spice64\bin\ngspice_con.exe"
```

The simulator runs from `FALSIFY_NGSPICE` and is never copied. The binary's SHA256 and
version are part of every result's cache fingerprint. Both `tools/` folders are git-ignored.

### Omnigent 0.16.0 (live workbench only)

```powershell
uv tool install --python 3.12 omnigent==0.16.0
```

This installs the tool interpreter at `%APPDATA%\uv\tools\omnigent\Scripts\python.exe` and the
`omni`/`omnigent` CLIs in `~\.local\bin`. `launch.ps1` uses these paths. The agents run on the
`claude-sdk` harness with `claude-opus-5-5` at reasoning effort `medium`. They authenticate
with the user's own Claude Code login on the local machine. `launch.ps1` removes
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `OPENAI_API_KEY` from its own process.

## Deterministic commands (no LLM)

Run these from the repository root with the project venv, not a global Python:

```powershell
uv sync --locked --python 3.12
$env:FALSIFY_NGSPICE = "<NGSPICE_DIR>\Spice64\bin\ngspice_con.exe"

# Short reproduction check: calibration + adaptive and random, seed 1001. Use a fresh root.
.\.venv\Scripts\python.exe -m falsify_lab.cli reproduce --root runs\reproduction\<unique-name>

# Prepare a live demo root: preflight, calibration, seed 1001 initial 3 (no updates yet).
.\.venv\Scripts\python.exe -m falsify_lab.cli demo-prepare --root runs\demo\<unique-name>

# Re-export stored evidence to <root>\snapshot.json without running experiments.
.\.venv\Scripts\python.exe -m falsify_lab.cli export --root <root>

# Reproduce the full frozen comparison on a fresh root (40 runs + full reference).
.\.venv\Scripts\python.exe -m falsify_lab.cli benchmark --root runs\primary\<unique-name> --kind primary --confirm-primary
.\.venv\Scripts\python.exe scripts\verify_campaign.py --root runs\primary\<unique-name> --output runs\primary\<unique-name>\independent-audit.json
```

Each command prints one JSON summary (`ok`, results, `usage`, `snapshot` path) and exits
nonzero on failure. A root is bound to one campaign kind and is never silently resumed or
extended. Use a new root for each attempt instead of deleting old ones.
The full comparison is the M5 research command. Reproducing it creates a separate
campaign; it does not extend or modify the published results.

The isolated M6 reproduction receipt is in
[`evidence/reproduction/validation.json`](evidence/reproduction/validation.json). It records
the commands, exact versions, lockfile, protocol, manifest and model hashes, usage and
outcome counts. It covers one seed pair as a reproduction check only. It is not the
primary comparison.

## Live workbench (Omnigent, user-local)

```powershell
.\launch.ps1 -Setup -RunsDir runs\demo\<unique-name>    # uv sync + render MCP sidecars for that root
.\launch.ps1 -Prepare -RunsDir runs\demo\<unique-name>  # demo-prepare; the 60-minute wall cap starts here
.\launch.ps1 -Validate                                  # validate the agent bundle with Omnigent 0.16.0
.\launch.ps1 -Server                                    # hidden loopback server + host, UI at http://127.0.0.1:6767
.\launch.ps1 -Live -RunsDir runs\demo\<unique-name>     # one bounded supervisor session (scripts\run_live.py)
.\launch.ps1 -Stop
```

- Workflow: analyst plan → experimenter execute → analyst plan → experimenter execute →
  analyst finalize. Caps: supervisor 12 tool calls, specialists 6 each, at most 4 search
  queries.
- Tools take no root, run id or path argument. A sidecar role (`FALSIFY_MCP_ROLE`) gates them
  on the server side. The experimenter can only execute the pending recorded decision.
  Held-out and other points are denied without spending budget.
- `scripts/run_live.py` writes a sanitized `<demo-root>/omnigent/session-proof.json`. Raw
  provider streams stay in `.omnigent-runtime/live-proof/`, which is git-ignored and never
  published. The runner follows the whole asynchronous workflow, including specialist
  and supervisor wake turns, and waits for an idle root, no busy specialists, and a
  closed live ledger. `--recover-session ID` collects an existing session without
  creating a session, sending a prompt or running another experiment. The published
  session proof records the initial premature collection and successful recovery.
- On Windows, the Omnigent home's default "Claude Code" entry (`claude-native`) is
  unavailable. Use the custom `falsify_supervisor` agent on the `claude-sdk` host that
  `-Server` starts.
- `-Smoke` (`scripts/offline_smoke.py`) and the legacy `falsify_lab/experiment.py` voltage-only
  surrogate are the original connection test only. They are not part of the research path.

## Results UI

`web/` is one static page that serves two modes:

- **Public static mode** (GitHub Pages, `docs/` in a release). It is a **recorded-run replay**
  of a sanitized snapshot. The page loads `./data/snapshot.json` and runs nothing remotely.
  A release combines the recorded live workbench and the separate primary benchmark, and
  keeps their costs separate.
- **Local controller** (`scripts/serve_app.py`). It binds `127.0.0.1` only and serves
  `GET /api/snapshot`. It can start exactly two fixed jobs, `demo-prepare` and `reproduce`,
  one at a time, on fixed roots. It never accepts command text or paths. `--read-only`
  disables job start.

```powershell
.\.venv\Scripts\python.exe scripts\serve_app.py --read-only --snapshot <root>\snapshot.json
```

## Provenance, failures and cost accounting

- `result_id`/`sim_id` are bound to the measurement schema, numerical setting, full netlist
  SHA256, ngspice version and binary SHA256. Cache reuse happens only within one campaign
  and only for an identical fingerprint, and it is re-checked against the ledger record hash.
- The ledger (`<root>/ledger/events.jsonl`) is append-only and hash-chained. Every physical
  attempt is admitted under a cross-process lock before ngspice starts. Failures, timeouts
  and crashes stay counted.
- **Logical vs physical.** A run spends one logical query per selection even when the result
  comes from the cache. Physical attempts count real ngspice executions. Both are reported,
  together with cache hits, simulation seconds, policy seconds and wall time.
- **Global caps per campaign root.** 1200 attempts and 60 minutes, covering preflight,
  calibration, runs, posthoc evaluation and postflight. ngspice has a 60 s limit per run.
  The published live and primary campaigns together used 223 attempts across
  468.714 seconds from the first live admission to the last primary event, also
  below the total research cap. Recovery only reads this already completed research.
- **Setup and posthoc cost.** Calibration and the shared initial points count inside every
  run's 24-query budget. The posthoc full-grid reference (175 points, including the 40
  held-out points) is reported separately from the research budget and is not presented as
  savings.

## Security boundary (be accurate)

Omnigent on native Windows runs in **degraded mode**. A Job Object contains the process
tree, but **nothing isolates the filesystem or the network**. bwrap/seatbelt and the
egress proxy exist only on Linux/macOS. What actually constrains this setup:

- No agent declares `os_env`, so no agent gets shell or file tools. `skills: none` keeps host
  Claude Code skills and settings out.
- Each specialist sees only its allowlisted MCP tools. The MCP server runs as the user and
  could technically reach anything the user can. Its code only writes under the configured
  demo root and executes ngspice.
- The Omnigent server and the local controller bind to loopback only. No permission-bypass
  flags are used.
- Prompt rules ("don't invent numbers") are not an OS sandbox. Evidence integrity comes from
  the tools storing and re-reading results and from the hash-chained ledger, recorded
  separately from any prompt.

For real isolation, run under WSL2/Linux, where Omnigent's bwrap sandbox applies.

## Layout

```
agent/                    supervisor + analyst/experimenter specs (claude-sdk, opus-5-5 medium)
falsify_lab/              protocol, simulator, model, storage, policies, benchmark, reporting, cli, mcp_server
scripts/                  run_live.py, serve_app.py, verify_campaign.py, package_release.py, validate_agent.py
web/                      results UI (static + local controller)
tests/                    unittest suites
evidence/                 live and primary scientific records; reproduction receipt
mockup/                   original mockup (invented numbers, not results)
```

## License

The project's own code is MIT ([`LICENSE`](LICENSE)). Third-party components are listed in
[`THIRD_PARTY.md`](THIRD_PARTY.md). No third-party code or binaries are vendored here.

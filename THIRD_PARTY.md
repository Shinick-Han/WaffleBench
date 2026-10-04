# Third-party components

The project's own source code is under the MIT License ([`LICENSE`](LICENSE)). This
repository does not vendor third-party source code or binaries. Everything listed below
is installed or downloaded separately by the user.

License names come from the metadata of the installed distributions on the validation
machine (2026-10-04): the `License-Expression` field, the `License` field or the trove
classifiers. For ngspice they come from the `docs/COPYING` file in the official archive.
The authoritative terms are the license files shipped with each component.

## Simulator and agent runtime (not bundled)

| Component | Version | License (from local metadata) | How it is obtained |
|---|---|---|---|
| ngspice | 47 (Win64 console, `ngspice_con.exe`) | Modified BSD for most of the source. Some parts use other licenses (LGPLv2, MPL-2.0, public domain and others), listed in `Spice64/docs/COPYING` | Official SourceForge archive `ngspice-47_64.7z` (13,814,879 bytes, SHA256 `59225971BD68CDD1199443649AA4615A9E6D684933F205AB49006A3942518F5A`). The user extracts it and points `FALSIFY_NGSPICE` at it |
| Omnigent | 0.16.0 | Apache-2.0 (classifier; `LICENSE` and `NOTICE` in the dist-info) | `uv tool install --python 3.12 omnigent==0.16.0`, separate from the project venv |
| Claude Code / Claude models | user's existing installation | Anthropic terms of service | Live workbench only. It uses the user's own login, and no credential is stored in this repository |

## Python dependencies (project `.venv`, pinned in `uv.lock`)

Direct dependencies: `mcp==1.30.0`, `numpy==2.3.3`. The full locked set installed by
`uv sync --locked --python 3.12` on Windows is:

| Package | Version | License (from local metadata) |
|---|---|---|
| annotated-types | 0.8.0 | MIT |
| anyio | 4.15.1 | MIT |
| attrs | 26.1.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.0 | BSD-3-Clause |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| httpx-sse | 0.4.3 | MIT |
| idna | 3.20 | BSD-3-Clause |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| mcp | 1.30.0 | MIT |
| numpy | 2.3.3 | BSD (classifier; full NumPy license text in metadata) |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.5 | MIT |
| pydantic-core | 2.46.5 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| PyJWT | 2.15.1 | MIT |
| python-dotenv | 1.2.4 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pywin32 | 312 | PSF (Windows only) |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.6.3 | MIT |
| sse-starlette | 3.5.0 | BSD-3-Clause |
| starlette | 1.7.0 | BSD-3-Clause |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |

## UI design reference

The static workbench adapts the flat table spacing, row dividers and hover treatment
from [Origin UI's Table on 21st.dev](https://21st.dev/@originui/components/table/data-table-with-filters-made-with-tan-stack-table)
(MIT). The component source was consulted during the Lovable refinement; its React
implementation and dependencies are not bundled. Existing native HTML tables and
filters retain their scientific data and behavior.

## Device models

The inverter netlist uses generic SPICE Level-1 textbook MOSFET parameters written in this
project. They are not a foundry PDK, and no foundry model files are included.

## Tooling assumed to be present

`uv` (validated with 0.12.20) and Windows `tar` (bsdtar) are used for setup and are not
distributed here.

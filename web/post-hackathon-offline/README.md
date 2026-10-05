# WaffleBench offline replay pack

**Post-hackathon offline replay — not part of the submitted version.**

This folder is local post-hackathon development, excluded from judging. The submitted version
(public repository, GitHub Pages site, runtime, data and assets) is unchanged.

## Open it

Double-click `index.html`. It works from `file://` with no server, no installation and no network.
Use the "Offline pack" links at the top of each page to move between the three pages.

| File | Page | Embedded frozen evidence |
|---|---|---|
| `index.html` | Overview | `web/data/inspection-v3.json` |
| `inspection-live.html` | Omnigent measurement replay | `web/data/inspection-live.json` |
| `discovery-cycle.html` | Completed research cycle | `web/data/discovery-cycle.json` |

## Scope

- **Recorded replay, not fresh execution.** Each page re-reads an embedded, byte-identical copy of
  one original frozen evidence file. Nothing is executed: no agent, model provider, sensor,
  simulator, endpoint or backend is contacted, and no new result, model, policy or performance
  claim is made. Scientific values and trace labels keep their original meaning.
- Each page inlines its original CSS and JavaScript unchanged. A small evidence-read adapter placed
  before the original script answers only the page's one recorded JSON path with the exact original
  bytes (re-hashed with SHA-256 in the browser when Web Crypto is available) and refuses every
  other request. A Content-Security-Policy blocks network connections.
- "Download" buttons save the exact original JSON bytes.
- Links to other original pages are labeled *online reference*; they open the unchanged GitHub
  Pages site and need a connection. Nothing is requested in the background.
- Not included: the live research-loop page, endpoint configuration, credentials, run buttons,
  API calls or any backend.

## Provenance

- Source commit `f42e096f4eaae0298200c53c1775d56972a6964b`, source date 2026-10-05T11:23:29+09:00. That date is the pack date; it is not a run date.
- `manifest.json` lists the SHA-256 and size of every source, evidence input and output file.
- Rebuild from the repository root with `python scripts/build_post_hackathon_offline.py`; verify with
  `python scripts/build_post_hackathon_offline.py --check`. The build is deterministic and refuses to run when a source or
  evidence file differs from its committed bytes.

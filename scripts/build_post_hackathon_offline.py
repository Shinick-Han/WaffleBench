"""Build the post-hackathon offline replay pack (local-only, not part of the submitted version).

Writes web/post-hackathon-offline/{index,inspection-live,discovery-cycle}.html, README.md and
manifest.json. Each page inlines its original CSS/JS unchanged plus the exact bytes of its one
frozen evidence JSON, served to the unchanged page script by a narrow evidence-read adapter that
refuses every other request. Output is deterministic: the only date used is the commit date of
the source files, never the wall clock.

    python scripts/build_post_hackathon_offline.py          # build
    python scripts/build_post_hackathon_offline.py --check  # rebuild in memory, compare with disk
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "web" / "post-hackathon-offline"
GENERATOR = "scripts/build_post_hackathon_offline.py"
NOTICE = "Post-hackathon offline replay — not part of the submitted version."
ONLINE_BASE = "https://shinick-han.github.io/WaffleBench/"
PACK_PAGES = ("./index.html", "./inspection-live.html", "./discovery-cycle.html")
LINK_ALIASES = {"./post-hackathon.html": "./index.html"}
EXCLUDED_MARKERS = ("discovery-run", "discovery-endpoint", "/api/")

PAGES = (
    {
        "out": "index.html",
        "label": "Overview",
        "html": "web/post-hackathon.html",
        "css": ("web/post-hackathon.css",),
        "js": "web/post-hackathon.js",
        "data": "web/data/inspection-v3.json",
        "data_url": "./data/inspection-v3.json",
    },
    {
        "out": "inspection-live.html",
        "label": "Omnigent measurement replay",
        "html": "web/inspection-live.html",
        "css": ("web/inspection-evidence.css", "web/inspection-live.css"),
        "js": "web/inspection-live.js",
        "data": "web/data/inspection-live.json",
        "data_url": "./data/inspection-live.json",
    },
    {
        "out": "discovery-cycle.html",
        "label": "Completed research cycle",
        "html": "web/discovery-cycle.html",
        "css": ("web/inspection-evidence.css", "web/discovery-cycle.css"),
        "js": "web/discovery-cycle.js",
        "data": "web/data/discovery-cycle.json",
        "data_url": "./data/discovery-cycle.json",
    },
)

CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
       "connect-src 'none'; font-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; "
       "worker-src 'none'; manifest-src 'none'; base-uri 'none'; form-action 'none'")

PACK_CSS = """
.ofl-pack{background:#f6f1e2;border-bottom:1px solid #d8c99c;color:#2b2618;font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.ofl-pack-inner{max-width:1180px;margin:0 auto;padding:10px 16px;display:grid;gap:6px}
.ofl-pack .ofl-notice{font-size:15px;margin:0}
.ofl-pack p{margin:0}
.ofl-pack nav{display:flex;flex-wrap:wrap;gap:4px 14px;align-items:baseline}
.ofl-pack nav a{color:#1f4f3a;font-weight:600}
.ofl-pack nav a[aria-current="page"]{color:#2b2618;text-decoration:none}
.ofl-pack code{font:12px/1.4 ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}
.ofl-pack details summary{cursor:pointer;font-weight:600}
.ofl-pack details ul{margin:6px 0 0;padding-left:20px}
a[data-ofl-online]::after{content:" \\2197  online reference";font-size:.82em;font-weight:400}
a[data-ofl-removed]{color:inherit;text-decoration:none;cursor:default}
""".strip()

ADAPTER_TEMPLATE = r"""(() => {
  'use strict';
  // Offline replay pack evidence reader. Serves exactly one embedded recorded file to the unchanged
  // page script below and refuses every other request; it never contacts a network or backend.
  const EXPECTED_URL = __URL__;
  const EXPECTED_SHA256 = __SHA__;
  const EMBEDDED_BASE64 = __B64__;
  const PACK_PAGES = __PACK__;
  const LINK_ALIASES = __ALIASES__;
  const ONLINE_BASE = __ONLINE__;
  let bytes = null;
  const decode = () => {
    const s = atob(EMBEDDED_BASE64), u = new Uint8Array(s.length);
    for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
    return u;
  };
  const refuse = what => {
    console.error('Offline replay pack refused a request:', what);
    return Promise.reject(new TypeError('Offline replay pack: network access is disabled (' + what + ')'));
  };
  const showCheck = text => { const el = document.getElementById('oflByteCheck'); if (el) el.textContent = text; };
  const verified = (async () => {
    bytes = decode();
    if (!(window.crypto && crypto.subtle)) { showCheck('not re-checked (Web Crypto unavailable in this context)'); return true; }
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    const hex = [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
    const ok = hex === EXPECTED_SHA256;
    showCheck(ok ? 'match — embedded bytes re-hashed in this browser' : 'MISMATCH — evidence withheld');
    return ok;
  })().catch(error => { console.error('Offline replay pack embedded evidence:', error); showCheck('unreadable — evidence withheld'); return false; });
  window.fetch = function offlineEvidenceFetch(input, init) {
    if (typeof input !== 'string' || input !== EXPECTED_URL) return refuse(String(input && input.url || input));
    const method = init && init.method ? String(init.method).toUpperCase() : 'GET';
    if (method !== 'GET') return refuse(method + ' ' + input);
    return verified.then(ok => ok ? new Response(bytes.slice(), {status: 200, headers: {'Content-Type': 'application/json'}})
      : refuse('embedded evidence failed its SHA-256 check'));
  };
  const blocked = name => function () { throw new TypeError('Offline replay pack: ' + name + ' is disabled'); };
  window.XMLHttpRequest = blocked('XMLHttpRequest');
  window.WebSocket = blocked('WebSocket');
  window.EventSource = blocked('EventSource');
  if (navigator.sendBeacon) navigator.sendBeacon = () => false;

  // Links: pack pages stay local; other original pages become labeled online references to the
  // unchanged GitHub Pages site; anything else (including the excluded run page) is disabled.
  const mark = (a, attr, title) => { a.setAttribute(attr, ''); a.title = title; };
  function guard(a) {
    const raw = a.getAttribute('href');
    if (raw == null || a.dataset.oflSeen === raw) return;
    let next = raw;
    if (raw.startsWith('#') || raw.startsWith('blob:')) { /* in-page anchor or exact-bytes download */ }
    else if (LINK_ALIASES[raw]) next = LINK_ALIASES[raw];
    else if (PACK_PAGES.includes(raw)) { /* local pack page */ }
    else if (/discovery-run/i.test(raw) || !/^(?:\.\/)?[\w.-]+\.html(?:#[\w-]*)?$|^https:\/\//.test(raw)) {
      a.removeAttribute('href'); a.dataset.oflSeen = '';
      mark(a, 'data-ofl-removed', 'Not included in the offline replay pack'); return;
    } else {
      if (!raw.startsWith('https://')) next = ONLINE_BASE + raw.replace(/^\.\//, '');
      mark(a, 'data-ofl-online', 'Online reference to the unchanged submitted site; needs a network connection');
      a.rel = 'noopener noreferrer';
    }
    a.dataset.oflSeen = next;
    if (next !== raw) a.setAttribute('href', next);
  }
  const scan = root => { if (root.matches && root.matches('a[href]')) guard(root); if (root.querySelectorAll) root.querySelectorAll('a[href]').forEach(guard); };
  new MutationObserver(list => {
    for (const m of list) {
      if (m.type === 'attributes') scan(m.target);
      else m.addedNodes.forEach(scan);
    }
  }).observe(document.documentElement, {subtree: true, childList: true, attributes: true, attributeFilter: ['href']});
  scan(document);
})();"""


class BuildError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(rel: str) -> bytes:
    return (ROOT / rel).read_bytes()


def js_literal(value) -> str:
    # JSON is a valid JS literal; escaping '<', '>' and '&' keeps it inert inside <script>.
    return (json.dumps(value, ensure_ascii=True)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def source_provenance(paths: list[str]) -> tuple[str, str]:
    """Commit and committer date of the newest commit touching the sources; inputs must be clean."""
    if git("status", "--porcelain", "--", *paths):
        raise BuildError("source or evidence files differ from the committed version; commit or restore them first")
    commit = git("log", "-1", "--format=%H", "--", *paths)
    date = git("log", "-1", "--format=%cI", "--", *paths)
    if not commit:
        raise BuildError("no commit found for the source files")
    for rel in paths:
        if subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=ROOT, check=True, capture_output=True).stdout != read(rel):
            raise BuildError(f"{rel} does not match its bytes at {commit}")
    return commit, date


def ensure_inline_safe(rel: str, text: str, tag: str) -> None:
    lowered = text.lower()
    for bad in (f"</{tag}", "<!--", "<script"):
        if bad in lowered:
            raise BuildError(f"{rel} contains {bad!r}; it cannot be inlined unchanged")


def rewrite_anchor(match: re.Match) -> str:
    attrs, href, inner = match.group(1), match.group(2), match.group(3)
    if "discovery-run" in href.lower():
        return ""  # the live research-loop page is excluded from the pack
    if href.startswith("#") or href in PACK_PAGES:
        return match.group(0)
    if href in LINK_ALIASES:
        return "<a" + attrs.replace(f'href="{href}"', f'href="{LINK_ALIASES[href]}"') + ">" + inner + "</a>"
    if re.fullmatch(r"(?:\./)?[\w.-]+\.html", href):
        target = ONLINE_BASE + href.removeprefix("./")
    elif href.startswith("https://"):
        target = href
    else:
        raise BuildError(f"unexpected link target {href!r}")
    attrs = attrs.replace(f'href="{href}"', f'href="{html.escape(target)}"')
    return (f'<a{attrs} data-ofl-online rel="noopener noreferrer" '
            f'title="Online reference to the unchanged submitted site; needs a network connection">{inner}</a>')


def build_page(page: dict, commit: str, date: str) -> bytes:
    source = read(page["html"]).decode("utf-8")
    data = read(page["data"])
    data_sha = sha256(data)
    try:
        json.loads(data.decode("utf-8"))
    except ValueError as error:
        raise BuildError(f"{page['data']} is not UTF-8 JSON: {error}") from error
    b64 = base64.b64encode(data).decode("ascii")
    if base64.b64decode(b64) != data:
        raise BuildError(f"{page['data']} did not round-trip through base64")

    # Static links first, before any script is inlined, so script text is never touched.
    if "\r" in source:
        raise BuildError(f"{page['html']}: unexpected CR line endings")
    out, n = re.subn(r'<a((?:\s[^>]*)?\shref="([^"]*)"[^>]*)>(.*?)</a>', rewrite_anchor, source, flags=re.S)
    if not n:
        raise BuildError(f"{page['html']}: no links found")

    links = re.findall(r'<link rel="stylesheet" href="\./([\w.-]+\.css)(?:\?v=[0-9a-f]+)?">', out)
    if tuple(f"web/{name}" for name in links) != page["css"]:
        raise BuildError(f"{page['html']}: stylesheets {links} differ from the expected {page['css']}")
    for rel in page["css"]:
        css = read(rel).decode("utf-8")
        ensure_inline_safe(rel, css, "style")
        out, k = re.subn(r'<link rel="stylesheet" href="\./' + re.escape(Path(rel).name) + r'(?:\?v=[0-9a-f]+)?">',
                         lambda _m, css=css, rel=rel: f'<style data-source="{rel}">\n{css}\n</style>', out, count=1)
        if k != 1:
            raise BuildError(f"{page['html']}: stylesheet {rel} not replaced")

    js = read(page["js"]).decode("utf-8")
    ensure_inline_safe(page["js"], js, "script")
    adapter = (ADAPTER_TEMPLATE.replace("__URL__", js_literal(page["data_url"]))
               .replace("__SHA__", js_literal(data_sha)).replace("__B64__", js_literal(b64))
               .replace("__PACK__", js_literal(list(PACK_PAGES))).replace("__ALIASES__", js_literal(LINK_ALIASES))
               .replace("__ONLINE__", js_literal(ONLINE_BASE)))
    ensure_inline_safe("adapter", adapter, "script")
    script_tag = re.compile(r'<script src="\./' + re.escape(Path(page["js"]).name) + r'(?:\?v=[0-9a-f]+)?" defer></script>')
    if len(script_tag.findall(out)) != 1 or out.count("<script") != 1:
        raise BuildError(f"{page['html']}: expected exactly one external script tag")
    out = script_tag.sub(lambda _m: (f'<script data-role="offline-evidence-adapter">\n{adapter}\n</script>\n'
                                     f'<script data-source="{page["js"]}">\n{js}\n</script>'), out)

    head = (f'<meta charset="utf-8">\n<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            f'<meta name="robots" content="noindex">')
    out = _replace_once(out, '<meta charset="utf-8">', head, page)
    out = re.sub(r"<title>(.*?)</title>", lambda m: f"<title>Offline replay · {m.group(1)}</title>", out, count=1)
    out = _replace_once(out, "</head>", f'<style data-role="offline-pack">\n{PACK_CSS}\n</style>\n</head>', page)
    skip = '<a class="skip" href="#main">Skip to main content</a>'
    out = _replace_once(out, skip, skip + "\n" + pack_header(page, commit, date, data, data_sha), page)

    lowered = out.replace(adapter, "").lower()  # the adapter names the excluded page only to block it
    for marker in EXCLUDED_MARKERS:
        if marker in lowered:
            raise BuildError(f"{page['out']}: excluded reference {marker!r} remains")
    if out.count("<script") != 2 or out.count("</script>") != 2:
        raise BuildError(f"{page['out']}: unexpected script structure")
    return out.encode("utf-8")


def _replace_once(text: str, old: str, new: str, page: dict) -> str:
    if text.count(old) != 1:
        raise BuildError(f"{page['html']}: expected exactly one {old!r}")
    return text.replace(old, new)


def pack_header(page: dict, commit: str, date: str, data: bytes, data_sha: str) -> str:
    current = ' aria-current="page"'
    nav = " ".join(f'<a href="./{p["out"]}"{current if p is page else ""}>{html.escape(p["label"])}</a>' for p in PAGES)
    e = html.escape
    return f"""<div class="ofl-pack" role="region" aria-label="Offline replay pack">
 <div class="ofl-pack-inner">
  <p class="ofl-notice"><strong>{e(NOTICE)}</strong></p>
  <nav aria-label="Offline replay pack pages"><span>Offline pack:</span> {nav}</nav>
  <p><strong>Recorded replay, not fresh execution.</strong> This file re-reads one embedded copy of original frozen evidence. No agent, model provider, sensor, simulator or backend is contacted, nothing is re-run or re-tuned, and no new result exists here. The built-in evidence reader answers only its one recorded file and refuses every other request; network connections are blocked. Links marked <em>online reference</em> open the unchanged submitted pages and need a network connection.</p>
  <details>
   <summary>Original frozen evidence and build provenance</summary>
   <ul>
    <li>Evidence file <code>{e(page["data"])}</code> · {len(data):,} bytes · SHA-256 <code>{data_sha}</code>. Embedded byte-for-byte; download buttons save these exact bytes. In-browser check: <span id="oflByteCheck">pending</span>.</li>
    <li>Page sources inlined unchanged: <code>{e(page["html"])}</code> (markup adapted for offline use), {", ".join(f"<code>{e(c)}</code>" for c in page["css"])}, <code>{e(page["js"])}</code>.</li>
    <li>Source commit <code>{e(commit)}</code> · source date {e(date)} (the pack date; not a run date) · built by <code>{GENERATOR}</code>. Hashes of every source, input and output: <code>manifest.json</code>.</li>
    <li>Post-hackathon local development, excluded from judging. The submitted site, data and claims are unchanged.</li>
   </ul>
  </details>
 </div>
</div>"""


def readme(commit: str, date: str) -> bytes:
    rows = "\n".join(f"| `{p['out']}` | {p['label']} | `{p['data']}` |" for p in PAGES)
    text = f"""# WaffleBench offline replay pack

**{NOTICE}**

This folder is local post-hackathon development, excluded from judging. The submitted version
(public repository, GitHub Pages site, runtime, data and assets) is unchanged.

## Open it

Double-click `index.html`. It works from `file://` with no server, no installation and no network.
Use the "Offline pack" links at the top of each page to move between the three pages.

| File | Page | Embedded frozen evidence |
|---|---|---|
{rows}

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

- Source commit `{commit}`, source date {date}. That date is the pack date; it is not a run date.
- `manifest.json` lists the SHA-256 and size of every source, evidence input and output file.
- Rebuild from the repository root with `python {GENERATOR}`; verify with
  `python {GENERATOR} --check`. The build is deterministic and refuses to run when a source or
  evidence file differs from its committed bytes.
"""
    return text.encode("utf-8")


def build() -> dict[str, bytes]:
    sources = sorted({p["html"] for p in PAGES} | {c for p in PAGES for c in p["css"]} | {p["js"] for p in PAGES})
    inputs = sorted({p["data"] for p in PAGES})
    commit, date = source_provenance(sources + inputs)
    files = {p["out"]: build_page(p, commit, date) for p in PAGES}
    files["README.md"] = readme(commit, date)

    def entry(rel: str, extra: dict) -> dict:
        data = read(rel)
        return {"path": rel, "bytes": len(data), "sha256": sha256(data), **extra}

    manifest = {
        "schema_version": 1,
        "kind": "wafflebench_post_hackathon_offline_replay_pack",
        "notice": NOTICE,
        "scope": "Recorded replay of original frozen evidence; no execution, no new results; excluded from judging.",
        "source_commit": commit,
        "source_date": date,
        "generator": entry(GENERATOR, {}),
        "online_reference_base": ONLINE_BASE,
        "inputs": [entry(rel, {"embedded_in": [p["out"] for p in PAGES if p["data"] == rel]}) for rel in inputs],
        "sources": [entry(rel, {"inlined_in": [p["out"] for p in PAGES if rel in (p["html"], p["js"], *p["css"])]})
                    for rel in sources],
        "outputs": [{"path": f"web/post-hackathon-offline/{name}", "bytes": len(data), "sha256": sha256(data)}
                    for name, data in sorted(files.items())],
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="rebuild in memory and compare with the files on disk")
    args = parser.parse_args()
    try:
        files = build()
    except BuildError as error:
        print(json.dumps({"status": "error", "error": str(error)}))
        return 1
    if args.check:
        stale = [n for n, d in files.items() if not (OUT_DIR / n).is_file() or (OUT_DIR / n).read_bytes() != d]
        extra = sorted(p.name for p in OUT_DIR.iterdir() if p.name not in files) if OUT_DIR.is_dir() else []
        ok = not stale and not extra
        print(json.dumps({"status": "ok" if ok else "stale", "stale": stale, "unexpected": extra}))
        return 0 if ok else 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    unexpected = sorted(p.name for p in OUT_DIR.iterdir() if p.name not in files)
    if unexpected:
        print(json.dumps({"status": "error", "error": f"unexpected files in output directory: {unexpected}"}))
        return 1
    for name, data in files.items():
        (OUT_DIR / name).write_bytes(data)
    print(json.dumps({"status": "ok", "output": str(OUT_DIR.relative_to(ROOT)).replace("\\", "/"),
                      "files": {n: sha256(d) for n, d in sorted(files.items())}}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

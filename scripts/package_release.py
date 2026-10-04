"""Package verified scientific campaigns and a recorded-run static demo.

Only the scientific allowlist is copied. Provider transcripts and account/runtime
state are deliberately outside this package. Run after independent M5 acceptance.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from falsify_lab.reporting import build_snapshot
from falsify_lab.storage import Campaign


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8", newline="\n")


def copy_source(source: Path, target: Path) -> None:
    """Match public Git LF normalization, preserving frozen byte-bound inputs."""
    data = source.read_bytes()
    relative = source.relative_to(PROJECT).as_posix()
    if relative not in {"research-manifest.json", "mockup/index.html"} and b"\0" not in data:
        data = data.replace(b"\r\n", b"\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def copy_campaign(source: Path, destination: Path) -> None:
    if destination.exists():
        raise RuntimeError(f"Evidence destination already exists: {destination}")
    # These folders contain simulator inputs/measurements and hash-bound records.
    # Do not copy a whole runs root: live provider logs may also be stored there.
    allowed = {".json", ".jsonl", ".cir", ".log", ".txt"}
    for folder in ("ledger", "evidence", "restricted", "attempts"):
        parent = source / folder
        if not parent.exists():
            continue
        for path in sorted(parent.rglob("*")):
            if path.is_file() and not path.is_symlink() and path.suffix in allowed:
                target = destination / path.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
    for name in ("model.json", "snapshot.json", "benchmark_report.json", "omnigent/session-proof.json"):
        path = source / name
        if path.is_file():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live-root", required=True, type=Path)
    p.add_argument("--primary-root", required=True, type=Path)
    p.add_argument("--destination", required=True, type=Path, help="Fresh staging directory")
    p.add_argument("--performance-root", type=Path, help="Audited supplementary campaign, separate from primary")
    p.add_argument("--preserve-snapshot", type=Path, help="Previously released combined snapshot to keep byte-for-byte")
    args = p.parse_args()
    live = Campaign(args.live_root.resolve(), None)
    primary = Campaign(args.primary_root.resolve(), None)
    live.verify_ledger()
    primary.verify_ledger()
    ls, ps = build_snapshot(live), build_snapshot(primary)
    if live.kind != "demo" or primary.kind != "primary":
        raise RuntimeError("Expected separate demo and primary campaign roots")
    if ls["data_mode"] != "real" or ps["data_mode"] != "real":
        raise RuntimeError("Fixture data cannot be released as scientific evidence")
    if not ls["run"] or ls["run"]["status"] != "complete":
        raise RuntimeError("Live run is not complete")
    updates = [e for e in live.events("analysis_update") if e["payload"].get("run_id") == ls["run"]["run_id"]]
    if not 2 <= len(updates) <= 4:
        raise RuntimeError("Expected two to four genuine recorded live updates")
    proof = ls.get("omnigent_session") or {}
    if proof.get("status") != "completed" or not proof.get("sub_agents"):
        raise RuntimeError("Actual completed SDK session and specialist transcript proof required")
    bench = ps["benchmark"]
    if not bench or bench["status"] != "complete":
        raise RuntimeError("Primary benchmark is not complete")
    runs = [r for policy in bench["policies"] for r in policy["runs"]]
    if len(runs) != 40 or any(r["status"] != "complete" or r["budget"]["used"] != 24 for r in runs):
        raise RuntimeError("Expected forty complete fixed-budget runs")
    for key in ("protocol_hash", "manifest_hash"):
        if ls[key] != ps[key]:
            raise RuntimeError(f"Campaign {key} mismatch")
    if ls["model"]["model_hash"] != ps["model"]["model_hash"]:
        raise RuntimeError("Live and primary frozen models differ")
    combined_attempts = ls["cost"]["attempts"] + ps["cost"]["attempts"]
    le, pe = live.events(), primary.events()
    combined_wall = max(le[-1]["unix"], pe[-1]["unix"]) - min(le[0]["unix"], pe[0]["unix"])
    if combined_attempts > min(ls["cost"]["caps"]["attempts_max"], ps["cost"]["caps"]["attempts_max"]) or combined_wall > min(ls["cost"]["caps"]["wall_seconds_max"], ps["cost"]["caps"]["wall_seconds_max"]):
        raise RuntimeError("Combined research execution exceeds the frozen cap")
    destination = args.destination.resolve()
    if destination.exists():
        raise RuntimeError("Use a fresh staging directory; existing releases are never overwritten")
    destination.mkdir(parents=True)
    # Start a fresh public repository from the reviewed source. The private
    # development history includes account/credit preparation notes.
    source_files = (
        ".gitattributes", ".gitignore", "LICENSE", "README.md", "RESULTS.md", "THIRD_PARTY.md", "CORE_API.md", "DATA_CONTRACT.md",
        "RESEARCH_PROTOCOL.md", "research-manifest.json", "pyproject.toml", "uv.lock", "launch.ps1",
    )
    for name in source_files:
        copy_source(PROJECT / name, destination / name)
    if (PROJECT / "PERFORMANCE.md").is_file():
        copy_source(PROJECT / "PERFORMANCE.md", destination / "PERFORMANCE.md")
    for folder in ("falsify_lab", "scripts", "tests", "web", "agent", "mockup"):
        for path in sorted((PROJECT / folder).rglob("*")):
            if not path.is_file() or path.is_symlink() or "__pycache__" in path.parts:
                continue
            if path.name == "falsify.yaml" or path.suffix == ".pyc":
                continue
            target = destination / path.relative_to(PROJECT)
            target.parent.mkdir(parents=True, exist_ok=True)
            copy_source(path, target)
    copy_campaign(args.live_root.resolve(), destination / "evidence" / "live")
    copy_campaign(args.primary_root.resolve(), destination / "evidence" / "primary")
    audit = PROJECT / "evidence" / "milestones" / "M5-independent-audit.json"
    if not audit.is_file():
        audit = args.primary_root.resolve() / "independent-audit.json"
    shutil.copyfile(audit, destination / "evidence" / "primary" / "independent-audit.json")
    reproduction = PROJECT / "evidence" / "reproduction" / "validation.json"
    if reproduction.is_file():
        target = destination / "evidence" / "reproduction" / "validation.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(reproduction, target)
    if args.performance_root:
        from verify_performance_extension import audit as audit_extension
        extension = args.performance_root.resolve()
        audit_result = audit_extension(extension)
        target_root = destination / "evidence" / "performance" / "extension"
        for load in (20, 8, 35):
            copy_campaign(extension / f"load-{load}f", target_root / f"load-{load}f")
        for name in ("plan.json", "report.json", "original-export.json"):
            shutil.copyfile(extension / name, target_root / name)
        shutil.copytree(extension / "executed_source", target_root / "executed_source")
        write_json(target_root / "independent-audit.json", audit_result)
        for name in ("core-timing.json", "ui-baseline.json", "ui-candidate.json", "ui-comparison.json"):
            source = PROJECT / "evidence" / "performance" / name
            target = destination / "evidence" / "performance" / name
            shutil.copyfile(source, target)
    combined = copy.deepcopy(ls)
    combined["benchmark"] = bench
    combined["release"] = {
        "mode": "recorded-run replay",
        "workbench_source": "evidence/live",
        "benchmark_source": "evidence/primary",
        "workbench_cost_scope": "Live demonstration campaign only; benchmark costs are separate.",
        "primary_campaign": ps["campaign"],
        "primary_cost": ps["cost"],
        "live_campaign": ls["campaign"],
        "live_cost": ls["cost"],
        "aggregate_research_usage": {"physical_attempts": combined_attempts, "first_to_last_event_seconds": round(combined_wall, 6)},
        "published_at": datetime.now(timezone.utc).isoformat(),
    }
    combined["limitations"].insert(0, "Recorded live workbench and separate primary benchmark. snapshot.cost is live-only; benchmark.cost includes the primary campaign and its posthoc reference. No remote execution is provided.")
    docs = destination / "docs"
    docs.mkdir()
    for name in ("index.html", "app.css", "app.js"):
        copy_source(PROJECT / "web" / name, docs / name)
    index = docs / "index.html"
    index.write_text(index.read_text(encoding="utf-8").replace("<head>", '<head>\n<meta name="falsify-mode" content="static">', 1), encoding="utf-8", newline="\n")
    (docs / ".nojekyll").write_text("", encoding="utf-8")
    write_json(docs / "data" / "snapshot.json", combined)
    if args.preserve_snapshot:
        previous = json.loads(args.preserve_snapshot.read_text(encoding="utf-8"))
        for key in ("protocol_hash", "manifest_hash", "model", "run", "benchmark"):
            if previous[key] != combined[key]:
                raise RuntimeError(f"Preserved snapshot has different scientific content: {key}")
        shutil.copyfile(args.preserve_snapshot, docs / "data" / "snapshot.json")
    write_json(destination / "evidence" / "primary" / "benchmark_report.json", bench)
    files = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            files[path.relative_to(destination).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(destination / "release-files.json", {"schema_version": 1, "sha256": files})
    print(json.dumps({"ok": True, "files": len(files), "destination": str(destination), "primary_success": bench["primary"]["success"]}))


if __name__ == "__main__":
    main()

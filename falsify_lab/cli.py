"""CLI: python -m falsify_lab.cli <command> --root <campaign-or-demo-directory>

Commands: calibrate, demo-prepare, benchmark, export, reproduce (see DATA_CONTRACT.md).
Prints one JSON summary on stdout; returns nonzero on failure. Artifacts remain on failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from . import benchmark as bm
from .reporting import export_snapshot
from .simulator import NgspiceSimulator, SimulationFailed
from .storage import Campaign, StorageError


def _campaign(root: Path, kind: str, label: str) -> Campaign:
    return Campaign.open_or_create(root, kind, NgspiceSimulator(), label=label)


def _finish(root: Path, c: Campaign | None, result: dict[str, Any], ok: bool) -> int:
    path, _ = export_snapshot(root)
    out = {"ok": ok, **result, "snapshot": str(path)}
    if c is not None:
        out["campaign_kind"] = c.kind
        out["usage"] = c.cost()
        out["ledger_events"] = c.verify_ledger()
    print(json.dumps(out, indent=2, default=str))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m falsify_lab.cli")
    ap.add_argument("command", choices=["calibrate", "demo-prepare", "benchmark", "export", "reproduce"])
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--kind", choices=["development", "primary"], default="development", help="calibrate/benchmark campaign kind")
    ap.add_argument("--confirm-primary", action="store_true", help="coordinator-only: authorize the full primary benchmark (M5)")
    ap.add_argument("--seeds", type=int, nargs="*", help="development benchmark only: subset of seeds")
    ap.add_argument("--policies", nargs="*", help="development benchmark only: subset of policies")
    ap.add_argument("--focus-run", help="export: run shown as the snapshot focus")
    ap.add_argument("--label", default="")
    args = ap.parse_args(argv)
    root: Path = args.root.resolve()
    c: Campaign | None = None
    try:
        if args.command == "export":
            path, snap = export_snapshot(root, args.focus_run)
            print(json.dumps({"ok": True, "snapshot": str(path), "empty": snap["campaign"] is None}, indent=2))
            return 0
        if args.command == "calibrate":
            c = _campaign(root, args.kind, args.label)
            m = bm.calibrate(c)
            return _finish(root, c, {"command": "calibrate", "model": m.to_dict()}, True)
        if args.command == "demo-prepare":
            c = _campaign(root, "demo", args.label)
            res = bm.demo_prepare(c)
            return _finish(root, c, {"command": "demo-prepare", **res}, True)
        if args.command == "reproduce":
            c = _campaign(root, "reproduce", args.label or "reproduction check")
            res = bm.reproduce(c)
            return _finish(root, c, {"command": "reproduce", **res}, True)
        if args.command == "benchmark":
            if args.kind == "primary" and not args.confirm_primary:
                raise StorageError("primary benchmark requires --confirm-primary (coordinator, M5)")
            c = _campaign(root, args.kind, args.label)
            res = bm.run_benchmark(c, args.seeds, args.policies, confirm_primary=args.confirm_primary)
            return _finish(root, c, {"command": "benchmark", **res}, res["status"] == "complete")
    except (StorageError, SimulationFailed, ValueError) as exc:
        if root.exists() and (root / "ledger" / "events.jsonl").exists():
            try:
                return _finish(root, c, {"command": args.command, "error": f"{type(exc).__name__}: {exc}"}, False)
            except Exception:  # noqa: BLE001 - still report the original failure
                traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": False, "command": args.command, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

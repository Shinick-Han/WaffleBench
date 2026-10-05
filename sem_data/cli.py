"""Command line: python -m sem_data.cli {extract,audit,split,validate}.

Each command prints one JSON summary and returns nonzero on failure.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from . import DEFAULT_SEED
from .audit import build_audit
from .manifest import read_json, validate_manifest, write_json
from .safezip import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    extract_archive,
)
from .split import make_split


def _emit(payload: dict) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _refuse_inside(out: pathlib.Path, data: pathlib.Path) -> None:
    out, data = out.resolve(), data.resolve()
    if out == data or data in out.parents:
        raise ValueError(f"output directory must not be inside the read-only data root: {out}")


def cmd_extract(args) -> int:
    record = extract_archive(
        args.archive,
        args.root,
        max_file_bytes=args.max_file_bytes,
        max_total_bytes=args.max_total_bytes,
        max_entries=args.max_entries,
    )
    _emit({"status": "ok", "command": "extract", **record})
    return 0


def cmd_audit(args) -> int:
    out = pathlib.Path(args.out)
    _refuse_inside(out, pathlib.Path(args.data))
    manifest, summary = build_audit(
        args.data, created_at=args.created_at, soft_mask_threshold=args.soft_mask_threshold
    )
    errors = validate_manifest(manifest, require_split=False, check_files=True)
    if errors:
        raise ValueError("audit manifest invalid: " + "; ".join(errors[:10]))
    write_json(out / "audit_manifest.json", manifest)
    write_json(out / "audit_summary.json", summary)
    status = "ok" if summary["accepted"] and not summary["rejected"] else (
        "ok_with_rejections" if summary["accepted"] else "failed"
    )
    _emit({
        "status": status,
        "command": "audit",
        "manifest": str((out / "audit_manifest.json").resolve()),
        "summary": str((out / "audit_summary.json").resolve()),
        "rows": summary["rows"],
        "accepted": summary["accepted"],
        "rejected": summary["rejected"],
        "rejections_by_reason": summary["rejections_by_reason"],
        "class_counts": summary["class_counts"],
        "classes_without_accepted_items": summary["classes_without_accepted_items"],
        "scarce_classes": summary["scarce_classes"],
        "mask_policy": summary["mask_policy"],
    })
    return 0 if summary["accepted"] else 1


def cmd_split(args) -> int:
    out = pathlib.Path(args.out)
    manifest = read_json(pathlib.Path(args.manifest))
    if isinstance(manifest.get("root"), str):
        _refuse_inside(out, pathlib.Path(manifest["root"]))
    result, summary = make_split(manifest, seed=args.seed)
    write_json(out / "manifest.json", result)
    write_json(out / "split_summary.json", summary)
    _emit({
        "status": "ok",
        "command": "split",
        "manifest": str((out / "manifest.json").resolve()),
        "summary": str((out / "split_summary.json").resolve()),
        **{key: summary[key] for key in ("seed", "items", "units", "counts", "group_level")},
    })
    return 0


def cmd_validate(args) -> int:
    manifest = read_json(pathlib.Path(args.manifest))
    errors = validate_manifest(manifest, require_split=not args.unsplit, check_files=args.check_files)
    _emit({"status": "ok" if not errors else "invalid", "command": "validate", "errors": errors})
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sem_data.cli", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    extract = commands.add_parser("extract", help="safely extract the dataset archive")
    extract.add_argument("--archive", required=True)
    extract.add_argument("--root", required=True)
    extract.add_argument("--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES)
    extract.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    extract.add_argument("--max-entries", type=int, default=DEFAULT_MAX_ENTRIES)
    extract.set_defaults(handler=cmd_extract)

    audit = commands.add_parser("audit", help="validate pairs and write an unsplit manifest")
    audit.add_argument("--data", required=True)
    audit.add_argument("--out", required=True)
    audit.add_argument("--created-at", default=None)
    audit.add_argument("--soft-mask-threshold", type=int, default=None,
                       help="opt-in: binarize 0..255 soft-edge masks as value >= threshold")
    audit.set_defaults(handler=cmd_audit)

    split = commands.add_parser("split", help="write the grouped deterministic split manifest")
    split.add_argument("--manifest", required=True)
    split.add_argument("--out", required=True)
    split.add_argument("--seed", type=int, default=DEFAULT_SEED)
    split.set_defaults(handler=cmd_split)

    validate = commands.add_parser("validate", help="check a manifest against the data interface")
    validate.add_argument("--manifest", required=True)
    validate.add_argument("--unsplit", action="store_true")
    validate.add_argument("--check-files", action="store_true")
    validate.set_defaults(handler=cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except Exception as exc:  # report every failure as JSON with a nonzero exit
        _emit({"status": "failed", "command": args.command, "error": f"{type(exc).__name__}: {exc}"})
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Manifest schema checks, path containment and deterministic JSON output."""

from __future__ import annotations

import json
import os
import pathlib
import re

from . import DATASET_ID, SCHEMA_VERSION, SPLITS

ITEM_FIELDS = (
    "id", "image", "mask", "defect_class", "width", "height",
    "image_sha256", "mask_sha256", "duplicate_group", "source_group", "split",
)
TOP_FIELDS = (
    "schema_version", "dataset_id", "data_mode", "created_at", "root",
    "license", "limitations", "split", "items",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """Raised when a manifest violates the shared data interface."""


def is_contained_relative(path: str) -> bool:
    """True when ``path`` is a relative POSIX path that cannot leave its root."""
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        return False
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") and ":" not in part for part in parts)


def resolve_contained(root: pathlib.Path, relative: str) -> pathlib.Path:
    """Resolve ``relative`` below ``root`` and refuse anything that escapes it."""
    if not is_contained_relative(relative):
        raise ManifestError(f"path is not a contained relative path: {relative!r}")
    root = root.resolve()
    target = (root / relative).resolve()
    if root not in target.parents:
        raise ManifestError(f"path escapes data root: {relative!r}")
    return target


def validate_manifest(manifest: dict, *, require_split: bool = True, check_files: bool = False) -> list[str]:
    """Return a list of contract violations (empty when the manifest is valid)."""
    errors: list[str] = []
    for field in TOP_FIELDS:
        if field not in manifest:
            errors.append(f"missing top-level field {field}")
    if errors:
        return errors
    if manifest["schema_version"] != SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    if manifest["dataset_id"] != DATASET_ID:
        errors.append(f"dataset_id must be {DATASET_ID!r}")
    if manifest["data_mode"] != "real":
        errors.append("data_mode must be 'real'")
    if not isinstance(manifest["root"], str) or not os.path.isabs(manifest["root"]):
        errors.append("root must be an absolute path")
    if not isinstance(manifest["limitations"], list):
        errors.append("limitations must be a list")
    if require_split and not isinstance(manifest["split"], dict):
        errors.append("split metadata missing")
    items = manifest["items"]
    if not isinstance(items, list):
        return errors + ["items must be a list"]

    ids: set[str] = set()
    dup_split: dict[str, set] = {}
    source_split: dict[str, set] = {}
    root = pathlib.Path(manifest["root"])
    for index, item in enumerate(items):
        label = f"item[{index}]"
        missing = [field for field in ITEM_FIELDS if field not in item]
        if missing:
            errors.append(f"{label} missing fields {missing}")
            continue
        label = f"item {item['id']!r}"
        if not isinstance(item["id"], str) or not item["id"]:
            errors.append(f"{label} has an invalid id")
        elif item["id"] in ids:
            errors.append(f"duplicate id {item['id']!r}")
        ids.add(item["id"])
        for field in ("image", "mask"):
            if not is_contained_relative(item[field]):
                errors.append(f"{label} {field} path is not contained: {item[field]!r}")
            elif check_files:
                try:
                    if not resolve_contained(root, item[field]).is_file():
                        errors.append(f"{label} {field} file missing")
                except ManifestError as exc:
                    errors.append(f"{label} {exc}")
        for field in ("image_sha256", "mask_sha256"):
            if not isinstance(item[field], str) or not _SHA256.match(item[field]):
                errors.append(f"{label} {field} is not a sha256 hex digest")
        for field in ("width", "height"):
            if not isinstance(item[field], int) or isinstance(item[field], bool) or item[field] <= 0:
                errors.append(f"{label} {field} must be a positive integer")
        if not isinstance(item["defect_class"], str) or not item["defect_class"]:
            errors.append(f"{label} defect_class must be a non-empty string")
        if not isinstance(item["duplicate_group"], str) or not item["duplicate_group"]:
            errors.append(f"{label} duplicate_group must be a non-empty string")
        if item["source_group"] is not None and not isinstance(item["source_group"], str):
            errors.append(f"{label} source_group must be a string or null")
        split = item["split"]
        if require_split:
            if split not in SPLITS:
                errors.append(f"{label} split must be one of {SPLITS}")
        elif split is not None and split not in SPLITS:
            errors.append(f"{label} split is invalid")
        dup_split.setdefault(item["duplicate_group"], set()).add(split)
        if item["source_group"] is not None:
            source_split.setdefault(item["source_group"], set()).add(split)
    if require_split:
        for group, splits in sorted(dup_split.items()):
            if len(splits) > 1:
                errors.append(f"duplicate group {group} spans splits {sorted(map(str, splits))}")
        for group, splits in sorted(source_split.items()):
            if len(splits) > 1:
                errors.append(f"source group {group} spans splits {sorted(map(str, splits))}")
    return errors


def dumps(payload: dict) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def write_json(path: pathlib.Path, payload: dict) -> None:
    """Write JSON atomically with stable key order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(dumps(payload), encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def read_json(path: pathlib.Path) -> dict:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))

"""Validator/adapter for the common Carinthia-S data manifest (schema_version 1).

Contract (shared with the data and image workers): top level ``schema_version=1``,
``dataset_id='carinthia-s'``, ``data_mode='real'``, ``created_at``, ``root`` (absolute
data directory), ``license``, ``limitations``, ``split`` metadata and ``items``. Each item
has ``id``, ``image`` and ``mask`` (relative to root), ``defect_class``, ``width``,
``height``, ``image_sha256``, ``mask_sha256``, ``duplicate_group``, ``source_group``
(null if unavailable) and ``split`` in {train, calibration, test}.

Checks: unique ids, relative paths that stay under root, positive integer sizes, 64-hex
digests, duplicate groups never shared across splits, and no synthetic normals (an item
flagged ``synthetic``/``generated``/``generator`` or a synthetic-named class is rejected).
File digests are verified only when ``verify_files=True``; a missing root is reported,
never silently treated as verified.

``prospective_view`` is the observed-vs-hidden boundary: it drops mask paths/digests and
the defect class for items a decision policy may see before review. Class values and masks
are evaluation-only; they are never prospective policy features.

Carinthia-S notes (from the dataset description, not verified here): classes are kept as
the recorded numeric values. A class whose images show no visible defect (empty masks,
possibly SEM misalignment) is NOT confirmed physically good material, so an empty mask is
never treated as a verified physical negative. Very small classes are reported as scarce
with their split counts, and framing artefacts (e.g. a black border) are potential
shortcuts for image models.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

SPLITS = ("train", "calibration", "test")
TOP_KEYS = ("schema_version", "dataset_id", "data_mode", "created_at", "root", "license", "limitations",
            "split", "items")
ITEM_KEYS = ("id", "image", "mask", "defect_class", "width", "height", "image_sha256", "mask_sha256",
             "duplicate_group", "source_group", "split")
HIDDEN_ITEM_KEYS = ("mask", "mask_sha256", "defect_class")
SCARCE_BELOW = 10
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SYNTHETIC_FLAGS = ("synthetic", "generated", "generator")


class ManifestError(ValueError):
    pass


def _relative_ok(p: Any) -> bool:
    if not isinstance(p, str) or not p:
        return False
    if PureWindowsPath(p).is_absolute() or PureWindowsPath(p).drive or p.startswith(("/", "\\")):
        return False
    return ".." not in PurePosixPath(p.replace("\\", "/")).parts


def validate(manifest: dict, *, verify_files: bool = False, allow_fixture: bool = False) -> dict:
    """Return a summary dict; raise ManifestError listing every violation found.

    ``allow_fixture`` accepts only ``data_mode='synthetic_fixture'`` (test fixtures, never
    evidence) instead of ``'real'``; every other check is unchanged."""
    errors: list[str] = []
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    missing = [k for k in TOP_KEYS if k not in manifest]
    if missing:
        errors.append(f"missing top-level keys {missing}")
    if manifest.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if manifest.get("dataset_id") != "carinthia-s":
        errors.append("dataset_id must be 'carinthia-s'")
    mode = manifest.get("data_mode")
    if mode != ("synthetic_fixture" if allow_fixture else "real"):
        errors.append("data_mode must be 'synthetic_fixture'" if allow_fixture else "data_mode must be 'real'")
    root = manifest.get("root")
    if not isinstance(root, str) or not (PureWindowsPath(root).is_absolute() or root.startswith("/")):
        errors.append("root must be an absolute path")
    if not isinstance(manifest.get("limitations"), list):
        errors.append("limitations must be a list")
    items = manifest.get("items")
    if not isinstance(items, list):
        errors.append("items must be a list")
        items = []
    ids: set[str] = set()
    group_split: dict[Any, str] = {}
    counts = {s: 0 for s in SPLITS}
    for n, it in enumerate(items):
        tag = f"item[{n}]"
        if not isinstance(it, dict):
            errors.append(f"{tag} is not an object")
            continue
        miss = [k for k in ITEM_KEYS if k not in it]
        if miss:
            errors.append(f"{tag} missing {miss}")
            continue
        iid = it["id"]
        tag = f"item {iid!r}"
        if not isinstance(iid, str) or not iid:
            errors.append(f"{tag}: id must be a non-empty string")
        elif iid in ids:
            errors.append(f"{tag}: duplicate id")
        ids.add(iid)
        for k in ("image", "mask"):
            if not _relative_ok(it[k]):
                errors.append(f"{tag}: {k} must be a relative path under root")
        for k in ("width", "height"):
            v = it[k]
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                errors.append(f"{tag}: {k} must be a positive integer")
        for k in ("image_sha256", "mask_sha256"):
            if not isinstance(it[k], str) or not _HEX64.match(it[k]):
                errors.append(f"{tag}: {k} must be 64 lowercase hex characters")
        if it["split"] not in SPLITS:
            errors.append(f"{tag}: split must be one of {SPLITS}")
        else:
            counts[it["split"]] += 1
        if it["source_group"] is not None and not isinstance(it["source_group"], str):
            errors.append(f"{tag}: source_group must be a string or null")
        if any(it.get(f) for f in _SYNTHETIC_FLAGS) or "synthetic" in str(it["defect_class"]).lower():
            errors.append(f"{tag}: synthetic items are not allowed in a real manifest")
        g = it["duplicate_group"]
        if g is None or isinstance(g, (dict, list)):
            errors.append(f"{tag}: duplicate_group must be a scalar")
        elif it["split"] in SPLITS:
            prev = group_split.setdefault(g, it["split"])
            if prev != it["split"]:
                errors.append(f"{tag}: duplicate_group {g!r} crosses splits {prev}/{it['split']}")
    file_check = "not_requested"
    if verify_files and not errors:
        file_check = _verify_files(Path(root), items, errors)
    if errors:
        raise ManifestError("; ".join(errors))
    return {"items": len(items), "splits": counts, "duplicate_groups": len(group_split),
            "file_check": file_check, "data_mode": mode, "classes": class_counts(items)}


def class_counts(items: list, scarce_below: int = SCARCE_BELOW) -> dict:
    """Per-class counts by split, keyed by the class value exactly as recorded (numeric
    classes stay numeric strings; no semantic names are invented). Classes with fewer than
    ``scarce_below`` items in total are listed as scarce."""
    table: dict[str, dict[str, int]] = {}
    for it in items:
        row = table.setdefault(str(it["defect_class"]), {s: 0 for s in SPLITS})
        row[it["split"]] += 1
    totals = {c: sum(r.values()) for c, r in table.items()}
    return {"by_split": dict(sorted(table.items())), "totals": dict(sorted(totals.items())),
            "scarce": sorted(c for c, t in totals.items() if t < scarce_below),
            "missing_from_split": {s: sorted(c for c, r in table.items() if r[s] == 0) for s in SPLITS}}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_files(root: Path, items: list, errors: list) -> str:
    if not root.is_dir():
        errors.append(f"root {root} does not exist; files cannot be verified")
        return "root_missing"
    for it in items:
        for k, d in (("image", "image_sha256"), ("mask", "mask_sha256")):
            p = root / it[k]
            if not p.is_file():
                errors.append(f"item {it['id']!r}: {k} file missing")
            elif _sha256(p) != it[d]:
                errors.append(f"item {it['id']!r}: {d} mismatch")
    return "verified"


def load(path: str | Path, *, verify_files: bool = False, allow_fixture: bool = False) -> tuple[dict, dict]:
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    return m, validate(m, verify_files=verify_files, allow_fixture=allow_fixture)


def split_ids(manifest: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {s: [] for s in SPLITS}
    for it in manifest["items"]:
        out[it["split"]].append(it["id"])
    return out


def duplicate_groups(manifest: dict) -> dict[str, Any]:
    return {it["id"]: it["duplicate_group"] for it in manifest["items"]}


def prospective_view(manifest: dict, split: str = "test") -> list[dict]:
    """Items of ``split`` without mask paths/digests or defect class (pre-review view)."""
    if split not in SPLITS:
        raise ManifestError(f"unknown split {split!r}")
    return [{k: v for k, v in it.items() if k not in HIDDEN_ITEM_KEYS}
            for it in manifest["items"] if it["split"] == split]

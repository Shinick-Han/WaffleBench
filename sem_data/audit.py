"""Carinthia-S layout adapter, image/mask validation and unsplit manifest.

The documented layout (carinthia-s_dataset.html on the Zenodo record) is a
``carinthia-s.csv`` table with ``image_path``, ``mask_path``, ``filename`` and
``label`` columns, paths relative to the table, ``images/*.jpg`` and
``masks/*.png``. The adapter locates that table anywhere below the data root.

Structural checks (decodable files, matching dimensions, binary masks, path
containment, unique ids, consistent duplicate annotations) reject items.
Semantic observations (empty or full masks per class) are reported but never
reject an item, because the source defines no per-class mask rule.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import pathlib
import re
from collections import Counter, defaultdict

from . import DATASET_ID, LICENSE, SCHEMA_VERSION
from .manifest import ManifestError, resolve_contained
from .safezip import EXTRACTION_RECORD

CSV_NAME = "carinthia-s.csv"
REQUIRED_COLUMNS = ("image_path", "mask_path", "label")
SOURCE_GROUP_COLUMNS = (
    "source_group", "lot", "lot_id", "wafer", "wafer_id", "acquisition", "acquisition_id", "session",
)
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 50_000_000
SCARCE_CLASS_ITEMS = 20
_CHUNK = 1024 * 1024

BASE_LIMITATIONS = (
    "Physical pixel size is unknown: no nm-per-pixel calibration is supplied, so all sizes are in pixels.",
    "Defect-only frames: the source supplies no representative clean whole-frame negatives; none are synthesized.",
    "Masks are validated structurally (binary, same dimensions as the image); per-class mask semantics are "
    "reported as observations only.",
    "Exact-image duplicates are grouped by decoded pixel content; near-duplicates are not detected.",
    "Class labels are kept as the source's numeric codes; no semantic class names are assigned.",
    "Source documentation: class 6 frames show no visible defect and may come from SEM misalignment; they are "
    "not confirmed defect-free material and must not be treated as clean negatives.",
    "Source documentation: defects are usually centered, frames have a three-pixel black right border and "
    "some show framing squares; position, border and framing cues are potential shortcuts.",
    "Labels and masks are evaluation targets only; they must not be used as prospective policy or inference "
    "features.",
)
IMAGE_LEVEL_LIMITATION = (
    "No lot, wafer or acquisition grouping is supplied by the source; splits are image-level and grouped "
    "only by exact-image duplicate identity, so correlated acquisitions may cross splits."
)


class AuditError(RuntimeError):
    """Raised when the data root cannot be audited at all."""


class NonBinaryMaskError(ValueError):
    """Raised when a mask is not a two-level (or constant) zero-background mask."""


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_dialect(handle):
    """Sniff the delimiter from the header line (the real table uses ';')."""
    header = handle.readline()
    handle.seek(0)
    counts = {delimiter: header.count(delimiter) for delimiter in (",", ";", "	")}
    delimiter = max(counts, key=lambda d: (counts[d], d == ","))
    return delimiter


def find_table(root: pathlib.Path) -> pathlib.Path:
    """Locate the single label table below ``root``."""
    candidates = sorted(
        path for path in root.rglob("*.csv")
        if "__MACOSX" not in path.parts and path.is_file()
    )
    named = [path for path in candidates if path.name.casefold() == CSV_NAME]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise AuditError(f"multiple {CSV_NAME} files found: {[str(p) for p in named]}")
    usable = []
    for path in candidates:
        with open(path, newline="", encoding="utf-8-sig") as handle:
            header = next(csv.reader(handle, delimiter=_table_dialect(handle)), [])
        if all(column in [h.strip() for h in header] for column in REQUIRED_COLUMNS):
            usable.append(path)
    if len(usable) == 1:
        return usable[0]
    if not usable:
        raise AuditError(f"no label table with columns {REQUIRED_COLUMNS} found below {root}")
    raise AuditError(f"ambiguous label tables: {[str(p) for p in usable]}")


def normalize_label(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    match = re.fullmatch(r"(\d+)(?:\.0+)?", value)
    return str(int(match.group(1))) if match else value


def binary_mask(image, soft_threshold: int | None = None):
    """Return (bool array, convention) or raise NonBinaryMaskError.

    With ``soft_threshold`` set, an 8-bit mask that has a zero background, a
    255 foreground and intermediate edge values is binarized as
    ``value >= soft_threshold`` instead of being rejected. The convention then
    records the threshold so the decision stays visible in the audit.
    """
    import numpy as np

    mode = image.mode
    if mode == "1":
        return np.array(image, dtype=bool), "1-bit"
    if mode == "P":
        image = image.convert("RGBA")
        mode = "RGBA"
    array = np.array(image)
    if mode in ("RGB", "RGBA", "LA"):
        channels = array.shape[-1]
        color = array[..., :1] if mode == "LA" else array[..., :3]
        if channels in (2, 4):
            alpha = array[..., -1]
            if np.unique(alpha).size != 1:
                raise NonBinaryMaskError("mask alpha channel is not constant")
        if mode != "LA" and not (np.array_equal(color[..., 0], color[..., 1])
                                 and np.array_equal(color[..., 0], color[..., 2])):
            raise NonBinaryMaskError("mask has distinct color channels")
        array = color[..., 0]
    elif array.ndim != 2:
        raise NonBinaryMaskError(f"unsupported mask mode {mode}")
    values = np.unique(array)
    if values.size > 2:
        if soft_threshold is not None and values[0] == 0 and values[-1] == 255:
            return array >= soft_threshold, f"soft-0..255>={soft_threshold}"
        raise NonBinaryMaskError(f"mask has {values.size} distinct values")
    if values.size == 2 and values[0] != 0:
        raise NonBinaryMaskError(f"mask values {values.tolist()} lack a zero background")
    high = int(values.max()) if values.size else 0
    return array != 0, f"0/{high}" if high else "0-only"


def _decode_pair(image_path: pathlib.Path, mask_path: pathlib.Path, soft_threshold: int | None = None) -> dict:
    import numpy as np
    from PIL import Image

    for path in (image_path, mask_path):
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
    with Image.open(image_path) as image:
        if image.size[0] * image.size[1] > MAX_PIXELS:
            raise ValueError("image exceeds pixel limit")
        image.load()
        image_mode = image.mode
        image_size = image.size
        pixel_digest = hashlib.sha256(
            f"{image_mode}|{image_size[0]}x{image_size[1]}|".encode() + image.tobytes()
        ).hexdigest()
    with Image.open(mask_path) as mask:
        if mask.size[0] * mask.size[1] > MAX_PIXELS:
            raise ValueError("mask exceeds pixel limit")
        mask.load()
        mask_size = mask.size
        mask_mode = mask.mode
        mask_bool, convention = binary_mask(mask, soft_threshold)
    if mask_size != image_size:
        raise LookupError(f"image {image_size} and mask {mask_size} dimensions differ")
    foreground = int(mask_bool.sum())
    mask_digest = hashlib.sha256(
        f"{mask_size[0]}x{mask_size[1]}|".encode() + np.packbits(mask_bool).tobytes()
    ).hexdigest()
    return {
        "width": image_size[0],
        "height": image_size[1],
        "image_mode": image_mode,
        "mask_mode": mask_mode,
        "mask_convention": convention,
        "pixel_digest": pixel_digest,
        "mask_digest": mask_digest,
        "foreground_pixels": foreground,
    }


def _to_root_relative(root: pathlib.Path, table_dir: pathlib.Path, raw: str) -> str:
    value = (raw or "").strip().replace("\\", "/")
    table_relative = table_dir.relative_to(root).as_posix()
    candidate = value if table_relative == "." else f"{table_relative}/{value}"
    resolve_contained(root, candidate)
    return candidate


def build_audit(
    data_root: str | pathlib.Path,
    *,
    created_at: str | None = None,
    soft_mask_threshold: int | None = None,
) -> tuple[dict, dict]:
    """Audit the extracted data and return (unsplit manifest, audit summary).

    ``soft_mask_threshold`` is off by default: masks with intermediate values
    are rejected as ``nonbinary_mask``. Setting it is an explicit, recorded
    preprocessing decision (see SEM_DATA_GUIDE.md).
    """
    if soft_mask_threshold is not None and not 1 <= soft_mask_threshold <= 255:
        raise ValueError("soft_mask_threshold must be between 1 and 255")
    root = pathlib.Path(data_root).resolve()
    if not root.is_dir():
        raise AuditError(f"data root is not a directory: {root}")
    table = find_table(root)
    table_dir = table.parent
    record_path = root / EXTRACTION_RECORD
    extraction = None
    if record_path.is_file():
        import json

        extraction = json.loads(record_path.read_text(encoding="utf-8"))
    if created_at is None:
        created_at = (extraction or {}).get("extracted_at") or (
            _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
        )

    with open(table, newline="", encoding="utf-8-sig") as handle:
        delimiter = _table_dialect(handle)
        reader = csv.DictReader(handle, delimiter=delimiter)
        header = [name.strip() for name in (reader.fieldnames or [])]
        rows = [{(k or "").strip(): v for k, v in row.items()} for row in reader]
    missing_columns = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing_columns:
        raise AuditError(f"label table lacks columns {missing_columns}")
    group_column = next((column for column in SOURCE_GROUP_COLUMNS if column in header), None)

    rejections: list[dict] = []

    def reject(row_number, item_id, reason, detail=""):
        rejections.append({"row": row_number, "id": item_id, "reason": reason, "detail": detail})

    id_rows: dict[str, list[int]] = defaultdict(list)
    for number, row in enumerate(rows, start=2):
        raw_id = (row.get("filename") or "").strip()
        if not raw_id:
            raw_id = pathlib.PurePosixPath((row.get("image_path") or "").replace("\\", "/")).stem
        row["_id"] = raw_id
        id_rows[raw_id].append(number)

    candidates: list[dict] = []
    mode_counts: Counter = Counter()
    conventions: Counter = Counter()
    for number, row in enumerate(rows, start=2):
        item_id = row["_id"]
        if not item_id:
            reject(number, None, "missing_id")
            continue
        if len(id_rows[item_id]) > 1:
            reject(number, item_id, "duplicate_id", f"rows {id_rows[item_id]}")
            continue
        label = normalize_label(row.get("label"))
        if label is None:
            reject(number, item_id, "missing_label")
            continue
        try:
            image_rel = _to_root_relative(root, table_dir, row.get("image_path") or "")
            mask_rel = _to_root_relative(root, table_dir, row.get("mask_path") or "")
        except ManifestError as exc:
            reject(number, item_id, "path_not_contained", str(exc))
            continue
        image_path = root / image_rel
        mask_path = root / mask_rel
        if not image_path.is_file():
            reject(number, item_id, "missing_image", image_rel)
            continue
        if not mask_path.is_file():
            reject(number, item_id, "missing_mask", mask_rel)
            continue
        try:
            decoded = _decode_pair(image_path, mask_path, soft_mask_threshold)
        except LookupError as exc:
            reject(number, item_id, "dimension_mismatch", str(exc))
            continue
        except NonBinaryMaskError as exc:
            reject(number, item_id, "nonbinary_mask", str(exc))
            continue
        except ValueError as exc:
            reject(number, item_id, "invalid_file", str(exc))
            continue
        except Exception as exc:  # undecodable or truncated image data
            reject(number, item_id, "undecodable", f"{type(exc).__name__}: {exc}")
            continue
        mode_counts[f"image:{decoded['image_mode']}"] += 1
        mode_counts[f"mask:{decoded['mask_mode']}"] += 1
        conventions[decoded["mask_convention"]] += 1
        source_group = None
        if group_column is not None:
            source_group = (row.get(group_column) or "").strip() or None
        candidates.append({
            "row": number,
            "id": item_id,
            "image": image_rel,
            "mask": mask_rel,
            "defect_class": label,
            "width": decoded["width"],
            "height": decoded["height"],
            "image_sha256": _sha256_file(image_path),
            "mask_sha256": _sha256_file(mask_path),
            "duplicate_group": "dup-" + decoded["pixel_digest"][:24],
            "source_group": source_group,
            "split": None,
            "_mask_digest": decoded["mask_digest"],
            "_foreground": decoded["foreground_pixels"],
        })

    by_group: dict[str, list[dict]] = defaultdict(list)
    for candidate in candidates:
        by_group[candidate["duplicate_group"]].append(candidate)
    conflicting = set()
    for group, members in by_group.items():
        annotations = {(m["defect_class"], m["_mask_digest"]) for m in members}
        if len(annotations) > 1:
            conflicting.add(group)
            for member in members:
                reject(member["row"], member["id"], "conflicting_duplicate_annotation",
                       f"{group}: {len(members)} copies, {len(annotations)} distinct annotations")

    accepted = sorted(
        (c for c in candidates if c["duplicate_group"] not in conflicting), key=lambda c: c["id"]
    )
    semantic = defaultdict(lambda: {"items": 0, "empty_mask": 0, "full_mask": 0, "foreground_fraction_sum": 0.0})
    for item in accepted:
        stats = semantic[item["defect_class"]]
        area = item["width"] * item["height"]
        stats["items"] += 1
        stats["empty_mask"] += item["_foreground"] == 0
        stats["full_mask"] += item["_foreground"] == area
        stats["foreground_fraction_sum"] += item["_foreground"] / area
    semantic_summary = {
        label: {
            "items": stats["items"],
            "empty_mask": stats["empty_mask"],
            "full_mask": stats["full_mask"],
            "mean_foreground_fraction": round(stats["foreground_fraction_sum"] / stats["items"], 6),
        }
        for label, stats in sorted(semantic.items())
    }
    items = [{key: value for key, value in item.items() if not key.startswith("_") and key != "row"}
             for item in accepted]

    limitations = list(BASE_LIMITATIONS)
    if group_column is None or all(item["source_group"] is None for item in items):
        limitations.append(IMAGE_LEVEL_LIMITATION)
    source_classes = Counter(
        label for label in (normalize_label(row.get("label")) for row in rows) if label is not None
    )
    accepted_classes = Counter(item["defect_class"] for item in items)
    lost = sorted(label for label in source_classes if not accepted_classes[label])
    scarce = sorted(label for label, n in accepted_classes.items() if n < SCARCE_CLASS_ITEMS)
    nonbinary = sum(1 for r in rejections if r["reason"] == "nonbinary_mask")
    if nonbinary:
        limitations.append(
            f"{nonbinary} source masks with intermediate (soft-edge) values were rejected under the "
            f"{'strict binary' if soft_mask_threshold is None else 'configured'} mask policy; per-class "
            "losses are in the audit summary."
        )
    if lost:
        limitations.append(f"Source classes with no accepted items: {', '.join(lost)}.")
    if scarce:
        limitations.append(
            f"Scarce classes (< {SCARCE_CLASS_ITEMS} accepted items): {', '.join(scarce)}; per-class "
            "estimates for them are not reliable."
        )
    mask_policy = (
        {"mode": "strict-binary", "soft_threshold": None, "development_only": False}
        if soft_mask_threshold is None
        else {"mode": "soft-threshold", "soft_threshold": soft_mask_threshold, "development_only": True,
              "provenance": "explicit --soft-mask-threshold conversion of soft-edge source masks"}
    )
    accepted_groups = Counter(item["duplicate_group"] for item in items)
    rejections.sort(key=lambda r: (r["row"], r["reason"]))
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "data_mode": "real",
        "created_at": created_at,
        "root": str(root),
        "license": dict(LICENSE),
        "limitations": limitations,
        "units": {"pixel_size_nm": None, "length_unit": "pixel"},
        "mask_policy": mask_policy,
        "source": {
            "table": table.relative_to(root).as_posix(),
            "table_sha256": _sha256_file(table),
            "table_delimiter": delimiter,
            "source_group_column": group_column,
            "archive_sha256": (extraction or {}).get("archive_sha256"),
        },
        "split": None,
        "items": items,
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": DATASET_ID,
        "created_at": created_at,
        "root": str(root),
        "table": manifest["source"]["table"],
        "rows": len(rows),
        "accepted": len(items),
        "rejected": len(rejections),
        "rejections_by_reason": dict(sorted(Counter(r["reason"] for r in rejections).items())),
        "rejections": rejections,
        "class_counts": dict(sorted(accepted_classes.items())),
        "source_class_counts": dict(sorted(source_classes.items())),
        "rejections_by_class": dict(sorted(Counter(
            normalize_label(rows[r["row"] - 2].get("label")) or "missing" for r in rejections
        ).items())),
        "classes_without_accepted_items": lost,
        "scarce_classes": scarce,
        "mask_policy": mask_policy,
        "dimension_counts": dict(sorted(Counter(f"{i['width']}x{i['height']}" for i in items).items())),
        "mode_counts": dict(sorted(mode_counts.items())),
        "mask_value_conventions": dict(sorted(conventions.items())),
        "duplicates": {
            "groups": len(accepted_groups),
            "groups_with_copies": sum(1 for n in accepted_groups.values() if n > 1),
            "items_in_copy_groups": sum(n for n in accepted_groups.values() if n > 1),
            "conflicting_groups_rejected": len(conflicting),
        },
        "source_groups": {
            "column": group_column,
            "available": any(item["source_group"] is not None for item in items),
        },
        "semantic_mask_observations": semantic_summary,
        "structural_validation": [
            "image and mask decode", "matching dimensions", "binary mask values",
            "contained relative paths", "unique ids", "consistent duplicate annotations",
        ],
        "physical_units": {"pixel_size_nm": None, "status": "unknown"},
        "limitations": limitations,
    }
    return manifest, summary


def read_mask(manifest: dict, item: dict):
    """Load an item's mask as a bool array using the manifest's recorded mask policy.

    Readers must use this (or the same rule) so training and evaluation see the
    masks that the audit accepted. A manifest without ``mask_policy`` is read
    strictly.
    """
    from PIL import Image

    policy = manifest.get("mask_policy") or {"mode": "strict-binary", "soft_threshold": None}
    threshold = policy.get("soft_threshold") if policy.get("mode") == "soft-threshold" else None
    path = resolve_contained(pathlib.Path(manifest["root"]), item["mask"])
    with Image.open(path) as mask:
        mask.load()
        array, _ = binary_mask(mask, threshold)
    if array.shape != (item["height"], item["width"]):
        raise LookupError(f"mask {item['id']} shape {array.shape} differs from manifest")
    return array

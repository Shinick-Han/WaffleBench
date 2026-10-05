"""Shared Carinthia-S manifest (schema_version 1) loading, validation and gated file access.

Masks are only reachable through a ``SplitReader`` constructed with ``masks_allowed=True``;
training builds a train-only reader and prediction never enables masks.
"""

import hashlib
import json
import pathlib

SPLITS = ("train", "calibration", "test")
ITEM_KEYS = ("id", "image", "mask", "defect_class", "width", "height", "image_sha256",
             "mask_sha256", "duplicate_group", "source_group", "split")
TOP_KEYS = ("schema_version", "dataset_id", "data_mode", "created_at", "root", "license",
            "limitations", "items")
SPLIT_META_KEYS = ("split", "splits", "split_metadata")  # common contract uses "split"
NORMAL_CLASSES = {"normal", "good", "ok", "clean", "synthetic_normal", "no_defect", "none"}


class ManifestError(ValueError):
    pass


class MaskAccessError(PermissionError):
    pass


class MaskFormatError(ManifestError):
    pass


STRICT_MASKS = {"type": "binary_strict"}


def _from_mask_policy(policy):
    """Data-worker ``mask_policy`` {mode: strict-binary|threshold-soft, soft_threshold, development_only}."""
    if not isinstance(policy, dict):
        raise ManifestError("mask_policy must be an object")
    mode = policy.get("mode")
    if mode == "strict-binary":
        if policy.get("soft_threshold") is not None:
            raise ManifestError("strict-binary mask_policy must not carry a soft_threshold")
        return dict(STRICT_MASKS)
    if mode == "threshold-soft":
        return {"type": "threshold", "threshold": policy.get("soft_threshold"),
                "development_only": policy.get("development_only"),
                "provenance": policy.get("provenance") or "manifest mask_policy threshold-soft"}
    raise ManifestError(f"unknown mask_policy mode {mode!r}; refusing to guess a mask conversion")


def mask_convention(manifest):
    """Mask reading convention; default strict binary ({0,255} or {0,1}), never silent thresholding.

    Declared by the data worker's ``mask_policy`` or by ``mask_convention``
    (``{"type": "threshold", "threshold": N, "development_only": true, "provenance": "..."}``); a
    threshold conversion is accepted only when explicitly declared development-only.
    """
    if "mask_policy" in manifest and "mask_convention" in manifest:
        raise ManifestError("declare either mask_policy or mask_convention, not both")
    if "mask_policy" in manifest:
        conv = _from_mask_policy(manifest["mask_policy"])
    else:
        conv = manifest.get("mask_convention") or STRICT_MASKS
    if conv.get("type") == "binary_strict":
        return dict(STRICT_MASKS)
    if conv.get("type") == "threshold":
        t = conv.get("threshold")
        if not (isinstance(t, int) and not isinstance(t, bool) and 1 <= t <= 255):
            raise ManifestError("mask_convention threshold must be an integer in 1..255")
        if conv.get("development_only") is not True or not conv.get("provenance"):
            raise ManifestError("threshold mask conversion must declare development_only=true and provenance")
        return dict(conv)
    raise ManifestError(f"unknown mask_convention {conv!r}")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(obj):
    return sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _safe_relative(root, rel, what, item_id):
    if not isinstance(rel, str) or not rel:
        raise ManifestError(f"item {item_id}: {what} must be a non-empty relative path")
    p = pathlib.PurePosixPath(rel.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or (p.parts and p.parts[0].endswith(":")):
        raise ManifestError(f"item {item_id}: {what} path {rel!r} must stay inside root")
    return root / pathlib.Path(*p.parts)


def validate(manifest, allow_fixture=False):
    """Return a validation report; raise ManifestError on contract violations."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    missing = [k for k in TOP_KEYS if k not in manifest]
    if missing:
        raise ManifestError(f"manifest missing keys: {missing}")
    split_meta_key = next((k for k in SPLIT_META_KEYS if k in manifest), None)
    if split_meta_key is None:
        raise ManifestError(f"manifest missing split metadata (one of {list(SPLIT_META_KEYS)})")
    if manifest["schema_version"] != 1:
        raise ManifestError(f"unsupported schema_version {manifest['schema_version']!r}")
    if manifest["dataset_id"] != "carinthia-s":
        raise ManifestError(f"dataset_id must be 'carinthia-s', got {manifest['dataset_id']!r}")
    allowed_modes = {"real", "fixture"} if allow_fixture else {"real"}
    if manifest["data_mode"] not in allowed_modes:
        raise ManifestError(f"data_mode {manifest['data_mode']!r} not accepted (allowed {sorted(allowed_modes)})")
    root = pathlib.Path(manifest["root"])
    if not root.is_absolute():
        raise ManifestError("manifest root must be an absolute data directory")
    if not isinstance(manifest["limitations"], list):
        raise ManifestError("limitations must be a list")
    items = manifest["items"]
    if not isinstance(items, list) or not items:
        raise ManifestError("items must be a non-empty list")
    ids, group_split, warnings = set(), {}, []
    source_split = {}
    counts = {s: 0 for s in SPLITS}
    class_counts = {}
    for item in items:
        if not isinstance(item, dict):
            raise ManifestError("each item must be an object")
        miss = [k for k in ITEM_KEYS if k not in item]
        if miss:
            raise ManifestError(f"item {item.get('id')!r} missing keys: {miss}")
        iid = item["id"]
        if not isinstance(iid, str) or not iid:
            raise ManifestError("item id must be a non-empty string")
        if iid in ids:
            raise ManifestError(f"duplicate item id {iid!r}")
        ids.add(iid)
        if item["split"] not in SPLITS:
            raise ManifestError(f"item {iid}: split {item['split']!r} not in {SPLITS}")
        if item.get("synthetic") or str(item["defect_class"]).strip().lower() in NORMAL_CLASSES:
            raise ManifestError(f"item {iid}: synthetic or normal items are not allowed in this defect-only manifest")
        for k in ("width", "height"):
            if not isinstance(item[k], int) or isinstance(item[k], bool) or item[k] <= 0:
                raise ManifestError(f"item {iid}: {k} must be a positive integer")
        for k in ("image_sha256", "mask_sha256"):
            v = item[k]
            if not (isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)):
                raise ManifestError(f"item {iid}: {k} must be a lowercase hex sha256")
        _safe_relative(root, item["image"], "image", iid)
        _safe_relative(root, item["mask"], "mask", iid)
        group = item["duplicate_group"]
        if group is None or group == "":
            raise ManifestError(f"item {iid}: duplicate_group is required")
        prev = group_split.setdefault(group, item["split"])
        if prev != item["split"]:
            raise ManifestError(f"duplicate_group {group!r} spans splits {prev} and {item['split']}")
        if item["source_group"] is not None:
            source_split.setdefault(item["source_group"], set()).add(item["split"])
        counts[item["split"]] += 1
        cls = str(item["defect_class"])
        class_counts.setdefault(item["split"], {}).setdefault(cls, 0)
        class_counts[item["split"]][cls] += 1
    leaky = sorted(str(g) for g, s in source_split.items() if len(s) > 1)
    if leaky:
        warnings.append(f"{len(leaky)} source_group values span several splits; source-level independence not guaranteed")
    if all(i["source_group"] is None for i in items):
        warnings.append("source_group unavailable for all items; acquisition-level independence unknown")
    conv = mask_convention(manifest)
    if conv["type"] == "threshold":
        warnings.append(f"development-only threshold mask conversion declared (>= {conv['threshold']})")
    all_classes = sorted({c for v in class_counts.values() for c in v})
    for c in all_classes:
        absent = [sp for sp in SPLITS if not class_counts.get(sp, {}).get(c)]
        if absent:
            warnings.append(f"defect_class {c!r} absent from splits {absent}")
    return {"counts": counts, "class_counts": {sp: dict(sorted(class_counts.get(sp, {}).items())) for sp in SPLITS},
            "split_metadata_key": split_meta_key, "warnings": warnings,
            "mask_convention": conv}


def load(path, allow_fixture=False):
    path = pathlib.Path(path)
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest {path} is not valid UTF-8 JSON: {exc}") from exc
    report = validate(manifest, allow_fixture=allow_fixture)
    return Manifest(manifest, sha256_bytes(raw), path.resolve(), report)


class Manifest:
    def __init__(self, data, file_sha256, path, report):
        self.data = data
        self.file_sha256 = file_sha256
        self.path = path
        self.report = report
        self.root = pathlib.Path(data["root"])
        self.data_mode = data["data_mode"]
        self.mask_convention = mask_convention(data)

    def items(self, split):
        if split not in SPLITS:
            raise ManifestError(f"unknown split {split!r}")
        return sorted((i for i in self.data["items"] if i["split"] == split), key=lambda i: i["id"])

    def split_identity(self, split):
        """Hash of (id, image hash, mask hash, class) for a split; masks are not read."""
        rows = [[i["id"], i["image_sha256"], i["mask_sha256"], i["defect_class"]] for i in self.items(split)]
        return {"split": split, "count": len(rows),
                "sha256": canonical_sha256({"rows": rows, "mask_convention": self.mask_convention}),
                "dataset_id": self.data["dataset_id"]}

    def reader(self, split, masks_allowed, access_log=None):
        return SplitReader(self, split, masks_allowed, access_log)


class SplitReader:
    """Hash-checked file access restricted to one split; mask reads need explicit permission."""

    def __init__(self, manifest, split, masks_allowed, access_log=None):
        self.manifest = manifest
        self.split = split
        self.masks_allowed = masks_allowed
        self.access_log = access_log if access_log is not None else []
        self._items = {i["id"]: i for i in manifest.items(split)}

    def ids(self):
        return sorted(self._items)

    def item(self, iid):
        if iid not in self._items:
            raise ManifestError(f"item {iid!r} is not in split {self.split!r}")
        return self._items[iid]

    def _read(self, iid, kind):
        item = self.item(iid)
        path = _safe_relative(self.manifest.root, item[kind], kind, iid)
        data = path.read_bytes()
        if sha256_bytes(data) != item[f"{kind}_sha256"]:
            raise ManifestError(f"item {iid}: {kind} sha256 mismatch for {path}")
        self.access_log.append((self.split, kind, iid))
        return data, item

    def load_image(self, iid):
        import io
        import numpy as np
        from PIL import Image
        data, item = self._read(iid, "image")
        with Image.open(io.BytesIO(data)) as im:
            arr = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
        _check_shape(arr, item, "image")
        return arr

    def load_mask(self, iid):
        if not self.masks_allowed:
            raise MaskAccessError(f"mask access is not permitted for split {self.split!r} in this stage")
        import io
        import numpy as np
        from PIL import Image
        data, item = self._read(iid, "mask")
        with Image.open(io.BytesIO(data)) as im:
            raw = np.asarray(im.convert("L"))
        _check_shape(raw, item, "mask")
        conv = self.manifest.mask_convention
        if conv["type"] == "threshold":
            return raw >= conv["threshold"]
        levels = np.unique(raw)
        if not set(levels.tolist()) <= {0, 1, 255} or (1 in levels and 255 in levels):
            raise MaskFormatError(f"item {iid}: mask has {levels.size} grey levels; strict binary convention "
                                  "rejects it (no silent thresholding)")
        return raw > 0


def _check_shape(arr, item, kind):
    if arr.shape != (item["height"], item["width"]):
        raise ManifestError(f"item {item['id']}: {kind} shape {arr.shape} != manifest "
                            f"({item['height']}, {item['width']})")

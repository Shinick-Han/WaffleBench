"""Deterministic, class-stratified, group-preserving train/calibration/test split.

Allocation units are connected components of items sharing a duplicate group
or a non-null source group, so neither can cross splits. Units are stratified
by their most common defect class, shuffled with a seed-derived generator per
stratum, and assigned greedily to the split with the largest remaining item
target. The test split is pristine: it is recorded but must stay unused until
a final, once-only evaluation.
"""

from __future__ import annotations

import copy
import random
from collections import Counter, defaultdict

from . import DEFAULT_FRACTIONS, DEFAULT_SEED, SPLITS
from .manifest import ManifestError, validate_manifest

TEST_POLICY = (
    "Pristine hold-out: the test split must not be used for training, calibration, threshold selection, "
    "model selection or inspection until one final pre-registered evaluation. Masks and labels of "
    "benchmark or test items are never inputs to inference."
)


class _UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a != b:
            self.parent[max(a, b)] = min(a, b)


def allocation_units(items: list[dict]) -> list[list[dict]]:
    """Group items into split-atomic units (duplicate and source groups)."""
    forest = _UnionFind()
    for item in items:
        anchor = "d:" + item["duplicate_group"]
        forest.find(anchor)
        if item.get("source_group") is not None:
            forest.union(anchor, "s:" + item["source_group"])
    units: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        units[forest.find("d:" + item["duplicate_group"])].append(item)
    return sorted(
        (sorted(members, key=lambda i: i["id"]) for members in units.values()),
        key=lambda members: members[0]["id"],
    )


def _stratum(unit: list[dict]) -> str:
    counts = Counter(item["defect_class"] for item in unit)
    best = max(counts.values())
    return min(label for label, count in counts.items() if count == best)


def make_split(
    manifest: dict,
    *,
    seed: int = DEFAULT_SEED,
    fractions: tuple[float, float, float] = DEFAULT_FRACTIONS,
) -> tuple[dict, dict]:
    """Return (split manifest, split summary). The input manifest is not modified."""
    if len(fractions) != 3 or any(f < 0 for f in fractions) or abs(sum(fractions) - 1.0) > 1e-9:
        raise ValueError("fractions must be three non-negative values summing to 1")
    errors = validate_manifest(manifest, require_split=False)
    if errors:
        raise ManifestError("input manifest invalid: " + "; ".join(errors[:10]))
    result = copy.deepcopy(manifest)
    items = result["items"]
    if not items:
        raise ManifestError("manifest has no items to split")

    strata: dict[str, list[list[dict]]] = defaultdict(list)
    for unit in allocation_units(items):
        strata[_stratum(unit)].append(unit)

    for label in sorted(strata):
        units = strata[label]
        random.Random(f"sem-data-split:{seed}:{label}").shuffle(units)
        total = sum(len(unit) for unit in units)
        targets = [fraction * total for fraction in fractions]
        assigned = [0, 0, 0]
        for unit in units:
            deficits = [targets[i] - assigned[i] for i in range(3)]
            choice = max(range(3), key=lambda i: (deficits[i], -i))
            assigned[choice] += len(unit)
            for item in unit:
                item["split"] = SPLITS[choice]

    has_source = any(item.get("source_group") is not None for item in items)
    counts = {name: sum(1 for item in items if item["split"] == name) for name in SPLITS}
    class_counts = {
        name: dict(sorted(Counter(i["defect_class"] for i in items if i["split"] == name).items()))
        for name in SPLITS
    }
    all_classes = sorted({item["defect_class"] for item in items})
    classes_absent = {name: [c for c in all_classes if c not in class_counts[name]] for name in SPLITS}
    result["split"] = {
        "seed": seed,
        "fractions": dict(zip(SPLITS, fractions)),
        "method": "class-stratified greedy allocation of duplicate/source-group units",
        "grouping": ["duplicate_group", "source_group"] if has_source else ["duplicate_group"],
        "group_level": "source groups supplied" if has_source else "image-level only (no lot or acquisition groups supplied)",
        "counts": counts,
        "class_counts": class_counts,
        "classes_absent": classes_absent,
        "absent_class_policy": "Per-class metrics for a class absent from a split are unavailable (null), never imputed.",
        "test_sealed": True,
        "test_policy": TEST_POLICY,
    }
    errors = validate_manifest(result, require_split=True)
    if errors:
        raise ManifestError("split manifest invalid: " + "; ".join(errors[:10]))
    summary = {
        "dataset_id": result["dataset_id"],
        "created_at": result["created_at"],
        "seed": seed,
        "items": len(items),
        "units": sum(len(units) for units in strata.values()),
        "counts": counts,
        "class_counts": class_counts,
        "classes_absent": classes_absent,
        "group_level": result["split"]["group_level"],
        "leakage_check": "passed: no duplicate or source group spans splits",
    }
    return result, summary


def inference_view(manifest: dict, split: str, *, allow_test: bool = False) -> list[dict]:
    """Inputs an inference step may see: image identity only, no mask or label."""
    if split not in SPLITS:
        raise ValueError(f"unknown split {split!r}")
    if split == "test" and not allow_test:
        raise PermissionError("test split is sealed; pass allow_test=True only for the final evaluation")
    return [
        {key: item[key] for key in ("id", "image", "width", "height", "image_sha256")}
        for item in manifest["items"]
        if item["split"] == split
    ]

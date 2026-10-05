"""Pixel and connected-component metrics for defect-only SEM masks.

Carinthia-S frames all contain defects, so no image-level false-alarm rate on normal images is
computed. Sizes are pixel areas; the physical nm/pixel scale is unknown.
"""

from collections import deque

import numpy as np

DEFAULT_COMPONENT_IOU = 0.25
SIZE_BINS = ((0, 64), (64, 256), (256, 1024), (1024, 4096), (4096, None))
NO_IMAGE_FAR = {"value": None, "reason": "defect-only data: no real normal images, so an image-level "
                                         "false-alarm rate on normals cannot be measured"}


def label_components(mask):
    """8-connected labels (int32) and component count. Pure numpy/python fallback."""
    mask = np.asarray(mask, dtype=bool)
    try:
        from scipy import ndimage  # optional, faster
        labels, n = ndimage.label(mask, structure=np.ones((3, 3), dtype=int))
        return labels.astype(np.int32), int(n)
    except ImportError:
        pass
    h, w = mask.shape
    labels = np.zeros((h, w), dtype=np.int32)
    n = 0
    for y, x in zip(*np.nonzero(mask)):
        if labels[y, x]:
            continue
        n += 1
        labels[y, x] = n
        queue = deque([(y, x)])
        while queue:
            cy, cx = queue.popleft()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not labels[ny, nx]:
                        labels[ny, nx] = n
                        queue.append((ny, nx))
    return labels, n


def pixel_counts(pred, truth):
    pred, truth = np.asarray(pred, dtype=bool), np.asarray(truth, dtype=bool)
    if pred.shape != truth.shape:
        raise ValueError(f"prediction shape {pred.shape} != mask shape {truth.shape}")
    tp = int(np.count_nonzero(pred & truth))
    fp = int(np.count_nonzero(pred & ~truth))
    fn = int(np.count_nonzero(~pred & truth))
    tn = int(pred.size - tp - fp - fn)
    return {"tp": tp, "fp": fp, "tn": tn, "fn": fn}


def dice_iou(c):
    d_den, i_den = 2 * c["tp"] + c["fp"] + c["fn"], c["tp"] + c["fp"] + c["fn"]
    return {"dice": (2 * c["tp"] / d_den) if d_den else None, "iou": (c["tp"] / i_den) if i_den else None}


def size_bin(area):
    for lo, hi in SIZE_BINS:
        if area >= lo and (hi is None or area < hi):
            return f"{lo}-{hi}px" if hi is not None else f">={lo}px"
    raise ValueError(area)


def component_matches(pred, truth, iou_threshold=DEFAULT_COMPONENT_IOU):
    """Per ground-truth component: IoU against the union of predicted components touching it."""
    pred, truth = np.asarray(pred, dtype=bool), np.asarray(truth, dtype=bool)
    gt_labels, n_gt = label_components(truth)
    pr_labels, n_pr = label_components(pred)
    rows, touched_pred = [], set()
    for g in range(1, n_gt + 1):
        g_mask = gt_labels == g
        hit = np.unique(pr_labels[g_mask])
        hit = hit[hit > 0]
        touched_pred.update(int(h) for h in hit)
        p_mask = np.isin(pr_labels, hit) if hit.size else np.zeros_like(g_mask)
        inter = int(np.count_nonzero(g_mask & p_mask))
        union = int(np.count_nonzero(g_mask | p_mask))
        iou = inter / union if union else 0.0
        area = int(np.count_nonzero(g_mask))
        rows.append({"area_px": area, "size_bin": size_bin(area), "iou": iou, "detected": iou >= iou_threshold})
    return {"components": rows, "predicted_components": n_pr,
            "predicted_components_without_gt_overlap": n_pr - len(touched_pred)}


RESIZE_REFERENCE = 448  # earlier whole-image pipelines resized to 448x448


def resize_bias(height, width, component_areas, target=RESIZE_REFERENCE):
    """Image-level resize bias reference (geometry only; no model is run on resized images).

    Compares native tiles (scale 1) against a square whole-image resize and an aspect-preserving
    resize to ``target``: scale factors, aspect distortion and how many ground-truth components
    would shrink below 1 or 4 pixels of area.
    """
    sx, sy = target / width, target / height
    s = target / max(height, width)

    def shrink(f):
        scaled = [a * f for a in component_areas]
        return {"area_factor": f, "components_below_1px": sum(a < 1 for a in scaled),
                "components_below_4px": sum(a < 4 for a in scaled)}

    return {"target": target, "native_tiles": {"scale": 1.0, "aspect_distortion": 1.0},
            "square_resize": {"scale_x": sx, "scale_y": sy, "aspect_distortion": sx / sy, **shrink(sx * sy)},
            "aspect_preserving_resize": {"scale": s, "aspect_distortion": 1.0, **shrink(s * s)}}


def aggregate_resize_bias(rows):
    out = {"images": len(rows), "components": 0}
    for key in ("square_resize", "aspect_preserving_resize"):
        out[key] = {"components_below_1px": sum(r[key]["components_below_1px"] for r in rows),
                    "components_below_4px": sum(r[key]["components_below_4px"] for r in rows),
                    "max_aspect_distortion": max((r[key]["aspect_distortion"] for r in rows), default=None),
                    "min_aspect_distortion": min((r[key]["aspect_distortion"] for r in rows), default=None)}
    out["components"] = sum(r["components"] for r in rows)
    out["target"] = rows[0]["target"] if rows else RESIZE_REFERENCE
    out["note"] = "Geometric reference only; native tiles use scale 1 and no aspect distortion."
    return out


def _rate(num, den):
    return {"detected": num, "denominator": den, "recall": (num / den) if den else None}


def aggregate(per_image, iou_threshold=DEFAULT_COMPONENT_IOU):
    """per_image rows: {id, defect_class, counts, components, predicted_components, ...}."""
    total = {"tp": 0, "fp": 0, "tn": 0, "fn": 0}
    by_class, by_size, all_comp = {}, {}, []
    unmatched_pred = 0
    for row in per_image:
        for k in total:
            total[k] += row["counts"][k]
        unmatched_pred += row["predicted_components_without_gt_overlap"]
        for comp in row["components"]:
            all_comp.append(comp["detected"])
            by_class.setdefault(row["defect_class"], []).append(comp["detected"])
            by_size.setdefault(comp["size_bin"], []).append(comp["detected"])
    order = [size_bin(lo) for lo, _ in SIZE_BINS]
    empty = [r for r in per_image if not r["components"]]
    return {
        # Descriptive only: e.g. Carinthia-S class '6' frames show no visible defect (SEM misalignment).
        # They are not representative normal wafers, so this is not an image-level false-alarm rate.
        "empty_mask_images": {"count": len(empty),
                              "with_any_predicted_component": sum(r["predicted_components"] > 0 for r in empty),
                              "by_class": {c: sum(r["defect_class"] == c for r in empty)
                                           for c in sorted({r["defect_class"] for r in empty})}},
        "images": len(per_image),
        "pixel": {**total, **dice_iou(total)},
        "component_recall": {"iou_threshold": iou_threshold,
                             "matching": "IoU of each ground-truth component against the union of predicted "
                                         "components overlapping it",
                             "overall": _rate(sum(all_comp), len(all_comp)),
                             "by_class": {c: _rate(sum(v), len(v)) for c, v in sorted(by_class.items())},
                             "by_size_px": {b: _rate(sum(by_size[b]), len(by_size[b])) for b in order if b in by_size}},
        "predicted_components_without_gt_overlap": unmatched_pred,
        "image_level_false_alarm_rate": NO_IMAGE_FAR,
        "resize_bias": aggregate_resize_bias([r["resize_bias"] for r in per_image if "resize_bias" in r]),
    }

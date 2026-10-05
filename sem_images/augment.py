"""Deterministic scratch/particle augmentation with generated masks, TRAIN split only.

Every augmented sample carries synthetic provenance. Augmentation is a training option, never an
evaluation input, and no recall improvement is claimed without a matched comparison run.
"""

import hashlib

import numpy as np

KINDS = ("scratch", "particle")


class AugmentationScopeError(PermissionError):
    pass


def _rng(seed, item_id, copy):
    digest = hashlib.sha256(f"{seed}|{item_id}|{copy}".encode("utf-8")).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "little"))


def _scratch(shape, rng):
    h, w = shape
    y0, x0 = rng.uniform(0, h), rng.uniform(0, w)
    length, angle = rng.uniform(0.15, 0.5) * min(h, w), rng.uniform(0, np.pi)
    width = int(rng.integers(1, 4))
    yy, xx = np.mgrid[0:h, 0:w]
    dy, dx = np.sin(angle), np.cos(angle)
    t = (yy - y0) * dy + (xx - x0) * dx
    d = np.abs((yy - y0) * dx - (xx - x0) * dy)
    mask = (np.abs(t) <= length / 2) & (d <= width / 2)
    return mask, {"center_yx": [float(y0), float(x0)], "length_px": float(length), "angle_rad": float(angle),
                  "width_px": width}


def _particle(shape, rng):
    h, w = shape
    cy, cx = rng.uniform(0, h), rng.uniform(0, w)
    ry, rx = rng.uniform(2, 8), rng.uniform(2, 8)
    yy, xx = np.mgrid[0:h, 0:w]
    mask = ((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2 <= 1
    return mask, {"center_yx": [float(cy), float(cx)], "radii_px": [float(ry), float(rx)]}


def augment(image, mask, item_id, copy, seed, split):
    """Return (image, mask, provenance). ``split`` must be 'train'."""
    if split != "train":
        raise AugmentationScopeError("synthetic augmentation is allowed for the train split only")
    rng = _rng(seed, item_id, copy)
    kind = KINDS[int(rng.integers(0, len(KINDS)))]
    gen, params = (_scratch if kind == "scratch" else _particle)(image.shape, rng)
    if not gen.any():  # degenerate draw: force a single-pixel particle so the mask is never empty
        gen = np.zeros(image.shape, bool)
        gen[image.shape[0] // 2, image.shape[1] // 2] = True
    contrast = float(rng.choice([-1.0, 1.0]) * rng.uniform(0.25, 0.5))
    out = image.copy()
    out[gen] = np.clip(out[gen] + contrast, 0.0, 1.0)
    prov = {"synthetic": True, "provenance": "sem_images.augment (generated, not real SEM)",
            "source_item": item_id, "copy": copy, "kind": kind, "contrast": contrast, "params": params,
            "generated_px": int(gen.sum())}
    return out, (mask | gen), prov

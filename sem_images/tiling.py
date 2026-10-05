"""Native-resolution, aspect-preserving overlapping tiles with explicit coordinates.

No resizing happens here. Edge tiles extend past the image and are padded (right/bottom only);
each tile records its image-space origin and the valid (non-padded) region. Stitching weights
overlaps and returns a map exactly the original image size, so padding never reaches metrics.
"""

import numpy as np

DEFAULT_TILE = 256
DEFAULT_OVERLAP = 64


def _starts(length, tile, stride):
    starts, s = [0], 0
    while s + tile < length:
        s += stride
        starts.append(s)
    return starts


def plan(height, width, tile=DEFAULT_TILE, overlap=DEFAULT_OVERLAP):
    """Tile plan for an image. Transform: image_xy = tile_xy + (x0, y0), scale 1."""
    if tile <= 0 or not (0 <= overlap < tile):
        raise ValueError("require tile > 0 and 0 <= overlap < tile")
    if height <= 0 or width <= 0:
        raise ValueError("image dimensions must be positive")
    stride = tile - overlap
    tiles = []
    for y0 in _starts(height, tile, stride):
        for x0 in _starts(width, tile, stride):
            vh, vw = min(tile, height - y0), min(tile, width - x0)
            tiles.append({"index": len(tiles), "x0": x0, "y0": y0, "x1": x0 + tile, "y1": y0 + tile,
                          "valid": {"x0": x0, "y0": y0, "x1": x0 + vw, "y1": y0 + vh},
                          "valid_in_tile": {"x": 0, "y": 0, "w": vw, "h": vh},
                          "padded": vh < tile or vw < tile})
    return {"height": height, "width": width, "tile": tile, "overlap": overlap, "stride": stride,
            "transform": "image_xy = tile_xy + (x0, y0); scale 1; padding right/bottom only",
            "tiles": tiles}


def extract(image, tile_plan, pad_mode="symmetric"):
    """Return (tiles[N,T,T], valid[N,T,T] bool). Padding content is marked invalid."""
    h, w = image.shape[:2]
    if (h, w) != (tile_plan["height"], tile_plan["width"]):
        raise ValueError("image shape does not match tile plan")
    t = tile_plan["tile"]
    last = tile_plan["tiles"][-1]
    ph, pw = max(0, last["y1"] - h), max(0, last["x1"] - w)
    kwargs = {"constant_values": 0} if pad_mode == "constant" else {}
    padded = np.pad(image, ((0, ph), (0, pw)), mode=pad_mode, **kwargs) if (ph or pw) else image
    out = np.empty((len(tile_plan["tiles"]), t, t), dtype=image.dtype)
    valid = np.zeros((len(tile_plan["tiles"]), t, t), dtype=bool)
    for k, tl in enumerate(tile_plan["tiles"]):
        out[k] = padded[tl["y0"]:tl["y1"], tl["x0"]:tl["x1"]]
        v = tl["valid_in_tile"]
        valid[k, :v["h"], :v["w"]] = True
    return out, valid


def blend_weights(tile, overlap):
    """Separable ramp weights: 1 in the core, linearly decreasing to a positive floor in overlaps."""
    if overlap == 0:
        return np.ones((tile, tile), dtype=np.float64)
    i = np.arange(tile, dtype=np.float64)
    edge = np.minimum(i + 1, tile - i) / (overlap + 1)
    ramp = np.clip(edge, 1.0 / (overlap + 1), 1.0)
    return np.outer(ramp, ramp)


def stitch(tile_maps, tile_plan):
    """Weighted average of per-tile maps over valid pixels; returns float64 [H, W]."""
    h, w, t = tile_plan["height"], tile_plan["width"], tile_plan["tile"]
    tile_maps = np.asarray(tile_maps, dtype=np.float64)
    if tile_maps.shape != (len(tile_plan["tiles"]), t, t):
        raise ValueError(f"tile maps shape {tile_maps.shape} does not match plan")
    acc = np.zeros((h, w), dtype=np.float64)
    wsum = np.zeros((h, w), dtype=np.float64)
    weights = blend_weights(t, tile_plan["overlap"])
    for k, tl in enumerate(tile_plan["tiles"]):
        v = tl["valid_in_tile"]
        ys, xs = slice(tl["y0"], tl["y0"] + v["h"]), slice(tl["x0"], tl["x0"] + v["w"])
        wt = weights[:v["h"], :v["w"]]
        acc[ys, xs] += wt * tile_maps[k, :v["h"], :v["w"]]
        wsum[ys, xs] += wt
    if not np.all(wsum > 0):
        raise RuntimeError("tile plan leaves uncovered pixels")
    return acc / wsum


def resize_bilinear(arr, height, width):
    """Align-corners=False bilinear resize of a 2D map (for low-resolution anomaly maps)."""
    arr = np.asarray(arr, dtype=np.float64)
    ih, iw = arr.shape
    if (ih, iw) == (height, width):
        return arr.copy()
    ys = np.clip((np.arange(height) + 0.5) * ih / height - 0.5, 0, ih - 1)
    xs = np.clip((np.arange(width) + 0.5) * iw / width - 0.5, 0, iw - 1)
    y0, x0 = np.floor(ys).astype(int), np.floor(xs).astype(int)
    y1, x1 = np.minimum(y0 + 1, ih - 1), np.minimum(x0 + 1, iw - 1)
    wy, wx = (ys - y0)[:, None], (xs - x0)[None, :]
    top = arr[y0][:, x0] * (1 - wx) + arr[y0][:, x1] * wx
    bot = arr[y1][:, x0] * (1 - wx) + arr[y1][:, x1] * wx
    return top * (1 - wy) + bot * wy

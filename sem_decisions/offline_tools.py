"""NIST detection_limits offline adapter and ARTIMAGEN launch configuration.

Neither upstream project is vendored, installed or executed here.

``offline_detection_curve`` reproduces the *analysis idea* (false-negative / false-positive
rates against an image-quality axis) in plain NumPy for offline evaluation. It may consume
mask-derived truth because it runs after evaluation is complete; it refuses a prospective
context, and its output says so. Reusing NIST code later requires keeping the NIST notice,
acknowledging NIST and marking modifications (see ``pins.PINS['detection_limits']``).

``artimagen_launch_config`` describes how a future isolated job would launch the pinned
ARTIMAGEN SEM image generator. Build prerequisites and runtime compatibility are NOT
verified; ARTIMAGEN is not a calibrated model of any specific commercial instrument, and a
simulation result is never physical-tool validation.
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from .pins import pin


def offline_detection_curve(records: Iterable[dict], *, quality_key: str, bins: list[float],
                            context: str) -> dict:
    """records: dicts with ``quality_key`` (measured), ``truth_defect`` (bool, from masks)
    and ``detected`` (bool). Invalid/unknown acquisitions must be excluded or counted upstream."""
    if context != "offline_evaluation":
        raise PermissionError("mask-dependent detection curves are offline-evaluation only")
    rows = list(records)
    edges = np.asarray(bins, float)
    if edges.ndim != 1 or edges.size < 2 or np.any(np.diff(edges) <= 0):
        raise ValueError("bins must be strictly increasing with at least two edges")
    out = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = [r for r in rows if lo <= float(r[quality_key]) < hi]
        pos = [r for r in sel if r["truth_defect"]]
        neg = [r for r in sel if not r["truth_defect"]]
        out.append({"bin": [float(lo), float(hi)], "n": len(sel), "n_truth_positive": len(pos),
                    "n_truth_negative": len(neg),
                    "false_negative_rate": None if not pos else sum(not r["detected"] for r in pos) / len(pos),
                    "false_positive_rate": None if not neg else sum(bool(r["detected"]) for r in neg) / len(neg)})
    return {"analysis": "fn_fp_vs_quality", "quality_key": quality_key, "bins": out,
            "prospective_use_allowed": False, "upstream_executed": False, "reference_pin": pin("detection_limits")}


def artimagen_launch_config(output_dir: str, *, seed: int, noise_levels: list[float],
                            contrast_levels: list[float], images_per_level: int) -> dict:
    if isinstance(images_per_level, bool) or not isinstance(images_per_level, int) or images_per_level <= 0:
        raise ValueError("images_per_level must be a positive integer")
    levels = list(noise_levels) + list(contrast_levels)
    if not noise_levels or not contrast_levels or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in levels):
        raise ValueError("noise/contrast levels must be non-empty lists of finite non-negative numbers")
    return {
        "tool": "ARTIMAGEN", "source_pin": pin("artimagen"), "execution": "not_run",
        "prerequisites_verified": False,
        "prerequisites": ["checkout of the pinned commit", "CMake and a C++ toolchain",
                          "libtiff", "fftw3", "lua5.3"],
        "prerequisites_source": "pinned upstream README.md / CMakeLists.txt; not installed or built here",
        "isolation": "separate process and output directory; outputs labeled simulated",
        "output_dir": output_dir, "seed": int(seed),
        "grid": {"noise_levels": list(map(float, noise_levels)), "contrast_levels": list(map(float, contrast_levels)),
                 "images_per_level": int(images_per_level)},
        "calibrated_instrument_model": False,
        "use": "noise/contrast stress and repeat-image policy tests; compare against equal-time single capture",
        "limitations": ["simulation validation is separate from physical-tool validation",
                        "repeated simulated captures are not independent physical observations"],
    }

"""Authored synthetic lot generator for the inspection review study v1.

Every lot is a deterministic function of (seed, scenario, protocol config) and the
frozen GENERATOR_ASSUMPTIONS below. Nothing here is calibrated against real sensor
data, SEM imagery, equipment logs or yield; all conditional feature signatures,
rates and detection limits are researcher-authored modelling assumptions.

Generation outline per lot (3 wafers x 1305 dies x 1 care site):

* layout stream: site layer and in-die care-site coordinate.
* latent stream: lot/chamber process stress (lot correlation), one spatial cluster
  per wafer, per-site defect class and latent optical signature.
* optical_sensor stream: measurement noise on the optical features and the process
  context sensor. Candidates are sites whose *measured* signal >= optical_threshold;
  no other feature overrides that rule.
* electrical stream: synthetic assumed potential electrical effect, sampled
  independently given DOI. Never a measured or corroborated yield outcome.
* review stream, keyed per attempt: precomputed review outcome for every site and
  attempt, so outcomes are identical regardless of policy or call order.

The policy-facing ``public`` dict carries no truth, review outcome or electrical
field. ``scenario`` and ``seed`` in it are evaluation metadata a runner must strip
before handing observations to a policy; ``lot_id`` and ``site_ids`` are opaque
(hashed lot id plus wafer/die grid) and do not spell out either.
"""
from __future__ import annotations

import hashlib
import json
import math
import zlib
from pathlib import Path

import numpy as np

PROTOCOL_PATH = Path(__file__).with_name("protocol.json")

FEATURES = ("signal", "size", "texture", "design_delta", "process_context", "radius", "layer")
OPTICAL_FEATURES = FEATURES[:4]
SCENARIOS = ("stationary", "novel_cluster", "low_contrast", "nuisance_heavy", "process_shift")
KNOWN_KINDS = ("particle", "bridge", "scratch", "void")
DOI_KINDS = KNOWN_KINDS + ("novel",)
KINDS = ("none", "benign") + DOI_KINDS
REVIEW_STATUSES = ("ok", "failure", "missing")

# Frozen before the first held-out campaign. Every constant that influences generation
# and is not in protocol.json lives here; changing any value is a generator change.
GENERATOR_ASSUMPTIONS = {
    "version": "inspection-generator-v1",
    "rng_salt": 20261004,
    "streams": {"layout": 1, "latent": 2, "optical_sensor": 3, "electrical": 4, "review": 5},
    "coordinate_normalization": "xy = (die center + in-die offset) / (wafer_diameter_mm / 2)",
    "die_corner_rule": "die kept when hypot(|x|+die_w/2, |y|+die_h/2) <= radius - edge_exclusion",
    "care_site_margin_mm": 0.5,
    "layer1_probability": 0.5,
    # Lot correlation: shared lot stress plus per-wafer chamber stress raise DOI rate.
    "lot_stress_sd": 0.35,
    "chamber_count": 2,
    "chamber_stress_sd": 0.25,
    "stress_rate_coefficient": 0.6,
    "edge_rate_gain": 0.6,
    "edge_rate_power": 4,
    "max_site_defect_probability": 0.95,
    "cluster_center_max_radius": 0.75,
    "cluster_kind_rule": "one known kind per wafer, uniform; all cluster excess known DOI take it",
    "base_kind_weights_by_layer": {
        "0": {"particle": 0.40, "bridge": 0.15, "scratch": 0.25, "void": 0.20},
        "1": {"particle": 0.25, "bridge": 0.35, "scratch": 0.15, "void": 0.25},
    },
    "nuisance_rule": "non-physical sites become optical nuisance with scenario nuisance rate; kind stays 'none'",
    # Process context sensor: stress + radial term + noise (+ shift in process_shift).
    "context_radius_coefficient": 0.15,
    "context_noise_sd": 0.10,
    "process_shift_context_offset": 0.6,
    # Latent optical signatures: mean and spread of (signal, size, texture, design_delta).
    "optical_background_signal": 0.20,
    "lot_signal_gain_sd": 0.03,
    "signatures": {
        "none":     {"mean": [0.20, 0.10, 0.30, 0.10], "sd": [0.10, 0.06, 0.10, 0.06]},
        "nuisance": {"mean": [0.66, 0.25, 0.65, 0.15], "sd": [0.10, 0.10, 0.12, 0.08]},
        "benign":   {"mean": [0.68, 0.45, 0.40, 0.10], "sd": [0.10, 0.12, 0.10, 0.08]},
        "particle": {"mean": [0.95, 0.55, 0.35, 0.25], "sd": [0.12, 0.15, 0.10, 0.10]},
        "bridge":   {"mean": [0.85, 0.30, 0.25, 0.70], "sd": [0.12, 0.10, 0.10, 0.12]},
        "scratch":  {"mean": [0.90, 0.75, 0.55, 0.30], "sd": [0.12, 0.15, 0.12, 0.10]},
        "void":     {"mean": [0.78, 0.35, 0.20, 0.50], "sd": [0.12, 0.12, 0.08, 0.12]},
        "novel":    {"mean": [0.80, 0.40, 0.75, 0.55], "sd": [0.12, 0.12, 0.12, 0.12]},
    },
    "low_contrast_rule": "DOI latent signal = background + low_contrast_factor * (signal - background)",
    "process_shift_rule": "DOI latent signal += process_shift_offset; context += process_shift_context_offset",
    # Modeled review mechanism limits (assumptions, not external facts).
    "review_size_detection_limit": 0.15,
    "review_below_limit_sensitivity_factor": 0.6,
    "review_kind_report_accuracy": 0.88,
    "review_misreport_rule": "wrong reports pick uniformly among known kinds other than the true kind",
    "review_false_positive_kind_rule": "uniform among known kinds",
    "review_quality_ok": {"base": 0.55, "size_gain": 0.35, "sd": 0.12},
    "review_quality_not_ok": {"mean": 0.25, "sd": 0.10},
}


def load_config(path=None):
    path = PROTOCOL_PATH if path is None else Path(path)
    with open(path, encoding="utf-8") as handle:
        config = json.load(handle)
    if tuple(config["features"]) != FEATURES:
        raise ValueError("protocol feature order does not match generator FEATURES")
    return config


def lot_id(seed, scenario):
    """Opaque, stable lot identifier; does not spell out scenario or seed."""
    digest = hashlib.sha256(f"{int(seed)}/{scenario}".encode("utf-8")).hexdigest()
    return f"lot-{digest[:16]}"


def review_attempts(config):
    return int(config["cost"]["retry_limit"]) + 1


def _check_lot_request(seed, scenario, config):
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or seed < 0:
        raise ValueError("seed must be a non-negative int")
    splits = config["splits"]
    if scenario not in splits["test_seeds_by_scenario"]:
        raise ValueError(f"unknown scenario {scenario!r}")
    if seed in splits["train_seeds"] or seed in splits["validation_seeds"]:
        if scenario != "stationary":
            raise ValueError("train/validation lots are stationary only")
    for name, seeds in splits["test_seeds_by_scenario"].items():
        if seed in seeds and name != scenario:
            raise ValueError(f"seed {seed} belongs to test scenario {name!r}")


def _rng(seed, scenario, stream, *extra):
    entropy = [GENERATOR_ASSUMPTIONS["rng_salt"], int(seed), zlib.crc32(scenario.encode()),
               GENERATOR_ASSUMPTIONS["streams"][stream], *extra]
    return np.random.default_rng(np.random.SeedSequence(entropy))


def die_positions(config):
    """Die centers (row, col, x_mm, y_mm) for one wafer with all corners inside the limit."""
    g = config["geometry"]
    radius = g["wafer_diameter_mm"] / 2
    limit = radius - g["edge_exclusion_mm"]
    pitch_x = g["die_width_mm"] + g["scribe_mm"]
    pitch_y = g["die_height_mm"] + g["scribe_mm"]
    half_w, half_h = g["die_width_mm"] / 2, g["die_height_mm"] / 2
    cmax, rmax = int(limit // pitch_x) + 1, int(limit // pitch_y) + 1
    out = []
    for r in range(-rmax, rmax + 1):
        for c in range(-cmax, cmax + 1):
            x, y = c * pitch_x, r * pitch_y
            if math.hypot(abs(x) + half_w, abs(y) + half_h) <= limit:
                out.append((r, c, x, y))
    return out


def generate_lot(seed, scenario, config):
    _check_lot_request(seed, scenario, config)
    A = GENERATOR_ASSUMPTIONS
    g, gen = config["geometry"], config["generator"]
    seed = int(seed)
    lid = lot_id(seed, scenario)
    radius_mm = g["wafer_diameter_mm"] / 2
    n_wafers = int(g["wafers_per_lot"])
    dies = die_positions(config)
    per_wafer = len(dies)
    n = per_wafer * n_wafers

    wafer = np.repeat(np.arange(n_wafers), per_wafer)
    die_xy = np.tile(np.array([[d[2], d[3]] for d in dies], dtype=float), (n_wafers, 1))
    site_ids = [f"{lid}:w{w}:r{r}:c{c}" for w in range(n_wafers) for (r, c, _, _) in dies]

    # Layout stream.
    rng = _rng(seed, scenario, "layout")
    layer = (rng.random(n) < A["layer1_probability"]).astype(np.int64)
    half = np.array([g["die_width_mm"] / 2, g["die_height_mm"] / 2]) - A["care_site_margin_mm"]
    in_die = rng.uniform(-1.0, 1.0, size=(n, 2)) * half
    xy = (die_xy + in_die) / radius_mm
    radius = np.hypot(xy[:, 0], xy[:, 1])

    # Latent stream: drawn in a fixed order regardless of scenario.
    rng = _rng(seed, scenario, "latent")
    lot_stress = rng.normal(0.0, A["lot_stress_sd"])
    chamber_stress = rng.normal(0.0, A["chamber_stress_sd"], size=A["chamber_count"])
    chamber = rng.integers(0, A["chamber_count"], size=n_wafers)
    c_r = A["cluster_center_max_radius"] * np.sqrt(rng.random(n_wafers))
    c_t = 2 * np.pi * rng.random(n_wafers)
    centers = np.stack([c_r * np.cos(c_t), c_r * np.sin(c_t)], axis=1)
    cluster_kind = rng.integers(0, len(KNOWN_KINDS), size=n_wafers)
    lot_gain = rng.normal(0.0, A["lot_signal_gain_sd"])
    u_doi, u_kind, u_benign, u_nuis = rng.random((4, n))
    z_feat = rng.standard_normal((n, 4))

    stress = lot_stress + chamber_stress[chamber][wafer]
    in_cluster = np.hypot(*(xy - centers[wafer]).T) <= gen["cluster_radius_normalized"]
    rate_mult = np.exp(A["stress_rate_coefficient"] * stress) * (1 + A["edge_rate_gain"] * radius ** A["edge_rate_power"])
    p_base = gen["base_doi_rate"] * rate_mult
    p_cluster = np.where(in_cluster, gen["cluster_excess_doi_rate"], 0.0)
    p_novel = np.where(in_cluster & (scenario == "novel_cluster"), gen["novel_cluster_excess_rate"], 0.0)
    total = p_base + p_cluster + p_novel
    shrink = np.minimum(1.0, A["max_site_defect_probability"] / total)
    p_base, p_cluster, p_novel = p_base * shrink, p_cluster * shrink, p_novel * shrink

    weights = np.array([[A["base_kind_weights_by_layer"][str(l)][k] for k in KNOWN_KINDS] for l in (0, 1)])
    cum = np.cumsum(weights / weights.sum(axis=1, keepdims=True), axis=1)
    base_kind = np.minimum((u_kind[:, None] >= cum[layer]).sum(axis=1), len(KNOWN_KINDS) - 1)
    kind_idx = np.zeros(n, dtype=np.int64)  # index into KINDS
    is_base = u_doi < p_base
    is_cluster = ~is_base & (u_doi < p_base + p_cluster)
    is_novel = ~is_base & ~is_cluster & (u_doi < p_base + p_cluster + p_novel)
    kind_idx[is_base] = 2 + base_kind[is_base]
    kind_idx[is_cluster] = 2 + cluster_kind[wafer][is_cluster]
    kind_idx[is_novel] = KINDS.index("novel")
    doi = kind_idx >= 2
    benign = ~doi & (u_benign < gen["benign_physical_rate"])
    kind_idx[benign] = KINDS.index("benign")
    nuisance_rate = gen["nuisance_heavy_rate"] if scenario == "nuisance_heavy" else gen["nuisance_rate"]
    nuisance = ~doi & ~benign & (u_nuis < nuisance_rate)

    sig_names = ["none", "nuisance"] + list(KINDS[1:])
    sig_idx = np.where(nuisance, 1, np.where(kind_idx == 0, 0, kind_idx + 1))
    means = np.array([A["signatures"][s]["mean"] for s in sig_names])
    sds = np.array([A["signatures"][s]["sd"] for s in sig_names])
    latent = means[sig_idx] + sds[sig_idx] * z_feat
    bg = A["optical_background_signal"]
    if scenario == "low_contrast":
        latent[doi, 0] = bg + gen["low_contrast_factor"] * (latent[doi, 0] - bg)
    if scenario == "process_shift":
        latent[doi, 0] += gen["process_shift_offset"]
    latent[:, 0] += lot_gain

    # Optical sensor stream: measurement noise and context sensor.
    rng = _rng(seed, scenario, "optical_sensor")
    measured = latent + rng.normal(0.0, gen["feature_noise_sd"], size=(n, 4))
    context = stress + A["context_radius_coefficient"] * radius + rng.normal(0.0, A["context_noise_sd"], size=n)
    if scenario == "process_shift":
        context = context + A["process_shift_context_offset"]
    candidate = measured[:, 0] >= gen["optical_threshold"]
    features = np.empty((n, len(FEATURES)), dtype=float)
    features[:, :4] = np.where(candidate[:, None], measured, np.nan)
    features[:, 4] = context
    features[:, 5] = radius
    features[:, 6] = layer

    # Electrical stream: synthetic assumed potential effect, independent given DOI.
    rng = _rng(seed, scenario, "electrical")
    electrical = doi & (rng.random(n) < gen["electrical_effect_probability_given_doi"])

    kind = np.array(KINDS, dtype=object)[kind_idx].astype(str)
    review = _review_outcomes(seed, scenario, config, doi, kind, latent[:, 1])

    public = {
        "lot_id": lid, "seed": seed, "scenario": scenario, "site_ids": site_ids,
        "wafer": wafer, "xy": xy, "die_xy_mm": die_xy, "in_die_xy_mm": in_die,
        "layer": layer, "candidate": candidate, "features": features,
    }
    oracle = {
        "doi": doi, "physical": doi | benign, "kind": kind, "electrical_effect": electrical,
        **review,
    }
    return {"public": public, "oracle": oracle}


def _review_outcomes(seed, scenario, config, doi, kind, latent_size):
    A, gen = GENERATOR_ASSUMPTIONS, config["generator"]
    n, attempts = len(doi), review_attempts(config)
    sens = np.array([gen["review_sensitivity"].get(k, 0.0) for k in kind])
    sens = np.where(latent_size < A["review_size_detection_limit"],
                    sens * A["review_below_limit_sensitivity_factor"], sens)
    p_pos = np.where(doi, sens, gen["review_false_positive_rate"])
    true_known = np.array([KNOWN_KINDS.index(k) if k in KNOWN_KINDS else -1 for k in kind])
    size01 = np.clip(latent_size, 0.0, 1.0)
    q_ok, q_bad = A["review_quality_ok"], A["review_quality_not_ok"]
    known = np.array(KNOWN_KINDS)

    status = np.empty((n, attempts), dtype="<U7")
    positive = np.zeros((n, attempts), dtype=bool)
    reported = np.full((n, attempts), "", dtype="<U8")
    quality = np.zeros((n, attempts), dtype=float)
    for a in range(attempts):
        rng = _rng(seed, scenario, "review", a)
        u_fail, u_miss, u_det, u_acc, u_alt = rng.random((5, n))
        z_q = rng.standard_normal(n)
        failed = u_fail < gen["review_failure_rate"]
        missing = ~failed & (u_miss < gen["review_missing_rate"])
        ok = ~failed & ~missing
        status[:, a] = np.where(failed, "failure", np.where(missing, "missing", "ok"))
        pos = ok & (u_det < p_pos)
        positive[:, a] = pos
        # Wrong report: one of the known kinds other than the true one (all four for novel).
        alt_slots = np.where(true_known >= 0, len(KNOWN_KINDS) - 1, len(KNOWN_KINDS))
        alt = np.minimum((u_alt * alt_slots).astype(int), alt_slots - 1)
        alt = np.where((true_known >= 0) & (alt >= true_known), alt + 1, alt)
        correct = u_acc < A["review_kind_report_accuracy"]
        doi_report = np.where(correct, kind, known[alt])
        fp_report = known[np.minimum((u_alt * len(KNOWN_KINDS)).astype(int), len(KNOWN_KINDS) - 1)]
        reported[:, a] = np.where(pos, np.where(doi, doi_report, fp_report), "")
        quality[:, a] = np.clip(np.where(
            ok, q_ok["base"] + q_ok["size_gain"] * size01 + q_ok["sd"] * z_q,
            q_bad["mean"] + q_bad["sd"] * z_q), 0.0, 1.0)
    return {"review_status": status, "review_positive": positive,
            "review_kind": reported, "review_quality": quality}


def review_observation(oracle, index, attempt):
    """Observable result of one review attempt at one site. No truth or electrical field."""
    status_table = oracle["review_status"]
    if isinstance(index, bool) or not isinstance(index, (int, np.integer)) or not 0 <= index < status_table.shape[0]:
        raise IndexError("site index out of range")
    if isinstance(attempt, bool) or not isinstance(attempt, (int, np.integer)) or not 0 <= attempt < status_table.shape[1]:
        raise IndexError("review attempt out of range")
    status = str(status_table[index, attempt])
    quality = float(oracle["review_quality"][index, attempt])
    if status != "ok":
        return {"status": status, "reported_doi": None, "reported_kind": None, "quality": quality}
    positive = bool(oracle["review_positive"][index, attempt])
    kind = str(oracle["review_kind"][index, attempt]) if positive else None
    return {"status": "ok", "reported_doi": positive, "reported_kind": kind, "quality": quality}

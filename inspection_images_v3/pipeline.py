"""train -> freeze -> verify -> single-shot evaluate for the preregistered VisA PCB3 replication.

Every design constant is the fixed v2 PCB2 pipeline; only the category, seed and study change.

``train`` reads official normal-train images only, using one memory/calibration split for
every method, and writes per-feature-set ``memory_<set>.npz`` plus ``freeze.json``.
``evaluate`` refuses unless every frozen input still matches, writes the single-shot marker,
scores the frozen test identities, persists the scores and only then reads test labels.
"""

import datetime
import hashlib
import importlib.metadata
import io
import json
import pathlib
import platform
import sys

import numpy as np

from inspection_images import core
from inspection_images.data import sha256_file, split_identities, test_labels, write_json
from inspection_images_v2 import scoring  # frozen, read-only reuse

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
PROTOCOL_DOC = REPO_ROOT / "IMAGE_PILOT_V3_PROTOCOL.md"
# Read-only v1/v2 files imported (directly, transitively or by the CLI extractor) plus the frozen v2
# reference files; each must match its protocol pin (LF-normalized sha256).
FROZEN_DEPENDENCIES = ["inspection_images/__init__.py", "inspection_images/core.py", "inspection_images/data.py",
                       "inspection_images/features.py", "inspection_images/requirements-cpu.txt",
                       "inspection_images_v2/__init__.py", "inspection_images_v2/scoring.py",
                       "inspection_images_v2/pipeline.py", "inspection_images_v2/cli.py",
                       "inspection_images_v2/protocol.json", "IMAGE_PILOT_V2_PROTOCOL.md"]
LIMITATIONS = [
    "Separate VisA PCB3 external-category replication; not wafer/SEM/factory accuracy and not tabular policy evidence.",
    "Not hyperparameter optimization: methods, feature sets and calibration quantile are the v2 PCB2 pipeline, selected for replication because of the prior PCB2 result.",
    "PCB1 v1 and PCB2 v2 test sets are consumed; their outcomes stand and nothing here was tuned on them or on PCB3 test data.",
    "primary448 changes resolution, grid, coreset size and score aggregation together; any difference is a composite pipeline effect, not a resolution effect.",
    "Images are resized whole with aspect-ratio distortion and no crop.",
    "Thresholds are each method's 95th percentile of held-out official normal-train calibration scores; no test data tunes them.",
    "Image AUROC/AP are secondary ranking statistics, never accuracy; highres448max is descriptive only.",
    "Single category, single seed, official PCB3 1cls test images (class counts read only after scoring).",
]


def load_protocol():
    return json.loads((PACKAGE_DIR / "protocol.json").read_text(encoding="utf-8"))


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical_digest(payload):
    body = {k: v for k, v in payload.items() if k != "freeze_digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_files():
    files = sorted(PACKAGE_DIR.glob("*.py")) + sorted(PACKAGE_DIR.glob("*.json"))
    files += [PROTOCOL_DOC] + [REPO_ROOT / rel for rel in FROZEN_DEPENDENCIES]
    missing = [f.as_posix() for f in files if not f.is_file()]
    if missing:
        raise RuntimeError(f"missing source/dependency files: {missing}")
    return files


def source_hashes():
    # LF-normalized so a CRLF working copy hashes like the committed blob.
    return {f.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for f in source_files()}


def check_frozen_dependencies(protocol, hashes):
    pins = protocol["frozen_dependency_sha256"]
    if set(pins) != set(FROZEN_DEPENDENCIES):
        return ["frozen dependency pin set differs"]
    return [f"frozen dependency {rel} differs from its pin" for rel in FROZEN_DEPENDENCIES if hashes.get(rel) != pins[rel]]


def check_environment_pins(protocol, env):
    pins = protocol["environment_pins"]
    versions = dict(p.split("==", 1) for p in env.get("packages", []) if "==" in p)
    found = {"python": env.get("python"), "numpy": versions.get("numpy"), "torch": versions.get("torch")}
    return [f"environment {k} is {found.get(k)!r}, pinned {v!r}" for k, v in pins.items() if found.get(k) != v]


def environment():
    dists = sorted(f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions())
    return {"python": sys.version.split()[0], "platform": platform.platform(), "packages": dists}


class Progress:
    def __init__(self, root):
        self.path = pathlib.Path(root) / "progress.json"
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"events": []}

    def mark(self, stage, **detail):
        self.state["events"].append({"stage": stage, "at": now(), **detail})
        self.state["stage"] = stage
        write_json(self.path, self.state)


def _npz_bytes(**arrays):
    buffer = io.BytesIO()
    np.savez(buffer, **arrays)
    return buffer.getvalue()


def _image_hashes(data_dir, ids):
    return {i: sha256_file(pathlib.Path(data_dir) / i) for i in ids}


def _model_identity(extractor):
    return {"extractor": extractor.identity(), "resize": list(getattr(extractor, "cfg", {}).get("resize", []))}


def _extract(extractor, data_dir, ids, grid):
    x = extractor.extract([pathlib.Path(data_dir) / i for i in ids])
    if x.ndim != 3 or x.shape[0] != len(ids) or x.shape[1] != grid[0] * grid[1]:
        raise RuntimeError(f"descriptor shape {x.shape} does not match {len(ids)} images on a {grid} grid")
    return x


def _methods_for(protocol, feature_set):
    return {m: cfg for m, cfg in protocol["methods"].items() if cfg["feature_set"] == feature_set}


def _check_split(protocol, train_ids, test_ids):
    expected = protocol["dataset"]["expected_counts"]
    if len(train_ids) != expected["train_normal"] or len(test_ids) != expected["test_total_unlabeled"]:
        raise RuntimeError(f"split counts {len(train_ids)}/{len(test_ids)} differ from protocol expectation")


def train(root, data_dir, csv_path, extractors, protocol=None):
    """Fit per-feature-set memory, calibrate per-method thresholds on normal-train only, then freeze."""
    protocol = protocol or load_protocol()
    root = pathlib.Path(root)
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"output root {root} is not empty; train never resumes or reuses a root")
    if set(extractors) != set(protocol["feature_sets"]):
        raise RuntimeError("extractors must cover exactly the protocol feature sets")
    sources = source_hashes()
    env = environment()
    problems = check_frozen_dependencies(protocol, sources) + check_environment_pins(protocol, env)
    if problems:
        raise RuntimeError("train refused: " + "; ".join(problems))
    root.mkdir(parents=True, exist_ok=True)
    progress = Progress(root)
    progress.mark("started", study=protocol["study"])
    category, seed = protocol["dataset"]["category"], protocol["seed"]
    csv_sha = sha256_file(csv_path)
    if csv_sha != protocol["dataset"]["split_csv_sha256"]:
        raise RuntimeError("split CSV hash differs from protocol")
    train_ids, test_ids = split_identities(csv_path, category)
    _check_split(protocol, train_ids, test_ids)
    memory_ids, calibration_ids = core.calibration_split(train_ids, protocol["calibration_fraction"], seed)
    if set(test_ids) & (set(memory_ids) | set(calibration_ids)):
        raise RuntimeError("test identity leaked into memory/calibration")
    progress.mark("split", memory=len(memory_ids), calibration=len(calibration_ids), test_unlabeled=len(test_ids))

    chunk, pct = protocol["distance_chunk"], protocol["threshold_percentile"]
    artifacts, cal_scores, models = {}, {}, {}
    for name, fs in sorted(protocol["feature_sets"].items()):
        extractor = extractors[name]
        models[name] = _model_identity(extractor)
        mem = np.array(_extract(extractor, data_dir, memory_ids, fs["grid"]), dtype=np.float32)
        mean, std = core.normalization_stats(mem)
        # In place; identical float32 arithmetic to core.normalize without a second copy.
        mem -= mean.astype(np.float32)
        mem /= std.astype(np.float32)
        n, p, d = mem.shape
        flat = mem.reshape(n * p, d)
        reservoir = core.reservoir_indices(len(flat), protocol["reservoir_max"], seed)
        sample = flat[reservoir]
        del mem, flat
        picked = core.greedy_coreset(sample, fs["coreset_size"], protocol["projection_dim"], seed)
        bank = sample[picked]
        progress.mark("memory", feature_set=name, descriptors=int(n * p), reservoir=int(len(reservoir)),
                      coreset=int(len(bank)))
        cal = core.normalize(_extract(extractor, data_dir, calibration_ids, fs["grid"]), mean, std)
        dist = scoring.patch_distances(cal, bank, chunk)
        for method, cfg in _methods_for(protocol, name).items():
            cal_scores[method] = scoring.aggregate(dist, cfg["aggregation"], cfg["top_k"])
        blob = _npz_bytes(mean=mean, std=std, bank=bank, reservoir=reservoir, coreset=picked)
        artifact = f"memory_{name}.npz"
        (root / artifact).write_bytes(blob)
        artifacts[name] = {"artifact": artifact, "sha256": hashlib.sha256(blob).hexdigest(), "descriptor_dim": int(d),
                           "patches_per_image": int(p), "train_descriptors": int(n * p),
                           "reservoir": int(len(reservoir)), "coreset": int(len(bank))}
        progress.mark("calibrated_feature_set", feature_set=name)

    if set(cal_scores) != set(protocol["methods"]):
        raise RuntimeError("not every protocol method was calibrated")
    thresholds = {m: core.threshold(s, pct) for m, s in cal_scores.items()}
    progress.mark("calibrated", thresholds=thresholds)
    freeze = {
        "schema_version": 1,
        "study": protocol["study"],
        "frozen_at": now(),
        "protocol": protocol,
        "sources": sources,
        "environment": env,
        "model": models,
        "data": {"category": category, "split_csv_sha256": csv_sha,
                 "train_image_sha256": _image_hashes(data_dir, train_ids),
                 "test_image_sha256": _image_hashes(data_dir, test_ids)},
        "splits": {"memory": memory_ids, "calibration": calibration_ids, "test_unlabeled": test_ids,
                   "shared_across_methods": True},
        "test_usage": {"scored": False, "labels_read": False, "in_memory": False, "in_calibration": False,
                       "in_normalization": False, "in_thresholds": False,
                       "only_bytes_hashed": "test image sha256 recorded for tamper detection"},
        "memory": artifacts,
        "calibration": {"percentile": pct, "thresholds": thresholds,
                        "scores": {m: dict(zip(calibration_ids, map(float, s))) for m, s in cal_scores.items()}},
        "primary_endpoint": protocol["primary_endpoint"],
        "limitations": LIMITATIONS,
    }
    freeze["freeze_digest"] = canonical_digest(freeze)
    write_json(root / "freeze.json", freeze)
    progress.mark("frozen", freeze_digest=freeze["freeze_digest"])
    return freeze


def verify_freeze(root, data_dir, csv_path, extractors):
    """Raise if anything that defines the frozen experiment changed."""
    root = pathlib.Path(root)
    path = root / "freeze.json"
    if not path.exists():
        raise RuntimeError("no freeze.json; evaluation refused")
    freeze = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    try:
        if freeze.get("freeze_digest") != canonical_digest(freeze):
            problems.append("freeze digest mismatch")
        protocol = load_protocol()
        if freeze.get("protocol") != protocol:
            problems.append("protocol differs")
        try:
            current = source_hashes()
            if freeze.get("sources") != current:
                problems.append("source/protocol/dependency file hashes differ")
            problems += check_frozen_dependencies(protocol, current)
        except RuntimeError as exc:
            problems.append(str(exc))
        env = environment()
        if freeze.get("environment") != env:
            problems.append("Python environment differs")
        problems += check_environment_pins(protocol, env)
        if set(extractors) != set(protocol["feature_sets"]) or set(freeze["memory"]) != set(protocol["feature_sets"]):
            problems.append("feature sets differ")
        else:
            for name, art in freeze["memory"].items():
                file = root / art["artifact"]
                if not file.is_file() or sha256_file(file) != art["sha256"]:
                    problems.append(f"memory artifact {name} hash mismatch")
                if _model_identity(extractors[name]) != freeze["model"].get(name):
                    problems.append(f"model identity {name} differs")
        if sha256_file(csv_path) != freeze["data"]["split_csv_sha256"]:
            problems.append("split CSV hash mismatch")
        train_ids, test_ids = split_identities(csv_path, freeze["data"]["category"])
        splits = freeze["splits"]
        if sorted(splits["memory"] + splits["calibration"]) != train_ids or set(splits["memory"]) & set(splits["calibration"]):
            problems.append("train/calibration identities differ")
        if splits["test_unlabeled"] != test_ids or set(test_ids) & set(train_ids):
            problems.append("test identities differ or overlap train")
        if _image_hashes(data_dir, test_ids) != freeze["data"]["test_image_sha256"]:
            problems.append("test image bytes differ")
        if _image_hashes(data_dir, train_ids) != freeze["data"]["train_image_sha256"]:
            problems.append("train image bytes differ")
        cal = freeze["calibration"]
        if set(cal["thresholds"]) != set(protocol["methods"]):
            problems.append("threshold methods differ")
        for method, scores in cal["scores"].items():
            if sorted(scores) != splits["calibration"] or \
                    core.threshold(list(scores.values()), cal["percentile"]) != cal["thresholds"].get(method):
                problems.append(f"threshold {method} is not the calibration-only percentile")
    except (KeyError, TypeError, ValueError, OSError) as exc:
        problems.append(f"malformed freeze or missing input: {type(exc).__name__}: {exc}")
    if problems:
        raise RuntimeError("freeze verification failed: " + "; ".join(problems))
    return freeze


def _threshold_metrics(scores, y, thr):
    c = core.confusion(scores, y, thr)
    return {**c, "predicted": (np.asarray(scores) > thr).astype(int)}


def primary_decision(baseline, primary, n_anomaly, n_normal, bootstrap):
    """Preregistered rule evaluated with exact integer counts (no float edge effects)."""
    gain_ok = 10 * (primary["true_positive"] - baseline["true_positive"]) >= n_anomaly
    ci_ok = bootstrap["defect_recall_gain"]["ci95"][0] > 0
    far_ok = 10 * primary["false_positive"] <= n_normal
    return {"recall_gain_at_least_10pp": bool(gain_ok), "bootstrap_ci_lower_above_zero": bool(ci_ok),
            "primary_far_at_most_10pct": bool(far_ok), "success": bool(gain_ok and ci_ok and far_ok)}


def evaluate(root, data_dir, csv_path, extractors):
    root = pathlib.Path(root)
    for name in ("evaluation.json", "evaluation.started", "test-scores.json"):
        if (root / name).exists():
            raise RuntimeError(f"{name} exists; evaluation is single-shot and never resumed")
    freeze = verify_freeze(root, data_dir, csv_path, extractors)
    protocol = freeze["protocol"]
    progress = Progress(root)
    (root / "evaluation.started").write_text(json.dumps({"at": now(), "freeze_digest": freeze["freeze_digest"]}),
                                             encoding="utf-8")
    progress.mark("evaluation_started", freeze_digest=freeze["freeze_digest"])
    test_ids = freeze["splits"]["test_unlabeled"]
    chunk = protocol["distance_chunk"]
    scores = {}
    for name, fs in sorted(protocol["feature_sets"].items()):
        with np.load(root / freeze["memory"][name]["artifact"]) as art:
            mean, std, bank = art["mean"], art["std"], art["bank"]
        x = core.normalize(_extract(extractors[name], data_dir, test_ids, fs["grid"]), mean, std)
        dist = scoring.patch_distances(x, bank, chunk)
        for method, cfg in _methods_for(protocol, name).items():
            scores[method] = scoring.aggregate(dist, cfg["aggregation"], cfg["top_k"])
    score_doc = {"freeze_digest": freeze["freeze_digest"], "scored_at": now(),
                 "scores": {m: dict(zip(test_ids, map(float, s))) for m, s in sorted(scores.items())}}
    write_json(root / "test-scores.json", score_doc)
    scores_sha = sha256_file(root / "test-scores.json")
    progress.mark("scored", images=len(test_ids), test_scores_sha256=scores_sha)

    labels_by_id = test_labels(csv_path, freeze["data"]["category"])  # first label read
    if set(labels_by_id) != set(test_ids):
        raise RuntimeError("test label identities differ from frozen test identities")
    y = np.array([labels_by_id[i] for i in test_ids])
    n_anomaly, n_normal = int(y.sum()), int(len(y) - y.sum())
    methods, preds = {}, {}
    for method, cfg in protocol["methods"].items():
        s = scores[method]
        metrics = _threshold_metrics(s, y, freeze["calibration"]["thresholds"][method])
        preds[method] = metrics.pop("predicted")
        methods[method] = {"role": cfg["role"], "label": cfg["label"], "frozen_threshold": metrics,
                           "image_auroc_secondary": core.auroc(s, y),
                           "image_average_precision_secondary": core.average_precision(s, y)}
    endpoint = protocol["primary_endpoint"]
    boot = endpoint["bootstrap"]
    bootstrap = scoring.paired_bootstrap(preds["primary448"], preds["baseline224"], y, boot["resamples"],
                                         boot["seed_stream"])
    descriptive = scoring.paired_bootstrap(preds["highres448max"], preds["baseline224"], y, boot["resamples"],
                                           boot["seed_stream"])
    decision = primary_decision(methods["baseline224"]["frozen_threshold"], methods["primary448"]["frozen_threshold"],
                                n_anomaly, n_normal, bootstrap)
    result = {
        "schema_version": 1, "study": freeze["study"], "evaluated_at": now(),
        "freeze_digest": freeze["freeze_digest"], "test_scores_sha256": scores_sha, "data_mode": "real",
        "counts": {"test_images": len(test_ids), "anomaly": n_anomaly, "normal": n_normal},
        "primary_endpoint": {"definition": endpoint, "primary_vs_baseline_bootstrap": bootstrap, "decision": decision},
        "secondary_descriptive": {"highres448max_vs_baseline224_bootstrap": descriptive},
        "metric_notes": ["Primary endpoint fixed before test scoring; it is never swapped after evaluation.",
                         "AUROC and AP are secondary ranking metrics, not accuracy.",
                         "Rates are counts over the official per-class test images; report them as such, not with extra precision."],
        "methods": methods,
        "images": [{"image": i, "label": int(l), **{f"{m}_score": float(scores[m][k]) for m in sorted(scores)}}
                   for k, (i, l) in enumerate(zip(test_ids, y))],
        "limitations": freeze["limitations"],
    }
    write_json(root / "evaluation.json", result)
    progress.mark("evaluated")
    return result

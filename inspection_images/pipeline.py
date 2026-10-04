"""train -> freeze -> evaluate for the VisA PCB1 image pilot.

``train`` touches official normal-train images only and writes ``freeze.json`` plus
``memory.npz``. ``evaluate`` refuses to run unless the freeze, artifacts, sources, protocol,
split file, model weights and test image bytes all match; only then are test images scored,
and test labels are read last.
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

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
PROTOCOL_DOC = REPO_ROOT / "IMAGE_PILOT_PROTOCOL.md"
LIMITATIONS = [
    "Separate VisA PCB1 image feasibility pilot; not wafer/SEM accuracy and not tabular policy evidence.",
    "Image AUROC/AP are ranking statistics, not accuracy.",
    "Threshold is the 95th percentile of held-out official normal-train calibration scores; no test data tunes it.",
    "PatchCore-inspired adaptation (ResNet18, 14x14 grid, 1024 coreset, 16-d projection); not the published PatchCore configuration.",
    "Single category, single seed, 100 normal + 100 anomaly official test images; no confidence intervals.",
]


def load_protocol():
    return json.loads((PACKAGE_DIR / "protocol.json").read_text(encoding="utf-8"))


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def canonical_digest(payload):
    body = {k: v for k, v in payload.items() if k != "freeze_digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def source_hashes():
    files = sorted(PACKAGE_DIR.glob("*.py")) + sorted(PACKAGE_DIR.glob("*.json")) + sorted(PACKAGE_DIR.glob("*.txt"))
    if PROTOCOL_DOC.exists():
        files.append(PROTOCOL_DOC)
    # LF-normalized so a CRLF working copy hashes like the committed blob.
    return {f.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for f in files}


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


def train(root, data_dir, csv_path, extractor, protocol=None):
    """Fit memory and calibrate thresholds on official normal-train images, then freeze."""
    protocol = protocol or load_protocol()
    root = pathlib.Path(root)
    if root.exists() and any(root.iterdir()):
        raise RuntimeError(f"output root {root} is not empty; train never resumes or reuses a root")
    root.mkdir(parents=True, exist_ok=True)
    progress = Progress(root)
    progress.mark("started", study=protocol["study"])
    category, seed = protocol["dataset"]["category"], protocol["seed"]
    csv_sha = sha256_file(csv_path)
    if csv_sha != protocol["dataset"]["split_csv_sha256"]:
        raise RuntimeError("split CSV hash differs from protocol")
    train_ids, test_ids = split_identities(csv_path, category)
    memory_ids, calibration_ids = core.calibration_split(train_ids, protocol["calibration_fraction"], seed)
    progress.mark("split", memory=len(memory_ids), calibration=len(calibration_ids), test_unlabeled=len(test_ids))

    data_dir = pathlib.Path(data_dir)
    mem_raw = extractor.extract([data_dir / i for i in memory_ids])
    mean, std = core.normalization_stats(mem_raw)
    mem = core.normalize(mem_raw, mean, std)
    del mem_raw
    n, p, d = mem.shape
    flat = mem.reshape(n * p, d)
    reservoir = core.reservoir_indices(len(flat), protocol["memory"]["reservoir_max"], seed)
    picked = core.greedy_coreset(flat[reservoir], protocol["memory"]["coreset_size"],
                                 protocol["memory"]["projection_dim"], seed)
    bank = flat[reservoir][picked]
    memory_global = core.global_vectors(mem)
    progress.mark("memory", descriptors=int(len(flat)), reservoir=int(len(reservoir)), coreset=int(len(bank)))

    cal = core.normalize(extractor.extract([data_dir / i for i in calibration_ids]), mean, std)
    chunk = protocol["distance_chunk"]
    cal_scores = {"global": core.global_scores(cal, memory_global, chunk),
                  "patch": core.patch_scores(cal, bank, chunk)}
    pct = protocol["threshold_percentile"]
    thresholds = {k: core.threshold(v, pct) for k, v in cal_scores.items()}
    progress.mark("calibrated", thresholds=thresholds)

    blob = _npz_bytes(mean=mean, std=std, bank=bank, memory_global=memory_global,
                      reservoir=reservoir, coreset=picked)
    (root / "memory.npz").write_bytes(blob)
    freeze = {
        "schema_version": 1,
        "study": protocol["study"],
        "frozen_at": now(),
        "protocol": protocol,
        "sources": source_hashes(),
        "environment": environment(),
        "model": extractor.identity(),
        "data": {"category": category, "split_csv_sha256": csv_sha,
                 "train_image_sha256": _image_hashes(data_dir, train_ids),
                 "test_image_sha256": _image_hashes(data_dir, test_ids)},
        "splits": {"memory": memory_ids, "calibration": calibration_ids, "test_unlabeled": test_ids},
        "memory": {"artifact": "memory.npz", "sha256": hashlib.sha256(blob).hexdigest(),
                   "descriptor_dim": int(d), "patches_per_image": int(p), "train_descriptors": int(len(flat)),
                   "reservoir": int(len(reservoir)), "coreset": int(len(bank)),
                   "normalization": "per-dimension mean/std over memory-train descriptors only"},
        "calibration": {"percentile": pct, "thresholds": thresholds,
                        "scores": {k: dict(zip(calibration_ids, map(float, v))) for k, v in cal_scores.items()}},
        "limitations": LIMITATIONS,
    }
    freeze["freeze_digest"] = canonical_digest(freeze)
    write_json(root / "freeze.json", freeze)
    progress.mark("frozen", freeze_digest=freeze["freeze_digest"])
    return freeze


def verify_freeze(root, data_dir, csv_path, extractor):
    """Raise if anything that defines the frozen experiment changed."""
    root = pathlib.Path(root)
    path = root / "freeze.json"
    if not path.exists():
        raise RuntimeError("no freeze.json; evaluation refused")
    freeze = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if freeze.get("freeze_digest") != canonical_digest(freeze):
        problems.append("freeze digest mismatch")
    if freeze.get("protocol") != load_protocol():
        problems.append("protocol differs")
    if freeze.get("sources") != source_hashes():
        problems.append("source/protocol file hashes differ")
    if freeze.get("environment") != environment():
        problems.append("Python environment differs")
    memory = root / freeze.get("memory", {}).get("artifact", "memory.npz")
    if not memory.exists() or sha256_file(memory) != freeze["memory"]["sha256"]:
        problems.append("memory artifact hash mismatch")
    if sha256_file(csv_path) != freeze["data"]["split_csv_sha256"]:
        problems.append("split CSV hash mismatch")
    train_ids, test_ids = split_identities(csv_path, freeze["data"]["category"])
    splits = freeze["splits"]
    if sorted(splits["memory"] + splits["calibration"]) != train_ids or set(splits["memory"]) & set(splits["calibration"]):
        problems.append("train/calibration identities differ")
    if splits["test_unlabeled"] != test_ids:
        problems.append("test identities differ")
    if _image_hashes(data_dir, test_ids) != freeze["data"]["test_image_sha256"]:
        problems.append("test image bytes differ")
    if _image_hashes(data_dir, train_ids) != freeze["data"]["train_image_sha256"]:
        problems.append("train image bytes differ")
    if extractor.identity() != freeze["model"]:
        problems.append("model identity differs")
    if problems:
        raise RuntimeError("freeze verification failed: " + "; ".join(problems))
    return freeze


def evaluate(root, data_dir, csv_path, extractor):
    root = pathlib.Path(root)
    for name in ("evaluation.json", "evaluation.started"):
        if (root / name).exists():
            raise RuntimeError(f"{name} exists; evaluation is single-shot and never resumed")
    freeze = verify_freeze(root, data_dir, csv_path, extractor)
    progress = Progress(root)
    (root / "evaluation.started").write_text(now(), encoding="utf-8")
    progress.mark("evaluation_started", freeze_digest=freeze["freeze_digest"])
    with np.load(root / "memory.npz") as art:
        mean, std, bank, memory_global = art["mean"], art["std"], art["bank"], art["memory_global"]
    test_ids = freeze["splits"]["test_unlabeled"]
    chunk = freeze["protocol"]["distance_chunk"]
    x = core.normalize(extractor.extract([pathlib.Path(data_dir) / i for i in test_ids]), mean, std)
    scores = {"global": core.global_scores(x, memory_global, chunk), "patch": core.patch_scores(x, bank, chunk)}
    progress.mark("scored", images=len(test_ids))

    labels_by_id = test_labels(csv_path, freeze["data"]["category"])  # first label read
    if set(labels_by_id) != set(test_ids):
        raise RuntimeError("test label identities differ from frozen test identities")
    y = np.array([labels_by_id[i] for i in test_ids])
    methods = {}
    for name, label in (("global", "global pooled nearest-normal baseline"),
                        ("patch", "patch nearest-memory max score (PatchCore-inspired)")):
        s = scores[name]
        methods[name] = {"label": label, "image_auroc": core.auroc(s, y), "image_average_precision": core.average_precision(s, y),
                         "frozen_threshold": core.confusion(s, y, freeze["calibration"]["thresholds"][name])}
    result = {
        "schema_version": 1, "study": freeze["study"], "evaluated_at": now(),
        "freeze_digest": freeze["freeze_digest"], "data_mode": "real",
        "counts": {"test_images": len(test_ids), "anomaly": int(y.sum()), "normal": int(len(y) - y.sum())},
        "metric_notes": ["AUROC and AP are ranking metrics, not accuracy.",
                         "Threshold metrics use the frozen train-normal calibration threshold."],
        "methods": methods,
        "images": [{"image": i, "label": int(l), "global_score": float(g), "patch_score": float(pv)}
                   for i, l, g, pv in zip(test_ids, y, scores["global"], scores["patch"])],
        "limitations": freeze["limitations"],
    }
    write_json(root / "evaluation.json", result)
    progress.mark("evaluated")
    return result

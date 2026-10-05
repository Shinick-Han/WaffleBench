"""Train / predict / evaluate for masked supervised SEM segmentation.

Ordering guarantees:
- ``train`` reads only train-split images and masks, then freezes the model file hash, config and
  the identities of every split (from manifest hashes only) into ``frozen.json``.
- ``predict`` never enables mask access and persists hashed probability maps plus a receipt.
- ``evaluate`` verifies frozen model, split identity and persisted predictions before the first
  mask read. Test-split work needs explicit opt-in and is refused after a test receipt exists.
"""

import datetime
import json
import os
import pathlib
import time

import numpy as np

from sem_images import NOTICE, PHYSICAL_SCALE, augment, manifest as mf, metrics, model as sem_model, tiling

FROZEN = "frozen.json"
MODEL_FILE = "model.pt"
TEST_RECEIPT = "test-receipt.json"
DEFAULT_THRESHOLD = 0.5
DEFAULT_SEED = 20261005


class ProtocolError(RuntimeError):
    pass


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _write_json(path, obj):
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _header(manifest, kind):
    return {"schema_version": 1, "kind": kind, "data_mode": manifest.data_mode, "created_at": now_iso(),
            "notice": NOTICE, "physical_scale": PHYSICAL_SCALE, "dataset_id": manifest.data["dataset_id"]}


def _set_threads(torch, threads):
    torch.set_num_threads(max(1, int(threads)))


def _tile_batches(net, torch, tiles, batch):
    out = []
    with torch.no_grad():
        for s in range(0, len(tiles), batch):
            x = torch.from_numpy(np.ascontiguousarray(tiles[s:s + batch], dtype=np.float32))[:, None]
            out.append(torch.sigmoid(net(x))[:, 0].numpy())
    return np.concatenate(out, axis=0)


def stratified_train_ids(reader, max_images, rng):
    """Seeded round-robin over sorted defect classes (train split only), so minority classes such as
    Carinthia-S class '5' are represented when present. Returns (ids, per-class chosen/available)."""
    by_class = {}
    for iid in reader.ids():
        by_class.setdefault(str(reader.item(iid)["defect_class"]), []).append(iid)
    queues = {c: list(rng.permutation(v)) for c, v in sorted(by_class.items())}
    chosen = []
    while len(chosen) < max_images and any(queues.values()):
        for c in sorted(queues):
            if queues[c] and len(chosen) < max_images:
                chosen.append(str(queues[c].pop(0)))
    picked = set(chosen)
    counts = {c: {"chosen": sum(i in picked for i in v), "available": len(v)} for c, v in sorted(by_class.items())}
    return sorted(chosen), counts


def _padded(arr, tile_plan, pad_mode):
    last = tile_plan["tiles"][-1]
    ph, pw = max(0, last["y1"] - arr.shape[0]), max(0, last["x1"] - arr.shape[1])
    if not (ph or pw):
        return arr
    kwargs = {"constant_values": 0} if pad_mode == "constant" else {}
    return np.pad(arr, ((0, ph), (0, pw)), mode=pad_mode, **kwargs)


def train(manifest, run_root, epochs=1, max_train_images=8, tile=tiling.DEFAULT_TILE,
          overlap=tiling.DEFAULT_OVERLAP, threads=2, model_name="unet", encoder="resnet18",
          encoder_weights_path=None, batch_size=8, lr=1e-3, seed=DEFAULT_SEED, augment_copies=0,
          access_log=None, build_fn=None):
    run_root = pathlib.Path(run_root)
    if (run_root / FROZEN).exists():
        raise ProtocolError(f"{run_root / FROZEN} exists; a frozen run is never retrained. Use a new --root.")
    if tile % 32:
        raise ValueError("tile size must be a multiple of 32 for the U-Net encoder")
    if epochs < 1 or max_train_images < 1 or augment_copies < 0:
        raise ValueError("epochs and max_train_images must be >= 1; augment_copies >= 0")
    config = sem_model.model_config(model_name, encoder, encoder_weights_path)
    torch = sem_model.import_torch()
    _set_threads(torch, threads)
    torch.manual_seed(seed)  # before model construction so initial weights are reproducible
    net = (build_fn or sem_model.build)(config)
    rng = np.random.default_rng(seed)

    reader = manifest.reader("train", masks_allowed=True, access_log=access_log)
    if not reader.ids():
        raise ProtocolError("manifest has no train items")
    chosen, class_counts = stratified_train_ids(reader, max_train_images, rng)
    pad_mode = "symmetric"
    # Images stay whole in memory; tiles are sliced per batch (native resolution, no resize).
    samples, index, provenance = [], [], []
    for iid in chosen:
        image, mask = reader.load_image(iid), reader.load_mask(iid)
        variants = [(image, mask, None)]
        for c in range(augment_copies):
            variants.append(augment.augment(image, mask, iid, c, seed, split="train"))
        for img, msk, prov in variants:
            tp = tiling.plan(*img.shape, tile=tile, overlap=overlap)
            valid = np.zeros(img.shape, dtype=np.float32) + 1.0
            samples.append((_padded(img, tp, pad_mode), _padded(msk.astype(np.float32), tp, "constant"),
                            _padded(valid, tp, "constant"), tp))
            index.extend((len(samples) - 1, k) for k in range(len(tp["tiles"])))
            if prov is not None:
                provenance.append(prov)

    def batch(idx):
        xs, ys, vs = [], [], []
        for i in idx:
            si, k = index[int(i)]
            img, msk, val, tp = samples[si]
            t = tp["tiles"][k]
            sl = (slice(t["y0"], t["y1"]), slice(t["x0"], t["x1"]))
            xs.append(img[sl]), ys.append(msk[sl]), vs.append(val[sl])
        return tuple(torch.from_numpy(np.stack(a).astype(np.float32))[:, None] for a in (xs, ys, vs))

    opt = torch.optim.Adam(net.parameters(), lr=lr)
    bce = torch.nn.BCEWithLogitsLoss(reduction="none")
    history = []
    t0 = time.perf_counter()
    net.train()
    for epoch in range(epochs):
        order = rng.permutation(len(index))
        total, n = 0.0, 0
        for s in range(0, len(order), batch_size):
            idx = order[s:s + batch_size]
            x, y, v = batch(idx)
            logits = net(x)
            # Padding pixels (v == 0) never contribute to the loss.
            pix = (bce(logits, y) * v).sum() / v.sum().clamp_min(1.0)
            p = torch.sigmoid(logits) * v
            dice = 1 - (2 * (p * y).sum() + 1) / (p.sum() + (y * v).sum() + 1)
            loss = pix + dice
            opt.zero_grad()
            loss.backward()
            opt.step()
            total += loss.item() * len(idx)
            n += len(idx)
        history.append({"epoch": epoch + 1, "mean_loss": total / max(n, 1)})
    net.eval()

    run_root.mkdir(parents=True, exist_ok=True)
    torch.save(net.state_dict(), run_root / MODEL_FILE)
    model_hash = mf.sha256_file(run_root / MODEL_FILE)
    frozen = {**_header(manifest, "sem-segmentation-frozen-model"),
              "model": {"config": config, "file": MODEL_FILE, "sha256": model_hash},
              "manifest": {"path": str(manifest.path), "file_sha256": manifest.file_sha256,
                           "validation": manifest.report},
              "split_identity": {s: manifest.split_identity(s) for s in mf.SPLITS},
              "training": {"split": "train", "item_ids": chosen, "epochs": epochs, "batch_size": batch_size,
                           "lr": lr, "seed": seed, "threads": threads, "tiles": len(index),
                           "sampling": "seeded round-robin over train defect classes",
                           "class_counts": class_counts,
                           "augmentation": {"copies_per_image": augment_copies, "synthetic_samples": provenance,
                                            "note": "Train-only generated defects; no recall gain is claimed "
                                                    "without a matched comparison."},
                           "history": history, "wall_seconds": time.perf_counter() - t0,
                           "loss": "BCE + soft Dice over valid (non-padded) pixels"},
              "tiling": {"tile": tile, "overlap": overlap, "pad_mode": pad_mode, "resize": None},
              "decision": {"threshold": DEFAULT_THRESHOLD,
                           "note": "Fixed before evaluation; not tuned on calibration or test."},
              "limitations": ["Smoke-scale CPU training; not a benchmark result.",
                              "Encoder trained from scratch unless explicit local weights were given."]}
    _write_json(run_root / FROZEN, frozen)
    return frozen


def load_frozen(manifest, run_root, split):
    run_root = pathlib.Path(run_root)
    if not (run_root / FROZEN).exists():
        raise ProtocolError(f"no {FROZEN} in {run_root}; train first")
    frozen = _read_json(run_root / FROZEN)
    if mf.sha256_file(run_root / frozen["model"]["file"]) != frozen["model"]["sha256"]:
        raise ProtocolError("model file hash differs from frozen model hash")
    if frozen["data_mode"] != manifest.data_mode or frozen["dataset_id"] != manifest.data["dataset_id"]:
        raise ProtocolError("manifest dataset/data_mode differs from the frozen run")
    for s in ("train", split):
        if manifest.split_identity(s) != frozen["split_identity"][s]:
            raise ProtocolError(f"{s} split identity differs from the frozen run; refusing mismatched manifest")
    return frozen


def _guard_split(run_root, split, confirm_test):
    if split not in ("calibration", "test"):
        raise ProtocolError("prediction/evaluation split must be calibration or test (never train)")
    if split == "test":
        if not confirm_test:
            raise ProtocolError("test split requires explicit --confirm-test opt-in")
        if (pathlib.Path(run_root) / TEST_RECEIPT).exists():
            raise ProtocolError("test receipt exists; rescoring the test split is refused")


def _receipt_path(run_root, split):
    return pathlib.Path(run_root) / f"predictions-{split}.json"


def predict(manifest, run_root, split="calibration", max_images=None, threads=2, confirm_test=False,
            batch_size=8, build_fn=None, access_log=None):
    run_root = pathlib.Path(run_root)
    _guard_split(run_root, split, confirm_test)
    frozen = load_frozen(manifest, run_root, split)
    receipt_path = _receipt_path(run_root, split)
    if receipt_path.exists():
        raise ProtocolError(f"{receipt_path.name} already exists; predictions are written once per run")
    torch = sem_model.import_torch()
    _set_threads(torch, threads)
    net = (build_fn or sem_model.build)(frozen["model"]["config"])
    net.load_state_dict(torch.load(run_root / MODEL_FILE, map_location="cpu", weights_only=True))
    net.eval()
    reader = manifest.reader(split, masks_allowed=False, access_log=access_log)
    ids = reader.ids()[:max_images] if max_images else reader.ids()
    out_dir = run_root / "predictions" / split
    out_dir.mkdir(parents=True, exist_ok=True)
    tcfg = frozen["tiling"]
    rows, latencies = [], []
    for iid in ids:
        t0 = time.perf_counter()
        image = reader.load_image(iid)
        tp = tiling.plan(*image.shape, tile=tcfg["tile"], overlap=tcfg["overlap"])
        tiles, _ = tiling.extract(image, tp, pad_mode=tcfg["pad_mode"])
        prob = tiling.stitch(_tile_batches(net, torch, tiles, batch_size), tp).astype(np.float16)
        latencies.append(time.perf_counter() - t0)
        name = f"{mf.sha256_bytes(iid.encode('utf-8'))[:24]}.npy"
        np.save(out_dir / name, prob, allow_pickle=False)
        rows.append({"id": iid, "file": f"predictions/{split}/{name}", "sha256": mf.sha256_file(out_dir / name),
                     "height": int(prob.shape[0]), "width": int(prob.shape[1]), "tiles": len(tp["tiles"]),
                     "predicted_positive_px": int(np.count_nonzero(prob >= frozen["decision"]["threshold"])),
                     "seconds": latencies[-1]})
    receipt = {**_header(manifest, "sem-segmentation-predictions"), "split": split,
               "model_sha256": frozen["model"]["sha256"], "split_identity": manifest.split_identity(split),
               "max_images": max_images, "threshold": frozen["decision"]["threshold"], "items": rows,
               "latency_seconds": _latency(latencies), "masks_read": False}
    _write_json(receipt_path, receipt)
    return receipt


def _latency(values):
    if not values:
        return {"n": 0, "p50": None, "p95": None}
    return {"n": len(values), "p50": float(np.percentile(values, 50)), "p95": float(np.percentile(values, 95))}


def evaluate(manifest, run_root, split="calibration", max_images=None, threads=2, confirm_test=False,
             iou_threshold=metrics.DEFAULT_COMPONENT_IOU, build_fn=None, access_log=None):
    run_root = pathlib.Path(run_root)
    _guard_split(run_root, split, confirm_test)
    frozen = load_frozen(manifest, run_root, split)
    receipt_path = _receipt_path(run_root, split)
    if not receipt_path.exists():
        predict(manifest, run_root, split, max_images, threads, confirm_test, build_fn=build_fn,
                access_log=access_log)
    receipt = _read_json(receipt_path)
    if receipt["model_sha256"] != frozen["model"]["sha256"]:
        raise ProtocolError("prediction receipt model hash differs from the frozen model")
    if receipt["split_identity"] != manifest.split_identity(split):
        raise ProtocolError("prediction receipt split identity differs from the manifest")
    if max_images is not None and max_images != receipt["max_images"]:
        raise ProtocolError("--max-images differs from the persisted prediction receipt")
    preds = {}
    for row in receipt["items"]:
        path = run_root / row["file"]
        if mf.sha256_file(path) != row["sha256"]:
            raise ProtocolError(f"persisted prediction for {row['id']} changed after receipt")
        preds[row["id"]] = np.load(path, allow_pickle=False)
    # Masks become readable only now, after every prediction is persisted and hash-verified.
    reader = manifest.reader(split, masks_allowed=True, access_log=access_log)
    threshold = receipt["threshold"]
    per_image = []
    for row in receipt["items"]:
        truth = reader.load_mask(row["id"])
        pred = preds[row["id"]].astype(np.float32) >= threshold
        counts = metrics.pixel_counts(pred, truth)
        comp = metrics.component_matches(pred, truth, iou_threshold)
        bias = metrics.resize_bias(truth.shape[0], truth.shape[1], [c["area_px"] for c in comp["components"]])
        bias["components"] = len(comp["components"])
        per_image.append({"id": row["id"], "defect_class": reader.item(row["id"])["defect_class"],
                          "counts": counts, **metrics.dice_iou(counts), **comp, "resize_bias": bias})
    result = {**_header(manifest, "sem-segmentation-evaluation"), "split": split,
              "role": "test" if split == "test" else "development-calibration",
              "model_sha256": frozen["model"]["sha256"], "split_identity": receipt["split_identity"],
              "predictions_receipt_sha256": mf.sha256_file(receipt_path), "threshold": threshold,
              "summary": metrics.aggregate(per_image, iou_threshold), "per_image": per_image,
              "limitations": ["Development-scale run; not a benchmark or equipment comparison.",
                              "Component sizes in pixels; nm/pixel unknown."] + manifest.report["warnings"]}
    _write_json(run_root / f"evaluation-{split}.json", result)
    if split == "test":
        _write_json(run_root / TEST_RECEIPT, {**_header(manifest, "sem-segmentation-test-receipt"),
                                               "evaluation_sha256": mf.sha256_file(run_root / "evaluation-test.json"),
                                               "model_sha256": frozen["model"]["sha256"]})
    return result

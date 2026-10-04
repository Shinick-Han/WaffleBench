"""Independent posthoc audit of the completed VisA PCB3 replication (inspection-image-replication-v3-pcb3).

python scripts/audit_inspection_images_v3.py EVIDENCE_DIR [--split-csv CSV] [--data-dir VISA]
    [--run-root ROOT] [--weights PTH] [--out AUDIT_JSON]

Reads only the captured ``freeze.json``, ``test-scores.json`` and ``evaluation.json`` (plus,
when given, the pinned split CSV, the extracted images and ``pcb3/image_anno.csv``, the
original run root and the ResNet18 weights file). Nothing is scored, trained or refit. None of
``inspection_images``, ``inspection_images_v2`` or ``inspection_images_v3`` is imported: the
freeze digest, LF-normalized source hashes, dependency pins, environment pins, thresholds,
strict-``>`` confusion counts, pairwise AUROC (ties half), tied-score-group AP, the paired
stratified bootstrap and the preregistered integer decision are reimplemented from the protocol
text and compared with what the pipeline wrote. numpy is used only to replay the preregistered
``default_rng`` bootstrap stream.

The reported headline (``HEADLINE``) is checked in a separate guard *after* the recomputation;
it is never a substitute for it. ``audit(...)`` writes the receipt to ``out`` only after every
check passed; on any finding it raises ``AuditError`` (all findings attached) and writes
nothing. The receipt carries no absolute paths. Critical checks never use ``assert``.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys

import numpy as np

APP = Path(__file__).resolve().parents[1]
STUDY = "inspection-image-replication-v3-pcb3"
CATEGORY = "pcb3"
METHODS = ("baseline224", "primary448", "highres448max")
REPORT_INPUTS = ("freeze.json", "test-scores.json", "evaluation.json")
REQUIRED_SOURCES = ("IMAGE_PILOT_V3_PROTOCOL.md", "inspection_images_v3/pipeline.py", "inspection_images_v3/cli.py",
                    "inspection_images_v3/protocol.json", "inspection_images_v2/scoring.py",
                    "inspection_images/features.py", "inspection_images/data.py")
SIZES = {"memory": 815, "calibration": 90, "test": 201, "train": 905}
# Reported headline. Checked only after, and separately from, the independent recomputation.
HEADLINE = {
    "counts": {"test_images": 201, "anomaly": 100, "normal": 101},
    "confusion": {"baseline224": {"tp": 44, "fn": 56, "fp": 8, "tn": 93},
                  "primary448": {"tp": 55, "fn": 45, "fp": 3, "tn": 98},
                  "highres448max": {"tp": 57, "fn": 43, "fp": 4, "tn": 97}},
    "recall_gain_ci95_2dp": [0.01, 0.21],
    "decision": {"recall_gain_at_least_10pp": True, "bootstrap_ci_lower_above_zero": True,
                 "primary_far_at_most_10pct": True, "success": True},
}
FLOAT_TOL = 1e-12
ABS_PATH = re.compile(rb'(?<![A-Za-z])[A-Za-z]:[\\/]|"/(?:Users|home|tmp|mnt)/')


class AuditError(RuntimeError):
    def __init__(self, findings):
        self.findings = list(findings)
        super().__init__("audit failed: " + "; ".join(self.findings))


def sha256_bytes(blob):
    return hashlib.sha256(blob).hexdigest()


def sha256_path(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def close(a, b, tol=FLOAT_TOL):
    return isinstance(a, (int, float)) and isinstance(b, (int, float)) and abs(a - b) <= tol * max(1.0, abs(b))


# ---- independent metric reimplementations (plain Python) -------------------------------------

def linear_percentile(values, pct):
    """numpy 'linear' (Hyndman-Fan type 7) percentile, written out."""
    v = sorted(float(x) for x in values)
    if not v or not all(math.isfinite(x) for x in v):
        raise ValueError("percentile needs finite values")
    pos = (len(v) - 1) * pct / 100.0
    lo = math.floor(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def confusion(scores, labels, thr):
    tp = fn = fp = tn = 0
    for s, y in zip(scores, labels):
        pred = s > thr
        if y == 1:
            tp, fn = tp + pred, fn + (not pred)
        else:
            fp, tn = fp + pred, tn + (not pred)
    return {"tp": int(tp), "fn": int(fn), "fp": int(fp), "tn": int(tn)}


def pairwise_auroc(scores, labels):
    pos = [s for s, y in zip(scores, labels) if y == 1]
    neg = [s for s, y in zip(scores, labels) if y == 0]
    total = 0.0
    for p in pos:
        for n in neg:
            total += 1.0 if p > n else 0.5 if p == n else 0.0
    return total / (len(pos) * len(neg))


def tied_group_ap(scores, labels):
    """AP over distinct score thresholds: tied scores enter together as one group."""
    groups = {}
    for s, y in zip(scores, labels):
        g = groups.setdefault(s, [0, 0])
        g[0 if y == 1 else 1] += 1
    n_pos = sum(1 for y in labels if y == 1)
    tp = fp = 0
    ap = prev_recall = 0.0
    for s in sorted(groups, reverse=True):
        tp += groups[s][0]
        fp += groups[s][1]
        recall = tp / n_pos
        ap += (recall - prev_recall) * (tp / (tp + fp))
        prev_recall = recall
    return ap


def paired_stratified_bootstrap(pred_a, pred_b, labels, resamples, seed_stream):
    """Replays the preregistered stream: defect stratum first, then normal, one shared draw per stratum."""
    rng = np.random.default_rng(list(seed_stream))
    out = {}
    for name, stratum in (("defect_recall_gain", 1), ("normal_far_difference", 0)):
        idx = [i for i, y in enumerate(labels) if y == stratum]
        diff = np.array([pred_a[i] - pred_b[i] for i in idx], dtype=np.int64)
        draws = rng.integers(0, len(idx), size=(resamples, len(idx)))
        stats = sorted((diff[draws].sum(axis=1) / len(idx)).tolist())
        out[name] = {"estimate": sum(diff.tolist()) / len(idx),
                     "ci95": [linear_percentile(stats, 2.5), linear_percentile(stats, 97.5)],
                     "resamples": int(resamples)}
    return out


def integer_decision(tp_primary, tp_baseline, fp_primary, n_anomaly, n_normal, ci_lower):
    """Preregistered endpoint on integer counts (IMAGE_PILOT_V3_PROTOCOL.md, primary endpoint)."""
    gain_ok = 10 * (tp_primary - tp_baseline) >= n_anomaly
    ci_ok = ci_lower > 0
    far_ok = 10 * fp_primary <= n_normal
    return {"recall_gain_at_least_10pp": gain_ok, "bootstrap_ci_lower_above_zero": ci_ok,
            "primary_far_at_most_10pct": far_ok, "success": gain_ok and ci_ok and far_ok}


def canonical_digest(payload):
    body = {k: v for k, v in payload.items() if k != "freeze_digest"}
    return sha256_bytes(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


def lf_hash(path):
    return sha256_bytes(Path(path).read_bytes().replace(b"\r\n", b"\n"))


def split_rows(csv_path, category):
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r["object"] == category]


def anno_types(anno_csv):
    with open(anno_csv, newline="", encoding="utf-8") as fh:
        return {r["image"]: r["label"] for r in csv.DictReader(fh)}


# ---- audit -----------------------------------------------------------------------------------

def audit(evidence_dir, repo_root=APP, split_csv=None, data_dir=None, run_root=None, weights=None, out=None):
    evidence_dir, repo_root = Path(evidence_dir), Path(repo_root)
    findings = []

    def check(ok, message):
        if not ok:
            findings.append(message)
        return bool(ok)

    raw, docs = {}, {}
    for name in REPORT_INPUTS:
        path = evidence_dir / name
        if not check(path.is_file(), f"missing report input {name}"):
            continue
        raw[name] = path.read_bytes()
        try:
            docs[name] = json.loads(raw[name].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            findings.append(f"{name} is not valid JSON: {exc}")
    if findings:
        raise AuditError(findings)
    freeze, test_scores, evaluation = docs["freeze.json"], docs["test-scores.json"], docs["evaluation.json"]
    input_hashes = {n: sha256_bytes(b) for n, b in raw.items()}
    headline = {}

    try:
        for n, b in raw.items():
            check(not ABS_PATH.search(b), f"{n} contains an absolute local path")

        # Freeze receipt integrity and chain to scores/evaluation.
        digest = freeze.get("freeze_digest")
        check(digest == canonical_digest(freeze), "freeze digest does not match canonical recomputation")
        check(freeze.get("study") == STUDY and evaluation.get("study") == STUDY, "study id mismatch")
        check(test_scores.get("freeze_digest") == digest, "test-scores freeze_digest differs from freeze")
        check(evaluation.get("freeze_digest") == digest, "evaluation freeze_digest differs from freeze")
        check(evaluation.get("test_scores_sha256") == input_hashes["test-scores.json"],
              "test-scores.json bytes differ from the sha256 recorded in evaluation.json")
        check(evaluation.get("data_mode") == "real", "evaluation data_mode is not real")
        test_usage = freeze.get("test_usage", {})
        check(all(test_usage.get(k) is False for k in ("scored", "labels_read", "in_memory", "in_calibration",
                                                       "in_normalization", "in_thresholds")),
              "freeze test-usage guard is not all false")

        # Protocol, endpoint and selection disclosure against the repository.
        protocol = json.loads((repo_root / "inspection_images_v3" / "protocol.json").read_text(encoding="utf-8"))
        check(freeze.get("protocol") == protocol, "freeze protocol differs from inspection_images_v3/protocol.json")
        check(freeze.get("primary_endpoint") == protocol["primary_endpoint"]
              == evaluation["primary_endpoint"]["definition"], "primary endpoint differs between protocol/freeze/evaluation")
        check(protocol["primary_endpoint"]["comparison"] == "primary448 minus baseline224",
              "primary comparison is not primary448 minus baseline224")
        check(evaluation.get("limitations") == freeze.get("limitations"), "evaluation limitations differ from freeze")
        check(protocol["dataset"]["category"] == freeze["data"]["category"] == CATEGORY, "category is not pcb3")
        pred = protocol["predecessor"]
        check([s["category"] for s in pred["studies"]] == ["pcb1", "pcb2"] and "PCB2" in pred["selection_disclosure"],
              "predecessor disclosure does not name PCB2 as the architecture selection source")
        for m in METHODS:
            check(evaluation["methods"][m]["role"] == protocol["methods"][m]["role"], f"{m} role differs from protocol")
        check(protocol["methods"]["highres448max"]["role"] == "secondary_descriptive",
              "highres448max is not descriptive-only")

        # Source receipt (independent LF-normalized rehash) and frozen v1/v2 dependency pins.
        sources = freeze.get("sources", {})
        rehashed = {}
        for rel in sorted(sources):
            path = repo_root / rel
            rehashed[rel] = lf_hash(path) if path.is_file() else None
            check(rehashed[rel] == sources[rel], f"source hash mismatch for {rel}")
        for required in REQUIRED_SOURCES:
            check(required in sources, f"freeze does not hash {required}")
        for rel, pin in protocol["frozen_dependency_sha256"].items():
            check(sources.get(rel) == pin, f"frozen dependency {rel} differs from its protocol pin")

        # Environment and model receipt.
        pins = protocol["environment_pins"]
        env = freeze["environment"]
        pkgs = dict(p.split("==", 1) for p in env["packages"])
        check(env["python"] == pins["python"] and pkgs.get("numpy") == pins["numpy"]
              and pkgs.get("torch") == pins["torch"], "freeze environment differs from protocol environment pins")
        bb = protocol["backbone"]
        weight_hashes = set()
        for fs_name, fs in protocol["feature_sets"].items():
            model = freeze["model"][fs_name]
            ex = model["extractor"]
            check(model["resize"] == fs["resize"], f"model {fs_name} resize differs from protocol")
            check(ex["backbone"] == bb["family"] and ex["weights_file"] == bb["weights_file"]
                  and ex["batch"] == bb["batch"] and ex["threads"] == bb["threads"],
                  f"model {fs_name} extractor differs from protocol backbone")
            check(ex["packages"]["numpy"] == pins["numpy"] and ex["packages"]["torch"] == pins["torch"],
                  f"model {fs_name} packages differ from environment pins")
            mem = freeze["memory"][fs_name]
            check(mem["coreset"] == fs["coreset_size"] and mem["descriptor_dim"] == bb["descriptor_dim"]
                  and mem["patches_per_image"] == fs["grid"][0] * fs["grid"][1]
                  and mem["train_descriptors"] == SIZES["memory"] * mem["patches_per_image"],
                  f"memory {fs_name} receipt inconsistent with protocol/memory split")
            weight_hashes.add(ex["weights_sha256"])
        check(len(weight_hashes) == 1, "feature sets use different backbone weights")
        weights_receipt = None
        if weights is not None:
            w = sha256_path(weights)
            check(w in weight_hashes and Path(weights).name == bb["weights_file"], "weights file differs from freeze")
            weights_receipt = {"file": Path(weights).name, "sha256": w}

        # Identities.
        splits = freeze["splits"]
        test_ids = splits["test_unlabeled"]
        train_ids = splits["memory"] + splits["calibration"]
        check(len(test_ids) == len(set(test_ids)) == SIZES["test"], "frozen test identities are not 201 unique images")
        check(len(train_ids) == len(set(train_ids)) == SIZES["train"], "normal-train identities are not 905 unique")
        check(not set(test_ids) & set(train_ids), "test identities overlap memory/calibration")
        check((len(splits["memory"]), len(splits["calibration"])) == (SIZES["memory"], SIZES["calibration"]),
              "memory/calibration sizes not 815/90")
        check(all(i.startswith(CATEGORY + "/") for i in test_ids + train_ids), "identities outside pcb3")
        check(sorted(test_scores["scores"]) == sorted(METHODS), "test-scores methods differ")
        for m in METHODS:
            check(list(test_scores["scores"].get(m, {})) == test_ids, f"test-scores identities/order differ for {m}")

        # Labels: evaluation rows, path convention and (optionally) the pinned split CSV must agree.
        rows = evaluation["images"]
        check([r["image"] for r in rows] == test_ids, "evaluation image order differs from frozen test identities")
        labels = [int(r["label"]) for r in rows]
        path_labels = [1 if "/Anomaly/" in i else 0 if "/Normal/" in i else -1 for i in test_ids]
        check(labels == path_labels, "evaluation labels disagree with Anomaly/Normal image paths")
        csv_receipt = None
        if split_csv is not None:
            csv_sha = sha256_path(split_csv)
            check(csv_sha == freeze["data"]["split_csv_sha256"] == protocol["dataset"]["split_csv_sha256"],
                  "split CSV hash differs from freeze/protocol")
            rows_csv = split_rows(split_csv, CATEGORY)
            csv_test = {r["image"]: int(r["label"] == "anomaly") for r in rows_csv if r["split"] == "test"}
            csv_train = sorted(r["image"] for r in rows_csv if r["split"] == "train")
            check(set(csv_test) == set(test_ids) and [csv_test.get(i) for i in test_ids] == labels,
                  "split CSV test labels differ from evaluation labels")
            check(csv_train == sorted(train_ids), "split CSV train identities differ from memory+calibration")
            csv_receipt = {"sha256": csv_sha, "pcb3_rows": len(rows_csv)}
        n_anomaly, n_normal = sum(labels), len(labels) - sum(labels)
        counts = {"test_images": len(labels), "anomaly": n_anomaly, "normal": n_normal}
        check(counts == evaluation["counts"], "test counts differ from evaluation counts block")

        # Thresholds: calibration-only linear 95th percentile.
        cal = freeze["calibration"]
        check(cal["percentile"] == protocol["threshold_percentile"] == 95.0, "threshold percentile is not 95")
        thresholds = {}
        for m in METHODS:
            scores = cal["scores"][m]
            check(sorted(scores) == sorted(splits["calibration"]), f"calibration identities differ for {m}")
            thresholds[m] = linear_percentile(scores.values(), cal["percentile"])
            check(close(thresholds[m], cal["thresholds"][m]), f"threshold {m} is not the calibration 95th percentile")
            check(close(evaluation["methods"][m]["frozen_threshold"]["threshold"], cal["thresholds"][m]),
                  f"evaluation threshold {m} differs from freeze")

        # Per-method metrics from scores + labels.
        methods, preds = {}, {}
        for m in METHODS:
            s = [test_scores["scores"][m][i] for i in test_ids]
            check(all(isinstance(x, (int, float)) and math.isfinite(x) for x in s), f"non-finite scores for {m}")
            check(all(close(r[f"{m}_score"], x) for r, x in zip(rows, s)),
                  f"evaluation per-image {m} scores differ from test-scores.json")
            thr = thresholds[m]
            c = confusion(s, labels, thr)
            preds[m] = [int(x > thr) for x in s]
            reported = evaluation["methods"][m]
            ft = reported["frozen_threshold"]
            check((ft["true_positive"], ft["false_negative"], ft["false_positive"], ft["true_negative"])
                  == (c["tp"], c["fn"], c["fp"], c["tn"]), f"{m} confusion counts differ from recomputation")
            check(ft["rule"] == "score > threshold => predicted defect", f"{m} threshold rule is not strict >")
            recall, far = c["tp"] / (c["tp"] + c["fn"]), c["fp"] / (c["fp"] + c["tn"])
            check(close(ft["defect_recall"], recall) and close(ft["normal_false_alarm_rate"], far),
                  f"{m} recall/FAR differ from counts")
            auc, ap = pairwise_auroc(s, labels), tied_group_ap(s, labels)
            check(close(reported["image_auroc_secondary"], auc, 1e-9), f"{m} AUROC differs from pairwise recomputation")
            check(close(reported["image_average_precision_secondary"], ap, 1e-9), f"{m} AP differs from recomputation")
            methods[m] = {"role": reported["role"], "threshold": thr, **c, "defect_recall": recall,
                          "normal_false_alarm_rate": far, "image_auroc": auc, "image_average_precision": ap,
                          "tied_scores": len(s) - len(set(s))}

        # Paired bootstrap replay and the preregistered integer decision.
        boot_cfg = protocol["primary_endpoint"]["bootstrap"]
        check(boot_cfg["resamples"] == 10000 and list(boot_cfg["seed_stream"]) == [2026100408, 7],
              "bootstrap config is not 10000 resamples with seed stream [2026100408, 7]")
        boots = {}
        for key, other, where in (("primary448", "baseline224", evaluation["primary_endpoint"]["primary_vs_baseline_bootstrap"]),
                                  ("highres448max", "baseline224",
                                   evaluation["secondary_descriptive"]["highres448max_vs_baseline224_bootstrap"])):
            b = paired_stratified_bootstrap(preds[key], preds[other], labels, boot_cfg["resamples"], boot_cfg["seed_stream"])
            for stat in ("defect_recall_gain", "normal_far_difference"):
                check(close(where[stat]["estimate"], b[stat]["estimate"]) and
                      all(close(x, y) for x, y in zip(where[stat]["ci95"], b[stat]["ci95"])) and
                      where[stat]["resamples"] == b[stat]["resamples"],
                      f"{key} vs {other} bootstrap {stat} differs from replay")
            boots[f"{key}_minus_{other}"] = b
        pb, pp = methods["baseline224"], methods["primary448"]
        decision = integer_decision(pp["tp"], pb["tp"], pp["fp"], n_anomaly, n_normal,
                                    boots["primary448_minus_baseline224"]["defect_recall_gain"]["ci95"][0])
        check(decision == evaluation["primary_endpoint"]["decision"], "preregistered decision differs from recomputation")

        # Optional: image bytes against the freeze, annotation breakdown, run-root copies and artifacts.
        image_receipt, types, anno_receipt = None, {}, None
        if data_dir is not None:
            data_dir = Path(data_dir)
            hashed = {**freeze["data"]["test_image_sha256"], **freeze["data"]["train_image_sha256"]}
            check(sorted(freeze["data"]["test_image_sha256"]) == sorted(test_ids), "freeze test hash set differs")
            check(sorted(freeze["data"]["train_image_sha256"]) == sorted(train_ids), "freeze train hash set differs")
            bad = [i for i, h in hashed.items() if not (data_dir / i).is_file() or sha256_path(data_dir / i) != h]
            check(not bad, f"{len(bad)} image(s) differ from freeze byte hashes (first: {bad[:3]})")
            image_receipt = {"images_rehashed": len(hashed), "test": len(freeze["data"]["test_image_sha256"]),
                             "train": len(freeze["data"]["train_image_sha256"]), "mismatches": len(bad)}
            anno = data_dir / CATEGORY / "image_anno.csv"
            if anno.is_file():
                types = anno_types(anno)
                anno_receipt = {"file": f"{CATEGORY}/image_anno.csv", "sha256": sha256_path(anno),
                                "use": "posthoc descriptive defect-type breakdown only"}
        errors = error_analysis(test_ids, labels, test_scores["scores"], thresholds, preds, types)
        check(len(errors["primary448_missed_defects"]) == pp["fn"], "missed-defect list length differs from FN")
        check(len(errors["primary448_false_alarms"]) == pp["fp"], "false-alarm list length differs from FP")
        run_receipt = None
        if run_root is not None:
            run_root = Path(run_root)
            same = {n: (run_root / n).is_file() and sha256_path(run_root / n) == input_hashes[n] for n in REPORT_INPUTS}
            check(all(same.values()), f"evidence copies differ from run root: {[n for n, ok in same.items() if not ok]}")
            for name, art in freeze["memory"].items():
                f = run_root / art["artifact"]
                check(f.is_file() and sha256_path(f) == art["sha256"], f"memory artifact {name} hash mismatch")
            started = run_root / "evaluation.started"
            if started.is_file():
                check(json.loads(started.read_text(encoding="utf-8")).get("freeze_digest") == digest,
                      "evaluation.started freeze_digest differs")
            run_receipt = {"byte_identical": same, "memory_artifacts_verified": sorted(freeze["memory"]),
                           "evaluation_started_checked": started.is_file()}

        # Separate headline guard: compares the recomputation with the numbers the report states.
        recomputed = {"counts": counts,
                      "confusion": {m: {k: methods[m][k] for k in ("tp", "fn", "fp", "tn")} for m in METHODS},
                      "recall_gain_ci95_2dp": [round(x, 2) for x in
                                               boots["primary448_minus_baseline224"]["defect_recall_gain"]["ci95"]],
                      "decision": decision}
        for key, want in HEADLINE.items():
            headline[key] = recomputed[key] == want
            check(headline[key], f"headline guard: recomputed {key} differs from the reported headline")
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OSError) as exc:
        findings.append(f"malformed or missing input: {type(exc).__name__}: {exc}")

    if findings:
        raise AuditError(findings)
    receipt = {
        "schema_version": 1, "study": STUDY, "audited_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass",
        "independence": "inspection_images, inspection_images_v2 and inspection_images_v3 not imported; metrics, "
                        "thresholds, digest, source hashes and bootstrap reimplemented; numpy only replays default_rng; "
                        "no image scoring, feature extraction or training",
        "report_input_sha256": input_hashes, "freeze_digest": digest,
        "source_hashes_rehashed": rehashed, "environment": {"python": env["python"], "numpy": pkgs["numpy"],
                                                             "torch": pkgs["torch"], "platform": env["platform"]},
        "weights": weights_receipt, "split_csv": csv_receipt, "images": image_receipt,
        "annotations": anno_receipt, "run_root": run_receipt,
        "splits": {"memory": len(splits["memory"]), "calibration": len(splits["calibration"]), "test": len(test_ids)},
        "counts": counts, "methods": methods, "bootstrap": boots, "decision": decision,
        "headline_guard": headline, "errors": errors,
    }
    if out is not None:
        Path(out).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return receipt


def error_analysis(test_ids, labels, scores, thresholds, preds, types):
    """Who the primary misses / falsely flags, how far from threshold, and what the other methods did."""
    def row(k):
        i = test_ids[k]
        return {"image": i, "defect_type": types.get(i), "primary448_margin": scores["primary448"][i] - thresholds["primary448"],
                "baseline224_flagged": bool(preds["baseline224"][k]), "highres448max_flagged": bool(preds["highres448max"][k])}

    missed = [row(k) for k, y in enumerate(labels) if y == 1 and not preds["primary448"][k]]
    alarms = [row(k) for k, y in enumerate(labels) if y == 0 and preds["primary448"][k]]
    by_type, missed_by_type = Counter(), Counter()
    for k, y in enumerate(labels):
        if y == 1:
            t = types.get(test_ids[k], "unannotated")
            by_type[t] += 1
            missed_by_type[t] += not preds["primary448"][k]

    def flips(y, a, b):
        return sum(1 for k, lab in enumerate(labels) if lab == y and preds[a][k] and not preds[b][k])

    return {"primary448_missed_defects": sorted(missed, key=lambda r: r["primary448_margin"]),
            "primary448_false_alarms": sorted(alarms, key=lambda r: -r["primary448_margin"]),
            "anomaly_by_defect_type": {t: {"test": by_type[t], "primary448_missed": missed_by_type[t]}
                                       for t in sorted(by_type)},
            "paired_flips_vs_baseline224": {
                "defect_caught_only_by_primary": flips(1, "primary448", "baseline224"),
                "defect_caught_only_by_baseline": flips(1, "baseline224", "primary448"),
                "normal_flagged_only_by_primary": flips(0, "primary448", "baseline224"),
                "normal_flagged_only_by_baseline": flips(0, "baseline224", "primary448")}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evidence_dir")
    parser.add_argument("--repo-root", default=str(APP))
    parser.add_argument("--split-csv")
    parser.add_argument("--data-dir")
    parser.add_argument("--run-root")
    parser.add_argument("--weights")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        receipt = audit(args.evidence_dir, args.repo_root, args.split_csv, args.data_dir, args.run_root,
                        args.weights, args.out)
    except AuditError as exc:
        print(json.dumps({"status": "fail", "findings": exc.findings}, indent=2))
        return 1
    print(json.dumps({"status": "pass", "decision": receipt["decision"], "headline_guard": receipt["headline_guard"],
                      "out": args.out and Path(args.out).name}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

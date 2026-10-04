"""Independent posthoc audit of the completed VisA PCB2 image study (inspection-image-pilot-v2-pcb2).

python scripts/audit_inspection_images_v2.py EVIDENCE_DIR [--split-csv CSV] [--data-dir VISA]
    [--anno-csv CSV] [--run-root ROOT] [--out AUDIT_JSON]

Reads only the captured ``freeze.json``, ``test-scores.json`` and ``evaluation.json`` (plus,
when given, the pinned split CSV, the extracted images, the dataset's ``image_anno.csv`` and
the original run root). Nothing is scored, trained or refit. Neither ``inspection_images``
nor ``inspection_images_v2`` is imported: the freeze digest, source hashes, thresholds,
strict-``>`` confusion counts, pairwise AUROC (ties half), tied-score-group AP, the paired
stratified bootstrap and the preregistered integer decision are all reimplemented from the
protocol text and compared with what the pipeline wrote. numpy is used only to replay the
preregistered ``default_rng`` bootstrap stream.

``audit(...)`` returns a structured receipt and writes it to ``out`` only after every check
passed; on any finding it raises ``AuditError`` (all findings attached) and writes nothing.
The CLI prints JSON and returns nonzero on failure. Critical checks never use ``assert``.
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
import sys

import numpy as np

APP = Path(__file__).resolve().parents[1]
STUDY = "inspection-image-pilot-v2-pcb2"
METHODS = ("baseline224", "primary448", "highres448max")
REPORT_INPUTS = ("freeze.json", "test-scores.json", "evaluation.json")
# Headline the written report states; the audit fails if the recomputation disagrees.
EXPECTED = {
    "counts": {"test_images": 200, "anomaly": 100, "normal": 100},
    "confusion": {"baseline224": {"tp": 51, "fn": 49, "fp": 11, "tn": 89},
                  "primary448": {"tp": 72, "fn": 28, "fp": 5, "tn": 95},
                  "highres448max": {"tp": 69, "fn": 31, "fp": 6, "tn": 94}},
    "auroc_4dp": {"baseline224": 0.8005, "primary448": 0.906, "highres448max": 0.8942},
    "recall_gain_ci95_2dp": [0.12, 0.30],
    "success": True,
}
FLOAT_TOL = 1e-12


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


def canonical_digest(payload):
    body = {k: v for k, v in payload.items() if k != "freeze_digest"}
    return sha256_bytes(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())


def lf_hash(path):
    return sha256_bytes(Path(path).read_bytes().replace(b"\r\n", b"\n"))


# ---- label / annotation readers (independent of inspection_images.data) ----------------------

def split_rows(csv_path, category):
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r["object"] == category]


def anno_types(anno_csv):
    with open(anno_csv, newline="", encoding="utf-8") as fh:
        return {r["image"]: r["label"] for r in csv.DictReader(fh)}


# ---- audit -----------------------------------------------------------------------------------

def audit(evidence_dir, repo_root=APP, split_csv=None, data_dir=None, anno_csv=None, run_root=None, out=None):
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

    try:
        # Freeze receipt integrity and chain to scores/evaluation.
        digest = freeze.get("freeze_digest")
        check(digest == canonical_digest(freeze), "freeze digest does not match canonical recomputation")
        check(freeze.get("study") == STUDY and evaluation.get("study") == STUDY, "study id mismatch")
        check(test_scores.get("freeze_digest") == digest, "test-scores freeze_digest differs from freeze")
        check(evaluation.get("freeze_digest") == digest, "evaluation freeze_digest differs from freeze")
        check(evaluation.get("test_scores_sha256") == input_hashes["test-scores.json"],
              "test-scores.json bytes differ from the sha256 recorded in evaluation.json")
        test_usage = freeze.get("test_usage", {})
        check(all(test_usage.get(k) is False for k in ("scored", "labels_read", "in_memory", "in_calibration",
                                                       "in_normalization", "in_thresholds")),
              "freeze test-usage guard is not all false")

        # Protocol and source receipt against the repository (independent LF-normalized rehash).
        protocol_path = repo_root / "inspection_images_v2" / "protocol.json"
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        check(freeze.get("protocol") == protocol, "freeze protocol differs from inspection_images_v2/protocol.json")
        sources = freeze.get("sources", {})
        rehashed = {}
        for rel in sorted(sources):
            path = repo_root / rel
            rehashed[rel] = lf_hash(path) if path.is_file() else None
            check(rehashed[rel] == sources[rel], f"source hash mismatch for {rel}")
        for required in ("IMAGE_PILOT_V2_PROTOCOL.md", "inspection_images_v2/pipeline.py",
                         "inspection_images_v2/scoring.py", "inspection_images_v2/protocol.json"):
            check(required in sources, f"freeze does not hash {required}")

        # Identities.
        splits = freeze["splits"]
        test_ids = splits["test_unlabeled"]
        train_ids = splits["memory"] + splits["calibration"]
        check(len(test_ids) == len(set(test_ids)) == 200, "frozen test identities are not 200 unique images")
        check(not set(test_ids) & set(train_ids), "test identities overlap memory/calibration")
        check(not set(splits["memory"]) & set(splits["calibration"]), "memory and calibration overlap")
        check((len(splits["memory"]), len(splits["calibration"])) == (811, 90), "memory/calibration sizes not 811/90")
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
            pcb2 = split_rows(split_csv, "pcb2")
            csv_test = {r["image"]: int(r["label"] == "anomaly") for r in pcb2 if r["split"] == "test"}
            csv_train = sorted(r["image"] for r in pcb2 if r["split"] == "train")
            check(set(csv_test) == set(test_ids) and [csv_test.get(i) for i in test_ids] == labels,
                  "split CSV test labels differ from evaluation labels")
            check(csv_train == sorted(train_ids), "split CSV train identities differ from memory+calibration")
            csv_receipt = {"sha256": csv_sha, "pcb2_rows": len(pcb2)}
        n_anomaly, n_normal = sum(labels), len(labels) - sum(labels)
        check({"test_images": len(labels), "anomaly": n_anomaly, "normal": n_normal} == evaluation["counts"]
              == EXPECTED["counts"], "test counts differ from evaluation/expected 200/100/100")

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
            thr = cal["thresholds"][m]
            c = confusion(s, labels, thr)
            preds[m] = [int(x > thr) for x in s]
            reported = evaluation["methods"][m]
            ft = reported["frozen_threshold"]
            check((ft["true_positive"], ft["false_negative"], ft["false_positive"], ft["true_negative"])
                  == (c["tp"], c["fn"], c["fp"], c["tn"]), f"{m} confusion counts differ from recomputation")
            check(ft["rule"] == "score > threshold => predicted defect", f"{m} threshold rule is not strict >")
            check(c == EXPECTED["confusion"][m], f"{m} confusion differs from the reported headline")
            recall, far = c["tp"] / (c["tp"] + c["fn"]), c["fp"] / (c["fp"] + c["tn"])
            check(close(ft["defect_recall"], recall) and close(ft["normal_false_alarm_rate"], far),
                  f"{m} recall/FAR differ from counts")
            auc, ap = pairwise_auroc(s, labels), tied_group_ap(s, labels)
            check(close(reported["image_auroc_secondary"], auc, 1e-9), f"{m} AUROC differs from pairwise recomputation")
            check(close(reported["image_average_precision_secondary"], ap, 1e-9), f"{m} AP differs from recomputation")
            check(round(auc, 4) == EXPECTED["auroc_4dp"][m], f"{m} AUROC differs from the reported headline")
            check(reported["role"] == protocol["methods"][m]["role"], f"{m} role differs from protocol")
            ties = len(s) - len(set(s))
            methods[m] = {"role": reported["role"], "threshold": thr, **c, "defect_recall": recall,
                          "normal_false_alarm_rate": far, "image_auroc": auc, "image_average_precision": ap,
                          "tied_scores": ties}

        # Paired bootstrap replay and the preregistered integer decision.
        boot_cfg = protocol["primary_endpoint"]["bootstrap"]
        boots = {}
        for key, other, where in (("primary448", "baseline224", evaluation["primary_endpoint"]["primary_vs_baseline_bootstrap"]),
                                  ("highres448max", "baseline224",
                                   evaluation["secondary_descriptive"]["highres448max_vs_baseline224_bootstrap"])):
            b = paired_stratified_bootstrap(preds[key], preds[other], labels, boot_cfg["resamples"], boot_cfg["seed_stream"])
            for stat in ("defect_recall_gain", "normal_far_difference"):
                check(close(where[stat]["estimate"], b[stat]["estimate"]) and
                      all(close(x, y) for x, y in zip(where[stat]["ci95"], b[stat]["ci95"])) and
                      where[stat]["resamples"] == b[stat]["resamples"] == 10000,
                      f"{key} vs {other} bootstrap {stat} differs from replay")
            boots[f"{key}_minus_{other}"] = b
        pb, pp = methods["baseline224"], methods["primary448"]
        gain_ok = 10 * (pp["tp"] - pb["tp"]) >= n_anomaly
        ci_ok = boots["primary448_minus_baseline224"]["defect_recall_gain"]["ci95"][0] > 0
        far_ok = 10 * pp["fp"] <= n_normal
        decision = {"recall_gain_at_least_10pp": gain_ok, "bootstrap_ci_lower_above_zero": ci_ok,
                    "primary_far_at_most_10pct": far_ok, "success": gain_ok and ci_ok and far_ok}
        check(decision == evaluation["primary_endpoint"]["decision"], "preregistered decision differs from recomputation")
        check(decision["success"] is EXPECTED["success"], "decision differs from the reported headline")
        ci = boots["primary448_minus_baseline224"]["defect_recall_gain"]["ci95"]
        check([round(x, 2) for x in ci] == EXPECTED["recall_gain_ci95_2dp"], "recall gain CI differs from headline")

        # Optional: image bytes against the freeze, annotation-based error breakdown, run-root copies.
        image_receipt = None
        if data_dir is not None:
            data_dir = Path(data_dir)
            bad = [i for group in ("test_image_sha256", "train_image_sha256")
                   for i, h in freeze["data"][group].items()
                   if not (data_dir / i).is_file() or sha256_path(data_dir / i) != h]
            check(not bad, f"{len(bad)} image(s) differ from freeze byte hashes (first: {bad[:3]})")
            check(sorted(freeze["data"]["test_image_sha256"]) == sorted(test_ids), "freeze test hash set differs")
            image_receipt = {"test_images_rehashed": len(freeze["data"]["test_image_sha256"]),
                             "train_images_rehashed": len(freeze["data"]["train_image_sha256"]), "mismatches": len(bad)}
        types, anno_receipt = {}, None
        if anno_csv is not None:
            types = anno_types(anno_csv)
            anno_receipt = {"sha256": sha256_path(anno_csv), "use": "posthoc descriptive defect-type breakdown only"}
        errors = error_analysis(test_ids, labels, test_scores["scores"], cal["thresholds"], preds, types)
        check(len(errors["primary448_missed_defects"]) == pp["fn"] == 28, "primary missed-defect list is not 28")
        check(len(errors["primary448_false_alarms"]) == pp["fp"] == 5, "primary false-alarm list is not 5")
        run_receipt = None
        if run_root is not None:
            run_root = Path(run_root)
            same = {n: (run_root / n).is_file() and sha256_path(run_root / n) == input_hashes[n] for n in REPORT_INPUTS}
            check(all(same.values()), f"evidence copies differ from run root: {[n for n, ok in same.items() if not ok]}")
            for name, art in freeze["memory"].items():
                f = run_root / art["artifact"]
                check(f.is_file() and sha256_path(f) == art["sha256"], f"memory artifact {name} hash mismatch")
            run_receipt = {"byte_identical": same, "memory_artifacts_verified": sorted(freeze["memory"])}
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, OSError) as exc:
        findings.append(f"malformed or missing input: {type(exc).__name__}: {exc}")

    if findings:
        raise AuditError(findings)
    receipt = {
        "schema_version": 1, "study": STUDY, "audited_at": datetime.now(timezone.utc).isoformat(),
        "status": "pass",
        "independence": "inspection_images and inspection_images_v2 not imported; metrics, thresholds, digest, "
                        "source hashes and bootstrap reimplemented; numpy only replays default_rng",
        "report_input_sha256": input_hashes, "freeze_digest": freeze["freeze_digest"],
        "source_hashes_rehashed": rehashed, "split_csv": csv_receipt, "images": image_receipt,
        "annotations": anno_receipt, "run_root": run_receipt,
        "counts": {"test_images": len(labels), "anomaly": n_anomaly, "normal": n_normal},
        "methods": methods, "bootstrap": boots, "decision": decision, "errors": errors,
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
    flips = {"defect_caught_only_by_primary": sum(1 for k, y in enumerate(labels)
                                                   if y and preds["primary448"][k] and not preds["baseline224"][k]),
             "defect_caught_only_by_baseline": sum(1 for k, y in enumerate(labels)
                                                    if y and preds["baseline224"][k] and not preds["primary448"][k]),
             "normal_flagged_only_by_primary": sum(1 for k, y in enumerate(labels)
                                                    if not y and preds["primary448"][k] and not preds["baseline224"][k]),
             "normal_flagged_only_by_baseline": sum(1 for k, y in enumerate(labels)
                                                     if not y and preds["baseline224"][k] and not preds["primary448"][k])}
    return {"primary448_missed_defects": sorted(missed, key=lambda r: r["primary448_margin"]),
            "primary448_false_alarms": sorted(alarms, key=lambda r: -r["primary448_margin"]),
            "anomaly_by_defect_type": {t: {"test": by_type[t], "primary448_missed": missed_by_type[t]}
                                       for t in sorted(by_type)},
            "paired_flips_vs_baseline224": flips}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("evidence_dir")
    parser.add_argument("--repo-root", default=str(APP))
    parser.add_argument("--split-csv")
    parser.add_argument("--data-dir")
    parser.add_argument("--anno-csv")
    parser.add_argument("--run-root")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        receipt = audit(args.evidence_dir, args.repo_root, args.split_csv, args.data_dir, args.anno_csv,
                        args.run_root, args.out)
    except AuditError as exc:
        print(json.dumps({"status": "fail", "findings": exc.findings}, indent=2))
        return 1
    print(json.dumps({"status": "pass", "decision": receipt["decision"], "out": args.out}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

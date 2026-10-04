"""Independently reconcile stored acquisitions with sensor, truth and budget evidence.

Read-only inspection: never generates lots, trains models or selects queries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inspection_review import cli, data, model


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def audit(root):
    root = Path(root).resolve()
    config = data.load_config()
    freeze, fitted = cli.verify_freeze(root, config, model, cli.REPO, cli.SOURCE_FILES)
    problems = cli.verify_outputs(root / "campaign")
    if problems:
        raise AssertionError(problems)
    records = read(root / "campaign/metrics.json")
    datasets = {m["lot_id"]: m for m in read(root / "campaign/test_manifest.json")["lots"]}
    cached = {}
    totals = {"runs": 0, "sites_reviewed": 0, "attempts": 0, "true_doi_confirmed": 0,
              "reported_false_positives": 0, "budget_violations": 0, "hidden_truth_in_ledger": 0}
    for record in records["runs"]:
        lid = record["lot_id"]
        if lid not in cached:
            directory = Path(datasets[lid]["path"])
            with np.load(directory / "public.npz", allow_pickle=False) as f:
                public = {k: f[k] for k in f.files}
            with np.load(directory / "oracle.npz", allow_pickle=False) as f:
                oracle = {k: f[k] for k in f.files}
            cached[lid] = public, oracle, model.predict(fitted, public["features"])
        public, oracle, frozen_p = cached[lid]
        path = root / "campaign" / record["ledger"]
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        visited, past_labels = set(), set()
        spent, confirmed, false_positives, frozen_fn, confident_fn = 0., 0, 0, 0, 0
        outside_confirmed, missed, unresolved, failures, missing, retries = 0, 0, 0, 0, 0, 0
        current_wafer, current_xy = None, None
        c = config["cost"]
        for step, row in enumerate(rows, 1):
            i = row["site_index"]
            assert i not in visited and row["step"] == step
            visited.add(i)
            assert not ({"doi", "true_kind", "electrical_effect", "review_positive"} & row.keys())
            assert {e["site_id"] for e in row["evidence_refs"]} <= past_labels
            assert row["site_id"] == str(public["site_ids"][i])
            assert abs(row["baseline_p"] - frozen_p[i]) < 1e-12
            candidate = bool(public["candidate"][i])
            if record["mode"] == "candidate_only":
                assert candidate
            wafer = int(public["wafer"][i])
            xy = public["xy"][i]
            switch = current_wafer is None or wafer != current_wafer
            distance = 0 if switch else float(np.linalg.norm(xy - current_xy))
            first = (c["wafer_load"] if switch else 0) + c["stage_base"] + c["stage_per_normalized_distance"] * distance + c["dwell"] + (0 if candidate else c["outside_rescan"])
            assert abs(row["reserved_cost"] - first - c["retry_dwell"] * c["retry_limit"]) < 1e-9
            assert spent + row["reserved_cost"] <= record["budget"] + 1e-9
            observations = row["attempts"]
            assert len(observations) in (1, 2)
            for a, obs in enumerate(observations):
                expected = data.review_observation(oracle, i, a)
                assert obs["attempt"] == a
                assert {k: obs[k] for k in expected} == expected
                failures += obs["status"] == "failure"
                missing += obs["status"] == "missing"
            if len(observations) == 2:
                assert observations[0]["status"] in ("failure", "missing")
                retries += 1
            charged = first + c["retry_dwell"] * (len(observations) - 1)
            assert abs(row["charged"] - charged) < 1e-9
            spent += charged
            assert abs(row["cumulative_spend"] - spent) < 1e-8
            ok = [o for o in observations if o["status"] == "ok"]
            positive = any(o["reported_doi"] for o in ok)
            assert row["reported_positive"] == positive
            if ok:
                past_labels.add(row["site_id"])
            else:
                unresolved += 1
            true = bool(oracle["doi"][i])
            confirmed += true and positive
            false_positives += not true and positive
            missed += true and bool(ok) and not positive
            outside_confirmed += true and positive and not candidate
            frozen_fn += true and positive and candidate and frozen_p[i] < config["model"]["classification_threshold"]
            confident_fn += true and positive and candidate and frozen_p[i] < config["model"]["confident_negative_threshold"]
            current_wafer, current_xy = wafer, xy
        metrics = record["metrics"]
        computed = {"true_doi_confirmed": confirmed, "reported_false_positives": false_positives,
                    "reviewed_true_doi_missed": missed, "unresolved_sites": unresolved,
                    "confirmed_true_doi_outside_candidates": outside_confirmed,
                    "discovered_frozen_false_negatives": frozen_fn,
                    "discovered_frozen_confident_false_negatives": confident_fn,
                    "attempted_sites": len(rows), "attempts": sum(len(r["attempts"]) for r in rows),
                    "failures": failures, "missing": missing, "retries": retries}
        for k, value in computed.items():
            assert metrics[k] == value, (record["lot_id"], record["policy"], k)
        assert abs(spent - metrics["spent"]) < 1e-8 and spent <= record["budget"] + 1e-9
        totals["runs"] += 1
        totals["sites_reviewed"] += len(rows)
        totals["attempts"] += computed["attempts"]
        totals["true_doi_confirmed"] += confirmed
        totals["reported_false_positives"] += false_positives
    protected = read(cli.REPO / "evidence/inspection-research/protected-before-v1.json")
    for path, expected in protected["protected_sha256"].items():
        assert hashlib.sha256((cli.REPO / path).read_bytes()).hexdigest() == expected
    return {"status": "pass", "freeze_sha256": freeze["receipt_sha256"], "totals": totals,
            "distinct_test_lots": len(datasets), "protected_legacy_files": len(protected["protected_sha256"]),
            "checks": ["artifact integrity", "only selected paid observations", "sensor outcome invariance",
                       "past-only decision evidence", "no hidden truth fields", "frozen predictions",
                       "independent stage/load/rescan/retry accounting", "reservation before observation",
                       "unique selected sites", "posthoc true-positive and frozen-candidate-FN reconciliation",
                       "legacy frozen input preservation"],
            "note": "Totals across policies/budgets are repeated reviews of the same sites; not independent lot or defect counts."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))

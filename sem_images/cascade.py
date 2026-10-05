"""Fast-score -> slow-decision cascade helper.

Every item gets a fast score; only items at or above the gate threshold reach the slow model.
The log records gate skips and per-stage latency. A cascade summary is refused until gate misses
(positives skipped by the gate) have been evaluated against labels, so a speed gain can never be
reported without the misses it introduced.
"""

import time

import numpy as np


class GateMissEvaluationRequired(RuntimeError):
    pass


def run(item_ids, fast_score, slow_decision, gate_threshold, clock=time.perf_counter):
    """fast_score(id) -> float; slow_decision(id) -> bool (defect). Returns a log (no labels used)."""
    log = []
    for iid in item_ids:
        t0 = clock()
        score = float(fast_score(iid))
        t1 = clock()
        gated = score >= gate_threshold
        row = {"id": iid, "fast_score": score, "fast_seconds": t1 - t0, "sent_to_slow": gated,
               "slow_seconds": None, "decision": False, "decided_by": "gate_skip"}
        if gated:
            decision = bool(slow_decision(iid))
            row.update(slow_seconds=clock() - t1, decision=decision, decided_by="slow")
        row["total_seconds"] = clock() - t0
        log.append(row)
    return {"gate_threshold": gate_threshold, "items": log}


def evaluate_misses(cascade_log, labels):
    """labels: id -> bool (truth defect). Labels are read only after the log is complete."""
    rows = cascade_log["items"]
    missing = [r["id"] for r in rows if r["id"] not in labels]
    if missing:
        raise KeyError(f"labels missing for {len(missing)} logged items")
    positives = [r for r in rows if labels[r["id"]]]
    gate_misses = [r["id"] for r in positives if not r["sent_to_slow"]]
    slow_misses = [r["id"] for r in positives if r["sent_to_slow"] and not r["decision"]]
    return {"positives": len(positives), "gate_misses": len(gate_misses), "gate_miss_ids": gate_misses,
            "slow_misses": len(slow_misses),
            "gate_miss_rate": (len(gate_misses) / len(positives)) if positives else None,
            "end_to_end_recall": ((len(positives) - len(gate_misses) - len(slow_misses)) / len(positives))
            if positives else None}


def summarize(cascade_log, miss_evaluation=None):
    if miss_evaluation is None:
        raise GateMissEvaluationRequired("evaluate gate misses against labels before summarizing a cascade")
    rows = cascade_log["items"]

    def pct(values):
        values = [v for v in values if v is not None]
        if not values:
            return {"n": 0, "p50": None, "p95": None}
        return {"n": len(values), "p50": float(np.percentile(values, 50)), "p95": float(np.percentile(values, 95))}

    return {"items": len(rows), "gate_threshold": cascade_log["gate_threshold"],
            "sent_to_slow": sum(r["sent_to_slow"] for r in rows),
            "gate_skips": sum(not r["sent_to_slow"] for r in rows),
            "latency_seconds": {"fast": pct([r["fast_seconds"] for r in rows]),
                                "slow": pct([r["slow_seconds"] for r in rows]),
                                "total": pct([r["total_seconds"] for r in rows])},
            "misses": miss_evaluation}

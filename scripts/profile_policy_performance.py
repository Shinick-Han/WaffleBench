"""Exactness gate and timing harness for the policy ranking code.

Replays every recorded ``decision`` event of one or more finished campaign roots
READ-ONLY (the ledger file is only opened for reading; no Campaign lock, no
simulation, no LLM), rebuilds the policy inputs from the queries revealed before
each decision, and compares three things field by field with ``==``:

* the scalar baseline below (a verbatim copy of the base-commit implementation,
  1eb3606, kept independent of ``falsify_lab.policies``),
* the current ``falsify_lab.policies.build_decision`` / ``rank``,
* the decision fields published in the ledger.

Only if every decision matches exactly are timings taken. Cold timings run each
implementation once over all decisions in a fresh interpreter; warm timings
repeat in-process passes after one discarded warm-up pass. Baseline and candidate
alternate (and swap who goes first) on every repetition; medians are reported.
The output JSON holds only counts, timings and a digest -- no point outcomes or
SDK metadata. Measured speedup covers policy ranking only, not ngspice or LLM time.

    python scripts/profile_policy_performance.py --root <campaign-root> [--root ...] --output <path.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from falsify_lab import policies  # noqa: E402
from falsify_lab.protocol import Point, Protocol, distance, load_protocol  # noqa: E402


# ------------------------------------------------------------ scalar baseline (base 1eb3606, verbatim)
@dataclass(frozen=True)
class BaseScored:
    point: Point
    score: float
    idw_pred: float | None
    dmin: float


def base_idw_predict(x: Point, evidence: list[tuple[Point, float]], power: int = 2, eps: float = 1e-9) -> float | None:
    if not evidence:
        return None
    num = 0.0
    den = 0.0
    for p, err in evidence:
        w = 1.0 / (distance(x, p) ** power + eps)
        num += w * err
        den += w
    return num / den


def base_min_distance(x: Point, queried: list[Point]) -> float:
    return min(distance(x, q) for q in queried)


def base_rank(
    policy: str,
    protocol: Protocol,
    candidates: list[Point],
    evidence: list[tuple[Point, float]],
    queried: list[Point],
    random_order: tuple[str, ...] = (),
) -> list[BaseScored]:
    pp = protocol.policy_parameters
    weight, divisor = float(pp["exploration_weight"]), float(pp["distance_divisor"])
    power, eps = int(pp["idw_power"]), float(pp["idw_epsilon"])
    position = {pid: i for i, pid in enumerate(random_order)}
    scored = []
    for c in sorted(set(candidates)):
        pred = base_idw_predict(c, evidence, power, eps)
        dmin = base_min_distance(c, queried)
        if policy == "adaptive_idw_plus_distance":
            score = pred + weight * dmin / divisor
        elif policy == "idw_without_distance":
            score = pred
        elif policy == "space_filling":
            score = dmin
        elif policy == "random":
            if c.id not in position:
                raise ValueError(f"{c.id} missing from the random order")
            score = -float(position[c.id])
        else:
            raise ValueError(f"unknown policy {policy!r}")
        scored.append(BaseScored(c, score, pred, dmin))
    scored.sort(key=lambda s: -s.score)
    return scored


def base_candidate_view(s: BaseScored, role: str) -> dict[str, Any]:
    return {
        "point_id": s.point.id,
        "pvt": s.point.pvt,
        "role": role,
        "score": s.score,
        "idw_predicted_abs_error": s.idw_pred,
        "min_normalized_distance": s.dmin,
        "cost_queries": 1,
    }


def base_build_decision(
    policy: str,
    protocol: Protocol,
    candidates: list[Point],
    evidence: list[tuple[Point, float]],
    queried: list[Point],
    random_order: tuple[str, ...],
    previous_evidence: list[tuple[Point, float]],
    evidence_result_ids: list[str],
) -> dict[str, Any]:
    after = base_rank(policy, protocol, candidates, evidence, queried, random_order)
    before = base_rank(policy, protocol, candidates, previous_evidence, queried, random_order)
    selected = after[0]
    exploit = base_rank("idw_without_distance", protocol, candidates, evidence, queried)[0]
    maximin = base_rank("space_filling", protocol, candidates, evidence, queried)
    explore = next((s for s in maximin if s.point.id != exploit.point.id), None)
    by_id = {s.point.id: s for s in after}
    views = [base_candidate_view(by_id[exploit.point.id], "exploitation")]
    if explore is not None:
        views.append(base_candidate_view(by_id[explore.point.id], "exploration"))
    if selected.point.id not in {v["point_id"] for v in views}:
        views.append(base_candidate_view(selected, "policy_choice"))
    return {
        "policy": policy,
        "evidence_result_ids": evidence_result_ids,
        "candidates": views,
        "selected_point_id": selected.point.id,
        "selected_score": selected.score,
        "rank_before": [s.point.id for s in before[:policies.RANK_DEPTH]],
        "rank_after": [s.point.id for s in after[:policies.RANK_DEPTH]],
        "selection_changed": before[0].point.id != selected.point.id,
        "score_rule": {
            "adaptive_idw_plus_distance": "idw + 0.05*min_distance/2",
            "idw_without_distance": "idw",
            "space_filling": "min_distance",
            "random": "-position in frozen order",
        }[policy],
    }


# ------------------------------------------------------------ read-only ledger replay
PUBLISHED_FIELDS = (
    "policy", "evidence_result_ids", "candidates", "selected_point_id", "selected_score",
    "rank_before", "rank_after", "selection_changed", "score_rule",
)


def _inputs(proto: Protocol, queries: list[dict[str, Any]]) -> dict[str, Any]:
    """Same reconstruction as benchmark.policy_inputs, from the revealed queries only."""
    evidence: list[tuple[Point, float]] = []
    ev_ids: list[str] = []
    seen: set[str] = set()
    for q in queries:
        if q["status"] == "success" and q["point_id"] not in seen:
            evidence.append((Point.parse(q["point_id"]), q["evaluation"]["abs_relative_error"]))
            ev_ids.append(q["result_id"])
            seen.add(q["point_id"])
    latest = None
    last = queries[-1] if queries else None
    if last is not None and last["status"] == "success" and not last.get("repeat"):
        latest = (Point.parse(last["point_id"]), last["evaluation"]["abs_relative_error"])
    previous = [e for e in evidence if e != latest] if latest else list(evidence)
    queried_ids = {p.id for p in proto.training} | {q["point_id"] for q in queries}
    queried = sorted(Point.parse(pid) for pid in queried_ids)
    candidates = [p for p in proto.search_pool if p.id not in queried_ids]
    return {"evidence": evidence, "previous": previous, "queried": queried, "candidates": candidates, "evidence_ids": ev_ids}


def load_decisions(root: Path, proto: Protocol) -> list[dict[str, Any]]:
    """Every recorded decision with the inputs it was made from. Opens the ledger read-only."""
    path = Path(root) / "ledger" / "events.jsonl"
    with open(path, "rb") as fh:
        events = [json.loads(line) for line in fh if line.strip()]
    created = events[0]["payload"]
    if events[0]["type"] != "campaign_created" or created["protocol_sha256"] != proto.protocol_sha256:
        raise SystemExit(f"{root}: not a campaign of the current frozen protocol")
    runs: dict[str, dict[str, Any]] = {}
    out = []
    for e in events:
        p = e["payload"]
        if e["type"] == "run_opened":
            runs[p["run_id"]] = {"seed": p["seed"], "policy": p["policy"], "queries": [], "by_index": {}}
        elif e["type"] == "query_admitted":
            q = {**p, "status": "pending"}
            runs[p["run_id"]]["by_index"][p["query_index"]] = q
            runs[p["run_id"]]["queries"].append(q)
        elif e["type"] == "query_result":
            runs[p["run_id"]]["by_index"][p["query_index"]].update(p)
        elif e["type"] == "decision":
            run = runs[p["run_id"]]
            inp = _inputs(proto, [dict(q) for q in run["queries"]])
            out.append({
                "root": str(root), "run_id": p["run_id"], "sequence": p["sequence"],
                "args": (run["policy"], proto, inp["candidates"], inp["evidence"], inp["queried"],
                         proto.replicate(run["seed"]).random_order, inp["previous"], inp["evidence_ids"]),
                "published": {k: p[k] for k in PUBLISHED_FIELDS},
            })
    return out


def _rank_rows(r: list[Any]) -> list[tuple[Any, ...]]:
    return [(s.point, s.score, s.idw_pred, s.dmin) for s in r]


def compare(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Strict equality at every decision: baseline == candidate == published, plus full rankings."""
    mismatches = []
    digest = hashlib.sha256()
    for d in decisions:
        args = d["args"]
        base = base_build_decision(*args)
        cand = policies.build_decision(*args)
        published = d["published"]
        problems = []
        if base != cand:
            problems.append("candidate != baseline")
        if {k: base[k] for k in PUBLISHED_FIELDS} != published:
            problems.append("baseline != published")
        policy, proto, cands, evidence, queried, order, previous, _ = args
        for pol in policies.LABELS:
            for ev in (evidence, previous):
                ro = order if pol == "random" else ()
                if _rank_rows(base_rank(pol, proto, cands, ev, queried, ro)) != _rank_rows(policies.rank(pol, proto, cands, ev, queried, ro)):
                    problems.append(f"full rank differs for {pol}")
        if problems:
            mismatches.append({"root": d["root"], "run_id": d["run_id"], "sequence": d["sequence"], "problems": problems})
        digest.update(json.dumps(cand, sort_keys=True).encode("utf-8"))
    return {"decisions": len(decisions), "mismatches": mismatches, "candidate_sha256": digest.hexdigest()}


# ------------------------------------------------------------ timing
IMPLS = {"baseline": base_build_decision, "candidate": policies.build_decision}


def one_pass(fn: Any, decisions: list[dict[str, Any]]) -> float:
    t0 = time.perf_counter()
    for d in decisions:
        fn(*d["args"])
    return time.perf_counter() - t0


def cold_child(roots: list[str], impl: str) -> None:
    proto = load_protocol()
    decisions = [d for r in roots for d in load_decisions(Path(r), proto)]
    print(json.dumps({"seconds": one_pass(IMPLS[impl], decisions)}))


def cold_once(roots: list[Path], impl: str) -> float:
    cmd = [sys.executable, str(Path(__file__).resolve()), "--cold-child", impl]
    for r in roots:
        cmd += ["--root", str(r)]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    return float(json.loads(out.strip().splitlines()[-1])["seconds"])


def summarize(xs: list[float]) -> dict[str, Any]:
    return {"median_s": statistics.median(xs), "min_s": min(xs), "max_s": max(xs), "runs_s": xs}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", action="append", required=True, type=Path, help="finished campaign root (read-only); repeatable")
    ap.add_argument("--output", type=Path, help="JSON result path")
    ap.add_argument("--repeats", type=int, default=7, help="alternated repetitions per timing mode")
    ap.add_argument("--cold-child", choices=sorted(IMPLS), help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.cold_child:
        cold_child([str(r) for r in args.root], args.cold_child)
        return 0
    if args.output is None:
        ap.error("--output is required")
    proto = load_protocol()
    decisions = [d for r in args.root for d in load_decisions(r, proto)]
    gate = compare(decisions)
    result: dict[str, Any] = {
        "roots": [str(r) for r in args.root],
        "python": platform.python_version(),
        "platform": platform.platform(),
        "equality_gate": {"passed": not gate["mismatches"] and gate["decisions"] > 0, **gate},
        "scope": "policy build_decision only; no ngspice, LLM or end-to-end campaign time",
    }
    if result["equality_gate"]["passed"]:
        cold: dict[str, list[float]] = {k: [] for k in IMPLS}
        warm: dict[str, list[float]] = {k: [] for k in IMPLS}
        for i in range(args.repeats):
            order = list(IMPLS) if i % 2 == 0 else list(IMPLS)[::-1]
            for impl in order:
                cold[impl].append(cold_once(args.root, impl))
        for impl in IMPLS:
            one_pass(IMPLS[impl], decisions)  # discarded warm-up
        for i in range(args.repeats):
            order = list(IMPLS) if i % 2 == 0 else list(IMPLS)[::-1]
            for impl in order:
                warm[impl].append(one_pass(IMPLS[impl], decisions))
        result["timing"] = {
            "repeats": args.repeats,
            "decisions_per_pass": len(decisions),
            "cold_fresh_process": {k: summarize(v) for k, v in cold.items()},
            "warm_in_process": {k: summarize(v) for k, v in warm.items()},
            "speedup_median": {
                "cold": statistics.median(cold["baseline"]) / statistics.median(cold["candidate"]),
                "warm": statistics.median(warm["baseline"]) / statistics.median(warm["candidate"]),
            },
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    g = result["equality_gate"]
    print(json.dumps({"equality_passed": g["passed"], "decisions": g["decisions"], "mismatches": len(g["mismatches"]),
                      "speedup_median": result.get("timing", {}).get("speedup_median"), "output": str(args.output)}))
    return 0 if g["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

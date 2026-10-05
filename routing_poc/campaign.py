"""Preregistered synthetic comparison campaign for the routing PoC (experiment scope).

``run_campaign(out, split='development', protocol=None, *, confirm_synthetic_test=False)``

- ``development`` (default) runs seeds 12000..12007; a caller may pass a reduced
  development protocol for checks, but never one touching reserved test seeds.
- ``test`` runs the reserved independent synthetic seeds 22000..22039 only with
  ``confirm_synthetic_test=True`` and only with the frozen ``PROTOCOL`` (no tuning on
  test). The protocol hash and source digests are written before the first job runs.

Both regimes; every policy replays the same archive per lot (paired). Policy RNG is
derived from the public job id + policy name, never from the latent fixture seed.
Output must not exist; it is created fresh and every artifact (including partial runs and
the failure record) is kept on failure. Source digests are re-verified after execution.
The summary reports paired lot-level bootstrap CIs (2000 replicates, seed 2026100511,
resampling whole lots). ``commercial_validated`` is always ``False``: a synthetic gain is
not an equipment or ROI claim, and recall is candidate-space recall only.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import platform
import random
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import fixtures, metrics

PROTOCOL: dict[str, Any] = {
    "name": "routing-poc-synthetic-comparison",
    "version": 1,
    "created": "2026-10-05",
    "contract": "ROUTING_POC_CONTRACT.md",
    "post_hackathon": True,
    "fixture_version": fixtures.FIXTURE_VERSION,
    "regimes": list(fixtures.REGIMES),
    "primary_regime": "clustered",
    "budgets_s": [120, 240],
    "audit_epsilon": 0.1,
    "policies": ["risk_only", "risk_per_second", "beam_route"],
    "development_seeds": list(range(12000, 12008)),
    "test_seeds": list(range(22000, 22040)),
    "primary_metric": "confirmed_reference_doi",
    "metrics": ["confirmed_reference_doi", "escapes", "false_confirmations", "candidate_recall",
                "detector_positive_reports", "selected", "spent_s"],
    "comparisons": [["beam_route", "risk_per_second"], ["beam_route", "risk_only"],
                    ["risk_per_second", "risk_only"]],
    "bootstrap": {"replicates": 2000, "seed": 2026100511, "unit": "lot", "interval": "percentile_95"},
    "policy_rng": "random.Random(int(sha256(job_id + '|' + policy)[:16], 16))",
    "commercial_validated": False,
}
SOURCE_MODULES = ("routing_poc", "routing_poc.contracts", "routing_poc.replay", "routing_poc.policies",
                  "routing_poc.fixtures", "routing_poc.metrics", "routing_poc.campaign", "routing_poc.cli")
SPLITS = ("development", "test")
LIMITATIONS = [
    "Synthetic fixtures with invented physical-layer parameters; not equipment evidence.",
    "Replay resource seconds are modeled action charges, not full physical elapsed time.",
    "Candidate-space recall only; never wafer-wide recall.",
    "No commercial ROI, throughput or superiority claim follows from this campaign.",
]


class CampaignError(RuntimeError):
    pass


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def policy_seed(job_id: str, policy: str) -> int:
    """Policy RNG seed from public job id + policy only (never the latent fixture seed)."""
    return int(hashlib.sha256(f"{job_id}|{policy}".encode()).hexdigest()[:16], 16)


def _resolve_protocol(split: str, protocol: dict | None, confirm: bool) -> dict:
    if split not in SPLITS:
        raise CampaignError(f"split must be one of {SPLITS}")
    if split == "test":
        if not confirm:
            raise CampaignError("reserved synthetic test requires explicit confirm_synthetic_test=True")
        if protocol is not None and canonical_hash(protocol) != canonical_hash(PROTOCOL):
            raise CampaignError("reserved synthetic test runs only the frozen PROTOCOL (no tuning on test)")
        return copy.deepcopy(PROTOCOL)
    if confirm:
        raise CampaignError("confirm_synthetic_test applies only to split='test'")
    proto = copy.deepcopy(PROTOCOL if protocol is None else protocol)
    if set(proto) != set(PROTOCOL):
        raise CampaignError("protocol keys differ from the frozen PROTOCOL")
    if proto["test_seeds"] != PROTOCOL["test_seeds"]:
        raise CampaignError("reserved test seeds cannot be changed")
    dev = proto["development_seeds"]
    if not dev or any(isinstance(s, bool) or not isinstance(s, int) for s in dev):
        raise CampaignError("development_seeds must be a non-empty list of integers")
    if set(dev) & set(PROTOCOL["test_seeds"]):
        raise CampaignError("development seeds overlap reserved synthetic test seeds")
    if not set(proto["regimes"]) <= set(fixtures.REGIMES) or not proto["regimes"]:
        raise CampaignError("unknown regime in protocol")
    if not set(proto["policies"]) <= set(PROTOCOL["policies"]) or not proto["policies"]:
        raise CampaignError("unknown policy in protocol")
    if proto["commercial_validated"] is not False:
        raise CampaignError("commercial_validated must stay False")
    return proto


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_digests() -> dict:
    out: dict[str, Any] = {}
    for name in SOURCE_MODULES:
        try:
            mod = importlib.import_module(name)
        except ImportError as exc:
            out[name] = {"status": "unavailable", "error": str(exc)}
            continue
        f = getattr(mod, "__file__", None)
        if not f or not Path(f).is_file():
            out[name] = {"status": "unavailable", "error": "no source file (namespace package?)"}
            continue
        out[name] = {"status": "hashed", "path": Path(f).name, "sha256": _sha256_file(Path(f))}
    contract = Path(__file__).resolve().parents[1] / "ROUTING_POC_CONTRACT.md"
    out["ROUTING_POC_CONTRACT.md"] = ({"status": "hashed", "path": contract.name, "sha256": _sha256_file(contract)}
                                      if contract.is_file() else {"status": "unavailable", "error": "not found"})
    return out


def _environment() -> dict:
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "runtime_dependencies": "python stdlib only"}


def _write(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1, sort_keys=True), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def paired_lot_bootstrap(lot_diffs: dict[str, float], replicates: int, seed: int, key: str = "") -> dict:
    """Percentile 95% CI of the mean paired difference, resampling whole lots."""
    lots = sorted(lot_diffs)
    vals = [float(lot_diffs[k]) for k in lots]
    n = len(vals)
    if n == 0:
        return {"n_lots": 0, "mean_difference": None, "ci95": None, "ci_excludes_zero": None}
    mean = sum(vals) / n
    if n < 2:
        return {"n_lots": n, "mean_difference": mean, "ci95": None, "ci_excludes_zero": None}
    rng = random.Random(f"{seed}|{key}")
    means = sorted(sum(vals[rng.randrange(n)] for _ in range(n)) / n for _ in range(replicates))
    lo, hi = means[int(math.floor(0.025 * replicates))], means[int(math.ceil(0.975 * replicates)) - 1]
    return {"n_lots": n, "mean_difference": mean, "ci95": [lo, hi], "ci_excludes_zero": lo > 0 or hi < 0,
            "replicates": replicates, "resampling_unit": "lot"}


def summarize(records: list[dict], proto: dict) -> dict:
    cells = []
    boot = proto["bootstrap"]
    for regime in proto["regimes"]:
        for budget in proto["budgets_s"]:
            sub = [r for r in records if r["regime"] == regime and r["budget_s"] == budget]
            by_policy: dict[str, dict] = {}
            for pol in proto["policies"]:
                rs = [r for r in sub if r["policy"] == pol]
                agg = {"n_lots": len(rs),
                       "decision_wall_s_total": sum(r["metrics"]["decision_wall_s"] or 0.0 for r in rs),
                       "observer_wall_s_total": sum(r["metrics"]["observer_wall_s"] or 0.0 for r in rs)}
                for m in proto["metrics"]:
                    vals = [r["metrics"][m] for r in rs]
                    agg[f"mean_{m}"] = (sum(vals) / len(vals)) if vals and all(v is not None for v in vals) else None
                by_policy[pol] = agg
            comparisons = []
            for a, b in proto["comparisons"]:
                if a not in proto["policies"] or b not in proto["policies"]:
                    continue
                for m in proto["metrics"]:
                    va = {r["job_id"]: r["metrics"][m] for r in sub if r["policy"] == a}
                    vb = {r["job_id"]: r["metrics"][m] for r in sub if r["policy"] == b}
                    diffs = {j: va[j] - vb[j] for j in va
                             if j in vb and va[j] is not None and vb[j] is not None}
                    key = f"{regime}|{budget}|{a}-{b}|{m}"
                    comparisons.append({"a": a, "b": b, "metric": m, "difference": "a - b",
                                        **paired_lot_bootstrap(diffs, boot["replicates"], boot["seed"], key)})
            cells.append({"regime": regime, "budget_s": budget, "primary": regime == proto["primary_regime"],
                          "policies": by_policy, "paired": comparisons})
    return {"cells": cells, "limitations": LIMITATIONS, "commercial_validated": False,
            "evidence_mode": "synthetic", "evidence_label": metrics.EVIDENCE_LABELS["synthetic"],
            "resource_seconds_note": metrics.RESOURCE_SECONDS_NOTE, "recall_scope": metrics.RECALL_SCOPE_NOTE,
            "wall_time_note": "decision/observer wall seconds are measured separately from replay resource seconds"}


def _load_dependencies() -> tuple[Any, Any, Any]:
    try:
        return (importlib.import_module("routing_poc.contracts"), importlib.import_module("routing_poc.replay"),
                importlib.import_module("routing_poc.policies"))
    except ImportError as exc:
        raise CampaignError(f"routing_poc adapter/policy modules unavailable: {exc}") from exc


def run_campaign(out: str | Path, split: str = "development", protocol: dict | None = None, *,
                 confirm_synthetic_test: bool = False) -> dict:
    proto = _resolve_protocol(split, protocol, confirm_synthetic_test)
    out = Path(out)
    if out.exists():
        raise CampaignError(f"output {out} already exists; campaigns never overwrite or resume")
    contracts, replay, policies = _load_dependencies()
    out.mkdir(parents=True)
    started = _now()
    status: dict[str, Any] = {"status": "running", "split": split, "started_at": started,
                              "commercial_validated": False, "post_hackathon": True}
    _write(out / "status.json", status)
    phase = "freeze"
    try:
        proto_hash = canonical_hash(proto)
        _write(out / "protocol.json", {"protocol": proto, "protocol_sha256": proto_hash, "split": split,
                                       "frozen_at": started})
        before = _source_digests()
        _write(out / "source_freeze.json", {"digests": before, "frozen_at": started})
        if split == "test" and any(v["status"] != "hashed" for v in before.values()):
            raise CampaignError("reserved test requires every source file to be hashed before execution")
        _write(out / "environment.json", _environment())
        status.update(protocol_sha256=proto_hash)
        _write(out / "status.json", status)

        phase = "execute"
        seeds = proto["development_seeds"] if split == "development" else proto["test_seeds"]
        eps = proto["audit_epsilon"]
        records = []
        for regime in proto["regimes"]:
            for seed in seeds:
                raw_job, raw_archive = fixtures.make_job(seed, regime)
                job = contracts.validate_job(raw_job)
                archive = contracts.validate_archive(job, raw_archive)
                jid = job["job_id"]
                _write(out / "jobs" / f"{jid}.json", {"split": split, "regime": regime, "fixture_seed": seed,
                                                      "job": raw_job, "archive": raw_archive})
                for budget in proto["budgets_s"]:
                    for pol in proto["policies"]:
                        def selector(state, rng, _pol=pol):
                            return policies.select(state, rng, policy=_pol, audit_epsilon=eps)
                        pseed = policy_seed(jid, pol)
                        run = replay.run_loop(job, archive, selector, float(budget), seed=pseed)
                        if run.get("commercial_validated") is not False:
                            raise CampaignError("run_loop output must carry commercial_validated: false")
                        m = metrics.evaluate(job, archive, run)
                        rec = {"split": split, "regime": regime, "job_id": jid, "policy": pol,
                               "budget_s": budget, "policy_seed": pseed, "metrics": m}
                        _write(out / "runs" / f"{jid}__{pol}__{budget}s.json", {**rec, "run": run})
                        records.append(rec)

        phase = "verify_sources"
        after = _source_digests()
        drift = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        _write(out / "source_verification.json", {"verified_at": _now(), "matches": not drift, "drift": drift})
        if drift:
            raise CampaignError(f"source digests changed during execution: {drift}")

        phase = "summarize"
        summary = {"split": split, "protocol_sha256": proto_hash, "created_at": started, "completed_at": _now(),
                   "post_hackathon_notice": "Post-hackathon development — not part of the submitted version.",
                   **summarize(records, proto)}
        _write(out / "summary.json", summary)
        status.update(status="complete", completed_at=summary["completed_at"], runs=len(records))
        _write(out / "status.json", status)
        return {"status": "complete", "out": str(out), "split": split, "protocol_sha256": proto_hash,
                "runs": len(records), "commercial_validated": False}
    except BaseException as exc:
        status.update(status="failed", failed_at=_now(), phase=phase, error=f"{type(exc).__name__}: {exc}")
        _write(out / "status.json", status)
        _write(out / "failure.json", {"phase": phase, "error": status["error"],
                                      "traceback": traceback.format_exc()})
        raise

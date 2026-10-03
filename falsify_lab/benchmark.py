"""Deterministic runner: numerical preflight, calibration, policy runs, posthoc, postflight.

Order inside a campaign (``run_benchmark``)::

    preflight -> calibrate/freeze -> 4 policies x 10 seeds -> all sequences closed
    -> posthoc full-grid reference (includes the 40 held-out points) -> postflight

Every step goes through ``storage.Campaign`` so the campaign's attempt and wall
caps cover all of it. A failed preflight stops before any run. Caps stop the
campaign and leave the remaining runs recorded as not run.
"""

from __future__ import annotations

import time
from typing import Any

from . import model as model_mod
from .policies import build_decision
from .protocol import PREFLIGHT_POINT_IDS, Point, full_grid
from .simulator import NUMERICAL_SETTINGS
from .storage import Campaign, CapReached, StorageError

ADAPTIVE = "adaptive_idw_plus_distance"
RUNNER = "deterministic runner"


class CampaignStopped(StorageError):
    pass


# ---------------------------------------------------------------------- numerical checks
def preflight(c: Campaign) -> dict[str, Any]:
    """Five points x {basic 1 ps, half 0.5 ps, tight 0.5 ps + strict tolerances}. Stored restricted."""
    done = c.events("preflight_completed")
    if done:
        return done[-1]["payload"]
    limit = c.protocol.preflight_max_rel_diff
    checks = []
    for pid in PREFLIGHT_POINT_IDS:
        point = Point.parse(pid)
        res = {s: c.restricted_observe("preflight", point, s) for s in NUMERICAL_SETTINGS}
        ok_meas = all(r["status"] == "success" and r["tphl_s"] > 0 and r["tplh_s"] > 0 for r in res.values())
        diffs: dict[str, float | None] = {"half_vs_basic": None, "tight_vs_basic": None}
        if ok_meas:
            base = res["basic"]["tpd_s"]
            diffs = {f"{s}_vs_basic": abs(res[s]["tpd_s"] - base) / base for s in ("half", "tight")}
        ok = ok_meas and all(d is not None and d <= limit for d in diffs.values())
        checks.append({"point_id": pid, "measured": ok_meas, "relative_differences": diffs, "passed": ok})
    payload = {"passed": all(ch["passed"] for ch in checks), "max_relative_difference": limit, "checks": checks}
    c.append("preflight_completed", payload)
    return payload


def postflight(c: Campaign) -> dict[str, Any] | None:
    """Top-3 |error| search points of adaptive seed 1001 rechecked with half and tight settings."""
    done = c.events("postflight_completed")
    if done:
        return done[-1]["payload"]
    rid = f"benchmark-{ADAPTIVE}-1001"
    if rid not in c.run_ids():
        return None
    st = c.run_state(rid)
    rows: dict[str, dict[str, Any]] = {}
    for q in st["queries"]:
        if q["phase"] != "calibration" and q["status"] == "success" and q["point_id"] not in rows:
            rows[q["point_id"]] = q
    top = sorted(rows.values(), key=lambda q: (-q["evaluation"]["abs_relative_error"], Point.parse(q["point_id"])))[:3]
    limit = c.protocol.preflight_max_rel_diff
    checks = []
    for rank_i, q in enumerate(top, 1):
        point = Point.parse(q["point_id"])
        base = q["observation"]["tpd_s"]
        diffs = {}
        for s in ("half", "tight"):
            r = c.restricted_observe("postflight", point, s, {"rank": rank_i, "source_run": rid, "source_result_id": q["result_id"]})
            diffs[f"{s}_vs_basic"] = abs(r["tpd_s"] - base) / base if r["status"] == "success" else None
        checks.append({
            "rank": rank_i, "point_id": q["point_id"], "source_result_id": q["result_id"],
            "abs_relative_error": q["evaluation"]["abs_relative_error"], "relative_differences": diffs,
            "within_limit": all(d is not None and d <= limit for d in diffs.values()),
        })
    payload = {"source_run": rid, "checks": checks, "max_relative_difference": limit, "reported_separately_from_primary": True}
    c.append("postflight_completed", payload)
    return payload


# ---------------------------------------------------------------------- calibration
def calibrate(c: Campaign) -> model_mod.FrozenModel:
    """Run the nine calibration observations and freeze the model (once per campaign)."""
    existing = c.model()
    if existing is not None:
        return existing
    if "calibration" in c.run_ids():
        raise StorageError("calibration was already attempted in this campaign without freezing; use a fresh root")
    rid = c.open_run("calibration")
    try:
        results = [c.query(rid, p.id, "calibration") for p in c.protocol.training]
    except CapReached as exc:
        c.close_run(rid, exc.reason)
        raise
    closed = c.close_run(rid)
    if closed["status"] != "complete":
        raise StorageError(f"calibration incomplete ({closed['failures']} failed queries); model not frozen")
    obs = [
        {"point_id": r["point_id"], "result_id": r["result_id"], "sim_id": r["sim_id"], "tpd_s": r["observation"]["tpd_s"]}
        for r in results
    ]
    m = model_mod.fit(list(c.protocol.training), obs, c.protocol.protocol_sha256, c.protocol.manifest_sha256)
    c.freeze_model(m)
    return m


# ---------------------------------------------------------------------- policy runs
def policy_inputs(c: Campaign, st: dict[str, Any]) -> dict[str, Any]:
    proto = c.protocol
    evidence: list[tuple[Point, float]] = []
    ev_ids: list[str] = []
    seen: set[str] = set()
    latest_contrib: tuple[Point, float] | None = None
    for q in st["queries"]:
        if q["status"] == "success" and q["point_id"] not in seen:
            item = (Point.parse(q["point_id"]), q["evaluation"]["abs_relative_error"])
            evidence.append(item)
            ev_ids.append(q["result_id"])
            seen.add(q["point_id"])
    last = st["queries"][-1] if st["queries"] else None
    if last is not None and last["status"] == "success" and not last.get("repeat"):
        latest_contrib = (Point.parse(last["point_id"]), last["evaluation"]["abs_relative_error"])
    previous = [e for e in evidence if e != latest_contrib] if latest_contrib else list(evidence)
    queried_ids = {p.id for p in proto.training} | {q["point_id"] for q in st["queries"]}
    queried = sorted(Point.parse(pid) for pid in queried_ids)
    candidates = [p for p in proto.search_pool if p.id not in queried_ids]
    return {"evidence": evidence, "previous": previous, "queried": queried, "candidates": candidates, "evidence_ids": ev_ids}


def preview_decision(c: Campaign, run_id: str) -> dict[str, Any] | None:
    """The decision the fixed rule would record next (nothing is recorded or revealed)."""
    st = c.run_state(run_id)
    if st["closed"] or st["run_kind"] == "calibration":
        return None
    counts = {ph: sum(q["phase"] == ph for q in st["queries"]) for ph in ("calibration", "initial", "search")}
    if counts["calibration"] < 9 or counts["initial"] < 3 or counts["search"] >= 12:
        return None
    inp = policy_inputs(c, st)
    if not inp["candidates"]:
        return None
    rep = c.protocol.replicate(st["seed"])
    return build_decision(st["policy"], c.protocol, inp["candidates"], inp["evidence"], inp["queried"], rep.random_order, inp["previous"], inp["evidence_ids"])


def step(c: Campaign, run_id: str) -> dict[str, Any]:
    """Record the fixed-rule decision for the next selection, then execute it."""
    t0 = time.perf_counter()
    decision = preview_decision(c, run_id)
    if decision is None:
        raise StorageError(f"run {run_id} has no selection left")
    decision["policy_seconds"] = time.perf_counter() - t0
    decision["actor"] = RUNNER
    recorded = c.record_decision(run_id, decision)
    result = c.query(run_id, recorded["selected_point_id"], "search", recorded["sequence"])
    return {"decision": recorded, "result": result}


def open_and_seed(c: Campaign, run_kind: str, policy: str, seed: int) -> str:
    rid = c.open_run(run_kind, policy, seed)
    for p in c.protocol.training:
        c.query(rid, p.id, "calibration")
    for p in c.protocol.replicate(seed).initial:
        c.query(rid, p.id, "initial")
    return rid


def run_policy(c: Campaign, policy: str, seed: int, run_kind: str = "benchmark") -> dict[str, Any]:
    rid = f"{run_kind}-{policy}-{seed}"
    try:
        open_and_seed(c, run_kind, policy, seed)
        for _ in range(int(c.protocol.budget["subsequent_search"])):
            step(c, rid)
    except CapReached as exc:
        if rid in c.run_ids() and c.run_state(rid)["closed"] is None:
            c.close_run(rid, exc.reason)
        raise
    return c.close_run(rid)


# ---------------------------------------------------------------------- posthoc
def posthoc(c: Campaign) -> dict[str, Any]:
    """Full-grid reference (175 points incl. the 40 held-out). Only after every sequence closed."""
    c.begin_posthoc()
    done = {e["payload"]["point_id"] for e in c.events("posthoc_result")}
    for p in full_grid():
        if p.id not in done:
            c.restricted_observe("posthoc", p, "basic", {"partition": c.protocol.partition(p)})
    res = c.events("posthoc_result")
    payload = {"points": len(res), "failures": sum(e["payload"]["status"] != "success" for e in res)}
    if not c.events("posthoc_completed"):
        c.append("posthoc_completed", payload)
    return payload


# ---------------------------------------------------------------------- orchestration
def run_benchmark(
    c: Campaign,
    seeds: list[int] | None = None,
    policies: list[str] | None = None,
    confirm_primary: bool = False,
) -> dict[str, Any]:
    proto = c.protocol
    all_seeds = [r.seed for r in proto.replicates]
    if c.kind == "primary":
        if not confirm_primary:
            raise StorageError("primary benchmark requires explicit coordinator confirmation (M5)")
        if seeds or policies:
            raise StorageError("primary benchmark always runs all policies and seeds")
    if c.kind not in ("primary", "development", "fixture"):
        raise StorageError(f"benchmark is not allowed in a {c.kind!r} campaign")
    seeds = list(seeds or all_seeds)
    policies = list(policies or proto.policies)
    if c.events("benchmark_started"):
        raise StorageError("benchmark already started in this campaign; partial runs are never silently resumed")
    planned = [f"benchmark-{p}-{s}" for s in seeds for p in policies]
    c.append("benchmark_started", {"seeds": seeds, "policies": policies, "planned_runs": planned, "actor": RUNNER})
    status, reason = "complete", None
    try:
        pf = preflight(c)
        if not pf["passed"]:
            status, reason = "aborted", "numerical preflight failed; primary runs not started"
        else:
            calibrate(c)
            for s in seeds:
                for p in policies:
                    run_policy(c, p, s)
            posthoc(c)
            postflight(c)
    except CapReached as exc:
        status, reason = "incomplete", exc.reason
        try:
            c.begin_posthoc()
        except StorageError:
            pass
    except StorageError as exc:
        status, reason = "failed", str(exc)
    run_ids = set(c.run_ids("benchmark"))
    not_run = [r for r in planned if r not in run_ids]
    incomplete = [r for r in run_ids if (c.run_state(r)["closed"] or {}).get("status") != "complete"]
    if status == "complete" and (not_run or incomplete):
        status, reason = "incomplete", "some runs incomplete or not run"
    payload = {"status": status, "reason": reason, "not_run": not_run, "incomplete_runs": sorted(incomplete)}
    c.append("benchmark_finished", payload)
    return payload


def demo_prepare(c: Campaign) -> dict[str, Any]:
    """Preflight, calibration and the live seed-1001 run up to its shared initial queries."""
    pf = preflight(c)
    if not pf["passed"]:
        raise CampaignStopped("numerical preflight failed; live run not prepared")
    calibrate(c)
    seed = int(c.protocol.manifest["live_demo"]["seed"])
    rid = f"live-{ADAPTIVE}-{seed}"
    if rid not in c.run_ids():
        open_and_seed(c, "live", ADAPTIVE, seed)
        c.append("live_prepared", {"run_id": rid, "actor": RUNNER, "adaptive_updates": 0})
    return {"run_id": rid, "next_decision_preview": preview_decision(c, rid)}


def reproduce(c: Campaign) -> dict[str, Any]:
    """Short labelled reproduction: calibration plus adaptive and random seed 1001 (no posthoc)."""
    if c.kind != "reproduce":
        raise StorageError("reproduce runs only in a dedicated reproduce campaign root")
    m = calibrate(c)
    out = {}
    for p in (ADAPTIVE, "random"):
        rid = f"reproduce-{p}-1001"
        if rid not in c.run_ids():
            run_policy(c, p, 1001, run_kind="reproduce")
        out[rid] = c.run_state(rid)["closed"]
    c.append("reproduce_completed", {"runs": list(out), "model_hash": m.model_hash, "actor": RUNNER})
    return {"model_hash": m.model_hash, "runs": out}

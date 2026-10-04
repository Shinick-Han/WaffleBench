"""Bounded posthoc computational discovery cycle over the frozen v3 quality diagnostics.

``prepare(root, evidence_dir)`` copies ``quality-diagnostics.json`` into ``<root>/inputs`` and
records its SHA-256 in the genesis event. ``DiscoveryCycle(root)`` then lets an analyst plan,
an experimenter run and a supervisor finalize at most two distinct catalog computations.

Every catalog test is a pre-authorized READ-ONLY computation over the immutable copied posthoc
ledger summary: zero CU, no external, physical or training action, and no new held-out gain.
The latent-DOI labels inside it are a privileged posthoc oracle; results are evaluation only and
never production selection. Anything outside the catalog is refused with ``approval_required``.

State is an append-only hash-chained ``events.jsonl`` plus a ``head.json`` (count, last hash),
written only under the shared cross-process lock and verified, with the input hash, on every
call. Refusals and reads never append.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from inspection_live.session import LiveError, clean_message, exclusive_lock

SCHEMA = 1
KIND = "discovery_cycle_posthoc"
INPUT_NAME = "quality-diagnostics.json"
INPUT_KIND = "inspection_v3_quality_diagnostics"
BUDGET = 2
MAX_TEXT = 2000
MAX_ACTOR = 120
ROLES = ("analyst", "experimenter", "supervisor")
ROLE_METHODS = {
    "analyst": ("read_context", "record_plan", "record_update"),
    "experimenter": ("read_context", "run_experiment"),
    "supervisor": ("read_context", "finalize"),
}
ID_RE = re.compile(r"^(plan|res)_[0-9a-f]{16}$")
_SECRET = re.compile(r"(?i)\b(?:sk|pk|ghp|gho|xox[abp])[-_][A-Za-z0-9_-]{8,}|\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+")
DATA_LABEL = ("authored numeric synthetic posthoc diagnostic of the frozen v3 cb400_route_full candidate-only "
              "ledgers; not SEM-image accuracy, factory throughput, wafer yield, a new held-out gain or a "
              "physical experiment")
EVALUATION_LABEL = ("privileged posthoc oracle evaluation over immutable frozen ledgers; never production "
                    "selection, never a new held-out gain")
PRIMARY = {
    "source": "INSPECTION_V3_RESULTS.md preregistered primary (unchanged by this cycle)",
    "comparison": "cb400_route_full vs freshly fitted logistic probability-per-cost baseline, 360 CU, 100 paired lots",
    "metric": "confirmed DOI per lot",
    "candidate_mean": 29.33,
    "baseline_mean": 27.38,
    "paired_mean_difference": 1.95,
    "ci95": [1.46, 2.43],
    "relative_gain_pct": 7.12,
}
ZERO_COST = {"cu": 0, "external_calls": 0, "physical_actions": 0, "training_runs": 0}
CATALOG = {
    "miss_partition": {
        "title": "Conserved partition of latent DOI",
        "question": "Where do latent DOI per lot go: never optically admitted, admitted but unselected, or selected "
                    "(and of those confirmed, sensor-missed or unresolved)?",
        "computation": "read-only sums over overall and per_scenario counts/means; checks both conservation "
                       "identities exactly on integer counts",
        "cost": ZERO_COST,
        "feasibility": "available: local read-only arithmetic on the copied frozen file",
    },
    "capacity_bound": {
        "title": "Optimistic review-capacity bound",
        "question": "How many paid reviews can 360 CU hold at the authored minimum fees, and what fraction of that "
                    "loose bound do observed visits reach?",
        "computation": "recomputes max n with first + later*(n-1) + retry_reserve <= budget from the authored fees "
                       "and compares with the recorded visit bound and observed visits",
        "cost": ZERO_COST,
        "feasibility": "available: local read-only arithmetic on the copied frozen file",
    },
    "calibration_shift": {
        "title": "Calibration gap by scenario",
        "question": "Is the frozen model's probability calibrated against latent truth, and does the gap shift "
                    "by scenario?",
        "computation": "count-weighted signed and absolute (mean_p - latent rate) gaps over the five fixed "
                       "per_scenario calibration_candidates bins, recomputed from n and latent_doi",
        "cost": ZERO_COST,
        "feasibility": "available: local read-only arithmetic on the copied frozen file",
    },
}


class CycleError(RuntimeError):
    """A refused call. Nothing was appended."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sanitize(text: str) -> str:
    return _SECRET.sub("<redacted>", clean_message(text))


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise CycleError("invalid", f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > MAX_TEXT:
        raise CycleError("invalid", f"{name} must be non-empty and at most {MAX_TEXT} characters")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise CycleError("invalid", f"{name} contains control characters")
    if len(re.findall(r"[^\W\d_]{2,}", value)) < 3:
        raise CycleError("invalid", f"{name} must be human-readable prose (at least three words)")
    return _sanitize(value)


def _id_list(name: str, value: Any, limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(v, str) and len(v) <= 64 for v in value):
        raise CycleError("invalid", f"{name} must be a list of at most {limit} short strings")
    if len(set(value)) != len(value):
        raise CycleError("invalid", f"{name} has duplicates")
    return list(value)


# ---------------------------------------------------------------- prepare
def prepare(root: str | os.PathLike, evidence_dir: str | os.PathLike) -> dict:
    """Create a new cycle root from ``<evidence_dir>/quality-diagnostics.json``. Refuses an existing root."""
    root, src = Path(root), Path(evidence_dir) / INPUT_NAME
    if root.exists():
        raise CycleError("conflict", "root already exists; a cycle is never re-prepared or resumed in place")
    if not src.is_file():
        raise CycleError("invalid", f"{INPUT_NAME} not found in evidence_dir")
    raw = src.read_bytes()
    doc = json.loads(raw)
    if doc.get("kind") != INPUT_KIND or doc.get("replaces_primary") is not False or "overall" not in doc:
        raise CycleError("invalid", "input is not a frozen v3 quality-diagnostics file")
    (root / "inputs").mkdir(parents=True)
    dst = root / "inputs" / INPUT_NAME
    dst.write_bytes(raw)
    os.chmod(dst, stat.S_IREAD)
    sha = _sha(raw)
    with exclusive_lock(root / ".lock"):
        _append(root, [], "prepared", "prepare", {
            "kind": KIND, "schema_version": SCHEMA, "input_name": INPUT_NAME, "input_sha256": sha,
            "input_freeze_sha256": doc.get("freeze_sha256"), "budget": BUDGET, "catalog": sorted(CATALOG)})
    return {"root_name": root.name, "input_sha256": sha, "budget": BUDGET}


# ---------------------------------------------------------------- event log
def _append(root: Path, events: list[dict], kind: str, actor: str, data: dict) -> dict:
    prev = events[-1]["hash"] if events else "0" * 64
    ev = {"seq": len(events), "type": kind, "actor": actor, "timestamp": _now(), "data": data, "prev_hash": prev}
    ev["hash"] = _sha(_canon(ev))
    with open(root / "events.jsonl", "ab") as fh:
        fh.write(_canon(ev) + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
    tmp = root / "head.json.tmp"
    tmp.write_bytes(_canon({"count": ev["seq"] + 1, "hash": ev["hash"]}))
    os.replace(tmp, root / "head.json")
    events.append(ev)
    return ev


def _load(root: Path) -> tuple[list[dict], dict]:
    """Verify the hash chain, head and input hash; return (events, input document)."""
    try:
        lines = (root / "events.jsonl").read_bytes().splitlines()
        head = json.loads((root / "head.json").read_bytes())
    except (OSError, ValueError):
        raise CycleError("tamper", "event log or head is missing or unreadable") from None
    events, prev = [], "0" * 64
    for i, line in enumerate(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            raise CycleError("tamper", f"event {i} is not JSON") from None
        body = {k: v for k, v in ev.items() if k != "hash"} if isinstance(ev, dict) else None
        if body is None or ev.get("seq") != i or ev.get("prev_hash") != prev or _sha(_canon(body)) != ev.get("hash"):
            raise CycleError("tamper", f"hash chain broken at event {i}")
        prev = ev["hash"]
        events.append(ev)
    if not events or events[0]["type"] != "prepared" or head != {"count": len(events), "hash": prev}:
        raise CycleError("tamper", "head does not match the event log (truncated or rewritten)")
    try:
        raw = (root / "inputs" / INPUT_NAME).read_bytes()
    except OSError:
        raise CycleError("tamper", "input copy is missing") from None
    if _sha(raw) != events[0]["data"]["input_sha256"]:
        raise CycleError("tamper", "input copy hash differs from the recorded SHA-256")
    return events, json.loads(raw)


def _state(events: list[dict]) -> dict:
    st = {"plans": {}, "pending_plan_id": None, "results": [], "updates": {}, "failed": [], "admitted_open": None,
          "finalized": None}
    for ev in events[1:]:
        d, t = ev["data"], ev["type"]
        if t == "plan_recorded":
            st["plans"][d["plan_id"]] = d
            st["pending_plan_id"] = d["plan_id"]
        elif t == "experiment_admitted":
            st["admitted_open"] = d
            st["pending_plan_id"] = None
        elif t == "experiment_completed":
            st["results"].append(d)
            st["admitted_open"] = None
        elif t == "experiment_failed":
            st["failed"].append(d)
            st["admitted_open"] = None
        elif t == "update_recorded":
            st["updates"][d["result_id"]] = d
        elif t == "finalized":
            st["finalized"] = d
    st["blocked"] = bool(st["failed"]) or st["admitted_open"] is not None
    st["used"] = len(st["results"]) + len(st["failed"]) + (st["admitted_open"] is not None)
    st["run_tests"] = [r["test_id"] for r in st["results"]] + [f["test_id"] for f in st["failed"]]
    if st["admitted_open"]:
        st["run_tests"].append(st["admitted_open"]["test_id"])
    return st


# ---------------------------------------------------------------- catalog computations
def _miss_partition(doc: dict) -> dict:
    keys = ("latent_doi", "never_optically_admitted_doi", "missed_candidate_doi", "selected_latent_doi",
            "selected_candidate_doi", "tp_reported_positive", "sensor_missed_doi", "unresolved_doi")

    def part(block: dict) -> dict:
        c, m = block["counts"], block["mean_per_lot"]
        r1 = c["latent_doi"] - (c["never_optically_admitted_doi"] + c["missed_candidate_doi"] + c["selected_latent_doi"])
        r2 = c["selected_latent_doi"] - (c["tp_reported_positive"] + c["sensor_missed_doi"] + c["unresolved_doi"])
        if r1 or r2:
            raise ValueError(f"conservation failed (residuals {r1}, {r2})")
        return {"counts": {k: c[k] for k in keys}, "mean_per_lot": {k: m[k] for k in keys},
                "optical_recall_ceiling": c["candidate_doi"] / c["latent_doi"],
                "selected_share_of_latent": c["selected_latent_doi"] / c["latent_doi"],
                "confirmed_share_of_selected_doi": c["tp_reported_positive"] / c["selected_latent_doi"],
                "conservation": {"latent=unadmitted+unselected+selected": r1 == 0,
                                 "selected=confirmed+sensor_missed+unresolved": r2 == 0}}

    per = {name: part(block) for name, block in sorted(doc["per_scenario"].items())}
    total = {k: sum(p["counts"][k] for p in per.values()) for k in keys}
    if total != {k: doc["overall"]["counts"][k] for k in keys}:
        raise ValueError("per-scenario counts do not sum to overall")
    return {"overall": part(doc["overall"]), "per_scenario": per,
            "labels": {"confirmed": "selected latent DOI the paid review reported positive",
                       "sensor_missed": "selected latent DOI reported negative (sensor error)",
                       "unresolved": "all attempts failed or missing; unknown, never a physical negative"}}


def _capacity_bound(doc: dict) -> dict:
    vb, budget = doc["visit_bound"], float(doc["budget_cu"])
    first, later, reserve = vb["first_review_min_cu"], vb["later_review_min_cu"], vb["retry_reserve_cu"]
    n = 1 + math.floor((budget - reserve - first) / later)
    charged = first + later * (n - 1)
    if n != vb["max_visits"] or charged != vb["charged_at_bound"]:
        raise ValueError("recomputed optimistic bound disagrees with the recorded visit_bound")
    observed = doc["overall"]["mean_visits_per_lot"]
    per = {name: {"mean_visits_per_lot": b["visits"]["mean_per_lot"],
                  "review_fraction_of_bound": b["visits"]["mean_per_lot"] / n,
                  "stage_movement_cu_above_minimum": b["visits"]["mean_stage_movement_cu_above_minimum"],
                  "extra_wafer_loads_cu": b["visits"]["mean_extra_wafer_loads_cu"],
                  "retry_cu": b["visits"]["mean_retry_cu"], "unspent_cu": b["visits"]["mean_unspent_cu"]}
           for name, b in sorted(doc["per_scenario"].items())}
    return {"authored_min_fees_cu": {"first_review": first, "later_review": later, "retry_reserve": reserve,
                                     "budget": budget},
            "optimistic_review_bound": n, "charged_at_bound_cu": charged, "unspent_at_bound_cu": budget - charged,
            "bound_matches_recorded": True,
            "current_mean_visits_per_lot": observed,
            "current_review_fraction_of_bound": observed / n,
            "dies_reviewed_fraction": doc["overall"]["dies_reviewed_fraction"],
            "visit_slack_vs_bound": n - observed,
            "min_cu_review_every_candidate_per_lot": doc["overall"]["min_cu_to_review_every_candidate_per_lot"],
            "min_cu_review_every_die_per_lot": doc["overall"]["min_cu_to_review_every_die_per_lot"],
            "per_scenario": per,
            "labels": {"bound": "loose optimistic bound: zero movement, one wafer, no failures; not an achievable "
                                "planner, not factory throughput",
                       "visit_slack_vs_bound": "visits, not achievable gain or confirmed DOI"}}


def _calibration_shift(doc: dict) -> dict:
    per = {}
    for name, block in sorted(doc["per_scenario"].items()):
        cal = block["calibration_candidates"]
        bins = cal["bins"]
        total = sum(b["n"] for b in bins)
        if total != cal["n"] or sum(b["latent_doi"] for b in bins) != cal["latent_doi"]:
            raise ValueError(f"{name}: calibration bins do not sum to totals")
        rows = []
        for b in bins:
            rate = b["latent_doi"] / b["n"] if b["n"] else None
            gap = b["mean_p"] - rate if rate is not None else None
            if gap is not None and abs(gap - b["gap_mean_p_minus_rate"]) > 1e-9:
                raise ValueError(f"{name} {b['bin']}: recomputed gap differs from the recorded gap")
            rows.append({"bin": b["bin"], "n": b["n"], "mean_p": b["mean_p"], "latent_doi_rate": rate, "gap": gap})
        signed = sum(r["n"] * r["gap"] for r in rows if r["gap"] is not None) / total
        absolute = sum(r["n"] * abs(r["gap"]) for r in rows if r["gap"] is not None) / total
        above = [r for r in rows if r["gap"] is not None and r["bin"] != "[0.0,0.2)"]
        n_above = sum(r["n"] for r in above)
        per[name] = {"n_candidates": total, "weighted_signed_gap": signed, "weighted_abs_gap": absolute,
                     "weighted_signed_gap_p_ge_0.2": (sum(r["n"] * r["gap"] for r in above) / n_above) if n_above else None,
                     "max_bin_gap": max(rows, key=lambda r: abs(r["gap"] or 0))["bin"], "bins": rows}
    order = sorted(per, key=lambda k: per[k]["weighted_abs_gap"], reverse=True)
    return {"per_scenario": per, "scenarios_by_weighted_abs_gap": order,
            "labels": {"gap": "mean frozen probability minus observed latent DOI rate; positive means overconfident",
                       "scope": "calibration against posthoc latent truth over candidates; not a classifier accuracy"}}


COMPUTE: dict[str, Callable[[dict], dict]] = {
    "miss_partition": _miss_partition, "capacity_bound": _capacity_bound, "calibration_shift": _calibration_shift}


# ---------------------------------------------------------------- cycle
class DiscoveryCycle:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        if not (self.root / "events.jsonl").is_file():
            raise CycleError("unknown", "root is not a prepared discovery cycle")
        _load(self.root)

    def _call(self, method: str, actor: Any, fn: Callable[[list[dict], dict, dict, str], dict]) -> dict:
        actor = self._actor(method, actor)
        try:
            with exclusive_lock(self.root / ".lock"):
                events, doc = _load(self.root)
                return fn(events, doc, _state(events), actor)
        except LiveError as exc:
            raise CycleError("conflict", str(exc)) from None

    @staticmethod
    def _actor(method: str, actor: Any) -> str:
        if not isinstance(actor, str) or not actor.strip() or len(actor) > MAX_ACTOR:
            raise CycleError("role_denied", "actor must be a short '<label>:<role>' or '<role>' string")
        role = actor.rsplit(":", 1)[-1].strip()
        if method not in ROLE_METHODS.get(role, ()):
            raise CycleError("role_denied", f"{method} is not available to role {role or '<unset>'}")
        return _sanitize(actor.strip())

    # ------------------------------------------------ reads
    def _view(self, events: list[dict], st: dict) -> dict:
        available = [t for t in CATALOG if t not in st["run_tests"]] if not st["blocked"] and not st["finalized"] else []
        latest = st["results"][-1]["result_id"] if st["results"] else None
        return {
            "kind": KIND, "schema_version": SCHEMA,
            "data_label": DATA_LABEL, "evaluation_label": EVALUATION_LABEL,
            "primary": PRIMARY,
            "input_sha256": events[0]["data"]["input_sha256"],
            "budget": {"limit": BUDGET, "used": st["used"], "remaining": BUDGET - st["used"]},
            "catalog": [{"test_id": t, **CATALOG[t], "available": t in available} for t in CATALOG],
            "available_test_ids": available,
            "pending_plan": st["plans"][st["pending_plan_id"]] if st["pending_plan_id"] else None,
            "completed_results": [{**r, "update": st["updates"].get(r["result_id"])} for r in st["results"]],
            "failed_experiments": st["failed"],
            "latest_result_id": latest,
            "latest_result_needs_update": bool(latest and latest not in st["updates"]),
            "blocked": st["blocked"],
            "finalized": st["finalized"],
            "event_count": len(events), "head_hash": events[-1]["hash"],
        }

    def read_context(self, actor: str) -> dict:
        return self._call("read_context", actor, lambda ev, doc, st, a: self._view(ev, st))

    def status(self) -> dict:
        try:
            with exclusive_lock(self.root / ".lock"):
                events, _ = _load(self.root)
        except LiveError as exc:
            raise CycleError("conflict", str(exc)) from None
        st = _state(events)
        latest = st["results"][-1]["result_id"] if st["results"] else None
        if st["finalized"]:
            phase = "finalized"
        elif st["blocked"]:
            phase = "blocked"
        elif st["pending_plan_id"]:
            phase = "awaiting_run"
        elif latest and latest not in st["updates"]:
            phase = "awaiting_update"
        elif st["used"] >= BUDGET:
            phase = "awaiting_finalize"
        else:
            phase = "awaiting_plan"
        return {"kind": KIND, "phase": phase, "budget": {"limit": BUDGET, "used": st["used"],
                                                          "remaining": BUDGET - st["used"]},
                "pending_plan_id": st["pending_plan_id"], "result_ids": [r["result_id"] for r in st["results"]],
                "updated_result_ids": sorted(st["updates"]), "failed": len(st["failed"]), "blocked": st["blocked"],
                "finalized": st["finalized"] is not None, "event_count": len(events), "head_hash": events[-1]["hash"],
                "input_sha256": events[0]["data"]["input_sha256"]}

    # ------------------------------------------------ analyst
    def record_plan(self, candidates: list, selected_test_id: str, hypothesis: str, expected_learning: str,
                    reason: str, evidence_result_ids: list, actor: str) -> dict:
        def fn(events, doc, st, a):
            if st["finalized"] or st["blocked"]:
                raise CycleError("blocked", "cycle is finalized or blocked by a failed admission; no retries")
            cands = _id_list("candidates", candidates, len(CATALOG))
            outside = [c for c in cands + [selected_test_id] if c not in CATALOG]
            if outside:
                raise CycleError("approval_required", f"outside the pre-authorized read-only catalog: {outside[:3]}")
            if st["pending_plan_id"]:
                raise CycleError("conflict", f"plan {st['pending_plan_id']} is still pending")
            if st["used"] >= BUDGET:
                raise CycleError("budget", "both experiments are used")
            latest = st["results"][-1]["result_id"] if st["results"] else None
            if latest and latest not in st["updates"]:
                raise CycleError("conflict", f"record_update for {latest} before planning again")
            if len(cands) < 2:
                raise CycleError("invalid", "at least two distinct candidate test IDs are required")
            unavailable = [c for c in cands if c in st["run_tests"]]
            if unavailable:
                raise CycleError("invalid", f"candidates already run: {unavailable}")
            if selected_test_id not in cands:
                raise CycleError("invalid", "selected_test_id must be one of the candidates")
            ev_ids = _id_list("evidence_result_ids", evidence_result_ids, BUDGET)
            if sorted(ev_ids) != sorted(r["result_id"] for r in st["results"]):
                raise CycleError("invalid", "evidence_result_ids must be exactly the previous completed result IDs")
            plan = {"plan_id": f"plan_{secrets.token_hex(8)}", "sequence": len(st["plans"]) + 1,
                    "candidates": cands, "selected_test_id": selected_test_id,
                    "hypothesis": _text("hypothesis", hypothesis),
                    "expected_learning": _text("expected_learning", expected_learning),
                    "reason": _text("reason", reason), "evidence_result_ids": ev_ids,
                    "remaining_budget_before": BUDGET - st["used"]}
            _append(self.root, events, "plan_recorded", a, plan)
            return {"plan": plan, "status": "pending"}
        return self._call("record_plan", actor, fn)

    def record_update(self, result_id: str, interpretation: str, next_hypothesis: str, next_experiment: str,
                      actor: str) -> dict:
        def fn(events, doc, st, a):
            if st["finalized"]:
                raise CycleError("blocked", "cycle is finalized")
            if not isinstance(result_id, str) or not st["results"] or result_id != st["results"][-1]["result_id"]:
                raise CycleError("unknown", "result_id must be the latest completed result")
            if result_id in st["updates"]:
                return {"update": st["updates"][result_id], "replayed": True}
            res = st["results"][-1]
            upd = {"result_id": result_id, "test_id": res["test_id"], "result_sha256": res["values_sha256"],
                   "interpretation": _text("interpretation", interpretation),
                   "next_hypothesis": {"text": _text("next_hypothesis", next_hypothesis), "status": "PROPOSED"},
                   "next_experiment": {"text": _text("next_experiment", next_experiment), "status": "PROPOSED",
                                       "executed": False},
                   "authorship": "agent interpretation; not a verified conclusion"}
            _append(self.root, events, "update_recorded", a, upd)
            return {"update": upd, "replayed": False}
        return self._call("record_update", actor, fn)

    # ------------------------------------------------ experimenter
    def run_experiment(self, plan_id: str, actor: str) -> dict:
        def fn(events, doc, st, a):
            if not isinstance(plan_id, str) or not ID_RE.match(plan_id) or plan_id not in st["plans"]:
                raise CycleError("unknown", "unknown plan_id")
            done = next((r for r in st["results"] if r["plan_id"] == plan_id), None)
            if done:
                return {"result": done, "replayed": True}
            if st["blocked"] or st["finalized"]:
                raise CycleError("blocked", "cycle is finalized or blocked by a failed admission; no retries")
            if plan_id != st["pending_plan_id"]:
                raise CycleError("unknown", "plan_id is not the pending plan")
            plan = st["plans"][plan_id]
            test_id = plan["selected_test_id"]
            if test_id not in COMPUTE or test_id in st["run_tests"] or st["used"] >= BUDGET:
                raise CycleError("approval_required", "selected test is not runnable within the catalog and budget")
            result_id = f"res_{secrets.token_hex(8)}"
            _append(self.root, events, "experiment_admitted", a, {
                "plan_id": plan_id, "test_id": test_id, "result_id": result_id, "cost": ZERO_COST,
                "action": "read-only computation over the copied frozen input"})
            t0 = time.perf_counter()
            try:
                values = COMPUTE[test_id](doc)
                _canon(values)
            except Exception as exc:  # noqa: BLE001 - recorded as a failed admission, never retried
                fail = {"plan_id": plan_id, "test_id": test_id, "result_id": result_id,
                        "elapsed_s": time.perf_counter() - t0,
                        "error": _sanitize(f"{type(exc).__name__}: {exc}")[:MAX_TEXT]}
                _append(self.root, events, "experiment_failed", a, fail)
                raise CycleError("failed", f"{test_id} failed after admission; cycle blocked, no retry") from None
            elapsed = time.perf_counter() - t0
            res = {"result_id": result_id, "plan_id": plan_id, "test_id": test_id, "elapsed_s": elapsed,
                   "evaluation_label": EVALUATION_LABEL, "privileged_posthoc_oracle": True,
                   "production_selection": False, "new_heldout_gain": False,
                   "input_sha256": events[0]["data"]["input_sha256"],
                   "values": values, "values_sha256": _sha(_canon(values))}
            _append(self.root, events, "experiment_completed", a, res)
            return {"result": res, "replayed": False}
        return self._call("run_experiment", actor, fn)

    # ------------------------------------------------ supervisor
    def finalize(self, actor: str) -> dict:
        def fn(events, doc, st, a):
            if st["finalized"]:
                return {"final": st["finalized"], "replayed": True}
            if st["blocked"]:
                raise CycleError("blocked", "a failed admission blocks finalization")
            if st["pending_plan_id"] or len(st["results"]) != BUDGET or len(st["updates"]) != BUDGET:
                raise CycleError("conflict", "finalize needs two completed results, two updates and no pending plan")
            final = {"result_ids": [r["result_id"] for r in st["results"]],
                     "test_ids": [r["test_id"] for r in st["results"]],
                     "values_sha256": [r["values_sha256"] for r in st["results"]],
                     "proposed_next_experiments": [st["updates"][r["result_id"]]["next_experiment"]
                                                   for r in st["results"]],
                     "conclusion": None,
                     "note": "no conclusion is fabricated; interpretations are agent-authored and proposals are "
                             "unexecuted", "evaluation_label": EVALUATION_LABEL,
                     "head_hash_before": events[-1]["hash"]}
            _append(self.root, events, "finalized", a, final)
            return {"final": final, "replayed": False}
        return self._call("finalize", actor, fn)

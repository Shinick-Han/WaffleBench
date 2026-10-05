"""Offline replay loop: admit selector choices, charge archived action times, log outcomes.

Seconds here are modeled/recorded replay resource charges, NOT physical elapsed time.
No network or hardware command is issued. The selector only ever sees public candidate
fields plus public cost estimates and the outcomes of actions it already paid for.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import time
from pathlib import Path

from .contracts import ContractError, estimate_cost, validate_archive, validate_job

# Absorbs float summation noise only; never used to admit a genuinely unaffordable action.
_EPS = 1e-9
TIME_SEMANTICS = "replay resource seconds (modeled/recorded action charges), not physical elapsed time"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_image(raw: str, image_root) -> Path | None:
    path = Path(raw)
    if image_root is None:
        return path
    root = Path(image_root).resolve()
    resolved = (root / path).resolve()
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


def _json_log(value):
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        return {"error": f"observer output not JSON-compatible: {exc}"}


def _observe(archived: dict, candidate: dict, observer, image_root) -> tuple[dict, float]:
    """Verify the archived image after admission and optionally run the observer.

    Returns (image log, observer wall seconds). The observer never sees the detector
    report and its output never replaces it.
    """
    if "image_path" not in archived:
        return {"image_status": "not_provided"}, 0.0
    log = {"image_path": archived["image_path"]}
    path = _resolve_image(archived["image_path"], image_root)
    if path is None:
        log["image_status"] = "outside_image_root"
        return log, 0.0
    if "image_sha256" in archived:
        try:
            actual = _sha256(path)
        except OSError:
            log["image_status"] = "unreadable"
            return log, 0.0
        if actual != archived["image_sha256"]:
            log["image_status"] = "hash_mismatch"
            log["actual_sha256"] = actual
            return log, 0.0
        log["image_status"] = "verified"
    else:
        log["image_status"] = "unverified_no_hash"
    if observer is None:
        return log, 0.0
    view = {k: archived[k] for k in ("status", "observed_at", "image_sha256") if k in archived}
    view["image_path"] = str(path)
    started = time.perf_counter()
    try:
        result = observer(view, copy.deepcopy(candidate))
    except Exception as exc:  # auxiliary evidence must never abort the replay
        log["observer_error"] = f"{type(exc).__name__}: {exc}"
    else:
        if isinstance(result, dict):
            log["observer"] = _json_log(result)
        else:
            log["observer_error"] = "observer must return a dict"
    return log, time.perf_counter() - started


def _attempt(archived, index: int, bound_capture: float, bound_inference: float,
             candidate: dict, observer, image_root) -> tuple[dict, float]:
    """Charge one attempt. Absent archive records are paid at bounds and stay unknown."""
    if archived is None:
        return ({"index": index, "status": "unavailable", "source": "not_in_archive",
                 "reported_doi": None, "capture_s": float(bound_capture),
                 "inference_s": float(bound_inference), "charged_at_bound": True,
                 "image_status": "not_provided"}, 0.0)
    entry = {"index": index, "status": archived["status"], "source": "archive",
             "reported_doi": archived["reported_doi"] if archived["status"] == "ok" else None,
             "capture_s": float(archived["capture_s"]),
             "inference_s": float(archived["inference_s"]),
             "observed_at": archived["observed_at"], "charged_at_bound": False}
    image_log, wall = _observe(archived, candidate, observer, image_root)
    entry.update(image_log)
    return entry, wall


def _validate_choice(choice, remaining: dict) -> tuple[str, dict]:
    if isinstance(choice, dict):
        site = choice.get("site_id")
    else:
        raise ContractError("selector must return a dict with site_id or None")
    if not isinstance(site, str) or site not in remaining:
        raise ContractError(f"selector chose unknown or already visited site {site!r}")
    try:
        logged = json.loads(json.dumps(choice, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"selector choice not JSON-compatible: {exc}") from exc
    return site, logged


def run_loop(job, archive, selector, budget_s, seed=0, observer=None, *, image_root=None):
    """Replay ``selector`` against an archive under a resource budget (seconds).

    ``image_root`` (extension, keyword-only) resolves relative archive image paths and
    refuses paths escaping it; by default paths resolve against the working directory.
    """
    job = validate_job(job)
    archive = validate_archive(job, archive)
    if not callable(selector):
        raise ContractError("selector must be callable")
    if observer is not None and not callable(observer):
        raise ContractError("observer must be callable or None")
    if isinstance(budget_s, bool) or not isinstance(budget_s, (int, float)) \
            or not math.isfinite(budget_s) or budget_s < 0:
        raise ContractError("budget_s must be a finite nonnegative number")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ContractError("seed must be an integer")

    rng = random.Random(seed)
    cost = job["cost"]
    retry_limit = cost["retry_limit"]
    remaining = {c["site_id"]: c for c in job["candidates"]}
    observations = archive["observations"]
    current = None
    spent = 0.0
    rows, history = [], []
    decision_wall = observer_wall = 0.0
    stop_reason = None

    while True:
        left = budget_s - spent
        if not remaining:
            stop_reason = "candidates_exhausted"
            break
        public = []
        for site in sorted(remaining):
            item = copy.deepcopy(remaining[site])
            item["cost"] = estimate_cost(item, current, cost)
            public.append(item)
        affordable = any(c["cost"]["reserved_s"] <= left + _EPS for c in public)
        state = {"job_id": job["job_id"], "candidates": public, "remaining_s": left,
                 "current": copy.deepcopy(current), "history": copy.deepcopy(history),
                 "cost": dict(cost)}
        started = time.perf_counter()
        choice = selector(copy.deepcopy(state), rng)
        decision_wall += time.perf_counter() - started
        if choice is None:
            stop_reason = "selector_declined" if affordable else "no_affordable_candidate"
            break
        site, logged_choice = _validate_choice(choice, remaining)
        candidate = remaining[site]
        estimate = next(c["cost"] for c in public if c["site_id"] == site)
        if estimate["reserved_s"] > left + _EPS:
            raise ContractError(f"selector chose {site!r} whose reserved "
                                f"{estimate['reserved_s']:.6g}s exceeds remaining {left:.6g}s")

        # Admitted: only now may archived outcomes and images for this site be read.
        archived = observations.get(site, [])
        attempts = []
        entry, wall = _attempt(archived[0] if archived else None, 0,
                               candidate["capture_bound_s"], candidate["inference_bound_s"],
                               candidate, observer, image_root)
        observer_wall += wall
        attempts.append(entry)
        if retry_limit >= 1 and entry["status"] in ("failed", "missing"):
            entry, wall = _attempt(archived[1] if len(archived) > 1 else None, 1,
                                   candidate["capture_bound_s"],
                                   candidate["inference_bound_s"],
                                   candidate, observer, image_root)
            observer_wall += wall
            attempts.append(entry)

        parts = {"load_s": estimate["load_s"], "move_s": estimate["move_s"],
                 "settle_s": estimate["settle_s"],
                 "capture_s": sum(a["capture_s"] for a in attempts),
                 "inference_s": sum(a["inference_s"] for a in attempts)}
        charged = sum(parts.values())
        if charged > estimate["reserved_s"] + _EPS:  # impossible for validated archives
            raise ContractError(f"charge for {site!r} exceeds its admission reserve")
        spent += charged
        final = attempts[-1]
        reported = final["reported_doi"] if final["status"] == "ok" else None
        rows.append({"site_id": site, "choice": logged_choice, "attempts": attempts,
                     "final_status": final["status"], "reported_doi": reported,
                     "charged_s": charged, "reserved_s": estimate["reserved_s"],
                     "cumulative_s": spent, "resource_parts": parts})
        history.append({"site_id": site, "wafer_id": candidate["wafer_id"],
                        "x_um": candidate["x_um"], "y_um": candidate["y_um"],
                        "final_status": final["status"], "reported_doi": reported,
                        "attempt_statuses": [a["status"] for a in attempts],
                        "charged_s": charged})
        current = {"wafer_id": candidate["wafer_id"], "x_um": candidate["x_um"],
                   "y_um": candidate["y_um"]}
        del remaining[site]

    return {"job_id": job["job_id"], "data_mode": job["data_mode"], "budget_s": budget_s,
            "spent_s": spent, "remaining_s": budget_s - spent, "rows": rows,
            "decision_wall_s": decision_wall, "observer_wall_s": observer_wall,
            "stop_reason": stop_reason, "seed": seed, "time_semantics": TIME_SEMANTICS,
            "commercial_validated": False}

"""Strict validation of routing PoC jobs/archives and public action-cost estimates.

Policy-visible inputs are whitelists: unknown keys are rejected rather than ignored, so
SEM images, labels, truth, scenarios or seeds cannot ride along as candidate features.
Every validator returns a sanitized deep copy and never mutates its input.
"""

from __future__ import annotations

import copy
import json
import math
import re
from datetime import datetime

SCHEMA_VERSION = 1
DATA_MODES = ("synthetic", "log_replay")
ATTEMPT_STATUSES = ("ok", "failed", "missing")
MAX_ATTEMPTS = 2

ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

JOB_KEYS = frozenset({"schema_version", "job_id", "data_mode", "cutoff_utc", "cost",
                      "candidates", "provenance"})
COST_KEYS = frozenset({"wafer_load_s", "settle_s", "move_um_per_s", "retry_limit"})
CANDIDATE_KEYS = frozenset({"site_id", "wafer_id", "x_um", "y_um", "prior_p",
                            "optical_observed_at", "recipe_id", "capture_bound_s",
                            "inference_bound_s"})
ARCHIVE_KEYS = frozenset({"job_id", "observations", "reference"})
ATTEMPT_REQUIRED = frozenset({"status", "reported_doi", "capture_s", "inference_s",
                              "observed_at"})
ATTEMPT_OPTIONAL = frozenset({"image_path", "image_sha256"})
REFERENCE_KEYS = frozenset({"complete", "source", "doi_by_site"})
CURRENT_KEYS = frozenset({"wafer_id", "x_um", "y_um"})


class ContractError(ValueError):
    """Input violates ROUTING_POC_CONTRACT.md. Never clipped or repaired silently."""


def _fail(where: str, message: str) -> None:
    raise ContractError(f"{where}: {message}")


def _require_dict(value, where: str) -> dict:
    if not isinstance(value, dict):
        _fail(where, "must be an object")
    return value


def _check_keys(value: dict, required: frozenset, optional: frozenset, where: str) -> None:
    keys = set(value)
    missing = sorted(required - keys)
    if missing:
        _fail(where, f"missing keys {missing}")
    extra = sorted(keys - required - optional, key=str)
    if extra:
        _fail(where, f"unexpected keys {extra} (strict whitelist)")


def _id(value, where: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        _fail(where, f"malformed id {value!r}")
    return value


def _number(value, where: str, *, minimum: float | None = None, positive: bool = False,
            maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(where, "must be a number (booleans rejected)")
    if not math.isfinite(value):
        _fail(where, "must be finite")
    if positive and not value > 0:
        _fail(where, "must be positive")
    if minimum is not None and value < minimum:
        _fail(where, f"must be >= {minimum}")
    if maximum is not None and value > maximum:
        _fail(where, f"must be <= {maximum}")
    return value


def parse_timestamp(value, where: str) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp; naive or malformed values are rejected."""
    if not isinstance(value, str):
        _fail(where, "timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _fail(where, f"malformed timestamp {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _fail(where, "timestamp must be timezone-aware")
    return parsed


def _json_copy(value, where: str):
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as exc:
        _fail(where, f"must be JSON-compatible ({exc})")


def _validate_cost(cost, where: str = "cost") -> dict:
    _require_dict(cost, where)
    _check_keys(cost, COST_KEYS, frozenset(), where)
    _number(cost["wafer_load_s"], f"{where}.wafer_load_s", minimum=0)
    _number(cost["settle_s"], f"{where}.settle_s", minimum=0)
    _number(cost["move_um_per_s"], f"{where}.move_um_per_s", positive=True)
    retry = cost["retry_limit"]
    if isinstance(retry, bool) or not isinstance(retry, int) or retry not in (0, 1):
        _fail(f"{where}.retry_limit", "must be integer 0 or 1")
    return dict(cost)


def _validate_candidate(candidate, cutoff: datetime, where: str) -> dict:
    _require_dict(candidate, where)
    _check_keys(candidate, CANDIDATE_KEYS, frozenset(), where)
    _id(candidate["site_id"], f"{where}.site_id")
    _id(candidate["wafer_id"], f"{where}.wafer_id")
    _id(candidate["recipe_id"], f"{where}.recipe_id")
    _number(candidate["x_um"], f"{where}.x_um")
    _number(candidate["y_um"], f"{where}.y_um")
    _number(candidate["prior_p"], f"{where}.prior_p", minimum=0, maximum=1)
    _number(candidate["capture_bound_s"], f"{where}.capture_bound_s", positive=True)
    _number(candidate["inference_bound_s"], f"{where}.inference_bound_s", minimum=0)
    observed = parse_timestamp(candidate["optical_observed_at"], f"{where}.optical_observed_at")
    if observed > cutoff:
        _fail(f"{where}.optical_observed_at", "is after cutoff_utc (future information)")
    return dict(candidate)


def validate_job(job) -> dict:
    """Return a sanitized deep copy of a job, or raise ContractError."""
    _require_dict(job, "job")
    _check_keys(job, JOB_KEYS, frozenset(), "job")
    version = job["schema_version"]
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        _fail("job.schema_version", f"must be {SCHEMA_VERSION}")
    _id(job["job_id"], "job.job_id")
    if job["data_mode"] not in DATA_MODES:
        _fail("job.data_mode", f"must be one of {list(DATA_MODES)}")
    cutoff = parse_timestamp(job["cutoff_utc"], "job.cutoff_utc")
    cost = _validate_cost(job["cost"], "job.cost")
    candidates = job["candidates"]
    if not isinstance(candidates, list):
        _fail("job.candidates", "must be a list")
    clean, seen = [], set()
    for index, candidate in enumerate(candidates):
        item = _validate_candidate(candidate, cutoff, f"job.candidates[{index}]")
        if item["site_id"] in seen:
            _fail(f"job.candidates[{index}].site_id", f"duplicate {item['site_id']!r}")
        seen.add(item["site_id"])
        clean.append(item)
    provenance = _require_dict(job["provenance"], "job.provenance")
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job["job_id"],
        "data_mode": job["data_mode"],
        "cutoff_utc": job["cutoff_utc"],
        "cost": cost,
        "candidates": copy.deepcopy(clean),
        "provenance": _json_copy(provenance, "job.provenance"),
    }


def _validate_attempt(attempt, candidate: dict, cutoff: datetime,
                      where: str) -> tuple[dict, datetime]:
    _require_dict(attempt, where)
    _check_keys(attempt, ATTEMPT_REQUIRED, ATTEMPT_OPTIONAL, where)
    status = attempt["status"]
    if status not in ATTEMPT_STATUSES:
        _fail(f"{where}.status", f"must be one of {list(ATTEMPT_STATUSES)}")
    reported = attempt["reported_doi"]
    if status == "ok":
        if not isinstance(reported, bool):
            _fail(f"{where}.reported_doi", "must be a bool for an ok attempt")
    elif reported is not None:
        _fail(f"{where}.reported_doi", "must be null unless status is ok (unknown is not negative)")
    capture = _number(attempt["capture_s"], f"{where}.capture_s", minimum=0)
    inference = _number(attempt["inference_s"], f"{where}.inference_s", minimum=0)
    if capture > candidate["capture_bound_s"]:
        _fail(f"{where}.capture_s", "exceeds declared capture_bound_s (invalid, not clipped)")
    if inference > candidate["inference_bound_s"]:
        _fail(f"{where}.inference_s", "exceeds declared inference_bound_s (invalid, not clipped)")
    observed = parse_timestamp(attempt["observed_at"], f"{where}.observed_at")
    if not observed > cutoff:
        _fail(f"{where}.observed_at", "must be strictly after cutoff_utc")
    if "image_path" in attempt:
        if status == "missing":
            _fail(f"{where}.image_path", "a missing attempt cannot carry an image")
        path = attempt["image_path"]
        if not isinstance(path, str) or not path or "\x00" in path:
            _fail(f"{where}.image_path", "must be a non-empty path string")
    if "image_sha256" in attempt:
        if "image_path" not in attempt:
            _fail(f"{where}.image_sha256", "requires image_path")
        digest = attempt["image_sha256"]
        if not isinstance(digest, str) or not SHA256_PATTERN.match(digest):
            _fail(f"{where}.image_sha256", "must be 64 lowercase hex characters")
    return dict(attempt), observed


def _validate_reference(reference, site_ids: set, where: str = "archive.reference") -> dict:
    _require_dict(reference, where)
    _check_keys(reference, REFERENCE_KEYS, frozenset(), where)
    if not isinstance(reference["complete"], bool):
        _fail(f"{where}.complete", "must be a bool")
    if not isinstance(reference["source"], str) or not reference["source"].strip():
        _fail(f"{where}.source", "must be a non-empty string")
    mapping = _require_dict(reference["doi_by_site"], f"{where}.doi_by_site")
    for site, value in mapping.items():
        _id(site, f"{where}.doi_by_site key")
        if site not in site_ids:
            _fail(f"{where}.doi_by_site", f"unknown site {site!r}")
        if value is not None and not isinstance(value, bool):
            _fail(f"{where}.doi_by_site[{site}]", "must be bool or null")
    if reference["complete"]:
        unresolved = sorted(s for s in site_ids if mapping.get(s) is None)
        if unresolved:
            _fail(f"{where}.complete", f"true but truth absent/null for {unresolved[:5]}")
    return {"complete": reference["complete"], "source": reference["source"],
            "doi_by_site": dict(mapping)}


def validate_archive(job, archive) -> dict:
    """Validate an archive against an already valid (or raw) job; return a sanitized copy."""
    job = validate_job(job)
    _require_dict(archive, "archive")
    _check_keys(archive, frozenset({"job_id", "observations"}), frozenset({"reference"}),
                "archive")
    if _id(archive["job_id"], "archive.job_id") != job["job_id"]:
        _fail("archive.job_id", "does not match job.job_id")
    cutoff = parse_timestamp(job["cutoff_utc"], "job.cutoff_utc")
    by_site = {c["site_id"]: c for c in job["candidates"]}
    retry_limit = job["cost"]["retry_limit"]
    observations = _require_dict(archive["observations"], "archive.observations")
    clean_obs = {}
    for site, attempts in observations.items():
        where = f"archive.observations[{site!r}]"
        _id(site, where)
        if site not in by_site:
            _fail(where, "site is not a job candidate")
        if not isinstance(attempts, list):
            _fail(where, "must be a list of attempts")
        if len(attempts) > MAX_ATTEMPTS:
            _fail(where, f"at most {MAX_ATTEMPTS} attempts")
        if len(attempts) > 1 + retry_limit:
            _fail(where, f"more attempts than 1 + retry_limit ({1 + retry_limit})")
        clean_attempts, previous = [], None
        for index, attempt in enumerate(attempts):
            item, observed = _validate_attempt(attempt, by_site[site], cutoff,
                                               f"{where}[{index}]")
            if index > 0:
                if clean_attempts[-1]["status"] == "ok":
                    _fail(f"{where}[{index}]", "retry recorded after an ok attempt")
                if not observed > previous:
                    _fail(f"{where}[{index}].observed_at", "retry must follow first attempt")
            clean_attempts.append(item)
            previous = observed
        clean_obs[site] = clean_attempts
    result = {"job_id": job["job_id"], "observations": copy.deepcopy(clean_obs)}
    if "reference" in archive:
        result["reference"] = _validate_reference(archive["reference"], set(by_site))
    return result


def _validate_current(current) -> dict | None:
    if current is None:
        return None
    _require_dict(current, "current")
    _check_keys(current, CURRENT_KEYS, frozenset(), "current")
    _id(current["wafer_id"], "current.wafer_id")
    _number(current["x_um"], "current.x_um")
    _number(current["y_um"], "current.y_um")
    return current


def estimate_cost(candidate, current, cost) -> dict:
    """Public, pre-acquisition resource seconds for visiting ``candidate`` from ``current``.

    Uses only declared bounds and public cost parameters, never archived outcomes.
    Loading is charged when no wafer is mounted or the wafer changes; movement
    (Euclidean distance / speed) only within the same wafer. ``reserved_s`` adds the
    full retry allowance so admission can never overrun the budget.
    """
    _require_dict(candidate, "candidate")
    cost = _validate_cost(cost)
    current = _validate_current(current)
    for key in ("x_um", "y_um"):
        _number(candidate.get(key), f"candidate.{key}")
    _id(candidate.get("wafer_id"), "candidate.wafer_id")
    capture_bound = _number(candidate.get("capture_bound_s"), "candidate.capture_bound_s",
                            positive=True)
    inference_bound = _number(candidate.get("inference_bound_s"),
                              "candidate.inference_bound_s", minimum=0)
    if current is not None and current["wafer_id"] == candidate["wafer_id"]:
        load_s = 0.0
        distance = math.hypot(candidate["x_um"] - current["x_um"],
                              candidate["y_um"] - current["y_um"])
        move_s = distance / cost["move_um_per_s"]
    else:
        load_s = float(cost["wafer_load_s"])
        move_s = 0.0
    settle_s = float(cost["settle_s"])
    action_bound = capture_bound + inference_bound
    first_s = load_s + move_s + settle_s + action_bound
    reserved_s = first_s + cost["retry_limit"] * action_bound
    return {"load_s": load_s, "move_s": move_s, "settle_s": settle_s,
            "first_s": first_s, "reserved_s": reserved_s}

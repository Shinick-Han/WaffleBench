"""Validator for structured prospective action payloads from Jev/Omnigent-style agents.

Validation only: no API call, no network, no credentials. A payload proposes one action;
numerical scores and costs come from the deterministic tools, and the payload may cite
only evidence ids that were actually observed. Keys that look like hidden truth, test
masks, seeds, scenarios or future observations are rejected anywhere in the payload.

Schema (schema_version 1):
  action        one of ACTIONS
  target_id     string (site/image id) for actions that need one, else null
  rationale     non-empty string, at most 2000 characters
  evidence_ids  list of strings, each in the caller-provided observed set
  expected_cost number >= 0, must not exceed remaining_budget when given
  source        "jev" | "omnigent" | "deterministic_runner"
"""

from __future__ import annotations

import math
from typing import Any, Iterable

ACTIONS = ("select_site", "request_audit", "repeat_image", "abstain_request_review", "stop")
TARGET_ACTIONS = ("select_site", "request_audit", "repeat_image", "abstain_request_review")
SOURCES = ("jev", "omnigent", "deterministic_runner")
REQUIRED = ("schema_version", "action", "target_id", "rationale", "evidence_ids", "expected_cost", "source")
FORBIDDEN_PARTS = ("oracle", "truth", "mask", "seed", "scenario", "hidden", "future", "test_label",
                   "ground", "api_key", "token", "password", "credential")


def _walk_keys(x: Any, path: str = "") -> Iterable[str]:
    if isinstance(x, dict):
        for k, v in x.items():
            yield f"{path}.{k}" if path else str(k)
            yield from _walk_keys(v, f"{path}.{k}" if path else str(k))
    elif isinstance(x, list):
        for n, v in enumerate(x):
            yield from _walk_keys(v, f"{path}[{n}]")


def validate_payload(payload: Any, *, observed_ids: Iterable[str], allowed_targets: Iterable[str] | None = None,
                     remaining_budget: float | None = None) -> list[str]:
    """Return a list of errors (empty when valid)."""
    if remaining_budget is not None and (isinstance(remaining_budget, bool)
                                         or not isinstance(remaining_budget, (int, float))
                                         or not math.isfinite(remaining_budget) or remaining_budget < 0):
        raise ValueError("remaining_budget must be a finite non-negative number or None")
    if not isinstance(payload, dict):
        return ["payload must be an object"]
    errs = []
    for key in _walk_keys(payload):
        leaf = key.rsplit(".", 1)[-1].lower()
        if any(p in leaf for p in FORBIDDEN_PARTS):
            errs.append(f"forbidden key {key}")
    missing = [k for k in REQUIRED if k not in payload]
    if missing:
        errs.append(f"missing {missing}")
        return errs
    extra = sorted(set(payload) - set(REQUIRED))
    if extra:
        errs.append(f"unexpected keys {extra}")
    if payload["schema_version"] != 1:
        errs.append("schema_version must be 1")
    act = payload["action"]
    if act not in ACTIONS:
        errs.append(f"action must be one of {ACTIONS}")
    tgt = payload["target_id"]
    if act in TARGET_ACTIONS:
        if not isinstance(tgt, str) or not tgt:
            errs.append("target_id required for this action")
        elif allowed_targets is not None and tgt not in set(allowed_targets):
            errs.append(f"target_id {tgt!r} is not a permitted target")
    elif tgt is not None:
        errs.append("target_id must be null for stop")
    r = payload["rationale"]
    if not isinstance(r, str) or not r.strip() or len(r) > 2000:
        errs.append("rationale must be a non-empty string of at most 2000 characters")
    ev = payload["evidence_ids"]
    obs = set(observed_ids)
    if not isinstance(ev, list) or not all(isinstance(e, str) for e in ev):
        errs.append("evidence_ids must be a list of strings")
    else:
        unseen = [e for e in ev if e not in obs]
        if unseen:
            errs.append(f"evidence_ids cite unobserved evidence {unseen}")
    c = payload["expected_cost"]
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not math.isfinite(c) or c < 0:
        errs.append("expected_cost must be a finite non-negative number")
    elif remaining_budget is not None and c > remaining_budget + 1e-9:
        errs.append("expected_cost exceeds remaining budget")
    if payload["source"] not in SOURCES:
        errs.append(f"source must be one of {SOURCES}")
    return errs

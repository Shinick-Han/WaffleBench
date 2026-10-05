"""``python -m sem_decisions.cli self-check|demo|evaluate-fixture`` (development only).

Every output is JSON and labeled; fixture results carry SYNTHETIC_LABEL and are never
primary or real evidence. ``evaluate-fixture --manifest PATH`` validates a real manifest
in the common schema (and optionally file digests) without reading masks into decisions.
Exit code is nonzero on failure.
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np

from . import DEVELOPMENT_SEED, SYNTHETIC_LABEL, acquisition, conformal, fixtures, jev_payload, manifest, shift
from .learning_backends import available_backends, facility_location
from .offline_tools import artimagen_launch_config
from .policy import SemBudgetAuditPolicy


def _demo(seed: int) -> dict:
    view, p = fixtures.random_lot(seed)
    pol = SemBudgetAuditPolicy(0, {"cost": fixtures.COST})
    sim = fixtures.simulate(pol, view, p, budget=400.0, seed=seed)
    sim.pop("rows")
    return {"kind": "development_simulation", "seed": seed, **sim}


def _self_check(seed: int) -> dict:
    checks = {}
    view, p = fixtures.random_lot(seed)
    a = fixtures.simulate(SemBudgetAuditPolicy(0, {"cost": fixtures.COST}), view, p, 300.0, seed=seed)
    b = fixtures.simulate(SemBudgetAuditPolicy(0, {"cost": fixtures.COST}), view, p, 300.0, seed=seed)
    checks["policy_deterministic"] = a["rows"] == b["rows"]
    checks["budget_respected"] = a["spent"] <= 300.0 + 1e-9
    rng = np.random.default_rng(seed)
    ref = rng.normal(size=(60, 3))
    checks["shift_detects_mean_shift"] = shift.feature_shift(ref, rng.normal(1.5, 1, (60, 3)), seed=seed)[
        "shift_detected"]
    ind = conformal.check_independent(["a"], ["b", "c"], ["d"], {"a": 1, "b": 2, "c": 3, "d": 4})
    model = conformal.calibrate(np.array([0.9, 0.2]), np.array([1, 0]), 0.2, independence=ind)
    alert = conformal.coverage_statement(model, {"shift_detected": True}, ind, exchangeability_assumed=True)
    no_alert = conformal.coverage_statement(model, {"shift_detected": False}, ind)
    checks["conformal_shift_invalidates"] = not alert["conditional_statement_valid"]
    checks["conformal_no_alert_does_not_certify"] = (not no_alert["conditional_statement_valid"]
                                                     and no_alert["empirical_guarantee"] == "unverified")
    costs = acquisition.RepeatCosts(1.0, 1.0, 2.0, 2.0, 2)
    r = acquisition.run_repeat_protocol(lambda k: {"snr": 0.1, "contrast": 1, "sharpness": 1,
                                                   "saturation_fraction": 0}, lambda m: False, costs,
                                        remaining_budget=10, remaining_time_s=100)
    checks["invalid_not_negative"] = r.outcome == "invalid_acquisition"
    checks["jev_rejects_hidden"] = bool(jev_payload.validate_payload(
        {"schema_version": 1, "action": "stop", "target_id": None, "rationale": "x", "evidence_ids": [],
         "expected_cost": 0, "source": "jev", "truth_mask": 1}, observed_ids=[]))
    checks["fixture_manifest_valid"] = manifest.validate(fixtures.fixture_manifest(), allow_fixture=True)[
        "items"] == 8
    checks["fallback_named"] = "fallback" in facility_location(ref[:5], 2)["backend"]
    return {"ok": all(checks.values()), "checks": checks, "optional_backends": available_backends(),
            "artimagen": artimagen_launch_config("<unset>", seed=seed, noise_levels=[0.1], contrast_levels=[0.5],
                                                 images_per_level=1)["execution"]}


def _evaluate(path: str | None, verify_files: bool) -> dict:
    if path is None:
        m = fixtures.fixture_manifest()
        summary = manifest.validate(m, allow_fixture=True)
        source = "built_in_fixture"
    else:
        m, summary = manifest.load(path, verify_files=verify_files)
        source = "manifest_file"
    ids = manifest.split_ids(m)
    ind = conformal.check_independent(ids["train"], ids["calibration"], ids["test"], manifest.duplicate_groups(m))
    return {"source": source, "summary": summary, "calibration_independence": ind,
            "prospective_test_items": len(manifest.prospective_view(m, "test")),
            "real_evidence": False if source == "built_in_fixture" else "manifest_validation_only"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sem_decisions.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("self-check", "demo"):
        s = sub.add_parser(name)
        s.add_argument("--seed", type=int, default=DEVELOPMENT_SEED)
    e = sub.add_parser("evaluate-fixture")
    e.add_argument("--manifest", default=None)
    e.add_argument("--verify-files", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "self-check":
            out = _self_check(args.seed)
            status = 0 if out["ok"] else 1
        elif args.cmd == "demo":
            out, status = _demo(args.seed), 0
        else:
            out, status = _evaluate(args.manifest, args.verify_files), 0
    except Exception as exc:  # noqa: BLE001 - CLI reports every failure as JSON
        out, status = {"error": f"{type(exc).__name__}: {exc}"}, 1
    out = {"command": args.cmd, "label": SYNTHETIC_LABEL if args.cmd != "evaluate-fixture" or args.manifest is None
           else "manifest validation (no model results)", "status": "ok" if status == 0 else "failed", **out}
    print(json.dumps(out, indent=2, default=str))
    return status


if __name__ == "__main__":
    sys.exit(main())

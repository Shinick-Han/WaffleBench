"""SEM decision tools tests with small SYNTHETIC fixtures (non-scientific, development only)."""

from __future__ import annotations

import copy
import math
import unittest

import numpy as np

from inspection_review.harness import cost_vectors
from sem_decisions import acquisition as acq
from sem_decisions import conformal, fixtures, jev_payload, manifest, shift
from sem_decisions.cli import main as cli_main
from sem_decisions.learning_backends import available_backends, facility_location, uncertainty_rank
from sem_decisions.offline_tools import artimagen_launch_config, offline_detection_curve
from sem_decisions.policy import InformationBoundaryError, SemBudgetAuditPolicy, decision_config

COST = fixtures.COST


def policy(**sem):
    return SemBudgetAuditPolicy(0, {"cost": dict(COST), "sem_decisions": sem})


def snapshot(state):
    return (state.visited.copy(), state.label.copy(), list(state.selected), list(state.labeled),
            state.current_wafer, None if state.current_xy is None else state.current_xy.copy(),
            state.frozen_p.copy(), state.online_p.copy(), copy.deepcopy(state.online_model), state.min_dist.copy())


def same(a, b):
    for x, y in zip(a, b):
        if isinstance(x, np.ndarray):
            if not np.array_equal(x, y, equal_nan=True):
                return False
        elif x != y:
            return False
    return True


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.view, self.p = fixtures.random_lot(11, n_per_wafer=8)

    def test_choice_inside_affordable_candidates(self):
        cand = np.array([True, False] * 12)
        view = fixtures.view_of(np.repeat([0, 1, 2], 8), np.random.default_rng(1).uniform(-1, 1, (24, 2)), cand)
        state = fixtures.state_of(view, self.p)
        cv = cost_vectors(state, COST)
        affordable = state.allowed() & (cv["max"] <= 30.0)
        self.assertFalse(affordable[~cand].any())
        c = policy().select(state, cv["max"], affordable)
        self.assertTrue(affordable[c.index] and cand[c.index])

    def test_rejects_disallowed_affordable_and_bad_costs(self):
        state = fixtures.state_of(self.view, self.p)
        cv = cost_vectors(state, COST)
        aff = state.allowed().copy()
        state.record_visit(0)
        with self.assertRaises(ValueError):
            policy().select(state, cost_vectors(state, COST)["max"], aff)  # visited site still marked affordable
        state = fixtures.state_of(self.view, self.p)
        bad = cv["max"].copy()
        bad[3] = np.nan
        with self.assertRaises(ValueError):
            policy().select(state, bad, state.allowed())

    def test_retry_reserve_must_be_in_max_cost(self):
        state = fixtures.state_of(self.view, self.p)
        cv = cost_vectors(state, COST)
        with self.assertRaises(ValueError):
            policy().select(state, cv["first"], state.allowed())  # reserve missing
        c = policy().select(state, cv["max"], state.allowed())
        self.assertEqual(c.components["cost"]["retry_reserve"], COST["retry_dwell"] * COST["retry_limit"])
        self.assertTrue(c.components["cost"]["wafer_switch"])

    def test_oracle_field_and_future_observation_rejected(self):
        state = fixtures.state_of(self.view, self.p)
        cv = cost_vectors(state, COST)
        state.oracle_truth = np.ones(self.view.n)
        with self.assertRaises(InformationBoundaryError):
            policy().select(state, cv["max"], state.allowed())
        state = fixtures.state_of(self.view, self.p)
        state.label[5] = 1.0  # outcome of a site never selected
        with self.assertRaises(InformationBoundaryError):
            policy().select(state, cv["max"], state.allowed())
        state = fixtures.state_of(self.view, self.p)
        state.labeled.append(4)
        with self.assertRaises(InformationBoundaryError):
            policy().select(state, cv["max"], state.allowed())

    def test_select_does_not_mutate_state(self):
        state = fixtures.state_of(self.view, self.p)
        state.record_visit(2)
        state.record_label(2, True)
        cv = cost_vectors(state, COST)
        before = snapshot(state)
        pol = policy(audit_min=0.0, audit_target=1.0, audit_max=1.0)
        for _ in range(3):
            pol.select(state, cv["max"], state.allowed())
        self.assertTrue(same(before, snapshot(state)))

    def test_deterministic_tie_break_lowest_index(self):
        n = 6
        view = fixtures.view_of(np.zeros(n, int), np.zeros((n, 2)))
        state = fixtures.state_of(view, np.full(n, 0.7))
        cv = cost_vectors(state, COST)
        c1 = policy().select(state, cv["max"], state.allowed())
        c2 = policy().select(state, cv["max"], state.allowed())
        self.assertEqual((c1.index, c2.index), (0, 0))
        self.assertEqual(c1.components["tie_break"], "lowest_site_index")

    def test_repeated_select_same_step_is_idempotent(self):
        state = fixtures.state_of(self.view, self.p)
        pol = policy(audit_min=0.0, audit_target=1.0, audit_max=1.0)
        cv = cost_vectors(state, COST)
        a = pol.select(state, cv["max"], state.allowed())
        b = pol.select(state, cv["max"], state.allowed())
        self.assertEqual((a.index, a.reason), (b.index, b.reason))

    def test_audit_fraction_bounds_and_separate_objectives(self):
        view, p = fixtures.random_lot(5, n_per_wafer=15)
        pol = policy(audit_min=0.1, audit_target=0.2, audit_max=0.3)
        sim = fixtures.simulate(pol, view, p, 2000.0, seed=7)
        k = 0
        audits = 0
        for row in sim["rows"]:
            k += 1
            audits += row["reason"].startswith("audit")
            self.assertLessEqual(audits, 0.3 * k + 1e-9)
        self.assertGreaterEqual(audits, math.floor(0.1 * k))
        self.assertGreater(audits, 0)
        state = fixtures.state_of(view, p)
        c = pol.select(state, cost_vectors(state, COST)["max"], state.allowed())
        self.assertIn("yield_objective", c.components)
        self.assertIn("learning_objective", c.components)
        self.assertNotIn("combined", str(c.components))

    def test_budget_invariant_and_determinism(self):
        for budget in (25.0, 90.0, 300.0):
            a = fixtures.simulate(policy(), self.view, self.p, budget, seed=3, fail_rate=0.4)
            b = fixtures.simulate(policy(), self.view, self.p, budget, seed=3, fail_rate=0.4)
            self.assertLessEqual(a["spent"], budget + 1e-9)
            self.assertEqual(a["rows"], b["rows"])
            for r in a["rows"]:
                if r["status"] == "failed":
                    self.assertIsNone(r["reported_doi"])

    def test_invalid_config_rejected(self):
        for bad in ({"audit_min": float("nan")}, {"audit_max": True}, {"audit_min": 0.5, "audit_max": 0.1},
                    {"uncertainty_weight": 0.9}):
            with self.assertRaises(ValueError):
                decision_config({"sem_decisions": bad})
        with self.assertRaises(ValueError):
            SemBudgetAuditPolicy(0, {"cost": {**COST, "dwell": float("inf")}})
        state = fixtures.state_of(self.view, self.p)
        state.remaining_budget = float("nan")
        with self.assertRaises(ValueError):
            policy().select(state, cost_vectors(state, COST)["max"], state.allowed())


class ConformalShiftTests(unittest.TestCase):
    def test_independent_calibration_split_required(self):
        groups = {"a": 1, "b": 2, "c": 3, "d": 1}
        with self.assertRaises(conformal.CalibrationError):
            conformal.check_independent(["a"], ["b", "d"], ["c"], groups)  # duplicate group leak
        with self.assertRaises(conformal.CalibrationError):
            conformal.check_independent(["a", "b"], ["b"], ["c"])
        with self.assertRaises(conformal.CalibrationError):
            conformal.calibrate(np.array([0.5]), np.array([1]), 0.1, independence=None)
        ind = conformal.check_independent(["a"], ["b"], ["c"], groups)
        self.assertTrue(ind["independent"])

    def test_coverage_on_exchangeable_fixture_and_abstention(self):
        rng = np.random.default_rng(0)
        y = rng.random(400) < 0.4
        p = np.clip(np.where(y, rng.beta(4, 2, 400), rng.beta(2, 4, 400)), 0, 1)
        ind = conformal.check_independent(["f"], [f"c{i}" for i in range(200)], ["t"])
        m = conformal.calibrate(p[:200], y[:200].astype(int), 0.1, independence=ind)
        self.assertGreaterEqual(conformal.empirical_coverage(m, p[200:], y[200:].astype(int)), 0.8)
        decisions = {d["decision"] for d in m.decide(p[200:])}
        self.assertIn("abstain_request_review", decisions)
        one = conformal.check_independent(["f"], ["c0"], ["t"])
        tiny = conformal.calibrate(np.array([0.9]), np.array([1]), 0.1, independence=one)
        self.assertTrue(math.isinf(tiny.qhat))
        self.assertEqual(tiny.decide([0.99])[0]["decision"], "abstain_request_review")

    def test_shift_invalidates_and_no_alert_does_not_certify(self):
        rng = np.random.default_rng(1)
        ref = rng.normal(size=(80, 2))
        cur = rng.normal(loc=2.0, size=(80, 2))
        rep = shift.feature_shift(ref, cur, seed=2)
        self.assertTrue(rep["shift_detected"])
        self.assertFalse(rep["uses_labels"])
        ind = conformal.check_independent(["a"], ["b"], ["c"])
        m = conformal.calibrate(np.array([0.8]), np.array([1]), 0.5, independence=ind)
        st = conformal.coverage_statement(m, rep, ind, exchangeability_assumed=True)
        self.assertFalse(st["conditional_statement_valid"])
        self.assertFalse(conformal.coverage_statement(m, None, ind, exchangeability_assumed=True)[
            "conditional_statement_valid"])
        same = shift.feature_shift(ref, rng.normal(size=(80, 2)), seed=2)
        self.assertFalse(same["shift_detected"])
        self.assertFalse(same["no_alert_establishes_exchangeability"])
        # No alert alone never certifies coverage.
        plain = conformal.coverage_statement(m, same, ind)
        self.assertFalse(plain["conditional_statement_valid"])
        self.assertIn("exchangeability not explicitly assumed", plain["invalid_reasons"][0])
        self.assertEqual(plain["empirical_guarantee"], "unverified")
        self.assertFalse(plain["empirical_coverage_certified"])
        # Only an explicit assumption yields the conditional theoretical statement; empirical stays unverified.
        cond = conformal.coverage_statement(m, same, ind, exchangeability_assumed=True)
        self.assertTrue(cond["conditional_statement_valid"])
        self.assertIn("IF", cond["conditional_statement"])
        self.assertEqual(cond["empirical_guarantee"], "unverified")
        self.assertFalse(cond["empirical_coverage_certified"])
        self.assertFalse(conformal.coverage_statement(m, same, ind, exchangeability_assumed="yes")[
            "conditional_statement_valid"])
        self.assertFalse(conformal.coverage_statement(m, {}, ind, exchangeability_assumed=True)[
            "conditional_statement_valid"])  # inconclusive report
        other = conformal.check_independent(["a"], ["b", "d"], ["c"])
        self.assertFalse(conformal.coverage_statement(m, same, other, exchangeability_assumed=True)[
            "conditional_statement_valid"])  # evidence does not match the model

    def test_probability_label_and_evidence_validation(self):
        ind = conformal.check_independent(["f"], ["c0", "c1"], ["t"])
        for bad_p in ([0.5, np.nan], [0.5, 1.2], [-0.1, 0.5], [np.inf, 0.5], [True, False], ["a", "b"]):
            with self.assertRaises(conformal.CalibrationError):
                conformal.calibrate(np.array(bad_p), np.array([1, 0]), 0.1, independence=ind)
        with self.assertRaises(conformal.CalibrationError):
            conformal.calibrate(np.array([0.5, 0.4]), np.array([1, 0, 1]), 0.1, independence=ind)
        with self.assertRaises(conformal.CalibrationError):
            conformal.calibrate(np.array([0.5, 0.4]), np.array([1, 2]), 0.1, independence=ind)
        with self.assertRaises(conformal.CalibrationError):
            conformal.calibrate(np.array([0.5, 0.4, 0.3]), np.array([1, 0, 1]), 0.1, independence=ind)  # n mismatch
        for bad_alpha in (float("nan"), True, 0.0, 1.0):
            with self.assertRaises(conformal.CalibrationError):
                conformal.calibrate(np.array([0.5, 0.4]), np.array([1, 0]), bad_alpha, independence=ind)
        m = conformal.calibrate(np.array([0.5, 0.4]), np.array([1, 0]), 0.1, independence=ind)
        with self.assertRaises(conformal.CalibrationError):
            m.sets([1.5])
        with self.assertRaises(conformal.CalibrationError):
            conformal.empirical_coverage(m, np.array([0.2, 0.3]), np.array([1]))

    def test_shift_parameter_validation(self):
        ref = np.random.default_rng(5).normal(size=(20, 2))
        for kw in ({"n_permutations": 0}, {"n_permutations": True}, {"n_permutations": 2.5},
                   {"alpha": float("nan")}, {"alpha": 1.0}):
            with self.assertRaises(ValueError):
                shift.feature_shift(ref, ref + 1, **kw)
        for kw in ({"n_splits": 0}, {"n_splits": -1}, {"n_permutations": 0}):
            with self.assertRaises(ValueError):
                shift.calibrate_false_alert(ref, **kw)
        with self.assertRaises(ValueError):
            shift.label_shift([0, 1], [1, 2])
        with self.assertRaises(ValueError):
            shift.label_shift([0, 1], [1, 0], n_permutations=0)

    def test_false_alert_calibration_and_label_availability(self):
        ref = np.random.default_rng(3).normal(size=(60, 2))
        cal = shift.calibrate_false_alert(ref, alpha=0.1, n_splits=10, n_permutations=49, seed=4)
        self.assertLessEqual(cal["false_alert_rate"], 0.5)
        self.assertEqual(shift.label_shift([0, 1, 0], None)["status"], "unavailable_labels_not_observed")
        self.assertEqual(shift.label_shift([0, 1, 0], [1, 1])["status"], "observed_labels_only")
        with self.assertRaises(ValueError):
            shift.feature_shift(ref, np.full((5, 2), np.nan))


GOOD = {"snr": 10.0, "contrast": 0.5, "sharpness": 0.8, "saturation_fraction": 0.0}


class AcquisitionTests(unittest.TestCase):
    costs = acq.RepeatCosts(capture_cost=2.0, repeat_cost=1.5, capture_time_s=3.0, repeat_time_s=2.0, retry_cap=2)

    def test_invalid_image_never_becomes_physical_negative(self):
        bad = dict(GOOD, snr=0.5)
        r = acq.run_repeat_protocol(lambda k: bad, lambda m: False, self.costs, remaining_budget=100,
                                    remaining_time_s=100)
        self.assertEqual(r.outcome, "invalid_acquisition")
        self.assertEqual(r.stop_reason, "retry_cap_reached")
        self.assertEqual(len(r.attempts), 3)
        self.assertAlmostEqual(r.cost, 2.0 + 2 * 1.5)
        self.assertEqual(acq.classify_outcome("unknown", False), "unknown")
        self.assertEqual(acq.classify_outcome("invalid", False), "invalid_acquisition")
        self.assertEqual(acq.classify_outcome("valid", False), "observed_negative")
        unk = acq.run_repeat_protocol(lambda k: {"snr": 9.0}, lambda m: False, self.costs,
                                      remaining_budget=100, remaining_time_s=100)
        self.assertEqual(unk.outcome, "unknown")
        self.assertEqual(r.as_dict()["thresholds_status"], acq.THRESHOLDS_STATUS)
        self.assertFalse(r.as_dict()["hardware_connected"])

    def test_repeat_then_valid_and_budget_reserve(self):
        seq = [dict(GOOD, out_of_focus=True), GOOD]
        r = acq.run_repeat_protocol(lambda k: seq[k], lambda m: True, self.costs, remaining_budget=100,
                                    remaining_time_s=100)
        self.assertEqual((r.outcome, len(r.attempts)), ("observed_positive", 2))
        r = acq.run_repeat_protocol(lambda k: dict(GOOD, snr=0.1), lambda m: False, self.costs,
                                    remaining_budget=3.0, remaining_time_s=100)
        self.assertEqual(r.stop_reason, "repeat_unaffordable")
        self.assertLessEqual(r.cost, 3.0)
        r = acq.run_repeat_protocol(lambda k: GOOD, lambda m: None, self.costs, remaining_budget=1.0,
                                    remaining_time_s=100)
        self.assertEqual((r.outcome, r.attempts), ("unknown", []))

    def test_truth_inputs_and_nonfinite_parameters_rejected(self):
        with self.assertRaises(ValueError):
            acq.assess_quality(dict(GOOD, mask_iou=0.9))
        with self.assertRaises(ValueError):
            acq.assess_quality(dict(GOOD, defect_class=6))
        for bad in (float("nan"), float("inf"), True, -1.0):
            with self.assertRaises(ValueError):
                acq.RepeatCosts(bad, 1.0, 1.0, 1.0, 1)
        with self.assertRaises(ValueError):
            acq.RepeatCosts(1.0, 1.0, 1.0, 1.0, 1.5)
        with self.assertRaises(ValueError):
            acq.RepeatCosts(1.0, 1.0, 1.0, 1.0, True)
        with self.assertRaises(ValueError):
            acq.run_repeat_protocol(lambda k: GOOD, lambda m: True, self.costs, remaining_budget=float("nan"),
                                    remaining_time_s=10)
        self.assertEqual(acq.assess_quality(GOOD)["thresholds_status"],
                         "development_default_not_instrument_calibrated")


class ManifestTests(unittest.TestCase):
    def test_fixture_manifest_and_real_mode_guard(self):
        m = fixtures.fixture_manifest()
        s = manifest.validate(m, allow_fixture=True)
        self.assertEqual(s["splits"], {"train": 3, "calibration": 3, "test": 2})
        with self.assertRaises(manifest.ManifestError):
            manifest.validate(m)  # a fixture never passes as data_mode='real'
        view = manifest.prospective_view(m, "test")
        self.assertTrue(all("mask" not in it and "defect_class" not in it and "mask_sha256" not in it
                            for it in view))

    def test_duplicate_group_crossing_and_paths_rejected(self):
        m = fixtures.fixture_manifest()
        m["items"][3]["duplicate_group"] = "g1"  # train group moved into calibration
        with self.assertRaisesRegex(manifest.ManifestError, "crosses splits"):
            manifest.validate(m, allow_fixture=True)
        m = fixtures.fixture_manifest()
        m["items"][0]["image"] = "../escape.png"
        m["items"][1]["id"] = "a"
        m["items"][2]["synthetic"] = True
        with self.assertRaises(manifest.ManifestError) as cm:
            manifest.validate(m, allow_fixture=True)
        msg = str(cm.exception)
        self.assertIn("relative path", msg)
        self.assertIn("duplicate id", msg)
        self.assertIn("synthetic", msg)

    def test_numeric_classes_preserved_and_scarcity(self):
        m = fixtures.fixture_manifest()
        for it in m["items"]:
            it["defect_class"] = 5 if it["id"] in ("a", "d") else 6
        s = manifest.validate(m, allow_fixture=True)["classes"]
        self.assertEqual(s["totals"], {"5": 2, "6": 6})
        self.assertIn("5", s["scarce"])
        self.assertEqual(s["missing_from_split"]["test"], ["5"])

    def test_verify_files_reports_missing_root(self):
        m = fixtures.fixture_manifest(root="C:/definitely-missing-sem-root-xyz")
        with self.assertRaisesRegex(manifest.ManifestError, "cannot be verified"):
            manifest.validate(m, allow_fixture=True, verify_files=True)


class PayloadAndToolsTests(unittest.TestCase):
    def payload(self, **kw):
        p = {"schema_version": 1, "action": "select_site", "target_id": "s1", "rationale": "highest yield/cost",
             "evidence_ids": ["r1"], "expected_cost": 20.0, "source": "jev"}
        p.update(kw)
        return p

    def test_payload_validation(self):
        ok = jev_payload.validate_payload(self.payload(), observed_ids=["r1"], allowed_targets=["s1"],
                                          remaining_budget=50)
        self.assertEqual(ok, [])
        self.assertTrue(jev_payload.validate_payload(self.payload(evidence_ids=["r9"]), observed_ids=["r1"]))
        self.assertTrue(jev_payload.validate_payload(self.payload(expected_cost=90), observed_ids=["r1"],
                                                     remaining_budget=50))
        self.assertTrue(jev_payload.validate_payload(self.payload(expected_cost=float("nan")), observed_ids=["r1"]))
        self.assertTrue(jev_payload.validate_payload(self.payload(action="launch"), observed_ids=["r1"]))
        nested = self.payload()
        nested["rationale"] = "x"
        nested["extra"] = {"test_mask_path": "m.png"}
        errs = jev_payload.validate_payload(nested, observed_ids=["r1"])
        self.assertTrue(any("forbidden" in e for e in errs))

    def test_backends_named_and_not_claimed_executed(self):
        r = facility_location(np.array([[0.0], [0.1], [5.0]]), 2)
        self.assertIn("not modAL/apricot", r["backend"])
        self.assertFalse(r["upstream_executed"])
        self.assertEqual(r["indices"][:2], facility_location(np.array([[0.0], [0.1], [5.0]]), 2)["indices"])
        self.assertEqual(uncertainty_rank([0.5, 0.5, 0.1], 2)["indices"], [0, 1])
        for info in available_backends().values():
            self.assertFalse(info["executed"])

    def test_nonfinite_or_negative_costs_budgets_rejected(self):
        view, p = fixtures.random_lot(2, n_per_wafer=4)
        for bad in (float("nan"), float("inf"), -1.0, True):
            with self.assertRaises(ValueError):
                fixtures.simulate(policy(), view, p, bad, seed=1)
            with self.assertRaises(ValueError):
                fixtures.simulate(policy(), view, p, 50.0, seed=1, cost={**COST, "dwell": bad})
            with self.assertRaises(ValueError):
                jev_payload.validate_payload(self.payload(), observed_ids=["r1"], remaining_budget=bad)
            with self.assertRaises(ValueError):
                SemBudgetAuditPolicy(0, {"cost": {**COST, "retry_dwell": bad}})
        with self.assertRaises(ValueError):
            SemBudgetAuditPolicy(0, {"cost": {**COST, "retry_limit": 1.5}})
        state = fixtures.state_of(view, p)
        state.remaining_budget = -5.0
        with self.assertRaises(ValueError):
            policy().select(state, cost_vectors(state, COST)["max"], state.allowed())
        for kw in ({"images_per_level": 0}, {"images_per_level": True}, {"noise_levels": [float("nan")]}):
            args = {"seed": 1, "noise_levels": [0.1], "contrast_levels": [0.5], "images_per_level": 1, **kw}
            with self.assertRaises(ValueError):
                artimagen_launch_config("out", **args)

    def test_offline_and_launch_tools(self):
        recs = [{"q": 0.2, "truth_defect": True, "detected": False}, {"q": 0.8, "truth_defect": False,
                                                                       "detected": True}]
        with self.assertRaises(PermissionError):
            offline_detection_curve(recs, quality_key="q", bins=[0, 0.5, 1], context="prospective")
        out = offline_detection_curve(recs, quality_key="q", bins=[0, 0.5, 1], context="offline_evaluation")
        self.assertEqual(out["bins"][0]["false_negative_rate"], 1.0)
        self.assertFalse(out["prospective_use_allowed"])
        cfg = artimagen_launch_config("out", seed=1, noise_levels=[0.1], contrast_levels=[0.5], images_per_level=2)
        self.assertEqual(cfg["execution"], "not_run")
        self.assertFalse(cfg["prerequisites_verified"])
        self.assertFalse(cfg["calibrated_instrument_model"])

    def test_cli_self_check(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(cli_main(["self-check"]), 0)
        self.assertIn("SYNTHETIC FIXTURE", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

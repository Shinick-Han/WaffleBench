"""Adapter scope tests for routing_poc.contracts and routing_poc.replay (stdlib only).

Uses small hand-written development fixtures; never the reserved synthetic test seeds.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from routing_poc.contracts import (ContractError, estimate_cost, validate_archive,
                                   validate_job)
from routing_poc.replay import run_loop

CUTOFF = "2026-10-05T00:00:00+00:00"
AFTER = "2026-10-05T01:00:00+00:00"
LATER = "2026-10-05T01:05:00+00:00"


def candidate(site, wafer="W1", x=0.0, y=0.0, p=0.5, cap=2.0, inf=1.0):
    return {"site_id": site, "wafer_id": wafer, "x_um": x, "y_um": y, "prior_p": p,
            "optical_observed_at": "2026-10-04T23:00:00Z", "recipe_id": "R1",
            "capture_bound_s": cap, "inference_bound_s": inf}


def make_job(retry=1, candidates=None):
    return {"schema_version": 1, "job_id": "dev-job-1", "data_mode": "synthetic",
            "cutoff_utc": CUTOFF,
            "cost": {"wafer_load_s": 10.0, "settle_s": 0.5, "move_um_per_s": 100.0,
                     "retry_limit": retry},
            "candidates": candidates if candidates is not None else [
                candidate("A", x=0.0, p=0.9), candidate("B", x=300.0, p=0.6),
                candidate("C", wafer="W2", p=0.2)],
            "provenance": {"source": "unit-test development fixture"}}


def ok(doi, cap=1.5, inf=0.5, at=AFTER, **extra):
    return {"status": "ok", "reported_doi": doi, "capture_s": cap, "inference_s": inf,
            "observed_at": at, **extra}


def bad(status, cap=1.0, inf=0.0, at=AFTER, **extra):
    return {"status": status, "reported_doi": None, "capture_s": cap, "inference_s": inf,
            "observed_at": at, **extra}


def make_archive(observations=None, reference=None):
    archive = {"job_id": "dev-job-1",
               "observations": observations if observations is not None else {
                   "A": [ok(True)], "B": [bad("failed"), ok(False, at=LATER)],
                   "C": [bad("missing"), bad("missing", at=LATER)]}}
    if reference is not None:
        archive["reference"] = reference
    return archive


def order_selector(order):
    def select(state, rng):
        ids = {c["site_id"] for c in state["candidates"]}
        for site in order:
            if site in ids:
                return {"site_id": site, "reason": "fixed order"}
        return None
    return select


class JobValidationTests(unittest.TestCase):
    def test_valid_job_returns_deep_copy(self):
        job = make_job()
        clean = validate_job(job)
        self.assertEqual(clean, job)
        clean["candidates"][0]["prior_p"] = 0.0
        clean["provenance"]["source"] = "x"
        self.assertEqual(job["candidates"][0]["prior_p"], 0.9)
        self.assertEqual(job["provenance"]["source"], "unit-test development fixture")

    def test_timestamp_cutoff(self):
        job = make_job()
        job["candidates"][0]["optical_observed_at"] = CUTOFF  # equal is allowed
        validate_job(job)
        job["candidates"][0]["optical_observed_at"] = "2026-10-05T00:00:01+00:00"
        with self.assertRaisesRegex(ContractError, "after cutoff"):
            validate_job(job)
        job = make_job()
        job["cutoff_utc"] = "2026-10-05T00:00:00"  # naive
        with self.assertRaisesRegex(ContractError, "timezone-aware"):
            validate_job(job)
        job = make_job()
        job["candidates"][1]["optical_observed_at"] = "yesterday"
        with self.assertRaisesRegex(ContractError, "malformed timestamp"):
            validate_job(job)

    def test_offset_timestamps_compared_in_utc(self):
        job = make_job()
        # 01:30+02:00 == 23:30Z previous day, before cutoff.
        job["candidates"][0]["optical_observed_at"] = "2026-10-05T01:30:00+02:00"
        validate_job(job)
        job["candidates"][0]["optical_observed_at"] = "2026-10-04T20:00:01-04:00"
        with self.assertRaises(ContractError):
            validate_job(job)

    def test_no_premeasurement_images_or_truth_features(self):
        for key in ("image_path", "image_sha256", "embedding", "label", "mask", "truth",
                    "doi", "reported_doi", "scenario", "seed", "sem_image"):
            job = make_job()
            job["candidates"][0][key] = "leak"
            with self.assertRaisesRegex(ContractError, "unexpected keys", msg=key):
                validate_job(job)
        job = make_job()
        job["reference"] = {}
        with self.assertRaisesRegex(ContractError, "unexpected keys"):
            validate_job(job)

    def test_malformed_ids(self):
        for value in ("", " A", "A B", "../A", "A/B", None, 7, "x" * 129):
            job = make_job()
            job["candidates"][0]["site_id"] = value
            with self.assertRaisesRegex(ContractError, "malformed id", msg=repr(value)):
                validate_job(job)
        job = make_job()
        job["candidates"][1]["site_id"] = "A"
        with self.assertRaisesRegex(ContractError, "duplicate"):
            validate_job(job)
        job = make_job()
        job["job_id"] = "bad id"
        with self.assertRaises(ContractError):
            validate_job(job)

    def test_numeric_rules(self):
        cases = [("prior_p", 1.5), ("prior_p", True), ("x_um", math.nan),
                 ("y_um", math.inf), ("capture_bound_s", 0.0), ("inference_bound_s", -1),
                 ("capture_bound_s", "2")]
        for key, value in cases:
            job = make_job()
            job["candidates"][0][key] = value
            with self.assertRaises(ContractError, msg=f"{key}={value!r}"):
                validate_job(job)
        for key, value in [("wafer_load_s", -1), ("move_um_per_s", 0), ("settle_s", False),
                           ("retry_limit", 2), ("retry_limit", True), ("retry_limit", 1.0)]:
            job = make_job()
            job["cost"][key] = value
            with self.assertRaises(ContractError, msg=f"{key}={value!r}"):
                validate_job(job)

    def test_schema_and_mode(self):
        for key, value in [("schema_version", 2), ("schema_version", True),
                           ("data_mode", "real")]:
            job = make_job()
            job[key] = value
            with self.assertRaises(ContractError):
                validate_job(job)


class ArchiveValidationTests(unittest.TestCase):
    def test_valid_archive_copy(self):
        archive = make_archive(reference={"complete": True, "source": "synthetic truth",
                                          "doi_by_site": {"A": True, "B": False, "C": True}})
        clean = validate_archive(make_job(), archive)
        clean["observations"]["A"][0]["reported_doi"] = False
        self.assertTrue(archive["observations"]["A"][0]["reported_doi"])

    def test_observation_must_follow_cutoff(self):
        with self.assertRaisesRegex(ContractError, "strictly after cutoff"):
            validate_archive(make_job(), make_archive({"A": [ok(True, at=CUTOFF)]}))
        with self.assertRaisesRegex(ContractError, "retry must follow"):
            validate_archive(make_job(), make_archive(
                {"B": [bad("failed", at=LATER), ok(True, at=AFTER)]}))

    def test_archive_bounds(self):
        three = [bad("failed"), bad("failed", at=LATER), ok(True, at="2026-10-05T02:00:00Z")]
        with self.assertRaisesRegex(ContractError, "at most 2"):
            validate_archive(make_job(), make_archive({"A": three}))
        with self.assertRaisesRegex(ContractError, "retry_limit"):
            validate_archive(make_job(retry=0), make_archive(
                {"A": [bad("failed"), ok(True, at=LATER)]}))
        with self.assertRaisesRegex(ContractError, "after an ok"):
            validate_archive(make_job(), make_archive({"A": [ok(True), ok(False, at=LATER)]}))
        with self.assertRaisesRegex(ContractError, "capture_bound_s"):
            validate_archive(make_job(), make_archive({"A": [ok(True, cap=2.0001)]}))
        with self.assertRaisesRegex(ContractError, "inference_bound_s"):
            validate_archive(make_job(), make_archive({"A": [bad("failed", inf=1.5)]}))

    def test_unknown_is_not_negative(self):
        with self.assertRaisesRegex(ContractError, "unknown is not negative"):
            validate_archive(make_job(), make_archive(
                {"A": [{**bad("missing"), "reported_doi": False}]}))
        with self.assertRaisesRegex(ContractError, "bool for an ok"):
            validate_archive(make_job(), make_archive({"A": [ok(None)]}))
        with self.assertRaisesRegex(ContractError, "bool for an ok"):
            validate_archive(make_job(), make_archive({"A": [ok(0)]}))

    def test_malformed_and_unknown_ids(self):
        with self.assertRaisesRegex(ContractError, "malformed id"):
            validate_archive(make_job(), make_archive({"A B": [ok(True)]}))
        with self.assertRaisesRegex(ContractError, "not a job candidate"):
            validate_archive(make_job(), make_archive({"Z": [ok(True)]}))
        archive = make_archive()
        archive["job_id"] = "other-job"
        with self.assertRaisesRegex(ContractError, "does not match"):
            validate_archive(make_job(), archive)

    def test_attempt_and_image_fields(self):
        with self.assertRaisesRegex(ContractError, "unexpected keys"):
            validate_archive(make_job(), make_archive({"A": [ok(True, label="x")]}))
        with self.assertRaisesRegex(ContractError, "requires image_path"):
            validate_archive(make_job(), make_archive({"A": [ok(True, image_sha256="0" * 64)]}))
        with self.assertRaisesRegex(ContractError, "64 lowercase hex"):
            validate_archive(make_job(), make_archive(
                {"A": [ok(True, image_path="a.png", image_sha256="ABC")]}))
        with self.assertRaisesRegex(ContractError, "missing attempt cannot carry"):
            validate_archive(make_job(), make_archive({"A": [bad("missing", image_path="a.png")]}))

    def test_reference_rules(self):
        incomplete = {"complete": True, "source": "s", "doi_by_site": {"A": True, "B": None}}
        with self.assertRaisesRegex(ContractError, "truth absent/null"):
            validate_archive(make_job(), make_archive(reference=incomplete))
        partial = {"complete": False, "source": "s", "doi_by_site": {"A": True, "B": None}}
        validate_archive(make_job(), make_archive(reference=partial))
        for ref in ({"complete": "yes", "source": "s", "doi_by_site": {}},
                    {"complete": False, "source": "", "doi_by_site": {}},
                    {"complete": False, "source": "s", "doi_by_site": {"Z": True}},
                    {"complete": False, "source": "s", "doi_by_site": {"A": 1}}):
            with self.assertRaises(ContractError, msg=ref):
                validate_archive(make_job(), make_archive(reference=ref))


class CostTests(unittest.TestCase):
    def setUp(self):
        self.cost = make_job()["cost"]

    def test_first_visit_loads_and_reserves_retry(self):
        est = estimate_cost(candidate("A", x=500.0), None, self.cost)
        self.assertEqual(est, {"load_s": 10.0, "move_s": 0.0, "settle_s": 0.5,
                               "first_s": 13.5, "reserved_s": 16.5})
        no_retry = dict(self.cost, retry_limit=0)
        self.assertEqual(estimate_cost(candidate("A"), None, no_retry)["reserved_s"], 13.5)

    def test_movement_only_on_same_wafer(self):
        current = {"wafer_id": "W1", "x_um": 0.0, "y_um": 0.0}
        same = estimate_cost(candidate("B", x=300.0, y=400.0), current, self.cost)
        self.assertEqual((same["load_s"], same["move_s"]), (0.0, 5.0))
        self.assertAlmostEqual(same["reserved_s"], 5.0 + 0.5 + 3.0 + 3.0)
        other = estimate_cost(candidate("C", wafer="W2", x=9e6), current, self.cost)
        self.assertEqual((other["load_s"], other["move_s"]), (10.0, 0.0))

    def test_accepts_state_candidate_with_cost_key_and_rejects_bad_current(self):
        item = dict(candidate("A"), cost={"reserved_s": 1})
        estimate_cost(item, None, self.cost)
        with self.assertRaises(ContractError):
            estimate_cost(candidate("A"), {"wafer_id": "W1", "x_um": 0.0}, self.cost)
        with self.assertRaises(ContractError):
            estimate_cost(candidate("A"), None, dict(self.cost, move_um_per_s=-1))


class ReplayLoopTests(unittest.TestCase):
    def test_charges_actual_times_including_failed_and_retry(self):
        run = run_loop(make_job(), make_archive(), order_selector(["A", "B", "C"]), 100.0)
        rows = {r["site_id"]: r for r in run["rows"]}
        # A: load 10 + settle 0.5 + 1.5 + 0.5
        self.assertAlmostEqual(rows["A"]["charged_s"], 12.5)
        self.assertEqual(rows["A"]["reserved_s"], 16.5)
        self.assertTrue(rows["A"]["reported_doi"])
        # B: move 3s, settle, failed (1+0) then ok (1.5+0.5)
        self.assertEqual(rows["B"]["resource_parts"],
                         {"load_s": 0.0, "move_s": 3.0, "settle_s": 0.5,
                          "capture_s": 2.5, "inference_s": 0.5})
        self.assertAlmostEqual(rows["B"]["charged_s"], 6.5)
        self.assertEqual([a["status"] for a in rows["B"]["attempts"]], ["failed", "ok"])
        self.assertIs(rows["B"]["reported_doi"], False)
        # C: new wafer load, two missing attempts paid; outcome stays unknown.
        self.assertAlmostEqual(rows["C"]["charged_s"], 10 + 0.5 + 2.0)
        self.assertIsNone(rows["C"]["reported_doi"])
        self.assertEqual(rows["C"]["final_status"], "missing")
        self.assertAlmostEqual(run["spent_s"], 12.5 + 6.5 + 12.5)
        self.assertAlmostEqual(rows["C"]["cumulative_s"], run["spent_s"])
        self.assertAlmostEqual(run["remaining_s"], 100.0 - run["spent_s"])
        self.assertEqual(run["stop_reason"], "candidates_exhausted")
        self.assertIs(run["commercial_validated"], False)
        self.assertIn("not physical elapsed time", run["time_semantics"])
        for row in run["rows"]:
            self.assertLessEqual(row["charged_s"], row["reserved_s"])
        json.dumps(run, allow_nan=False)

    def test_no_retry_when_limit_zero_or_after_ok(self):
        archive = make_archive({"A": [bad("failed")], "B": [ok(True)]})
        run = run_loop(make_job(retry=0), archive, order_selector(["A", "B"]), 100.0)
        self.assertEqual([len(r["attempts"]) for r in run["rows"]], [1, 1])
        self.assertIsNone(run["rows"][0]["reported_doi"])

    def test_missing_archive_record_is_paid_unknown(self):
        archive = make_archive({"B": [bad("failed")]})
        run = run_loop(make_job(), archive, order_selector(["A", "B"]), 100.0)
        a, b = run["rows"]
        self.assertEqual(a["final_status"], "unavailable")
        self.assertIsNone(a["reported_doi"])
        self.assertAlmostEqual(a["charged_s"], a["reserved_s"] - 3.0)  # first_s at bounds
        self.assertTrue(a["attempts"][0]["charged_at_bound"])
        self.assertEqual([x["status"] for x in b["attempts"]], ["failed", "unavailable"])
        self.assertIsNone(b["reported_doi"])

    def test_admission_reserves_full_bounds(self):
        def greedy(state, rng):
            ok_ = [c for c in state["candidates"]
                   if c["cost"]["reserved_s"] <= state["remaining_s"]]
            return {"site_id": ok_[0]["site_id"]} if ok_ else None
        # A reserves 16.5 even though it would only charge 12.5.
        run = run_loop(make_job(), make_archive(), greedy, 16.4)
        self.assertEqual(run["rows"], [])
        self.assertEqual(run["stop_reason"], "no_affordable_candidate")
        with self.assertRaisesRegex(ContractError, "exceeds remaining"):
            run_loop(make_job(), make_archive(), order_selector(["A"]), 16.4)
        run = run_loop(make_job(), make_archive(), greedy, 16.5)
        self.assertEqual([r["site_id"] for r in run["rows"]], ["A"])
        self.assertEqual(run["stop_reason"], "no_affordable_candidate")
        with self.assertRaisesRegex(ContractError, "exceeds remaining"):
            run_loop(make_job(), make_archive(), order_selector(["A", "C"]), 20.0)

    def test_selector_declined_and_invalid_choices(self):
        run = run_loop(make_job(), make_archive(), lambda s, r: None, 100.0)
        self.assertEqual(run["stop_reason"], "selector_declined")
        for choice in ({"site_id": "Z"}, "A", {"site_id": "A", "x": math.nan}):
            with self.assertRaises(ContractError, msg=repr(choice)):
                run_loop(make_job(), make_archive(), lambda s, r, c=choice: c, 100.0)
        with self.assertRaisesRegex(ContractError, "already visited"):
            run_loop(make_job(), make_archive(), lambda s, r: {"site_id": "A"}, 100.0)

    def test_state_exposes_only_public_fields_and_paid_history(self):
        seen = []
        reference = {"complete": True, "source": "synthetic truth",
                     "doi_by_site": {"A": True, "B": True, "C": False}}

        def spy(state, rng):
            seen.append(copy.deepcopy(state))
            text = json.dumps(state)
            for leak in ("reference", "observations", "synthetic truth", "image",
                         "\"observed_at\"", "doi_by_site", "capture_s\"", "attempts\""):
                self.assertNotIn(leak, text)
            self.assertEqual(set(state), {"job_id", "candidates", "remaining_s", "current",
                                          "history", "cost"})
            for c in state["candidates"]:
                self.assertEqual(set(c), {"site_id", "wafer_id", "x_um", "y_um", "prior_p",
                                          "optical_observed_at", "recipe_id",
                                          "capture_bound_s", "inference_bound_s", "cost"})
            choice = order_selector(["A", "B", "C"])(state, rng)
            state["candidates"].clear()  # mutation must not affect the loop
            state["history"].append({"site_id": "C"})
            return choice

        archive = make_archive({"A": [ok(True, image_path="a.png", image_sha256="0" * 64)],
                                "B": [ok(False)]}, reference)
        run = run_loop(make_job(), archive, spy, 100.0)
        self.assertEqual([r["site_id"] for r in run["rows"]], ["A", "B", "C"])
        self.assertEqual(seen[0]["history"], [])
        self.assertIsNone(seen[0]["current"])
        self.assertEqual([h["site_id"] for h in seen[2]["history"]], ["A", "B"])
        self.assertEqual(seen[1]["current"], {"wafer_id": "W1", "x_um": 0.0, "y_um": 0.0})
        self.assertIs(seen[1]["history"][0]["reported_doi"], True)
        self.assertEqual(seen[1]["candidates"][0]["cost"]["move_s"], 3.0)

    def test_rng_is_seeded_policy_randomness(self):
        def random_pick(state, rng):
            return {"site_id": rng.choice(sorted(c["site_id"] for c in state["candidates"]))}
        a = run_loop(make_job(), make_archive(), random_pick, 100.0, seed=7)
        b = run_loop(make_job(), make_archive(), random_pick, 100.0, seed=7)
        self.assertEqual([r["site_id"] for r in a["rows"]], [r["site_id"] for r in b["rows"]])
        self.assertIsInstance(random.Random(7), type(random.Random()))

    def test_inputs_not_mutated_and_bad_args(self):
        job, archive = make_job(), make_archive()
        snapshot = copy.deepcopy((job, archive))
        run_loop(job, archive, order_selector(["A", "B", "C"]), 100.0)
        self.assertEqual((job, archive), snapshot)
        for budget in (-1, math.inf, True, "10"):
            with self.assertRaises(ContractError):
                run_loop(job, archive, order_selector(["A"]), budget)
        with self.assertRaises(ContractError):
            run_loop(job, archive, order_selector(["A"]), 10, seed=1.5)
        with self.assertRaises(ContractError):
            run_loop(job, archive, None, 10)
        with self.assertRaises(ContractError):
            run_loop(job, archive, order_selector(["A"]), 10, observer="x")


class ObserverAndImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a.png").write_bytes(b"image-a")
        self.digest = hashlib.sha256(b"image-a").hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def test_observer_called_only_after_admission_and_cannot_overwrite(self):
        events = []

        def selector(state, rng):
            events.append(("select", len(state["history"])))
            return order_selector(["A", "B"])(state, rng)

        def observer(attempt, cand):
            events.append(("observe", cand["site_id"]))
            self.assertNotIn("reported_doi", attempt)
            self.assertEqual(Path(attempt["image_path"]).read_bytes(), b"image-a")
            return {"reported_doi": False, "score": 0.25}

        archive = make_archive({"A": [ok(True, image_path="a.png", image_sha256=self.digest)],
                                "B": [ok(False)]})
        run = run_loop(make_job(), archive, selector, 30.0, observer=observer,
                       image_root=self.root)
        self.assertEqual(events, [("select", 0), ("observe", "A"), ("select", 1),
                                  ("select", 2)])
        attempt = run["rows"][0]["attempts"][0]
        self.assertEqual(attempt["image_status"], "verified")
        self.assertEqual(attempt["observer"], {"reported_doi": False, "score": 0.25})
        self.assertIs(run["rows"][0]["reported_doi"], True)
        self.assertGreaterEqual(run["observer_wall_s"], 0.0)
        self.assertGreaterEqual(run["decision_wall_s"], 0.0)

    def test_unselected_images_never_read(self):
        calls = []
        archive = make_archive({"A": [ok(True)],
                                "B": [ok(True, image_path="a.png", image_sha256=self.digest)]})
        run_loop(make_job(), archive, order_selector(["A"]), 100.0,
                 observer=lambda a, c: calls.append(c["site_id"]) or {},
                 image_root=self.root)
        self.assertEqual(calls, [])

    def test_hash_mismatch_blocks_observer(self):
        calls = []
        archive = make_archive({"A": [ok(True, image_path="a.png", image_sha256="f" * 64)]})
        run = run_loop(make_job(), archive, order_selector(["A"]), 100.0,
                       observer=lambda a, c: calls.append(1) or {}, image_root=self.root)
        attempt = run["rows"][0]["attempts"][0]
        self.assertEqual(attempt["image_status"], "hash_mismatch")
        self.assertEqual(attempt["actual_sha256"], self.digest)
        self.assertEqual(calls, [])
        self.assertIs(run["rows"][0]["reported_doi"], True)

    def test_unreadable_escape_and_observer_failures(self):
        archive = make_archive({
            "A": [ok(True, image_path="gone.png", image_sha256=self.digest)],
            "B": [ok(True, image_path="../outside.png")],
            "C": [ok(False, image_path="a.png")]})

        def boom(attempt, cand):
            raise RuntimeError("model crashed")
        run = run_loop(make_job(), archive, order_selector(["A", "B", "C"]), 100.0,
                       observer=boom, image_root=self.root)
        statuses = [r["attempts"][0]["image_status"] for r in run["rows"]]
        self.assertEqual(statuses, ["unreadable", "outside_image_root", "unverified_no_hash"])
        self.assertIn("model crashed", run["rows"][2]["attempts"][0]["observer_error"])
        self.assertIs(run["rows"][2]["reported_doi"], False)
        run = run_loop(make_job(), make_archive({"A": [ok(True, image_path="a.png")]}),
                       order_selector(["A"]), 100.0, observer=lambda a, c: [1],
                       image_root=self.root)
        self.assertIn("must return a dict", run["rows"][0]["attempts"][0]["observer_error"])


if __name__ == "__main__":
    unittest.main()

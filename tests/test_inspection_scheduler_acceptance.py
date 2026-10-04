"""Independent acceptance of paid observation, frozen audit and cost semantics."""
import json
import unittest

import numpy as np

from inspection_review import data, harness, model, policies


class SchedulerAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = data.load_config()
        cls.lot = data.generate_lot(200, "stationary", cls.config)
        cls.fitted = model.train_model([data.generate_lot(100, "stationary", cls.config)], cls.config)
        cls.view = policies.sanitize_public(cls.lot["public"], cls.fitted["mean"], cls.fitted["scale"])
        cls.probabilities = model.predict(cls.fitted, cls.view.features_imputed)

    def run_policy(self, name, mode="candidate_only", budget=120, oracle=None):
        calls = []
        private = self.lot["oracle"] if oracle is None else oracle

        def paid_review(i, attempt):
            calls.append((i, attempt))
            return data.review_observation(private, i, attempt)

        result = harness.run_selection(
            self.view, paid_review, policy_name=name, mode=mode, budget=budget,
            frozen_model=self.fitted, frozen_p=self.probabilities, model_api=model, config=self.config,
        )
        return result, calls

    def test_policy_input_has_no_truth_or_scenario_metadata(self):
        self.assertFalse(set(vars(self.view)) & {"seed", "scenario", "oracle", "doi", "kind", "electrical_effect"})
        self.assertNotIn("stationary", self.view.lot_id)
        self.assertTrue(all("stationary" not in s for s in self.view.site_ids))
        with self.assertRaises(ValueError):
            self.view.features_imputed[0, 0] = 99

    def test_all_policies_charge_only_selected_attempts_with_past_evidence(self):
        before = model.hash_model(self.fitted)
        for name in self.config["policies"]:
            with self.subTest(policy=name):
                result, calls = self.run_policy(name)
                expected = [(r["site_index"], a["attempt"]) for r in result["rows"] for a in r["attempts"]]
                self.assertEqual(calls, expected)
                self.assertLessEqual(result["spent"], 120 + 1e-9)
                self.assertEqual(len(set(i for i, a in calls if a == 0)), len(result["rows"]))
                past_labels = set()
                for row in result["rows"]:
                    self.assertTrue(row["original_candidate"])
                    self.assertFalse(set(row) & {"doi", "true_kind", "electrical_effect"})
                    self.assertTrue({e["site_id"] for e in row["evidence_refs"]} <= past_labels)
                    self.assertAlmostEqual(row["charged"], sum(row["cost"].values()))
                    self.assertLessEqual(row["charged"], row["reserved_cost"] + 1e-9)
                    if row["label"] is not None:
                        past_labels.add(row["site_id"])
                self.assertEqual(before, model.hash_model(self.fitted))

    def test_first_site_includes_loading_and_stage_base(self):
        result, _ = self.run_policy("learned")
        first = result["rows"][0]
        self.assertEqual(first["cost"]["load"], self.config["cost"]["wafer_load"])
        self.assertEqual(first["cost"]["stage"], self.config["cost"]["stage_base"])
        self.assertEqual(first["cost"]["dwell"], self.config["cost"]["dwell"])

    def test_identical_paid_evidence_has_identical_choices_despite_changed_oracle(self):
        # The runner receives the controlled observation callback, never oracle.
        # Changing latent truth while retaining those observations must affect only
        # posthoc evaluation, not any choice or acquisition score.
        a, _ = self.run_policy("falsify")
        original = self.lot["oracle"]
        changed = {k: v.copy() for k, v in original.items()}
        changed["doi"] = ~changed["doi"]
        changed["kind"][:] = "novel"
        b, _ = self.run_policy("falsify", oracle=changed)
        for x, y in zip(a["rows"], b["rows"]):
            self.assertEqual(x["site_index"], y["site_index"])
            self.assertEqual(x["score"], y["score"])
            self.assertEqual(x["evidence_refs"], y["evidence_refs"])
        ma = harness.evaluate_run(a, original, self.view.candidate, self.probabilities, self.config)
        mb = harness.evaluate_run(b, changed, self.view.candidate, self.probabilities, self.config)
        self.assertNotEqual(ma["all_doi"], mb["all_doi"])

    def test_failures_are_paid_and_cannot_be_predicted_for_admission(self):
        calls = []

        def failed(i, attempt):
            calls.append((i, attempt))
            return {"status": "failure", "reported_doi": None, "reported_kind": None, "quality": .1}

        minimum_reserve = sum(self.config["cost"][k] for k in ("wafer_load", "stage_base", "dwell", "retry_dwell"))
        common = dict(policy_name="learned", mode="candidate_only", frozen_model=self.fitted,
                      frozen_p=self.probabilities, model_api=model, config=self.config)
        empty = harness.run_selection(self.view, failed, budget=minimum_reserve - .01, **common)
        self.assertFalse(calls)
        self.assertFalse(empty["rows"])
        result = harness.run_selection(self.view, failed, budget=minimum_reserve, **common)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(result["rows"][0]["label"])
        self.assertAlmostEqual(result["spent"], minimum_reserve)
        self.assertEqual(result["rows"][0]["cost"]["retry"], self.config["cost"]["retry_dwell"])


if __name__ == "__main__":
    unittest.main()

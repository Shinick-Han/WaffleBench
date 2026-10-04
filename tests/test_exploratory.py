"""Non-scientific regression checks for the supplementary policy boundary."""
import math
import unittest

from falsify_lab.exploratory import LoadSimulator, kernel_candidates
from falsify_lab.protocol import Point
from falsify_lab.simulator import render_netlist


class ExploratoryTests(unittest.TestCase):
    def test_missing_and_invalid_evidence_is_refused(self):
        p = Point("TT", 2.5, 27)
        self.assertEqual(kernel_candidates([], []), [])
        for evidence in ([], [(p, math.nan)], [(p, -0.1)]):
            with self.assertRaises(ValueError):
                kernel_candidates([p], evidence)

    def test_evidence_updates_do_not_contaminate_other_calls(self):
        observed = Point("FF", 1.2, 125)
        candidates = [Point("TT", 3.3, -40), Point("FF", 1.5, 125)]
        low = kernel_candidates(candidates, [(observed, 0.01)])
        high = kernel_candidates(candidates, [(observed, 0.4)])
        self.assertEqual(low, kernel_candidates(candidates, [(observed, 0.01)]))
        self.assertNotEqual(low, high)
        self.assertEqual(high[0]["point_id"], candidates[1].id)
        self.assertTrue(all(math.isfinite(r["score"]) for r in high))

    def test_candidate_order_and_duplicate_input_do_not_change_ranking(self):
        a, b, o = Point("FF", 1.2, 125), Point("SS", 3.3, -40), Point("TT", 2.5, 27)
        self.assertEqual(kernel_candidates([b, a, b], [(o, 0.03)]),
                         kernel_candidates([a, b], [(o, 0.03)]))

    def test_load_change_is_explicit_and_primary_netlist_stays_identical(self):
        p = Point("TT", 2.5, 27)
        for setting in ("basic", "half", "tight"):
            self.assertEqual(LoadSimulator(20).netlist(p, setting), render_netlist(p, setting))
            self.assertEqual(LoadSimulator(8).netlist(p, setting).replace("\ncl out 0 8f\n", "\ncl out 0 20f\n"), render_netlist(p, setting))
        with self.assertRaises(ValueError):
            LoadSimulator(99)


if __name__ == "__main__":
    unittest.main()

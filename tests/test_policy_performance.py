"""The shared-geometry policy code must equal the base scalar implementation exactly.

Baseline: scripts/profile_policy_performance.py (verbatim copy of base 1eb3606).
Synthetic fixtures here are NON-SCIENTIFIC; the replay test reads the finished
primary ledger read-only when it is present on this machine.
"""

from __future__ import annotations

import hashlib
import os
import sys
import unittest
from pathlib import Path

from test_core_support import ROOT

sys.path.insert(0, str(ROOT / "scripts"))

import profile_policy_performance as perf  # noqa: E402

from falsify_lab.policies import LABELS, build_decision, rank  # noqa: E402
from falsify_lab.protocol import Point, load_protocol  # noqa: E402

PROTO = load_protocol()
POOL = list(PROTO.search_pool)
TRAIN = list(PROTO.training)
ORDER = PROTO.replicate(1001).random_order
_PRIMARY_NAME = Path("runs") / "primary-20261004-0824"
PRIMARY = Path(os.environ["FALSIFY_PRIMARY_ROOT"]) if "FALSIFY_PRIMARY_ROOT" in os.environ else next(
    (p for p in (ROOT / _PRIMARY_NAME, ROOT.parents[1] / "falsify-lab" / _PRIMARY_NAME) if p.is_dir()), ROOT / _PRIMARY_NAME
)


def rows(r):
    return [(s.point, s.score, s.idw_pred, s.dmin) for s in r]


def err(seed: int, p: Point) -> float:
    """Deterministic pseudo-error in [0, 0.3) (NON-SCIENTIFIC)."""
    return int(hashlib.sha256(f"{seed}|{p.id}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF * 0.3


def outcome(fn, *args):
    try:
        return ("ok", fn(*args))
    except Exception as exc:  # noqa: BLE001 - comparing error behavior
        return ("error", type(exc), str(exc))


class EqualityHelpers(unittest.TestCase):
    def assert_rank_equal(self, policy, cands, evidence, queried, order=()):
        base = outcome(lambda: rows(perf.base_rank(policy, PROTO, cands, evidence, queried, order)))
        new = outcome(lambda: rows(rank(policy, PROTO, cands, evidence, queried, order)))
        self.assertEqual(new, base, policy)

    def assert_decision_equal(self, policy, cands, evidence, queried, order, previous, ids):
        args = (policy, PROTO, cands, evidence, queried, order, previous, ids)
        self.assertEqual(outcome(build_decision, *args), outcome(perf.base_build_decision, *args), policy)


class TieTests(EqualityHelpers):
    def test_all_zero_errors_tie_lexically_in_every_policy(self):
        evidence = [(p, 0.0) for p in TRAIN]
        for pol in LABELS:
            order = ORDER if pol == "random" else ()
            self.assert_rank_equal(pol, POOL, evidence, TRAIN, order)
            self.assert_decision_equal(pol, POOL, evidence, TRAIN, ORDER, evidence, [])
        r = rank("idw_without_distance", PROTO, list(reversed(POOL)), evidence, TRAIN)
        self.assertEqual([s.point for s in r], sorted(POOL))

    def test_symmetric_geometry_ties_and_duplicates(self):
        # One queried point in the middle of the VDD/temperature span: many candidates
        # share exactly equal min distances, and equal errors give equal IDW sums.
        mid = Point("TT", 2.2, 27.0)
        cands = [p for p in POOL if p.corner in ("FF", "SS", "FS", "SF")]
        dup = cands + list(reversed(cands)) + [Point("FF", 1.2, -40)]  # int temp equals -40.0
        evidence = [(mid, 0.1), (mid, 0.1)]  # duplicate evidence location
        for pol in LABELS:
            order = ORDER if pol == "random" else ()
            self.assert_rank_equal(pol, dup, evidence, [mid], order)
            self.assert_rank_equal(pol, dup, evidence, [mid, mid], order)
            self.assert_decision_equal(pol, dup, evidence, [mid], ORDER, evidence[:1], ["a", "b"])
        sf = rank("space_filling", PROTO, dup, evidence, [mid])
        self.assertEqual(len(sf), len(set(dup)))
        self.assertGreater(sum(s.score == sf[0].score for s in sf), 1)

    def test_coincident_candidate_and_evidence(self):
        p = POOL[0]
        evidence = [(p, 0.2), (POOL[1], 0.05)]
        for pol in LABELS:
            order = ORDER if pol == "random" else ()
            self.assert_rank_equal(pol, POOL[:6], evidence, [p, POOL[1]], order)


class ErrorAndEdgeTests(EqualityHelpers):
    def test_error_behavior_matches_baseline(self):
        ev = [(TRAIN[0], 0.1)]
        cases = [
            ("adaptive_idw_plus_distance", POOL, [], TRAIN, ()),  # no evidence: None + float
            ("idw_without_distance", POOL, [], TRAIN, ()),  # None scores cannot sort
            ("space_filling", POOL, [], TRAIN, ()),  # fine: outcome-free
            ("space_filling", POOL, ev, [], ()),  # no queried locations
            ("random", POOL, ev, TRAIN, ORDER[:5]),  # incomplete frozen order
            ("bogus", POOL, ev, TRAIN, ()),
            ("bogus", [], ev, TRAIN, ()),  # no candidates: nothing to reject
            ("adaptive_idw_plus_distance", [], ev, TRAIN, ()),
            ("space_filling", [Point("XX", 1.2, 0.0)], ev, TRAIN, ()),  # off-grid corner
        ]
        for pol, cands, evidence, queried, order in cases:
            with self.subTest(pol=pol, n=len(cands), ev=len(evidence), q=len(queried)):
                self.assert_rank_equal(pol, cands, evidence, queried, order)
                self.assert_decision_equal(pol, cands, evidence, queried, order, evidence, [])
                self.assert_decision_equal(pol, cands, evidence, queried, ORDER, [], [])

    def test_random_and_space_filling_keep_score_metadata(self):
        evidence = [(p, err(7, p)) for p in TRAIN]
        for pol in ("random", "space_filling"):
            r = rank(pol, PROTO, POOL, evidence, TRAIN, ORDER)
            base = perf.base_rank(pol, PROTO, POOL, evidence, TRAIN, ORDER)
            self.assertEqual(rows(r), rows(base))
            self.assertTrue(all(s.idw_pred is not None and s.dmin > 0 for s in r))


class SuccessiveEvidenceTests(EqualityHelpers):
    """No state may carry over between calls, decisions or runs."""

    def simulate(self, policy: str, seed: int):
        order = PROTO.replicate(seed).random_order
        queried = list(TRAIN) + list(PROTO.replicate(seed).initial)
        evidence = [(p, err(seed, p)) for p in queried]
        previous = evidence[:-1]
        decisions = []
        for step in range(12):
            cands = [p for p in POOL if p not in set(queried)]
            args = (policy, PROTO, cands, list(evidence), sorted(set(queried)), order, list(previous), [str(step)])
            d = build_decision(*args)
            self.assertEqual(d, perf.base_build_decision(*args))
            decisions.append(d)
            pick = Point.parse(d["selected_point_id"])
            queried.append(pick)
            previous = list(evidence)
            if step % 4 != 3:  # every fourth query "fails": queried, but no new evidence
                evidence.append((pick, err(seed, pick)))
        return decisions

    def test_interleaved_runs_equal_fresh_runs(self):
        fresh = {(pol, seed): self.simulate(pol, seed) for pol in LABELS for seed in (1001, 1002)}
        # Re-run in a different interleaving and mutate returned objects in between.
        for seed in (1002, 1001):
            for pol in reversed(list(LABELS)):
                again = self.simulate(pol, seed)
                self.assertEqual(again, fresh[(pol, seed)])
                for d in again:
                    d["candidates"][0]["pvt"]["vdd"] = -1.0
                    d["candidates"].clear()
                    d["rank_after"].clear()
        for (pol, seed), ds in fresh.items():
            self.assertEqual(self.simulate(pol, seed), ds)

    def test_same_inputs_after_other_calls_are_identical(self):
        evidence = [(p, err(3, p)) for p in TRAIN]
        first = rows(rank("adaptive_idw_plus_distance", PROTO, POOL, evidence, TRAIN))
        rank("adaptive_idw_plus_distance", PROTO, POOL, [(p, 1.0 - e) for p, e in evidence], TRAIN + POOL[:5])
        build_decision("idw_without_distance", PROTO, POOL[10:], evidence[:2], TRAIN, ORDER, evidence[:1], [])
        self.assertEqual(rows(rank("adaptive_idw_plus_distance", PROTO, POOL, evidence, TRAIN)), first)


@unittest.skipUnless((PRIMARY / "ledger" / "events.jsonl").is_file(), f"primary ledger not present at {PRIMARY}")
class PrimaryReplayTests(unittest.TestCase):
    def test_all_480_published_decisions_match_exactly(self):
        decisions = perf.load_decisions(PRIMARY, PROTO)
        self.assertEqual(len(decisions), 480)
        for d in decisions:
            args = d["args"]
            new = build_decision(*args)
            self.assertEqual(new, perf.base_build_decision(*args), (d["run_id"], d["sequence"]))
            self.assertEqual({k: new[k] for k in perf.PUBLISHED_FIELDS}, d["published"], (d["run_id"], d["sequence"]))


if __name__ == "__main__":
    unittest.main()

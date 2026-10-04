"""Static checks for the shipped web/ app (no browser required)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BASE_COMMIT = "a726052c45f6b4210e52567c52b5690883931cc7"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class FrontendStaticTests(unittest.TestCase):
    def setUp(self):
        self.html = read(WEB / "index.html")
        self.js = read(WEB / "app.en.js")
        self.css = read(WEB / "app.css")

    def test_index_loads_only_local_assets(self):
        self.assertRegex(self.html, r'href="\./app\.css(?:\?v=[a-f0-9]+)?"')
        self.assertRegex(self.html, r'src="\./app\.en\.js(?:\?v=[a-f0-9]+)?"')
        self.assertNotRegex(self.html, r"<script(?![^>]*\bsrc=)[^>]*>", "inline scripts break the CSP")
        for text in (self.html, self.js, self.css):
            self.assertNotRegex(text, r"https?://(?!127\.0\.0\.1)", "no external resources")

    def test_no_mockup_values_or_generators_in_real_app(self):
        for marker in ("demo_sim_0", "positiveResults", "negativeResults", "bootstrapInterval",
                       "Math.random", "MOCK-DATA", "149.3", "203.9", "heatError", "scenarioSelect"):
            self.assertNotIn(marker, self.js + self.html, marker)

    def test_data_sources_match_contract(self):
        self.assertIn("'./data/snapshot.json'", self.js)
        self.assertIn("'./api/snapshot'", self.js)
        self.assertIn("'./api/jobs'", self.js)
        self.assertIn("'./api/jobs/cancel'", self.js)
        self.assertIn("'./api/job'", self.js)
        # Export writes the fetched raw text, the same bytes the view parsed.
        self.assertIn("new Blob([state.raw]", self.js)

    def test_ids_used_by_script_exist(self):
        ids = set(re.findall(r"\$\('([A-Za-z][\w-]*)'\)", self.js))
        dynamic = {"rawMeas", "rawRecord", "rawCand", "jobSummary", "jobStderr", "cancelJob", "traceAll"}
        missing = sorted(i for i in ids - dynamic if f'id="{i}"' not in self.html)
        self.assertEqual(missing, [])

    def test_css_preserves_frozen_presentation_bytes(self):
        protected = json.loads(read(ROOT / "evidence" / "inspection-research" / "protected-before-v1.json"))
        self.assertEqual(hashlib.sha256((WEB / "app.css").read_bytes()).hexdigest(),
                         protected["protected_sha256"]["web/app.css"])

    def test_mockup_unchanged_from_base(self):
        try:
            out = subprocess.run(["git", "diff", "--quiet", BASE_COMMIT, "--", "mockup/index.html"],
                                 cwd=ROOT, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            self.skipTest("git unavailable")
        if out.returncode not in (0, 1):
            self.skipTest("base commit unavailable")
        self.assertEqual(out.returncode, 0, "mockup/index.html must stay identical to the base")

    def test_fixture_is_labelled_and_not_default(self):
        fixture = json.loads(read(WEB / "tests" / "fixtures" / "snapshot.fixture.json"))
        self.assertEqual(fixture["data_mode"], "fixture")
        self.assertTrue(any("FIXTURE" in s for s in fixture["limitations"]))
        default = json.loads(read(WEB / "data" / "snapshot.json"))
        self.assertEqual(default["data_mode"], "real")
        self.assertNotIn("tests/fixtures", self.js + self.html)

    def test_trace_gating_never_compares_global_and_local_counters(self):
        # Trace sequence is the global ledger counter; decision sequence is run-local.
        self.assertNotIn("t.sequence<=cut", self.js)
        self.assertIn("m.traceStage[i]<=state.stage", self.js)
        fixture = json.loads(read(WEB / "tests" / "fixtures" / "snapshot.fixture.json"))
        dec = [d["sequence"] for d in fixture["decisions"]]
        trace = [t["sequence"] for t in fixture["trace"]]
        self.assertEqual(dec, [1, 2])
        self.assertGreater(min(trace), max(dec), "fixture must use non-matching global/local numbering")
        self.assertIn("decision_sequence", json.dumps(fixture["trace"]))

    def test_benchmark_mean_is_exported_not_recomputed(self):
        self.assertIn("p.mean_cumulative_clear.map(num)", self.js)
        self.assertIn("p.complete_runs", self.js)
        self.assertNotIn("reduce((s,x)=>s+x,0)/vals.length", self.js)
        fixture = json.loads(read(WEB / "tests" / "fixtures" / "snapshot.fixture.json"))
        pols = {p["id"]: p for p in fixture["benchmark"]["policies"]}
        a = pols["adaptive_idw_plus_distance"]
        complete = [r["cumulative_clear"] for r in a["runs"] if r["status"] == "complete"]
        everything = [r["cumulative_clear"] for r in a["runs"]]
        self.assertEqual(a["complete_runs"], len(complete))
        self.assertEqual(a["mean_cumulative_clear"], [sum(v) / len(v) for v in zip(*complete)])
        # The artificially high incomplete run would change the mean if it were included.
        self.assertNotEqual(a["mean_cumulative_clear"], [sum(v) / len(v) for v in zip(*everything)])
        self.assertIsNone(pols["space_filling"]["mean_cumulative_clear"])
        self.assertEqual(pols["space_filling"]["complete_runs"], 0)
        self.assertIsNone(fixture["benchmark"]["primary"]["ci95"])
        self.assertIn("pr.ci95", self.js)
        self.assertNotRegex(self.js, r"\bfunction\s+bootstrap\b|\b(?:const|let|var)\s+bootstrap\s*=")

    def test_post_result_update_optional(self):
        fixture = json.loads(read(WEB / "tests" / "fixtures" / "snapshot.fixture.json"))
        d1, d2 = fixture["decisions"]
        self.assertEqual(d1["post_result_update"]["observed_result_id"], d1["observed_result_id"])
        self.assertNotIn("post_result_update", d2, "absence must stay covered")
        # Rendered only for a decision whose result has been revealed (the previous stage).
        self.assertIn("prev=state.stage>0?m.decisions[state.stage-1]:null", self.js)
        self.assertIn("prev.post_result_update", self.js)
        self.assertNotIn("cur.post_result_update", self.js)


if __name__ == "__main__":
    unittest.main()

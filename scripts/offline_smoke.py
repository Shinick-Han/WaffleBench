"""Offline deterministic smoke: no LLM, no network.

1. plumbing_smoke (labelled, not science)
2. ngspice calibration point + one off-nominal point
3. surrogate evaluation from stored results
4. next-decision handoff

Writes runs/smoke/offline_smoke.json. Uses a separate runs dir so the smoke
does not consume the research budget.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ["FALSIFY_RUNS_DIR"] = str(ROOT / "runs" / "smoke")
sys.path.insert(0, str(ROOT))

from falsify_lab import experiment as exp  # noqa: E402


def main() -> int:
    plumbing = exp.plumbing_smoke(10)
    assert plumbing["sum_of_squares"] == plumbing["closed_form"] == 385

    cal = exp.simulate_pvt_point(*exp.CALIBRATION_POINT)
    off = exp.simulate_pvt_point("SS", 1.2, 125.0)
    again = exp.simulate_pvt_point("SS", 1.2, 125.0)
    assert again["cache_hit"] and again["tpd_s"] == off["tpd_s"]

    ev_cal = exp.evaluate_surrogate(cal["result_id"])
    ev_off = exp.evaluate_surrogate(off["result_id"])
    assert abs(ev_cal["relative_error"]) < 1e-12, "calibration point must fit exactly"

    try:
        exp.evaluate_surrogate("sim_000000000000")
        raise AssertionError("unknown result_id must be rejected")
    except exp.ExperimentError:
        pass
    try:
        exp.simulate_pvt_point("XX", 9.9, 500)
        raise AssertionError("out-of-domain input must be rejected")
    except exp.ExperimentError:
        pass

    decision = exp.propose_next_point()
    assert decision == exp.propose_next_point(), "decision must be deterministic"

    out = {
        "plumbing_smoke": plumbing,
        "simulations": [
            {k: r[k] for k in ("result_id", "pvt", "tphl_s", "tplh_s", "tpd_s")}
            | {"simulator": r["provenance"]["simulator"], "netlist_sha256": r["provenance"]["netlist_sha256"]}
            for r in (cal, off)
        ],
        "evaluations": [
            {k: e[k] for k in ("result_id", "pvt", "relative_error", "fails")} for e in (ev_cal, ev_off)
        ],
        "next_decision": decision,
        "note": "Smoke of the pipeline on 2 points; not a research result.",
    }
    path = exp.RUNS_DIR / "offline_smoke.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))
    print(f"OFFLINE_SMOKE_PASS -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Statistics and JSON/Markdown reports computed from stored campaign metrics only.

Primary: falsify vs learned, candidate_only, primary budget, true_doi_confirmed,
paired over independent lots, scenario-stratified bootstrap (equal lot weight).
Everything else is exploratory. Ratios with a zero denominator are null.
Model classification validation is reported separately from scheduler capture.
"""

from __future__ import annotations

from typing import Any

import numpy as np

DEFAULT_MATCHED_CAPTURE_TARGET = 5
SCHEDULER_METRICS = ("true_doi_confirmed", "review_reported_positives", "reported_false_positives",
                     "reviewed_true_doi_missed", "unresolved_sites", "discovered_frozen_false_negatives",
                     "discovered_frozen_confident_false_negatives", "confirmed_true_doi_outside_candidates",
                     "confirmed_electrical_potential", "attempts", "failures", "missing", "retries", "spent")


def _mean(xs: list[float]) -> float | None:
    return float(np.mean(xs)) if xs else None


def stratified_paired_bootstrap(diffs: list[float], strata: list[str], seed: int, replicates: int) -> list[float] | None:
    """Percentile 95% CI of the mean paired difference, resampling lots within each stratum."""
    if not diffs:
        return None
    d = np.asarray(diffs, float)
    groups = [np.flatnonzero(np.asarray(strata) == s) for s in sorted(set(strata))]
    rng = np.random.Generator(np.random.PCG64(seed))
    total = np.zeros(replicates)
    for g in groups:
        draw = g[rng.integers(0, g.size, size=(replicates, g.size))]
        total += d[draw].sum(axis=1)
    means = total / d.size
    return [float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


def _index(runs: list[dict]) -> dict[tuple, dict]:
    return {(r["lot_id"], r["policy"], r["mode"], int(r["budget"])): r for r in runs if r.get("status") == "complete"}


def paired_comparison(runs: list[dict], a: str, b: str, mode: str, budget: int, metric: str,
                      seed: int, replicates: int, *, target: float | None = None, primary: bool = False) -> dict:
    idx = _index(runs)
    lots = sorted({r["lot_id"] for r in runs})
    da, db, strata = [], [], []
    for lot in lots:
        ra, rb = idx.get((lot, a, mode, budget)), idx.get((lot, b, mode, budget))
        if ra is None or rb is None:
            continue
        da.append(float(ra["metrics"][metric]))
        db.append(float(rb["metrics"][metric]))
        strata.append(ra["scenario"])
    ma, mb = _mean(da), _mean(db)
    diff = [x - y for x, y in zip(da, db)]
    ci = stratified_paired_bootstrap(diff, strata, seed, replicates)
    rel = None if ma is None or not mb else ma / mb - 1.0
    out = {"policy": a, "comparator": b, "mode": mode, "budget": budget, "metric": metric,
           "complete_pairs": len(diff), "mean_policy": ma, "mean_comparator": mb,
           "mean_difference": _mean(diff), "ci95_difference": ci, "relative_gain": rel,
           "role": "primary" if primary else "exploratory"}
    if primary:
        ok = rel is not None and target is not None and rel >= target and ci is not None and ci[0] > 0
        out["success"] = bool(ok)
        out["reason"] = ("relative gain >= target and CI lower bound > 0" if ok else
                         "no complete pairs" if not diff else
                         "comparator mean is zero; relative gain null" if rel is None else
                         "target not met")
    return out


def matched_cost_savings(runs: list[dict], a: str, b: str, mode: str, budget: int, target: int,
                         saving_target: float) -> dict:
    """Cost to reach ``target`` true confirmed DOI in each max-budget ledger; savings only
    on lots where BOTH policies reached it, with attainment fractions reported."""
    idx = _index(runs)
    lots = sorted({r["lot_id"] for r in runs})
    both, only_a, only_b, neither, pairs = 0, 0, 0, 0, []
    total = 0
    for lot in lots:
        ra, rb = idx.get((lot, a, mode, budget)), idx.get((lot, b, mode, budget))
        if ra is None or rb is None:
            continue
        total += 1
        sa = ra["metrics"]["true_confirmation_cumulative_spend"]
        sb = rb["metrics"]["true_confirmation_cumulative_spend"]
        ca = sa[target - 1] if len(sa) >= target else None
        cb = sb[target - 1] if len(sb) >= target else None
        if ca is not None and cb is not None:
            both += 1
            pairs.append((ca, cb))
        elif ca is not None:
            only_a += 1
        elif cb is not None:
            only_b += 1
        else:
            neither += 1
    mean_a = _mean([p[0] for p in pairs])
    mean_b = _mean([p[1] for p in pairs])
    saving = None if not pairs or not mean_b else 1.0 - mean_a / mean_b
    per_lot = [1.0 - x / y for x, y in pairs if y > 0]
    return {"policy": a, "comparator": b, "mode": mode, "budget_ledger": budget, "target_true_doi": target,
            "lots": total, "both_attained": both, "only_policy_attained": only_a,
            "only_comparator_attained": only_b, "neither_attained": neither,
            "policy_attainment_fraction": None if not total else (both + only_a) / total,
            "comparator_attainment_fraction": None if not total else (both + only_b) / total,
            "both_attainment_fraction": None if not total else both / total,
            "mean_cost_policy_common": mean_a, "mean_cost_comparator_common": mean_b,
            "saving_of_means_common": saving, "mean_paired_saving_common": _mean(per_lot),
            "saving_target": saving_target,
            "meets_saving_target": None if saving is None else bool(saving >= saving_target),
            "note": "supplementary; restricted to lots where both reached the target; see attainment fractions"}


def summarize(runs: list[dict], key_metrics: tuple = SCHEDULER_METRICS) -> list[dict]:
    """Mean per policy/mode/budget pooled (equal lot weight) and per scenario."""
    groups: dict[tuple, list[dict]] = {}
    for r in runs:
        if r.get("status") != "complete":
            continue
        groups.setdefault((r["policy"], r["mode"], int(r["budget"]), "pooled"), []).append(r)
        groups.setdefault((r["policy"], r["mode"], int(r["budget"]), r["scenario"]), []).append(r)
    out = []
    for (p, m, b, s), rs in sorted(groups.items()):
        row = {"policy": p, "mode": m, "budget": b, "scenario": s, "lots": len(rs)}
        for k in key_metrics:
            row[k] = _mean([float(r["metrics"][k]) for r in rs])
        for k in ("candidate_capture", "allsite_recall", "candidate_detection_ceiling"):
            vals = [r["metrics"][k] for r in rs if r["metrics"][k] is not None]
            row[k] = _mean(vals)
            row[k + "_lots_defined"] = len(vals)
        for k in ("load", "stage", "dwell", "outside_rescan", "retry"):
            row["cost_" + k] = _mean([r["metrics"]["cost_totals"][k] for r in rs])
        out.append(row)
    return out


def expected_run_keys(config: dict) -> set[tuple]:
    return {(sc, int(seed), p, m, int(b))
            for sc, seeds in config["splits"]["test_seeds_by_scenario"].items() for seed in seeds
            for p in config["policies"] for m in config["modes"] for b in config["budgets"]}


def completeness(metrics: dict, config: dict, status: dict | None) -> dict:
    """Primary success is only allowed for a completed primary campaign with every frozen run."""
    runs = metrics.get("runs", [])
    keys = [(r.get("scenario"), int(r.get("seed", -1)), r.get("policy"), r.get("mode"), int(r.get("budget", -1)))
            for r in runs if r.get("status") == "complete"]
    expected = expected_run_keys(config)
    n_lots = sum(len(v) for v in config["splits"]["test_seeds_by_scenario"].values())
    reasons = []
    if status is None or status.get("state") != "completed":
        reasons.append(f"campaign state is {None if status is None else status.get('state')}, not completed")
    if metrics.get("kind") != "primary_campaign":
        reasons.append(f"kind is {metrics.get('kind')}, not primary_campaign")
    if len(keys) != len(set(keys)) or len(keys) != len(runs):
        reasons.append("duplicate or incomplete run records")
    missing, extra = expected - set(keys), set(keys) - expected
    if missing:
        reasons.append(f"{len(missing)} expected runs missing")
    if extra:
        reasons.append(f"{len(extra)} unexpected runs")
    return {"expected_runs": len(expected), "complete_runs": len(keys), "missing_runs": len(missing),
            "unexpected_runs": len(extra), "expected_primary_pairs": n_lots, "eligible": not reasons,
            "reasons": reasons}


def build_report(metrics: dict, config: dict, status: dict | None, freeze: dict | None,
                 model_validation: dict | None) -> dict:
    st = config["statistics"]
    runs = metrics.get("runs", [])
    seed, reps = int(st["bootstrap_seed"]), int(st["bootstrap_replicates"])
    pb, pmode, pmetric = int(config["primary_budget"]), st["primary_mode"], st["primary_metric"]
    target = int(st.get("matched_capture_target", DEFAULT_MATCHED_CAPTURE_TARGET))
    primary = paired_comparison(runs, "falsify", st["primary_comparator"], pmode, pb, pmetric, seed, reps,
                                target=float(st["success_relative_gain_target"]), primary=True)
    gate = completeness(metrics, config, status)
    if gate["eligible"] and primary["complete_pairs"] != gate["expected_primary_pairs"]:
        gate["eligible"] = False
        gate["reasons"].append(f"{primary['complete_pairs']} primary pairs, expected {gate['expected_primary_pairs']}")
    primary["conclusion"] = ("success" if primary["success"] else "not met") if gate["eligible"] else "inconclusive"
    if not gate["eligible"]:
        primary["success"] = False
        primary["reason"] = "inconclusive: " + "; ".join(gate["reasons"])
    exploratory = []
    for comp in ("uncertainty_diversity", "learned", "recipe", "random"):
        for mode in config["modes"]:
            for b in config["budgets"]:
                if comp == st["primary_comparator"] and mode == pmode and int(b) == pb:
                    continue
                exploratory.append(paired_comparison(runs, "falsify", comp, mode, int(b), pmetric, seed, reps))
    max_b = int(max(config["budgets"]))
    savings = [matched_cost_savings(runs, "falsify", comp, mode, max_b, target, float(st["cost_reduction_target"]))
               for comp in (st["primary_comparator"], "uncertainty_diversity") for mode in config["modes"]]
    return {
        "schema_version": 1, "kind": metrics.get("kind"), "data_mode": config.get("data_mode"),
        "study_id": config.get("study_id"), "campaign_status": None if status is None else status.get("state"),
        "freeze_sha256": None if freeze is None else freeze.get("receipt_sha256"),
        "runs_total": len(runs), "runs_complete": sum(r.get("status") == "complete" for r in runs),
        "completeness": gate, "primary": primary, "exploratory_comparisons": exploratory, "matched_cost_savings": savings,
        "summary": summarize(runs),
        "model_classification_validation": model_validation,
        "limitations": [
            "Authored synthetic tabular simulation; no SEM images, tool logs or fab data.",
            "Costs are synthetic equipment cost units, not seconds, currency or throughput.",
            "Only the primary comparison is confirmatory; all others are exploratory without multiplicity control.",
            "Scenario mix is an equal-weight stress test, not a field frequency.",
            "confirmed_electrical_potential is a posthoc simulated flag, not measured yield.",
            "Outside-candidate review positives are not an improved initial optical candidate set.",
            "Model classification validation is separate from scheduler confirmed capture.",
        ],
    }


def _f(x: Any, nd: int = 2) -> str:
    if x is None:
        return "null"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def render_markdown(rep: dict) -> str:
    p = rep["primary"]
    lines = [f"# Inspection review selection report ({rep.get('kind')})", "",
             f"Campaign status: {rep['campaign_status']}; runs complete {rep['runs_complete']}/{rep['runs_total']}.",
             f"Freeze receipt: `{rep['freeze_sha256']}`.", "",
             "## Primary (confirmatory)", "",
             f"falsify vs {p['comparator']}, {p['mode']}, budget {p['budget']}, metric {p['metric']}: "
             f"pairs {p['complete_pairs']}, means {_f(p['mean_policy'])} vs {_f(p['mean_comparator'])}, "
             f"difference {_f(p['mean_difference'])}, 95% paired lot bootstrap CI {p['ci95_difference']}, "
             f"relative gain {_f(p['relative_gain'], 3)}, conclusion {p['conclusion']}, success {p['success']} ({p['reason']}).", "",
             "## Exploratory paired comparisons (falsify minus comparator)", "",
             "| comparator | mode | budget | pairs | mean diff | CI95 | relative gain |", "|---|---|---|---|---|---|---|"]
    for c in rep["exploratory_comparisons"]:
        lines.append(f"| {c['comparator']} | {c['mode']} | {c['budget']} | {c['complete_pairs']} | "
                     f"{_f(c['mean_difference'])} | {c['ci95_difference']} | {_f(c['relative_gain'], 3)} |")
    lines += ["", "## Matched-target cost (supplementary)", "",
              "| comparator | mode | target | both | falsify only | comparator only | neither | saving of means | paired mean saving |",
              "|---|---|---|---|---|---|---|---|---|"]
    for s in rep["matched_cost_savings"]:
        lines.append(f"| {s['comparator']} | {s['mode']} | {s['target_true_doi']} | {s['both_attained']} | "
                     f"{s['only_policy_attained']} | {s['only_comparator_attained']} | {s['neither_attained']} | "
                     f"{_f(s['saving_of_means_common'], 3)} | {_f(s['mean_paired_saving_common'], 3)} |")
    lines += ["", "## All conditions (pooled = equal lot weight)", "",
              "| policy | mode | budget | scenario | lots | true DOI confirmed | reported FP | frozen FN found | "
              "candidate capture | all-site recall | candidate ceiling | spent | retry cost | outside cost |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["summary"]:
        lines.append(f"| {r['policy']} | {r['mode']} | {r['budget']} | {r['scenario']} | {r['lots']} | "
                     f"{_f(r['true_doi_confirmed'])} | {_f(r['reported_false_positives'])} | "
                     f"{_f(r['discovered_frozen_false_negatives'])} | {_f(r['candidate_capture'], 3)} | "
                     f"{_f(r['allsite_recall'], 3)} | {_f(r['candidate_detection_ceiling'], 3)} | {_f(r['spent'])} | "
                     f"{_f(r['cost_retry'])} | {_f(r['cost_outside_rescan'])} |")
    lines += ["", "## Model classification validation (separate from scheduler capture)", "",
              "```", str(rep.get("model_classification_validation")), "```", "", "## Limitations", ""]
    lines += [f"- {x}" for x in rep["limitations"]]
    return "\n".join(lines) + "\n"

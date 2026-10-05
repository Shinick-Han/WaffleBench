"""Post-loop evaluation of one routing run (ROUTING_POC_CONTRACT.md, experiment scope).

``evaluate(job, archive, run)`` is called only after ``replay.run_loop`` has finished, so
reference truth never reaches a policy. It keeps four quantities separate:

- detector reports on selected sites (``ok`` positives/negatives, and unknown finals);
- confirmed reference DOI (paid positive report AND independent reference positive) and
  false confirmations (positive report, reference negative);
- total reference DOI over the candidate space, escapes and candidate recall;
- resource seconds (modeled/recorded replay charges) versus decision/observer wall time.

An unknown/failed/missing final outcome is never counted as negative. Reported labels are
never promoted to reference truth. Without a complete independent reference covering every
candidate, total/escapes/recall are ``None`` (unidentifiable), not zero. Recall is
candidate-space recall only, never wafer-wide recall. Historical partial logs give no
counterfactual support for actions that were not taken. ``commercial_validated`` is
always ``False``.
"""

from __future__ import annotations

from typing import Any

RESOURCE_SECONDS_NOTE = ("replay resource seconds are modeled/recorded action charges, "
                         "not full physical elapsed time")
RECALL_SCOPE_NOTE = "candidate-space recall over optical candidates only; never wafer-wide recall"
EVIDENCE_LABELS = {
    "synthetic": "synthetic fixture replay — invented parameters, not equipment evidence",
    "log_replay": "historical log replay — modeled schedule over recorded actions, not certified throughput",
}


class MetricsError(ValueError):
    pass


def _row_site(row: dict) -> Any:
    sid = row.get("site_id")
    if sid is None and isinstance(row.get("choice"), dict):
        sid = row["choice"].get("site_id")
    return sid


def _reference_view(archive: dict, candidate_ids: list[str]) -> tuple[dict, bool, str | None]:
    """Return (doi_by_site restricted to bool values, complete_for_all_candidates, source)."""
    ref = archive.get("reference")
    if not isinstance(ref, dict):
        return {}, False, None
    raw = ref.get("doi_by_site") or {}
    if not isinstance(raw, dict):
        raise MetricsError("reference.doi_by_site must be a mapping")
    known = {k: v for k, v in raw.items() if isinstance(v, bool)}
    complete = ref.get("complete") is True and all(c in known for c in candidate_ids)
    return known, complete, ref.get("source")


def _counterfactual_support(job: dict, archive: dict, candidate_ids: list[str]) -> dict:
    obs = archive.get("observations") or {}
    covered = sum(1 for c in candidate_ids if obs.get(c))
    if job.get("data_mode") == "synthetic":
        return {"available": True, "scope": "synthetic_complete_archive",
                "observed_candidates": covered, "candidates": len(candidate_ids),
                "note": "every candidate has generated attempts; support is modeled, not physical"}
    reason = ("historical partial log: outcomes of actions that were not taken are unobserved, "
              "so alternative-policy outcomes are unidentifiable")
    if covered == len(candidate_ids):
        reason = ("historical log covers every candidate but records one realized schedule; "
                  "other schedules are modeled, not observed")
    return {"available": False, "scope": "historical_log", "observed_candidates": covered,
            "candidates": len(candidate_ids), "reason": reason}


def evaluate(job: dict, archive: dict, run: dict) -> dict:
    if run.get("job_id") != job.get("job_id") or archive.get("job_id") != job.get("job_id"):
        raise MetricsError("job, archive and run job_id must match")
    candidate_ids = [c["site_id"] for c in job["candidates"]]
    cand_set = set(candidate_ids)
    rows = run.get("rows") or []
    selected: list[str] = []
    final: dict[str, Any] = {}
    attempts = 0
    for row in rows:
        sid = _row_site(row)
        if sid not in cand_set:
            raise MetricsError(f"run row selects unknown site {sid!r}")
        if sid in final:
            raise MetricsError(f"site {sid!r} selected more than once")
        rep = row.get("reported_doi")
        if rep is not None and not isinstance(rep, bool):
            raise MetricsError(f"site {sid!r}: reported_doi must be bool or null")
        selected.append(sid)
        final[sid] = rep
        attempts += len(row.get("attempts") or [])

    pos = [s for s in selected if final[s] is True]
    neg = [s for s in selected if final[s] is False]
    unknown = [s for s in selected if final[s] is None]

    known, complete, source = _reference_view(archive, candidate_ids)
    has_reference = isinstance(archive.get("reference"), dict)
    # Confirmations are identifiable when every selected positive report has a known reference.
    if has_reference and all(s in known for s in pos):
        confirmed = sum(1 for s in pos if known[s])
        false_conf = sum(1 for s in pos if not known[s])
    else:
        confirmed = false_conf = None
    if complete:
        total = sum(1 for c in candidate_ids if known[c])
        escapes = total - confirmed
        recall = confirmed / total if total else None
        detector_misses = sum(1 for s in neg if known[s])
        unknown_doi = sum(1 for s in unknown if known[s])
        unselected_doi = sum(1 for c in candidate_ids if known[c] and c not in final)
    else:
        total = escapes = recall = detector_misses = unknown_doi = unselected_doi = None

    mode = job.get("data_mode")
    return {
        "job_id": job["job_id"],
        "data_mode": mode,
        "evidence_label": EVIDENCE_LABELS.get(mode, f"unlabeled data mode {mode!r}"),
        "budget_s": run.get("budget_s"),
        "spent_s": run.get("spent_s"),
        "resource_seconds_note": RESOURCE_SECONDS_NOTE,
        "decision_wall_s": run.get("decision_wall_s"),
        "observer_wall_s": run.get("observer_wall_s"),
        "stop_reason": run.get("stop_reason"),
        "candidates": len(candidate_ids),
        "selected": len(selected),
        "attempts": attempts,
        "detector_positive_reports": len(pos),
        "detector_negative_reports": len(neg),
        "unknown_final_reports": len(unknown),
        "reference_available": has_reference,
        "reference_complete": complete,
        "reference_source": source,
        "confirmed_reference_doi": confirmed,
        "false_confirmations": false_conf,
        "total_reference_doi": total,
        "escapes": escapes,
        "escape_breakdown": None if not complete else {
            "unselected": unselected_doi, "detector_negative": detector_misses, "unknown_final": unknown_doi},
        "candidate_recall": recall,
        "recall_scope": RECALL_SCOPE_NOTE,
        "wafer_wide_recall": None,
        "counterfactual_support": _counterfactual_support(job, archive, candidate_ids),
        "commercial_validated": False,
    }

"""The four frozen selection policies.

All policies rank the same candidate set: search-pool points not yet queried in
this run. Distances use the four normalized axes of ``Point.coords``.

* adaptive_idw_plus_distance: IDW(|error|) + 0.05 * min_distance / 2
* random: next unqueried point of the seed's frozen SHA256 order
* space_filling: largest min_distance; never reads outcomes
* idw_without_distance: IDW(|error|) only

IDW weights are 1/(d^2 + 1e-9) over the calibration points and this run's
successfully revealed search points (their |relative error| under the frozen
model). ``min_distance`` is measured to every location queried in this run
(calibration points and all search queries, including failed ones), so the
geometric quantities never depend on outcomes. Ties: exact score equality is
broken by corner string, then VDD, then temperature (``Point`` ordering).

IDW scores are a learning-progress heuristic, not probabilities, information
gain or calibrated uncertainty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .protocol import Point, Protocol, distance

LABELS = {
    "adaptive_idw_plus_distance": "Adaptive (IDW + distance bonus)",
    "random": "Random (fixed order)",
    "space_filling": "Space filling (maximin)",
    "idw_without_distance": "IDW without distance bonus",
}
RANK_DEPTH = 5


@dataclass(frozen=True)
class Scored:
    point: Point
    score: float
    idw_pred: float | None
    dmin: float


def idw_predict(x: Point, evidence: list[tuple[Point, float]], power: int = 2, eps: float = 1e-9) -> float | None:
    if not evidence:
        return None
    num = 0.0
    den = 0.0
    for p, err in evidence:
        w = 1.0 / (distance(x, p) ** power + eps)
        num += w * err
        den += w
    return num / den


def min_distance(x: Point, queried: list[Point]) -> float:
    return min(distance(x, q) for q in queried)


def rank(
    policy: str,
    protocol: Protocol,
    candidates: list[Point],
    evidence: list[tuple[Point, float]],
    queried: list[Point],
    random_order: tuple[str, ...] = (),
) -> list[Scored]:
    """Return candidates best-first under ``policy``. Pure function of its inputs."""
    return _rank(policy, protocol, sorted(set(candidates)), evidence, queried, random_order)[0]


def _rank(
    policy: str,
    protocol: Protocol,
    cands: list[Point],
    evidence: list[tuple[Point, float]],
    queried: list[Point],
    random_order: tuple[str, ...] = (),
    preds: list[float | None] | None = None,
    dmins: list[float] | None = None,
) -> tuple[list[Scored], list[float | None], list[float]]:
    """``rank`` over already sorted, de-duplicated ``cands``; also returns per-candidate (pred, dmin).

    Same arithmetic as ``idw_predict``/``min_distance`` (math.dist on ``Point.coords``,
    identical accumulation order), but each point's coordinates are computed once per
    call instead of once per pair. ``preds``/``dmins`` from an earlier call over the same
    ``cands`` and the same evidence/queried lists are reused verbatim. Nothing outlives
    the call, so no state leaks between decisions or runs.
    """
    pp = protocol.policy_parameters
    weight, divisor = float(pp["exploration_weight"]), float(pp["distance_divisor"])
    power, eps = int(pp["idw_power"]), float(pp["idw_epsilon"])
    position = {pid: i for i, pid in enumerate(random_order)}
    ev_coords: list[tuple[tuple[float, ...], float]] | None = None
    q_coords: list[tuple[float, ...]] | None = None
    out_preds: list[float | None] = []
    out_dmins: list[float] = []
    scored = []
    for i, c in enumerate(cands):
        if preds is None or dmins is None:
            xc = c.coords
        if preds is not None:
            pred = preds[i]
        elif not evidence:
            pred = None
        else:
            if ev_coords is None:
                ev_coords = [(p.coords, err) for p, err in evidence]
            num = 0.0
            den = 0.0
            for pc, err in ev_coords:
                w = 1.0 / (math.dist(xc, pc) ** power + eps)
                num += w * err
                den += w
            pred = num / den
        if dmins is not None:
            dmin = dmins[i]
        else:
            if q_coords is None:
                q_coords = [q.coords for q in queried]
            dmin = min(math.dist(xc, qc) for qc in q_coords)
        if policy == "adaptive_idw_plus_distance":
            score = pred + weight * dmin / divisor
        elif policy == "idw_without_distance":
            score = pred
        elif policy == "space_filling":
            score = dmin
        elif policy == "random":
            if c.id not in position:
                raise ValueError(f"{c.id} missing from the random order")
            score = -float(position[c.id])
        else:
            raise ValueError(f"unknown policy {policy!r}")
        out_preds.append(pred)
        out_dmins.append(dmin)
        scored.append(Scored(c, score, pred, dmin))
    # Stable sort over lexically pre-sorted points: exact ties keep corner/VDD/temperature order.
    scored.sort(key=lambda s: -s.score)
    return scored, out_preds, out_dmins


def candidate_view(s: Scored, role: str) -> dict[str, Any]:
    return {
        "point_id": s.point.id,
        "pvt": s.point.pvt,
        "role": role,
        "score": s.score,
        "idw_predicted_abs_error": s.idw_pred,
        "min_normalized_distance": s.dmin,
        "cost_queries": 1,
    }


def build_decision(
    policy: str,
    protocol: Protocol,
    candidates: list[Point],
    evidence: list[tuple[Point, float]],
    queried: list[Point],
    random_order: tuple[str, ...],
    previous_evidence: list[tuple[Point, float]],
    evidence_result_ids: list[str],
) -> dict[str, Any]:
    """Selection plus the two contract candidates and the before/after-result rankings.

    ``previous_evidence`` is the evidence without the most recent outcome (same
    queried locations), so ``rank_before`` vs ``rank_after`` shows mechanically
    whether that outcome changed the ranking and the choice.
    """
    # The four rankings share one candidate order, one set of min distances and the
    # IDW predictions under ``evidence``; only ``rank_before`` needs its own IDW pass.
    cands = sorted(set(candidates))
    after, preds, dmins = _rank(policy, protocol, cands, evidence, queried, random_order)
    before = _rank(policy, protocol, cands, previous_evidence, queried, random_order, dmins=dmins)[0]
    selected = after[0]
    exploit = _rank("idw_without_distance", protocol, cands, evidence, queried, preds=preds, dmins=dmins)[0][0]
    maximin = _rank("space_filling", protocol, cands, evidence, queried, preds=preds, dmins=dmins)[0]
    # Two distinct tests whenever more than one candidate remains: if the maximin
    # optimum coincides with the exploitation optimum, take the next maximin point.
    explore = next((s for s in maximin if s.point.id != exploit.point.id), None)
    by_id = {s.point.id: s for s in after}
    views = [candidate_view(by_id[exploit.point.id], "exploitation")]
    if explore is not None:
        views.append(candidate_view(by_id[explore.point.id], "exploration"))
    if selected.point.id not in {v["point_id"] for v in views}:
        views.append(candidate_view(selected, "policy_choice"))
    return {
        "policy": policy,
        "evidence_result_ids": evidence_result_ids,
        "candidates": views,
        "selected_point_id": selected.point.id,
        "selected_score": selected.score,
        "rank_before": [s.point.id for s in before[:RANK_DEPTH]],
        "rank_after": [s.point.id for s in after[:RANK_DEPTH]],
        "selection_changed": before[0].point.id != selected.point.id,
        "score_rule": {
            "adaptive_idw_plus_distance": "idw + 0.05*min_distance/2",
            "idw_without_distance": "idw",
            "space_filling": "min_distance",
            "random": "-position in frozen order",
        }[policy],
    }

"""Deterministic review-selection policies for the synthetic inspection study.

Policy input boundary: a policy sees only ``PolicyInput`` (a sanitized whitelist of
public lot arrays, never seed/scenario/oracle) plus ``SelectionState`` (frozen and
online model probabilities and the outcomes of reviews that were actually selected).
The harness owns the oracle and the review simulator; policies never receive them.

Fixed score definitions (frozen with the protocol, not tuned on held-out lots):

- random: deterministic permutation from an externally derived public policy seed.
- recipe: sigmoid(0.5 z_signal + 0.3 z_size + 0.2 z_design_delta) / max action cost,
  z standardized by the training-candidate mean/sd; missing optical inputs are imputed
  with the training mean (z = 0).
- learned: frozen logistic DOI probability / max action cost.
- uncertainty_diversity: (p_online + w_u 4p(1-p) + w_d diversity) / max cost.
- falsify: ((1-w_s) p_online + w_s spatial_posterior + w_d diversity) / max cost;
  every ``audit_period``-th decision audits a frozen-negative (p<0.5) candidate by
  (w_n novelty + (1-w_n) spatial_posterior) / cost; in with_rescan mode every
  ``rescan_period``-th decision reviews an outside-candidate site (rescan wins a tie
  with the audit slot). Ablations: no_audit drops the audit slot only; no_spatial
  drops neighborhood-label and diversity terms (audit keeps novelty only); no_update
  keeps the frozen model while spatial evidence still adapts.

Spatial posterior (per site, same wafer only, Gaussian kernel of bandwidth h on
normalized xy) = (k * base + sum K*y) / (k + sum K) with base = mean frozen
probability over original candidates and y = reported DOI of successful reviews.
Diversity = min(1, distance to nearest selected site on the same wafer / h).
Both are updated incrementally in O(n) per observation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

FEATURES = ("signal", "size", "texture", "design_delta", "process_context", "radius", "layer")
OPTICAL_COUNT = 4
RECIPE_WEIGHTS = {"signal": 0.5, "size": 0.3, "design_delta": 0.2}
PUBLIC_WHITELIST = ("lot_id", "site_ids", "wafer", "xy", "layer", "candidate", "features")
POLICIES = ("random", "recipe", "learned", "uncertainty_diversity", "falsify",
            "falsify_no_audit", "falsify_no_spatial", "falsify_no_update")
MODES = ("candidate_only", "with_rescan")
MAX_EVIDENCE_REFS = 5


def _ro(a: Any, dtype: Any = None) -> np.ndarray:
    out = np.array(a, dtype=dtype, copy=True)
    out.flags.writeable = False
    return out


@dataclass(frozen=True)
class PolicyInput:
    """Sanitized public whitelist. Seed, scenario and oracle fields never enter."""

    lot_id: str
    site_ids: tuple
    wafer: np.ndarray
    xy: np.ndarray
    layer: np.ndarray
    candidate: np.ndarray
    features: np.ndarray  # raw, outside-candidate optical features are NaN
    features_imputed: np.ndarray  # NaN replaced by training mean
    zscores: np.ndarray  # standardized by training mean/sd

    @property
    def n(self) -> int:
        return len(self.site_ids)


def sanitize_public(public: dict, train_mean: Any, train_sd: Any) -> PolicyInput:
    feats = np.asarray(public["features"], dtype=float)
    mean = np.asarray(train_mean, dtype=float)
    sd = np.where(np.asarray(train_sd, dtype=float) > 1e-12, np.asarray(train_sd, dtype=float), 1.0)
    imputed = np.where(np.isnan(feats), mean[None, :], feats)
    return PolicyInput(
        lot_id=str(public["lot_id"]),
        site_ids=tuple(str(s) for s in public["site_ids"]),
        wafer=_ro(public["wafer"]),
        xy=_ro(public["xy"], float),
        layer=_ro(public["layer"]),
        candidate=_ro(public["candidate"], bool),
        features=_ro(feats),
        features_imputed=_ro(imputed),
        zscores=_ro((imputed - mean[None, :]) / sd[None, :]),
    )


def policy_seed(study_id: str, policy: str, lot_id: str) -> int:
    """Public, externally derived seed: never the latent lot seed."""
    digest = hashlib.sha256(f"{study_id}|policy|{policy}|{lot_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


class SelectionState:
    """Everything a policy may read: public view, model probabilities, past reviews."""

    def __init__(self, view: PolicyInput, frozen_p: np.ndarray, online_model: dict,
                 online_p: np.ndarray, mode: str, selection_cfg: dict, classification_threshold: float):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        n = view.n
        self.view = view
        self.mode = mode
        self.cfg = selection_cfg
        self.threshold = float(classification_threshold)
        self.frozen_p = _ro(frozen_p, float)
        self.online_model = online_model
        self.online_p = _ro(online_p, float)
        self.h = float(selection_cfg["spatial_bandwidth"])
        self.prior_k = float(selection_cfg["spatial_prior_strength"])
        cand = view.candidate
        self.spatial_base = float(self.frozen_p[cand].mean()) if cand.any() else float(self.frozen_p.mean())
        self.visited = np.zeros(n, dtype=bool)
        self.label = np.full(n, np.nan)
        self.kernel_sum = np.zeros(n)
        self.kernel_pos = np.zeros(n)
        self.min_dist = np.full(n, np.inf)
        self.labeled: list[int] = []
        self.selected: list[int] = []
        self.current_wafer: Any = None
        self.current_xy: np.ndarray | None = None
        self._wafer_index = {w: np.flatnonzero(view.wafer == w) for w in np.unique(view.wafer)}
        z = view.zscores
        self.novelty = _ro(1.0 - np.exp(-0.5 * np.mean(z * z, axis=1)))

    @property
    def step(self) -> int:
        """1-based index of the decision about to be made."""
        return len(self.selected) + 1

    def allowed(self) -> np.ndarray:
        pool = ~self.visited
        return pool & self.view.candidate if self.mode == "candidate_only" else pool

    def spatial_posterior(self) -> np.ndarray:
        return (self.prior_k * self.spatial_base + self.kernel_pos) / (self.prior_k + self.kernel_sum)

    def diversity(self) -> np.ndarray:
        return np.minimum(1.0, self.min_dist / self.h)

    def _same_wafer(self, i: int) -> tuple[np.ndarray, np.ndarray]:
        idx = self._wafer_index[self.view.wafer[i]]
        d = np.linalg.norm(self.view.xy[idx] - self.view.xy[i], axis=1)
        return idx, d

    def record_visit(self, i: int) -> None:
        """Mark selected (always, even when the review fails) and move the stage."""
        self.visited[i] = True
        self.selected.append(int(i))
        idx, d = self._same_wafer(i)
        self.min_dist[idx] = np.minimum(self.min_dist[idx], d)
        self.current_wafer = self.view.wafer[i]
        self.current_xy = self.view.xy[i]

    def record_label(self, i: int, reported_doi: bool) -> None:
        """Only a successful review's reported result becomes evidence."""
        y = 1.0 if reported_doi else 0.0
        self.label[i] = y
        self.labeled.append(int(i))
        idx, d = self._same_wafer(i)
        k = np.exp(-0.5 * (d / self.h) ** 2)
        self.kernel_sum[idx] += k
        self.kernel_pos[idx] += k * y

    def set_online(self, model: dict, online_p: np.ndarray) -> None:
        self.online_model = model
        self.online_p = _ro(online_p, float)

    def evidence_refs(self, i: int) -> list[dict]:
        """Past successful reviews on the same wafer that most influence site i."""
        if not self.labeled:
            return []
        lab = np.asarray(self.labeled)
        lab = lab[self.view.wafer[lab] == self.view.wafer[i]]
        if lab.size == 0:
            return []
        d = np.linalg.norm(self.view.xy[lab] - self.view.xy[i], axis=1)
        w = np.exp(-0.5 * (d / self.h) ** 2)
        order = np.argsort(-w, kind="stable")[:MAX_EVIDENCE_REFS]
        return [{"site_id": self.view.site_ids[int(lab[o])], "reported_doi": bool(self.label[lab[o]]),
                 "kernel_weight": round(float(w[o]), 6)} for o in order if w[o] > 1e-3]


@dataclass
class Choice:
    index: int
    reason: str
    score: float
    components: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)


def _argmax(score: np.ndarray, mask: np.ndarray) -> int:
    s = np.where(mask, score, -np.inf)
    return int(np.argmax(s))


class Policy:
    name = "base"
    updates_model = False
    uses_spatial = False

    def __init__(self, seed: int = 0):
        self.seed = seed

    def select(self, state: SelectionState, max_cost: np.ndarray, affordable: np.ndarray) -> Choice:
        raise NotImplementedError


class RandomPolicy(Policy):
    name = "random"

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self._order: np.ndarray | None = None

    def select(self, state, max_cost, affordable):
        if self._order is None:
            self._order = np.random.default_rng(self.seed).permutation(state.view.n)
        pick = self._order[affordable[self._order]][0]
        return Choice(int(pick), "random_order", 0.0, {"policy_seed_digest": self.seed % 10**8})


class RecipePolicy(Policy):
    name = "recipe"

    def __init__(self, seed: int = 0):
        super().__init__(seed)
        self._raw: np.ndarray | None = None

    def select(self, state, max_cost, affordable):
        if self._raw is None:
            z = state.view.zscores
            combo = sum(w * z[:, FEATURES.index(f)] for f, w in RECIPE_WEIGHTS.items())
            self._raw = 1.0 / (1.0 + np.exp(-combo))
        score = self._raw / max_cost
        i = _argmax(score, affordable)
        return Choice(i, "recipe_signal_size_design_per_cost", float(score[i]),
                      {"recipe_score": float(self._raw[i]), "max_cost": float(max_cost[i])})


class LearnedPolicy(Policy):
    name = "learned"

    def select(self, state, max_cost, affordable):
        score = state.frozen_p / max_cost
        i = _argmax(score, affordable)
        return Choice(i, "frozen_probability_per_cost", float(score[i]),
                      {"frozen_p": float(state.frozen_p[i]), "max_cost": float(max_cost[i])})


class UncertaintyDiversityPolicy(Policy):
    name = "uncertainty_diversity"
    updates_model = True

    def select(self, state, max_cost, affordable):
        c = state.cfg
        p = state.online_p
        unc = 4.0 * p * (1.0 - p)
        div = state.diversity()
        risk = p + c["uncertainty_weight"] * unc + c["diversity_weight"] * div
        score = risk / max_cost
        i = _argmax(score, affordable)
        return Choice(i, "online_probability_uncertainty_diversity_per_cost", float(score[i]),
                      {"online_p": float(p[i]), "uncertainty": float(unc[i]), "diversity": float(div[i]),
                       "max_cost": float(max_cost[i])})


class FalsifyPolicy(Policy):
    name = "falsify"
    updates_model = True
    uses_spatial = True
    audit = True

    def _risk(self, state):
        c = state.cfg
        p = state.online_p
        if not self.uses_spatial:
            return p, {"online_p": p}
        post = state.spatial_posterior()
        div = state.diversity()
        w = c["spatial_weight"]
        return (1.0 - w) * p + w * post + c["diversity_weight"] * div, {
            "online_p": p, "spatial_posterior": post, "diversity": div}

    def _choice(self, state, i, reason, score, parts, max_cost):
        comp = {k: float(v[i]) for k, v in parts.items()}
        comp["frozen_p"] = float(state.frozen_p[i])
        comp["max_cost"] = float(max_cost[i])
        ev = state.evidence_refs(i) if self.uses_spatial else []
        return Choice(int(i), reason, float(score[i]), comp, ev)

    def select(self, state, max_cost, affordable):
        c = state.cfg
        k = state.step
        cand = state.view.candidate
        if state.mode == "with_rescan" and k % int(c["rescan_period"]) == 0:
            mask = affordable & ~cand
            if mask.any():
                risk, parts = self._risk(state)
                score = risk / max_cost
                return self._choice(state, _argmax(score, mask), "scheduled_outside_rescan", score, parts, max_cost)
        if self.audit and k % int(c["audit_period"]) == 0:
            mask = affordable & cand & (state.frozen_p < state.threshold)
            if mask.any():
                wn = c["novelty_weight"]
                if self.uses_spatial:
                    post = state.spatial_posterior()
                    a = wn * state.novelty + (1.0 - wn) * post
                    parts = {"novelty": state.novelty, "spatial_posterior": post}
                else:
                    a = state.novelty
                    parts = {"novelty": state.novelty}
                score = a / max_cost
                return self._choice(state, _argmax(score, mask), "scheduled_frozen_negative_audit", score, parts, max_cost)
        risk, parts = self._risk(state)
        score = risk / max_cost
        return self._choice(state, _argmax(score, affordable), "online_risk_spatial_per_cost", score, parts, max_cost)


class FalsifyNoAudit(FalsifyPolicy):
    name = "falsify_no_audit"
    audit = False


class FalsifyNoSpatial(FalsifyPolicy):
    name = "falsify_no_spatial"
    uses_spatial = False


class FalsifyNoUpdate(FalsifyPolicy):
    name = "falsify_no_update"
    updates_model = False


_REGISTRY: dict[str, Callable[[int], Policy]] = {
    "random": RandomPolicy, "recipe": RecipePolicy, "learned": LearnedPolicy,
    "uncertainty_diversity": UncertaintyDiversityPolicy, "falsify": FalsifyPolicy,
    "falsify_no_audit": FalsifyNoAudit, "falsify_no_spatial": FalsifyNoSpatial,
    "falsify_no_update": FalsifyNoUpdate,
}


def make_policy(name: str, seed: int) -> Policy:
    if name not in _REGISTRY:
        raise ValueError(f"unknown policy {name}")
    return _REGISTRY[name](seed)

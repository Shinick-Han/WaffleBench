"""Paid inspection live session over one frozen v3 campaign (INSPECTION_LIVE_CONTRACT.md).

Separate live demonstration on one fresh authored synthetic lot, never the v3 primary
benchmark. The frozen ``cb400_route_full`` variant (``cb400/identity`` model, deterministic
``route_full_gamma`` planner) chooses every site; callers only carry IDs between steps.

Layout of a live root (``prepare`` refuses an existing directory)::

    live.json            manifest: input/source/model digests, budget, review cap, lot split
    inputs/              immutable copies: frozen-config.json, model.json, review-yield.json,
                         freeze.json, frozen_p.npy, reported_yield.npy
    private/lot/         public.npz, oracle.npz, metadata.json (seed/scenario stay here)
    events.jsonl         append-only hash-chained events (decision, admission, observation,
                         analysis_update, tool_call, closed)
    .lock                cross-process exclusive lock (every read and mutation)

Paid review order: the admission (full maximum CU reservation and result ID) is appended and
fsynced BEFORE the sensor runs. An admission without its observation keeps the reservation
charged and blocks the session; nothing silently retries. Policies see only the sanitized
public view plus already paid observations; the oracle is opened only inside one sensor call.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from inspection_review import data
from inspection_review.cli import _load_npz, load_public, read_json, save_lot, sha256_file, write_json
from inspection_review.harness import EPS, _clean_obs, _success, cost_vectors
from inspection_review.policies import MODES, SelectionState, policy_seed, sanitize_public
from inspection_v3 import cli as v3cli
from inspection_v3 import model as v3model
from inspection_v3.inference import compile_predictor
from inspection_v3.policies import make_policy

SCHEMA = 1
KIND = "inspection_live"
VARIANT = "cb400_route_full"
RESULT_ID = re.compile(r"^ir_[0-9a-f]{16}$")
LOCK_TIMEOUT_S = 60.0
INPUT_FILES = ("frozen-config.json", "model.json", "review-yield.json", "freeze.json", "frozen_p.npy", "reported_yield.npy")
DATA_LABEL = "authored numeric synthetic live demonstration; separate from the v3 primary benchmark and classifier accuracy"
NEGATIVE_MEANING = ("reported_positive=false means the sensor did not report a DOI on this review; it does not prove "
                    "the absence of a physical defect")
REWARD_MEANING = {
    "latent_doi_probability": "frozen model DOI probability used as the planner's reward; a prediction, not an observed outcome",
    "reported_review_positive": "frozen reported-review-positive yield score used as the planner's reward; a prediction, not an observed outcome",
}
SCORE_RULE = ("route_full_gamma: value(i) = max_j (r_i + g r_j) / (c_i + g c_j) over feasible second sites j, "
              "else r_i / c_i; r = selection reward, c = reserved CU, g = v3_selection.gamma")
_ABS_PATH = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|\\\\|/(?:Users|home|tmp|var)/)[^\s'\"]*")


class LiveError(RuntimeError):
    """A refused call. Nothing new was charged unless the message says so."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _py(x: Any) -> Any:
    if isinstance(x, np.generic):
        return x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


def clean_message(text: str) -> str:
    return _ABS_PATH.sub("<path>", text)


# ---------------------------------------------------------------- cross-process lock
@contextlib.contextmanager
def exclusive_lock(path: Path, timeout: float = LOCK_TIMEOUT_S):
    """Exclusive byte lock shared by sibling MCP processes (msvcrt on Windows, flock elsewhere)."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise LiveError("live session lock timed out; another call is still running") from None
                time.sleep(0.02)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# ---------------------------------------------------------------- prepare
def _used_seeds(frozen_config: dict) -> set[int]:
    used: set[int] = set()
    configs = [data.load_config(), read_json(v3cli.REPO / "inspection_v2/protocol.json"), read_json(v3cli.PROTOCOL), frozen_config]
    for c in configs:
        used.update(c["splits"]["train_seeds"] + c["splits"]["validation_seeds"])
        used.update(s for seeds in c["splits"]["test_seeds_by_scenario"].values() for s in seeds)
        used.update(r["seed"] for rows in (c.get("v2_splits") or {}).values() for r in rows)
    return used


def _variant(config: dict) -> dict:
    variant = next((v for v in config.get("v2_variants", []) if v["id"] == VARIANT), None)
    if variant is None or variant["policy"] != "route_full_gamma" or variant["model"] != "cb400/identity":
        raise LiveError(f"frozen config has no {VARIANT} route_full_gamma/cb400 variant")
    return variant


def prepare(root, campaign_root, *, seed: int = 9800, scenario: str = "stationary", mode: str = "candidate_only",
            budget: float = 120, max_reviews: int = 4) -> dict:
    """Create one fresh live root from a verified frozen v3 campaign. Never modifies the campaign."""
    root, campaign_root = Path(root), Path(campaign_root)
    if root.exists():
        raise LiveError("live root must not exist")
    try:
        receipt = v3cli.verify_freeze(campaign_root)  # receipt digest, frozen v3 sources, frozen files
    except Exception as exc:
        raise LiveError(f"frozen v3 campaign invalid: {clean_message(str(exc))}") from exc
    if receipt.get("study") != "v3":
        raise LiveError("campaign receipt is not a v3 freeze")
    if receipt.get("candidate") != VARIANT:
        raise LiveError("live planner requires the same cb400_route_full candidate selected in the frozen receipt")
    config = read_json(campaign_root / "frozen-config.json")
    variant = _variant(config)
    models = read_json(campaign_root / "models.json")
    fitted = models.get(variant["model"])
    if fitted is None or v3model.hash_model(fitted) != receipt["model_hashes"].get(variant["model"]):
        raise LiveError("frozen model missing or does not match the freeze receipt")
    yield_model = read_json(campaign_root / "review-yield.json")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 or seed in _used_seeds(config):
        raise LiveError("live seed must be a fresh non-negative integer unused by any v1/v2/v3 split")
    if scenario not in config["splits"]["test_seeds_by_scenario"]:
        raise LiveError("unknown scenario")
    if mode not in MODES:
        raise LiveError("unknown mode")
    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
        raise LiveError("budget must be a positive finite CU amount")
    if isinstance(max_reviews, bool) or not isinstance(max_reviews, int) or max_reviews < 2:
        raise LiveError("max_reviews must be an integer >= 2")

    lot = data.generate_lot(seed, scenario, config)  # same frozen physics/sensor, fresh lot
    root.mkdir(parents=True)
    ref = save_lot(root / "private" / "lot", lot, "live_fresh", seed, scenario)
    inputs = root / "inputs"
    inputs.mkdir()
    for name in ("frozen-config.json", "review-yield.json", "freeze.json"):
        shutil.copyfile(campaign_root / name, inputs / name)
    write_json(inputs / "model.json", fitted)
    public = load_public(root / "private" / "lot")
    frozen_p = np.asarray(compile_predictor(fitted).predict(public["features"]), dtype=float)
    reported_yield = np.clip(np.asarray(compile_predictor(yield_model).predict_raw(public["features"]), dtype=float), 0.0, 1.0)
    np.save(inputs / "frozen_p.npy", frozen_p, allow_pickle=False)
    np.save(inputs / "reported_yield.npy", reported_yield, allow_pickle=False)
    manifest = {
        "schema_version": SCHEMA, "kind": KIND, "created_at": _now(), "data_label": DATA_LABEL,
        "variant": variant, "policy": variant["policy"], "mode": mode, "budget": float(budget),
        "max_reviews": int(max_reviews), "lot_id": ref["lot_id"], "n_sites": int(len(public["site_ids"])),
        "fresh_lot": {"split": "live_fresh", "disjoint_from": "every v1/v2/v3 train/validation/calibration/development/test seed",
                      "primary_test_or_training_modified": False},
        "public_sha256": ref["public_sha256"], "oracle_sha256": ref["oracle_sha256"],
        "lot_metadata_sha256": ref["metadata_sha256"],
        "model_hash": v3model.hash_model(fitted), "review_yield_model_hash": v3model.hash_model(yield_model),
        "frozen_p_sha256": hashlib.sha256(frozen_p.tobytes()).hexdigest(),
        "reported_yield_sha256": hashlib.sha256(reported_yield.tobytes()).hexdigest(),
        "source_freeze": {"receipt_sha256": receipt["receipt_sha256"], "source_commit": receipt["source_commit"],
                          "source_hashes_sha256": _digest(receipt["source_hashes"]), "candidate": receipt.get("candidate")},
        "campaign_file_hashes": receipt["file_hashes"],
        "input_hashes": {name: sha256_file(inputs / name) for name in INPUT_FILES},
        "cost_unit": config["cost"]["unit"],
    }
    manifest["manifest_sha256"] = _digest(manifest)
    write_json(root / "live.json", manifest)
    (root / "events.jsonl").write_bytes(b"")
    return {"status": "prepared", "lot_id": manifest["lot_id"], "model_hash": manifest["model_hash"],
            "manifest_sha256": manifest["manifest_sha256"], "budget": manifest["budget"], "max_reviews": manifest["max_reviews"]}


# ---------------------------------------------------------------- session
class InspectionLive:
    """Reconstructs one live session from verified immutable inputs and append-only paid events."""

    def __init__(self, root):
        self.root = Path(root)
        if not (self.root / "live.json").is_file():
            raise LiveError("not a prepared inspection live root")
        m = read_json(self.root / "live.json")
        if m.get("kind") != KIND or _digest({k: v for k, v in m.items() if k != "manifest_sha256"}) != m.get("manifest_sha256"):
            raise LiveError("live manifest changed")
        for name, expected in m["input_hashes"].items():
            if sha256_file(self.root / "inputs" / name) != expected:
                raise LiveError(f"live input changed: {name}")
        lot_dir = self.root / "private" / "lot"
        if (sha256_file(lot_dir / "public.npz") != m["public_sha256"] or sha256_file(lot_dir / "metadata.json") != m["lot_metadata_sha256"]):
            raise LiveError("live lot changed")
        if _digest(v3cli.source_hashes()) != m["source_freeze"]["source_hashes_sha256"]:
            raise LiveError("frozen v3 source changed since prepare")
        self.config = read_json(self.root / "inputs" / "frozen-config.json")
        self.model = read_json(self.root / "inputs" / "model.json")
        if v3model.hash_model(self.model) != m["model_hash"]:
            raise LiveError("frozen model hash mismatch")
        self.frozen_p = np.load(self.root / "inputs" / "frozen_p.npy", allow_pickle=False)
        self.reported_yield = np.load(self.root / "inputs" / "reported_yield.npy", allow_pickle=False)
        if (hashlib.sha256(self.frozen_p.tobytes()).hexdigest() != m["frozen_p_sha256"]
                or hashlib.sha256(self.reported_yield.tobytes()).hexdigest() != m["reported_yield_sha256"]):
            raise LiveError("frozen score arrays changed")
        self.frozen_p.flags.writeable = False
        self.reported_yield.flags.writeable = False
        self.manifest = m
        public = load_public(lot_dir)
        self.view = sanitize_public(public, self.model["mean"], self.model["scale"])
        self.index = {sid: i for i, sid in enumerate(self.view.site_ids)}
        self.cost = self.config["cost"]
        self.reward = self.reported_yield if m["variant"].get("reward") == "review_yield" else None
        self.reward_semantics = "reported_review_positive" if self.reward is not None else "latent_doi_probability"

    # ------------------------------------------------------------ ledger
    def _lock(self):
        return exclusive_lock(self.root / ".lock")

    def _events(self) -> list[dict]:
        prev = self.manifest["manifest_sha256"]
        events = []
        raw = (self.root / "events.jsonl").read_bytes()
        if raw and not raw.endswith(b"\n"):
            raise LiveError("event log has a torn final record; session blocked")
        for n, line in enumerate(raw.decode("utf-8").splitlines()):
            e = json.loads(line)
            body = {k: v for k, v in e.items() if k != "sha256"}
            if e.get("index") != n or e.get("prev_sha256") != prev or _digest(body) != e.get("sha256"):
                raise LiveError("event log chain broken")
            prev = e["sha256"]
            events.append(e)
        return events

    def _append(self, events: list[dict], kind: str, payload: dict) -> dict:
        e = {"index": len(events), "type": kind, "at": _now(), "payload": payload,
             "prev_sha256": events[-1]["sha256"] if events else self.manifest["manifest_sha256"]}
        e["sha256"] = _digest(e)
        with open(self.root / "events.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(e, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        events.append(e)
        return e

    def _ledger(self, events: list[dict]) -> dict:
        by = lambda t: [e["payload"] for e in events if e["type"] == t]  # noqa: E731
        decisions, admissions, observations = by("decision"), by("admission"), by("observation")
        observed = {o["result_id"] for o in observations}
        incomplete = [a for a in admissions if a["result_id"] not in observed]
        spent = sum(o["charged"] for o in observations) + sum(a["reserved_cost"] for a in incomplete)
        admitted = {a["decision_sequence"] for a in admissions}
        pending = next((d for d in decisions if d["decision_sequence"] not in admitted), None)
        closed = by("closed")
        return {"decisions": decisions, "admissions": admissions, "observations": observations,
                "incomplete": incomplete, "updates": by("analysis_update"), "spent": spent,
                "pending": pending, "closed": closed[0] if closed else None}

    # ------------------------------------------------------------ planning
    def _state(self, ledger: dict) -> SelectionState:
        """Selection state from the public view and paid observations only (no oracle)."""
        state = SelectionState(self.view, self.frozen_p, self.model, self.frozen_p, self.manifest["mode"],
                               self.config["selection"], self.config["model"]["classification_threshold"])
        if self.reward is not None:
            state.selection_reward = self.reward
        for o in ledger["observations"]:
            i = self.index[o["site_id"]]
            state.record_visit(i)
            if o["label"] is not None:
                state.record_label(i, bool(o["label"]))
        for a in ledger["incomplete"]:
            state.record_visit(self.index[a["site_id"]])
        return state

    def _candidate(self, i: int, role: str, cv: dict, reward: np.ndarray) -> dict:
        reserved = float(cv["max"][i])
        return {"site_id": self.view.site_ids[i], "wafer": _py(self.view.wafer[i]), "role": role,
                "baseline_p": float(self.frozen_p[i]), "reported_yield_score": float(self.reported_yield[i]),
                "selection_reward": float(reward[i]), "reserved_cost": reserved,
                "single_step_value": float(reward[i] / reserved),
                "cost_parts": {"load": float(cv["load"][i]), "stage": float(cv["stage"][i]), "dwell": float(cv["dwell"][i]),
                               "outside_rescan": float(cv["outside"][i]),
                               "retry_reserve": float(self.cost["retry_dwell"]) * int(self.cost["retry_limit"])}}

    def _plan(self, ledger: dict) -> dict | None:
        """Deterministic cb400_route_full preview; None when nothing is affordable."""
        state = self._state(ledger)
        budget = self.manifest["budget"]
        cv = cost_vectors(state, self.cost)
        affordable = state.allowed() & (ledger["spent"] + cv["max"] <= budget + EPS)
        if not affordable.any():
            return None
        state.remaining_budget = budget - ledger["spent"]
        policy = make_policy(self.manifest["policy"], policy_seed(self.config.get("study_id", ""), self.manifest["policy"],
                                                                  self.view.lot_id), self.config)
        choice = policy.select(state, cv["max"], affordable)
        i = int(choice.index)
        if not affordable[i]:
            raise LiveError("planner chose an unaffordable site")
        reward = self.reward if self.reward is not None else self.frozen_p
        cands = [self._candidate(i, "selected_route_first", cv, reward)]
        j = choice.components.get("next_site_index")
        if j is not None and j != i and affordable[j]:
            cands.append(self._candidate(int(j), "route_lookahead_next", cv, reward))
        single = np.where(affordable, reward / cv["max"], -np.inf)
        for k in [int(k) for k in np.lexsort((np.arange(self.view.n), -single))]:
            if affordable[k] and self.view.site_ids[k] not in {c["site_id"] for c in cands}:
                cands.append(self._candidate(k, "single_step_alternative", cv, reward))
                break
        parts = {k: _py(v) for k, v in choice.components.items() if k != "next_site_index"}
        return {"selected_site_id": self.view.site_ids[i], "reason": choice.reason, "score": float(choice.score),
                "score_parts": {**parts, "rule": SCORE_RULE}, "candidates": cands,
                "evidence_result_ids": [o["result_id"] for o in ledger["observations"]],
                "remaining_budget": budget - ledger["spent"], "affordable_sites": int(affordable.sum())}

    # ------------------------------------------------------------ views
    def _result_view(self, o: dict) -> dict:
        return {**{k: o[k] for k in ("result_id", "site_id", "wafer", "decision_sequence", "reported_doi", "reported_kind",
                                     "quality", "status", "attempts", "cost", "charged", "reserved_cost", "cumulative_spend",
                                     "baseline_p", "reported_positive", "reported_yield_score", "selection_reward",
                                     "selection_reward_semantics")},
                "selection_reward_meaning": REWARD_MEANING[o["selection_reward_semantics"]],
                "negative_report_meaning": NEGATIVE_MEANING, "data_label": DATA_LABEL}

    def _blocked(self, ledger: dict) -> dict | None:
        if not ledger["incomplete"]:
            return None
        return {"reason": "incomplete_admission", "result_ids": [a["result_id"] for a in ledger["incomplete"]],
                "conservative_charge": sum(a["reserved_cost"] for a in ledger["incomplete"])}

    def _public(self, ledger: dict) -> dict:
        m = self.manifest
        return {"kind": KIND, "data_label": DATA_LABEL, "lot_id": m["lot_id"], "n_sites": m["n_sites"], "mode": m["mode"],
                "variant": m["variant"]["id"], "policy": m["policy"], "model_hash": m["model_hash"],
                "budget": {"limit": m["budget"], "spent": ledger["spent"], "remaining": m["budget"] - ledger["spent"],
                           "unit": m["cost_unit"]},
                "max_reviews": m["max_reviews"], "reviews_admitted": len(ledger["admissions"]),
                "reviews_executed": len(ledger["observations"]),
                "pending_decision": ledger["pending"], "decisions": ledger["decisions"],
                "observations": [self._result_view(o) for o in ledger["observations"]],
                "updates": ledger["updates"], "closed": ledger["closed"], "blocked": self._blocked(ledger)}

    # ------------------------------------------------------------ analyst
    def analyze_and_plan(self, finalize: bool = False, *, actor: str = "direct_call") -> dict:
        if not isinstance(finalize, bool):
            raise LiveError("finalize must be a boolean")
        with self._lock():
            events = self._events()
            ledger = self._ledger(events)
            if ledger["closed"]:
                return {"status": "closed", **self._public(ledger)}
            if ledger["incomplete"]:
                raise LiveError("session blocked: an admitted review has no recorded outcome; its reservation stays charged")
            pending = ledger["pending"]
            if pending is not None and not finalize:
                return {"status": "decision_pending", "decision": pending, **self._public(ledger)}  # idempotent
            if finalize and pending is not None:
                raise LiveError("finalize denied: the recorded decision has not been reviewed")
            if finalize and len(ledger["observations"]) < 2:
                raise LiveError("finalize denied: at least two executed reviews are required")
            plan = self._plan(ledger)
            cap = len(ledger["admissions"]) >= self.manifest["max_reviews"]
            record = not finalize and not cap and plan is not None
            if record and len({c["site_id"] for c in plan["candidates"]}) < 2 and plan["affordable_sites"] >= 2:
                raise LiveError("planner produced fewer than two distinct affordable candidates")
            next_state = ("recorded" if record else "not_recorded_finalized" if finalize
                          else "not_recorded_review_cap" if cap else "not_recorded_none_affordable")
            updated = {u["observed_result_id"] for u in ledger["updates"]}
            decisions = {d["decision_sequence"]: d for d in ledger["decisions"]}
            new_updates = []
            for o in ledger["observations"]:
                if o["result_id"] in updated:
                    continue
                prior = decisions[o["decision_sequence"]]["score_parts"].get("next_site_id")
                u = {"update_index": len(ledger["updates"]) + len(new_updates) + 1, "actor": actor,
                     "observed_result_id": o["result_id"], "observed_site_id": o["site_id"],
                     "decision_sequence": o["decision_sequence"], "observed_status": o["status"],
                     "observed_reported_positive": o["reported_positive"],
                     "evidence_result_ids": [x["result_id"] for x in ledger["observations"]],
                     "prior_lookahead_site_id": prior,
                     "next_selected_site_id": None if plan is None else plan["selected_site_id"],
                     "next_candidate_site_ids": [] if plan is None else [c["site_id"] for c in plan["candidates"]],
                     "lookahead_followed": None if plan is None or prior is None else plan["selected_site_id"] == prior,
                     "next_decision": next_state, "remaining_budget": self.manifest["budget"] - ledger["spent"]}
                self._append(events, "analysis_update", u)
                new_updates.append(u)
            if finalize:
                closed = {"status": "finalized", "actor": actor, "reviews_executed": len(ledger["observations"]),
                          "spent": ledger["spent"], "updates": len(ledger["updates"]) + len(new_updates)}
                self._append(events, "closed", closed)
                self._append(events, "tool_call", {"tool": "analyze_and_plan", "actor": actor, "finalize": True,
                                                    "status": "closed", "result_ids": [u["observed_result_id"] for u in new_updates]})
                return {"status": "closed", "new_updates": new_updates,
                        "next_preview_unrecorded": None if plan is None else {**plan, "recorded": False},
                        **self._public(self._ledger(events))}
            if not record:
                if new_updates:
                    self._append(events, "tool_call", {"tool": "analyze_and_plan", "actor": actor, "finalize": False,
                                                        "status": next_state, "result_ids": [u["observed_result_id"] for u in new_updates]})
                return {"status": "review_cap_reached" if cap else "none_affordable", "new_updates": new_updates,
                        "message": "call analyze_and_plan(finalize=true)", **self._public(self._ledger(events))}
            decision = {"decision_sequence": len(ledger["decisions"]) + 1, "actor": actor, **plan}
            self._append(events, "decision", decision)
            self._append(events, "tool_call", {"tool": "analyze_and_plan", "actor": actor, "finalize": False,
                                                "status": "decision_recorded", "decision_sequence": decision["decision_sequence"],
                                                "result_ids": [u["observed_result_id"] for u in new_updates]})
            return {"status": "decision_recorded", "decision": decision, "new_updates": new_updates,
                    **self._public(self._ledger(events))}

    # ------------------------------------------------------------ experimenter
    def _sensor(self, i: int, attempt: int) -> dict:
        """The only oracle access: one authored review attempt at one admitted site."""
        lot = self.root / "private" / "lot"
        if sha256_file(lot / "oracle.npz") != self.manifest["oracle_sha256"]:
            raise LiveError("private sensor data changed")
        return dict(data.review_observation(_load_npz(lot / "oracle.npz"), int(i), int(attempt)))

    def review_site(self, site_id: str, decision_sequence: int, *, actor: str = "direct_call") -> dict:
        if not isinstance(site_id, str) or site_id not in self.index:
            raise LiveError("denied: unknown site_id; nothing charged")
        if isinstance(decision_sequence, bool) or not isinstance(decision_sequence, int):
            raise LiveError("denied: decision_sequence must be an integer; nothing charged")
        with self._lock():
            events = self._events()
            ledger = self._ledger(events)
            if ledger["closed"]:
                raise LiveError("denied: live session is closed; nothing charged")
            if ledger["incomplete"]:
                raise LiveError("denied: session blocked by an admitted review without outcome; nothing new charged")
            pending = ledger["pending"]
            if pending is None:
                if any(a["decision_sequence"] == decision_sequence for a in ledger["admissions"]):
                    raise LiveError("denied: decision already executed; nothing charged")
                raise LiveError("denied: no recorded decision awaits review; ask the analyst first; nothing charged")
            if pending["decision_sequence"] != decision_sequence or pending["selected_site_id"] != site_id:
                raise LiveError(f"denied: only {pending['selected_site_id']} (decision {pending['decision_sequence']}) "
                                "may be reviewed; nothing charged")
            if len(ledger["admissions"]) >= self.manifest["max_reviews"]:
                raise LiveError("denied: review cap reached; nothing charged")
            state = self._state(ledger)
            i = self.index[site_id]
            cv = cost_vectors(state, self.cost)
            reserved = float(cv["max"][i])
            if not state.allowed()[i] or ledger["spent"] + reserved > self.manifest["budget"] + EPS:
                raise LiveError("denied: reservation exceeds the CU budget; nothing charged")
            breakdown = {"load": float(cv["load"][i]), "stage": float(cv["stage"][i]), "dwell": float(cv["dwell"][i]),
                         "outside_rescan": float(cv["outside"][i]), "retry": 0.0}
            result_id = "ir_" + secrets.token_hex(8)
            self._append(events, "admission", {"result_id": result_id, "site_id": site_id, "decision_sequence": decision_sequence,
                                                "reserved_cost": reserved, "actor": actor,
                                                "spent_before": ledger["spent"]})
            # Admission is durable; any failure from here on leaves the reservation charged.
            attempts = [_clean_obs(self._sensor(i, 0), 0)]
            if not _success(attempts[0]) and int(self.cost["retry_limit"]) >= 1:
                attempts.append(_clean_obs(self._sensor(i, 1), 1))  # same authored retry rule, billed
                breakdown["retry"] = float(self.cost["retry_dwell"])
            charged = sum(breakdown.values())
            if charged > reserved + EPS:
                raise LiveError("charged cost exceeded reservation; session blocked")
            ok = [a for a in attempts if _success(a)]
            last_ok = ok[-1] if ok else None
            reward = self.reward if self.reward is not None else self.frozen_p
            row = {"result_id": result_id, "site_id": site_id, "wafer": _py(self.view.wafer[i]),
                   "decision_sequence": decision_sequence, "actor": actor, "attempts": attempts,
                   "status": "ok" if ok else attempts[-1]["status"],
                   "label": None if last_ok is None else last_ok["reported_doi"],
                   "reported_doi": None if last_ok is None else last_ok["reported_doi"],
                   "reported_kind": None if last_ok is None else last_ok["reported_kind"],
                   "quality": attempts[-1]["quality"], "reported_positive": bool(any(a["reported_doi"] for a in ok)),
                   "baseline_p": float(self.frozen_p[i]), "reported_yield_score": float(self.reported_yield[i]),
                   "selection_reward": float(reward[i]), "selection_reward_semantics": self.reward_semantics,
                   "reserved_cost": reserved, "cost": breakdown, "charged": charged,
                   "cumulative_spend": ledger["spent"] + charged}
            self._append(events, "observation", row)
            self._append(events, "tool_call", {"tool": "review_site", "actor": actor, "status": row["status"],
                                                "decision_sequence": decision_sequence, "result_ids": [result_id]})
            return {"status": "observed", "result": self._result_view(row), "budget": self._public(self._ledger(events))["budget"]}

    # ------------------------------------------------------------ shared
    def read_result(self, result_id: str, *, actor: str = "direct_call") -> dict:
        if not isinstance(result_id, str) or not RESULT_ID.match(result_id):
            raise LiveError("denied: malformed result_id")
        with self._lock():
            ledger = self._ledger(self._events())
        row = next((o for o in ledger["observations"] if o["result_id"] == result_id), None)
        if row is None:
            raise LiveError("denied: unknown or unobserved result_id")
        return self._result_view(row)

    def status(self) -> dict:
        with self._lock():
            return self._public(self._ledger(self._events()))

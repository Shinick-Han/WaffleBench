"""Campaign storage: hash-chained append-only ledger, attempt admission, evidence cache, runs.

Layout of one campaign root (primary, development, demo, reproduce or fixture)::

    <root>/ledger/events.jsonl   append-only, SHA256-chained events (source of truth)
    <root>/ledger/.lock          cross-process exclusive lock
    <root>/evidence/sims/        run-accessible physical results (cache), write-once
    <root>/restricted/sims/      preflight / posthoc / postflight results, never served to runs
    <root>/attempts/<id>/        netlist and simulator log of every physical attempt
    <root>/model.json            frozen model (write-once copy of the model_frozen event)

Information flow rules enforced here:

* Every physical simulator attempt is admitted under the lock against the campaign
  caps (attempts, wall time). Admission is recorded before the simulator starts, so
  crashed, failed and timed-out attempts stay counted.
* A run reveals only what it queried. Logical queries are admitted under the lock
  against the run budget (9 calibration + 3 shared initial + 12 choices = 24; the
  live demonstration run: 9 + 3 + at most 4 choices = 16, complete only when
  finalized after 2..4 successful choices with nothing pending).
  Failures consume budget and are never replaced; repeats consume budget and never
  add a distinct point. Held-out, prior-excluded and calibration points cannot be
  queried in the search phase.
* Cached evidence is reused only when the full fingerprint (measurement schema,
  numerical setting, netlist sha256, simulator version and binary sha256) matches
  and the stored record hash matches the ledger. Otherwise it raises.
* Posthoc truth is simulated only after every run in the campaign is closed, and it
  lives in the restricted store that no run query reads.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import model as model_mod
from .protocol import Point, Protocol, ProtocolError, canonical_json, load_protocol, sha256_bytes
from .simulator import PRIMARY_SETTING, SimulationFailed

CAMPAIGN_KINDS = ("primary", "development", "demo", "reproduce", "fixture")
RUN_KINDS = ("calibration", "benchmark", "live", "reproduce")
RESTRICTED_PURPOSES = ("preflight", "posthoc", "postflight")
GENESIS = "0" * 64
LIVE_FINALIZED = "live_finalized"


class StorageError(RuntimeError):
    pass


class LedgerCorrupt(StorageError):
    pass


class AccessDenied(StorageError):
    pass


class BudgetExhausted(StorageError):
    """The run has no logical query left for this request."""


class CapReached(StorageError):
    """A campaign-wide cap (attempts or wall time) stops further work."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class CacheIntegrityError(StorageError):
    pass


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _write_once(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.link(tmp, path)  # fails if path exists: never overwrite evidence
    except FileExistsError:
        pass
    except OSError:
        if not path.exists():
            os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


@dataclass
class SimOutcome:
    record: dict[str, Any]
    cache_hit: bool
    attempt_id: str | None


class Campaign:
    def __init__(self, root: Path | str, simulator: Any, protocol: Protocol | None = None):
        self.root = Path(root)
        self.sim = simulator
        self.protocol = protocol or load_protocol()
        self._events: list[dict[str, Any]] = []
        self._offset = 0
        self._lock_depth = 0
        self._lock_fh: Any = None
        self._tlock = threading.RLock()  # guards _lock_depth/_events across threads of this instance
        if not self.events_path.exists():
            raise StorageError(f"{self.root} is not a campaign root (no ledger)")
        with self.lock():  # syncs under the thread + process lock
            pass
        if not self._events or self._events[0]["type"] != "campaign_created":
            raise LedgerCorrupt("ledger does not start with campaign_created")
        created = self.info
        # simulator=None opens read-only for export/reporting; simulate() then fails.
        if simulator is not None and created["simulator"] != simulator.banner():
            raise StorageError(
                f"campaign was created with simulator {created['simulator']!r}; refusing to mix with {simulator.banner()!r}"
            )
        if created["protocol_sha256"] != self.protocol.protocol_sha256 or created["manifest_canonical_sha256"] != self.protocol.manifest_canonical_sha256:
            raise StorageError("campaign protocol/manifest hash differs from the current frozen inputs")

    # ------------------------------------------------------------------ setup
    @property
    def events_path(self) -> Path:
        return self.root / "ledger" / "events.jsonl"

    @classmethod
    def create(
        cls,
        root: Path | str,
        kind: str,
        simulator: Any,
        *,
        caps: dict[str, Any] | None = None,
        label: str = "",
        protocol: Protocol | None = None,
    ) -> Campaign:
        protocol = protocol or load_protocol()
        if kind not in CAMPAIGN_KINDS:
            raise StorageError(f"campaign kind must be one of {CAMPAIGN_KINDS}")
        if kind == "fixture" and simulator.scientific:
            raise StorageError("fixture campaigns use the non-scientific fixture simulator")
        if kind != "fixture" and not simulator.scientific:
            raise StorageError("only fixture campaigns may use a non-scientific simulator")
        b = protocol.budget
        default_caps = {
            "attempts_max": int(b["all_attempts_global_max"]),
            "wall_seconds_max": float(b["global_wall_time_minutes"]) * 60.0,
        }
        caps = {**default_caps, **(caps or {})}
        if kind == "primary" and caps != default_caps:
            raise StorageError("primary campaigns use the frozen protocol caps")
        root = Path(root)
        (root / "ledger").mkdir(parents=True, exist_ok=True)
        self = cls.__new__(cls)
        self.root, self.sim, self.protocol = root, simulator, protocol
        self._events, self._offset, self._lock_depth, self._lock_fh = [], 0, 0, None
        self._tlock = threading.RLock()
        with self.lock():
            if self.events_path.exists() and self.events_path.stat().st_size > 0:
                raise StorageError(f"{root} already holds a campaign; use a fresh root")
            self.events_path.touch()
            self._append(
                "campaign_created",
                {
                    "kind": kind,
                    "label": label,
                    "caps": caps,
                    "scientific": bool(simulator.scientific),
                    "simulator": simulator.banner(),
                    "protocol_sha256": protocol.protocol_sha256,
                    "manifest_sha256": protocol.manifest_sha256,
                    "manifest_canonical_sha256": protocol.manifest_canonical_sha256,
                    "primary_setting": PRIMARY_SETTING,
                },
            )
        return cls(root, simulator, protocol)

    @classmethod
    def open_or_create(cls, root: Path | str, kind: str, simulator: Any, **kw: Any) -> Campaign:
        root = Path(root)
        events = root / "ledger" / "events.jsonl"
        if events.exists() and events.stat().st_size > 0:
            camp = cls(root, simulator, kw.get("protocol"))
            if camp.kind != kind:
                raise StorageError(f"{root} is a {camp.kind!r} campaign, not {kind!r}")
            return camp
        return cls.create(root, kind, simulator, **kw)

    @property
    def info(self) -> dict[str, Any]:
        return self._events[0]["payload"]

    @property
    def kind(self) -> str:
        return self.info["kind"]

    @property
    def caps(self) -> dict[str, Any]:
        return self.info["caps"]

    @property
    def created_unix(self) -> float:
        return float(self._events[0]["unix"])

    # ------------------------------------------------------------------ lock + ledger
    @contextlib.contextmanager
    def lock(self, timeout_s: float = 300.0) -> Iterator[None]:
        """Thread lock (RLock) plus cross-process file lock. Reentrant only for the owning thread."""
        if not self._tlock.acquire(timeout=timeout_s):
            raise StorageError("timed out waiting for the campaign thread lock")
        try:
            with self._process_lock(timeout_s):
                yield
        finally:
            self._tlock.release()

    @contextlib.contextmanager
    def _process_lock(self, timeout_s: float) -> Iterator[None]:
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        (self.root / "ledger").mkdir(parents=True, exist_ok=True)
        fh = open(self.root / "ledger" / ".lock", "a+b")
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    fh.close()
                    raise StorageError("timed out waiting for the campaign lock")
                time.sleep(0.01)
        self._lock_depth = 1
        try:
            self._sync()
            yield
        finally:
            self._lock_depth = 0
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    @staticmethod
    def _event_hash(ev: dict[str, Any]) -> str:
        return sha256_bytes(canonical_json({k: v for k, v in ev.items() if k != "hash"}).encode("utf-8"))

    def _sync(self) -> None:
        if not self.events_path.exists():
            return
        with open(self.events_path, "rb") as fh:
            fh.seek(self._offset)
            data = fh.read()
        cut = data.rfind(b"\n") + 1
        data = data[:cut]
        for raw in data.splitlines():
            try:
                ev = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LedgerCorrupt(f"unparseable ledger line after seq {len(self._events)}") from exc
            prev = self._events[-1]["hash"] if self._events else GENESIS
            if ev.get("seq") != len(self._events) + 1 or ev.get("prev_hash") != prev or ev.get("hash") != self._event_hash(ev):
                raise LedgerCorrupt(f"ledger hash chain broken at seq {ev.get('seq')}")
            self._events.append(ev)
        self._offset += len(data)

    def _append(self, etype: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._lock_depth:
            raise StorageError("ledger append requires the campaign lock")
        self._sync()
        size = self.events_path.stat().st_size
        if size != self._offset:
            raise LedgerCorrupt("ledger has a partial trailing line; refusing to append")
        ev = {
            "seq": len(self._events) + 1,
            "ts": iso_now(),
            "unix": time.time(),
            "type": etype,
            "payload": payload,
            "prev_hash": self._events[-1]["hash"] if self._events else GENESIS,
        }
        ev["hash"] = self._event_hash(ev)
        with open(self.events_path, "ab") as fh:
            fh.write((canonical_json(ev) + "\n").encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        self._sync()
        return ev

    def append(self, etype: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock():
            return self._append(etype, payload)

    def events(self, etype: str | None = None) -> list[dict[str, Any]]:
        with self.lock():  # never observe a partial append or uncommitted state
            return [e for e in self._events if etype is None or e["type"] == etype]

    def verify_ledger(self) -> int:
        """Re-read the whole ledger from disk and verify the chain. Returns the event count."""
        fresh = Campaign.__new__(Campaign)
        fresh.root, fresh._events, fresh._offset, fresh._lock_depth = self.root, [], 0, 0
        fresh._tlock = threading.RLock()
        with self.lock():
            fresh._sync()
        return len(fresh._events)

    # ------------------------------------------------------------------ caps
    def elapsed_s(self) -> float:
        return time.time() - self.created_unix

    def _cap_check(self, need_attempt: bool) -> None:
        reason = None
        if self.elapsed_s() >= float(self.caps["wall_seconds_max"]):
            reason = "wall_time_cap"
        elif need_attempt and len([e for e in self._events if e["type"] == "attempt_admitted"]) >= int(self.caps["attempts_max"]):
            reason = "attempt_cap"
        if reason:
            if not any(e["type"] == "cap_reached" and e["payload"]["reason"] == reason for e in self._events):
                self._append("cap_reached", {"reason": reason, "elapsed_s": self.elapsed_s()})
            raise CapReached(reason, f"campaign {reason.replace('_', ' ')} reached; stop and report")

    def check_caps(self) -> None:
        with self.lock():
            self._cap_check(need_attempt=False)

    # ------------------------------------------------------------------ physical evidence
    def _sim_id(self, point: Point, fp: dict[str, Any]) -> str:
        return "sim_" + sha256_bytes(canonical_json({"point_id": point.id, **fp}).encode("utf-8"))[:20]

    def _store(self, restricted: bool) -> Path:
        return self.root / ("restricted" if restricted else "evidence") / "sims"

    def _ledger_record_hash(self, sim_id: str, restricted: bool) -> str | None:
        for e in self._events:
            p = e["payload"]
            if e["type"] == "attempt_finished" and p.get("sim_id") == sim_id and p["status"] == "success" and p.get("restricted") == restricted:
                return e["payload"]["record_sha256"]
        return None

    def _read_cached(self, restricted: bool, sim_id: str, fp: dict[str, Any], point: Point) -> dict[str, Any] | None:
        path = self._store(restricted) / f"{sim_id}.json"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        rec = json.loads(text)
        if rec.get("sim_id") != sim_id or rec.get("point_id") != point.id or rec.get("fingerprint") != fp:
            raise CacheIntegrityError(f"cached evidence {sim_id} has an incompatible fingerprint; refusing reuse")
        if self._ledger_record_hash(sim_id, restricted) != sha256_bytes(text.encode("utf-8")):
            raise CacheIntegrityError(f"cached evidence {sim_id} is not bound to this campaign ledger; refusing reuse")
        return rec

    def lookup_cached(self, point: Point, setting: str, *, restricted: bool) -> dict[str, Any] | None:
        fp = self.sim.fingerprint(point, setting)
        sim_id = self._sim_id(point, fp)
        stores = (False, True) if restricted else (False,)
        # Under the lock: a sim file and its attempt_finished hash are committed in one locked block.
        with self.lock():
            for store in stores:
                rec = self._read_cached(store, sim_id, fp, point)
                if rec is not None:
                    return rec
        return None

    def simulate(self, point: Point, setting: str, *, purpose: str, run_id: str | None = None) -> SimOutcome:
        """Return cached evidence with an identical fingerprint, else admit and run one attempt."""
        if self.sim is None:
            raise StorageError("campaign opened read-only (no simulator)")
        restricted = purpose in RESTRICTED_PURPOSES
        fp = self.sim.fingerprint(point, setting)
        sim_id = self._sim_id(point, fp)
        cached = self.lookup_cached(point, setting, restricted=restricted)
        if cached is not None:
            return SimOutcome(cached, True, None)
        with self.lock():
            self._cap_check(need_attempt=True)
            attempt_id = f"att_{len([e for e in self._events if e['type'] == 'attempt_admitted']) + 1:05d}"
            self._append(
                "attempt_admitted",
                {"attempt_id": attempt_id, "sim_id": sim_id, "point_id": point.id, "setting": setting, "purpose": purpose, "run_id": run_id},
            )
        workdir = self.root / "attempts" / attempt_id
        try:
            meas = self.sim.run(point, setting, workdir)
        except SimulationFailed as exc:
            workdir.mkdir(parents=True, exist_ok=True)
            (workdir / "failure.txt").write_text(f"{exc}\n{exc.log}", encoding="utf-8", newline="\n")
            with self.lock():
                self._append(
                    "attempt_finished",
                    {"attempt_id": attempt_id, "sim_id": sim_id, "status": exc.status, "error": str(exc), "wall_time_s": exc.wall_time_s},
                )
            raise
        except Exception as exc:  # unexpected backend error: retain the attempt, then surface it
            with self.lock():
                self._append(
                    "attempt_finished",
                    {"attempt_id": attempt_id, "sim_id": sim_id, "status": "error", "error": f"{type(exc).__name__}: {exc}", "wall_time_s": 0.0},
                )
            raise SimulationFailed(f"simulator error: {exc}", status="error") from exc
        record = {
            "kind": "simulation" if self.sim.scientific else "NON-SCIENTIFIC FIXTURE simulation",
            "sim_id": sim_id,
            "point_id": point.id,
            "pvt": point.pvt,
            "setting": setting,
            "fingerprint": fp,
            "tphl_s": meas.tphl_s,
            "tplh_s": meas.tplh_s,
            "tpd_s": meas.tpd_s,
            "raw_meas_lines": meas.raw_meas_lines,
            "wall_time_s": round(meas.wall_time_s, 6),
            "attempt_id": attempt_id,
            "netlist_file": f"attempts/{attempt_id}/netlist.cir",
        }
        text = json.dumps(record, indent=2, sort_keys=True)
        path = self._store(restricted) / f"{sim_id}.json"
        with self.lock():
            if path.exists():
                # A concurrent process produced the same fingerprint first; keep its record.
                self._append("attempt_finished", {"attempt_id": attempt_id, "sim_id": None, "status": "duplicate_discarded", "wall_time_s": meas.wall_time_s, "tpd_s": meas.tpd_s})
                return SimOutcome(self._read_cached(restricted, sim_id, fp, point), False, attempt_id)
            _write_once(path, text)
            self._append(
                "attempt_finished",
                {"attempt_id": attempt_id, "sim_id": sim_id, "status": "success", "restricted": restricted, "wall_time_s": meas.wall_time_s, "record_sha256": sha256_bytes(text.encode("utf-8"))},
            )
        return SimOutcome(record, False, attempt_id)

    # ------------------------------------------------------------------ model
    def model(self) -> model_mod.FrozenModel | None:
        evs = self.events("model_frozen")
        if not evs:
            return None
        m = model_mod.FrozenModel.from_dict(evs[0]["payload"]["model"])
        if m.protocol_sha256 != self.protocol.protocol_sha256:
            raise StorageError("frozen model belongs to another protocol")
        return m

    def freeze_model(self, model: model_mod.FrozenModel) -> None:
        with self.lock():
            if any(e["type"] == "model_frozen" for e in self._events):
                raise StorageError("model already frozen in this campaign; scientific decisions are not overwritten")
            _write_once(self.root / "model.json", json.dumps(model.to_dict(), indent=2, sort_keys=True))
            self._append("model_frozen", {"model": model.to_dict()})

    # ------------------------------------------------------------------ runs
    def run_ids(self, run_kind: str | None = None) -> list[str]:
        return [e["payload"]["run_id"] for e in self.events("run_opened") if run_kind is None or e["payload"]["run_kind"] == run_kind]

    def open_run(self, run_kind: str, policy: str | None = None, seed: int | None = None) -> str:
        if run_kind not in RUN_KINDS:
            raise StorageError(f"run kind must be one of {RUN_KINDS}")
        b = self.protocol.budget
        quotas = {"calibration": int(b["calibration"]), "initial": int(b["shared_initial_search"]), "search": int(b["subsequent_search"])}
        extra: dict[str, Any] = {}
        if run_kind == "calibration":
            run_id, budget = "calibration", int(b["calibration"])
            quotas = {"calibration": int(b["calibration"])}
        else:
            if policy not in self.protocol.policies:
                raise StorageError(f"policy must be one of {self.protocol.policies}")
            self.protocol.replicate(int(seed))
            run_id, budget = f"{run_kind}-{policy}-{int(seed)}", int(b["per_policy_replicate"])
            if run_kind == "live":
                # Protocol live cap: 9 calibration + 3 shared initial + 2..4 adaptive search queries.
                live = self.protocol.manifest["live_demo"]
                quotas["search"] = int(live["adaptive_updates_max"])
                budget = int(b["live_demo_max"])
                if budget != sum(quotas.values()):
                    raise StorageError("live budget differs from calibration + initial + adaptive maximum")
                extra = {"search_min": int(live["adaptive_updates_min"])}
        with self.lock():
            self._cap_check(need_attempt=False)
            if run_id in self.run_ids():
                raise StorageError(f"run {run_id} already exists; partial runs are never silently resumed")
            if any(e["type"] == "posthoc_started" for e in self._events):
                raise AccessDenied("posthoc evaluation started; no new runs may open in this campaign")
            model = self.model()
            if run_kind != "calibration" and model is None:
                raise StorageError("freeze the model (calibrate) before opening a search run")
            self._append(
                "run_opened",
                {
                    "run_id": run_id,
                    "run_kind": run_kind,
                    "policy": policy,
                    "seed": None if seed is None else int(seed),
                    "model_hash": model.model_hash if model else None,
                    "budget": budget,
                    "quotas": quotas,
                    **extra,
                },
            )
        return run_id

    def run_state(self, run_id: str) -> dict[str, Any]:
        with self.lock():
            return self._run_state(run_id)

    def _run_state(self, run_id: str) -> dict[str, Any]:
        opened = next((e for e in self._events if e["type"] == "run_opened" and e["payload"]["run_id"] == run_id), None)
        if opened is None:
            raise AccessDenied(f"unknown run {run_id!r}")
        st: dict[str, Any] = {**opened["payload"], "opened_at": opened["ts"], "queries": [], "decisions": [], "closed": None}
        by_index: dict[int, dict[str, Any]] = {}
        for e in self._events:
            p = e["payload"]
            if p.get("run_id") != run_id:
                continue
            if e["type"] == "query_admitted":
                q = {**p, "status": "pending", "admitted_at": e["ts"]}
                by_index[p["query_index"]] = q
                st["queries"].append(q)
            elif e["type"] == "query_result":
                by_index[p["query_index"]].update({**p, "completed_at": e["ts"]})
            elif e["type"] == "decision":
                st["decisions"].append({**p, "recorded_at": e["ts"]})
            elif e["type"] == "run_closed":
                st["closed"] = {**p, "closed_at": e["ts"]}
        st["used"] = len(st["queries"])
        st["remaining"] = st["budget"] - st["used"]
        return st

    def _phase_counts(self, st: dict[str, Any]) -> dict[str, int]:
        counts = {"calibration": 0, "initial": 0, "search": 0}
        for q in st["queries"]:
            counts[q["phase"]] += 1
        return counts

    def record_decision(self, run_id: str, decision: dict[str, Any]) -> dict[str, Any]:
        """Append one selection decision before its query. Decisions are never rewritten."""
        with self.lock():
            st = self.run_state(run_id)
            if st["closed"]:
                raise StorageError(f"run {run_id} is closed")
            counts = self._phase_counts(st)
            if counts["calibration"] < 9 or counts["initial"] < 3:
                raise StorageError("decisions start after the calibration and shared initial queries")
            used = {q.get("decision_sequence") for q in st["queries"]}
            if any(d["sequence"] not in used for d in st["decisions"]):
                raise StorageError("previous decision has not been executed yet")
            if counts["search"] >= st["quotas"]["search"] or st["remaining"] <= 0:
                raise BudgetExhausted(f"run {run_id} has no selection left")
            ids = [c["point_id"] for c in decision["candidates"]]
            if decision["selected_point_id"] not in ids:
                raise StorageError("selected point must be one of the recorded candidates")
            payload = {**decision, "run_id": run_id, "sequence": len(st["decisions"]) + 1, "remaining_budget": st["remaining"]}
            self._append("decision", payload)
            return payload

    def query(self, run_id: str, point_id: str, phase: str, decision_sequence: int | None = None) -> dict[str, Any]:
        """Spend one logical query of ``run_id`` on ``point_id``; returns the run-scoped observation."""
        try:
            point = Point.parse(point_id)
        except ProtocolError as exc:
            raise AccessDenied(str(exc)) from exc
        proto = self.protocol
        with self.lock():
            st = self.run_state(run_id)
            if st["closed"]:
                raise StorageError(f"run {run_id} is closed")
            self._cap_check(need_attempt=False)
            if st["remaining"] <= 0:
                raise BudgetExhausted(f"run {run_id} used its {st['budget']} logical queries")
            counts = self._phase_counts(st)
            quotas = st["quotas"]
            if phase not in quotas:
                raise AccessDenied(f"phase {phase!r} is not allowed in a {st['run_kind']} run")
            if counts[phase] >= quotas[phase]:
                raise BudgetExhausted(f"{phase} quota of run {run_id} is used")
            repeat_of = None
            if phase == "calibration":
                expected = proto.training[counts["calibration"]]
                if point != expected:
                    raise AccessDenied(f"next calibration point is {expected.id}")
            elif phase == "initial":
                if counts["calibration"] < 9:
                    raise AccessDenied("finish the nine calibration queries first")
                expected = proto.replicate(st["seed"]).initial[counts["initial"]]
                if point != expected:
                    raise AccessDenied(f"next shared initial point for seed {st['seed']} is {expected.id}")
            else:
                if counts["calibration"] < 9 or counts["initial"] < 3:
                    raise AccessDenied("search choices start after calibration and the shared initial queries")
                part = proto.partition(point)
                if part == "held_out":
                    raise AccessDenied(f"{point.id} is held out; its truth is fetched only by posthoc evaluation")
                if part != "search_pool":
                    raise AccessDenied(f"{point.id} is a {part} point, not a search candidate")
                dec = next((d for d in st["decisions"] if d["sequence"] == decision_sequence), None)
                if dec is None or dec["selected_point_id"] != point.id:
                    raise AccessDenied("a search query must execute a recorded decision for the same point")
                if any(q.get("decision_sequence") == decision_sequence for q in st["queries"]):
                    raise AccessDenied("this decision was already executed")
                prior = [q for q in st["queries"] if q["point_id"] == point.id]
                if any(q["status"] != "success" for q in prior):
                    raise AccessDenied(f"{point.id} failed earlier in this run; failed selections are never replaced")
                if prior:
                    repeat_of = prior[0]
            index = st["used"] + 1
            self._append(
                "query_admitted",
                {"run_id": run_id, "query_index": index, "phase": phase, "point_id": point.id, "decision_sequence": decision_sequence, "repeat": repeat_of is not None},
            )
        result: dict[str, Any] = {"run_id": run_id, "query_index": index, "point_id": point.id, "phase": phase}
        cap_exc: CapReached | None = None
        try:
            if repeat_of is not None:
                out = SimOutcome(self._run_sim_record(repeat_of["sim_id"]), True, None)
            else:
                out = self.simulate(point, PRIMARY_SETTING, purpose=st["run_kind"], run_id=run_id)
            rec = out.record
            model = self.model()
            evaluation = (
                model_mod.evaluate(model, point, rec["tpd_s"], proto.clear_threshold, proto.secondary_threshold) if model else None
            )
            result.update(
                {
                    "status": "success",
                    "result_id": "res_" + sha256_bytes(f"{run_id}|{index}|{rec['sim_id']}".encode("utf-8"))[:16],
                    "sim_id": rec["sim_id"],
                    "cache_hit": out.cache_hit,
                    "repeat": repeat_of is not None,
                    "attempt_id": out.attempt_id,
                    "observation": {k: rec[k] for k in ("pvt", "tphl_s", "tplh_s", "tpd_s", "wall_time_s", "setting")},
                    "evaluation": evaluation,
                }
            )
        except SimulationFailed as exc:
            result.update({"status": "failed", "result_id": None, "error": str(exc), "failure_status": exc.status, "cache_hit": False, "repeat": False})
        except CapReached as exc:
            result.update({"status": "failed", "result_id": None, "error": str(exc), "failure_status": exc.reason, "cache_hit": False, "repeat": False})
            cap_exc = exc
        with self.lock():
            self._append("query_result", result)
        if cap_exc is not None:
            raise cap_exc
        return result

    def _run_sim_record(self, sim_id: str) -> dict[str, Any]:
        path = self._store(False) / f"{sim_id}.json"
        with self.lock():
            text = path.read_text(encoding="utf-8")
            bound = self._ledger_record_hash(sim_id, False)
        if bound != sha256_bytes(text.encode("utf-8")):
            raise CacheIntegrityError(f"evidence {sim_id} does not match the ledger")
        return json.loads(text)

    def close_run(self, run_id: str, termination_reason: str | None = None) -> dict[str, Any]:
        with self.lock():
            st = self.run_state(run_id)
            if st["closed"]:
                raise StorageError(f"run {run_id} already closed")
            all_success = all(q["status"] == "success" for q in st["queries"])
            extra: dict[str, Any] = {}
            if st["run_kind"] == "live":
                # Complete only through an intentional finalization after 2..4 successful
                # search queries with nothing pending; any other stop is incomplete.
                counts = self._phase_counts(st)
                executed = {q.get("decision_sequence") for q in st["queries"]}
                pending_decisions = sum(d["sequence"] not in executed for d in st["decisions"])
                ok = (
                    termination_reason == LIVE_FINALIZED
                    and all_success
                    and pending_decisions == 0
                    and counts["calibration"] == st["quotas"]["calibration"]
                    and counts["initial"] == st["quotas"]["initial"]
                    and st["search_min"] <= counts["search"] <= st["quotas"]["search"]
                )
                extra = {"search_queries": counts["search"], "pending_decisions": pending_decisions, "remaining_budget": st["remaining"]}
            else:
                ok = st["used"] == st["budget"] and all_success
            payload = {
                "run_id": run_id,
                "status": "complete" if ok else "incomplete",
                "termination_reason": termination_reason or ("budget_spent" if ok else "incomplete"),
                "logical_queries": st["used"],
                "failures": sum(q["status"] == "failed" for q in st["queries"]),
                "pending": sum(q["status"] == "pending" for q in st["queries"]),
                **extra,
            }
            self._append("run_closed", payload)
            return payload

    # ------------------------------------------------------------------ run-scoped access (agent API)
    def revealed_result_ids(self, run_id: str) -> list[str]:
        return [q["result_id"] for q in self.run_state(run_id)["queries"] if q.get("result_id")]

    def get_observation(self, run_id: str, result_id: str) -> dict[str, Any]:
        """Return an observation only if ``result_id`` was revealed by a query of ``run_id``."""
        for q in self.run_state(run_id)["queries"]:
            if q.get("result_id") == result_id:
                rec = self._run_sim_record(q["sim_id"])
                return {
                    "result_id": result_id,
                    "run_id": run_id,
                    "point_id": q["point_id"],
                    "phase": q["phase"],
                    "pvt": rec["pvt"],
                    "tphl_s": rec["tphl_s"],
                    "tplh_s": rec["tplh_s"],
                    "tpd_s": rec["tpd_s"],
                    "cache_hit": q["cache_hit"],
                    "repeat": q["repeat"],
                    "wall_time_s": rec["wall_time_s"],
                    "evaluation": q.get("evaluation"),
                    "provenance": {
                        "sim_id": rec["sim_id"],
                        "netlist_sha256": rec["fingerprint"]["netlist_sha256"],
                        "simulator": rec["fingerprint"]["simulator"],
                        "setting": rec["setting"],
                        "raw_meas_lines": rec["raw_meas_lines"],
                    },
                }
        raise AccessDenied(f"result {result_id!r} was not revealed in run {run_id!r}")

    # ------------------------------------------------------------------ restricted evaluations
    def begin_posthoc(self) -> None:
        with self.lock():
            if any(e["type"] == "posthoc_started" for e in self._events):
                return
            if self.model() is None:
                raise StorageError("no frozen model")
            open_runs = [r for r in self.run_ids() if self.run_state(r)["closed"] is None]
            if open_runs:
                raise AccessDenied(f"held-out truth is fetched only after every sequence closes; open: {open_runs}")
            self._append("posthoc_started", {"closed_runs": self.run_ids()})

    def restricted_observe(self, purpose: str, point: Point, setting: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        if purpose not in RESTRICTED_PURPOSES:
            raise StorageError(f"restricted purpose must be one of {RESTRICTED_PURPOSES}")
        if purpose in ("posthoc", "postflight") and not self.events("posthoc_started"):
            raise AccessDenied("posthoc evaluation has not started; close every run first")
        payload: dict[str, Any] = {"point_id": point.id, "setting": setting, **(extra or {})}
        try:
            out = self.simulate(point, setting, purpose=purpose)
            rec = out.record
            payload.update(
                {"status": "success", "sim_id": rec["sim_id"], "cache_hit": out.cache_hit, "attempt_id": out.attempt_id,
                 "tphl_s": rec["tphl_s"], "tplh_s": rec["tplh_s"], "tpd_s": rec["tpd_s"], "wall_time_s": rec["wall_time_s"]}
            )
            if purpose == "posthoc":
                m = self.model()
                payload["evaluation"] = model_mod.evaluate(m, point, rec["tpd_s"], self.protocol.clear_threshold, self.protocol.secondary_threshold)
        except SimulationFailed as exc:
            payload.update({"status": "failed", "error": str(exc), "failure_status": exc.status})
        self.append(f"{purpose}_result", payload)
        return payload

    # ------------------------------------------------------------------ cost
    def cost(self) -> dict[str, Any]:
        ev = self.events()
        admitted = [e["payload"] for e in ev if e["type"] == "attempt_admitted"]
        finished = {e["payload"]["attempt_id"]: e["payload"] for e in ev if e["type"] == "attempt_finished"}
        successes = sum(1 for f in finished.values() if f["status"] in ("success", "duplicate_discarded"))
        failures = sum(1 for f in finished.values() if f["status"] not in ("success", "duplicate_discarded"))
        unfinished = sum(1 for a in admitted if a["attempt_id"] not in finished)
        q_results = [e["payload"] for e in ev if e["type"] == "query_result"]
        r_results = [e["payload"] for e in ev if e["type"] in ("preflight_result", "posthoc_result", "postflight_result")]
        by_purpose: dict[str, int] = {}
        for a in admitted:
            by_purpose[a["purpose"]] = by_purpose.get(a["purpose"], 0) + 1
        return {
            "logical_queries": len([e for e in ev if e["type"] == "query_admitted"]),
            "attempts": len(admitted),
            "successes": successes,
            "failures": failures + unfinished,
            "unfinished_attempts": unfinished,
            "cache_hits": sum(1 for r in q_results if r.get("cache_hit")),
            "restricted_cache_hits": sum(1 for r in r_results if r.get("cache_hit")),
            "simulation_seconds": round(sum(float(f.get("wall_time_s") or 0.0) for f in finished.values()), 6),
            "policy_seconds": round(sum(float(e["payload"].get("policy_seconds") or 0.0) for e in ev if e["type"] == "decision"), 6),
            "wall_seconds": round((ev[-1]["unix"] - ev[0]["unix"]) if ev else 0.0, 6),
            "attempts_by_purpose": by_purpose,
            "caps": self.caps,
            "campaign_kind": self.kind,
        }

"""Frozen research inputs: RESEARCH_PROTOCOL.md and research-manifest.json.

This module only *reads* the frozen files. Partitions, initial queries, random
orders, thresholds and policy parameters come from the manifest and are
re-derived from the documented SHA256 algorithms as an integrity check. Nothing
here is tuned from results.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PROTOCOL_PATH = ROOT / "RESEARCH_PROTOCOL.md"
MANIFEST_PATH = ROOT / "research-manifest.json"

# Synthetic process conditions (NMOS dVt [V], NMOS KP scale, PMOS |dVt| [V], PMOS KP scale).
# Identical to the connection-test definition in experiment.CORNERS, as the protocol requires.
CORNERS: dict[str, tuple[float, float, float, float]] = {
    "TT": (0.00, 1.00, 0.00, 1.00),
    "FF": (-0.05, 1.10, -0.05, 1.10),
    "SS": (0.05, 0.90, 0.05, 0.90),
    "FS": (-0.05, 1.10, 0.05, 0.90),
    "SF": (0.05, 0.90, -0.05, 1.10),
}
GRID_VDD = (1.2, 1.5, 1.8, 2.2, 2.5, 2.9, 3.3)
GRID_TEMP = (-40.0, 0.0, 27.0, 85.0, 125.0)
VDD_SPAN = (1.2, 3.3)
TEMP_SPAN = (-40.0, 125.0)
DVT_SPAN = (-0.05, 0.05)
LOW_VDD_MAX = 1.8
HIGH_VDD_MIN = 2.9

HOLDOUT_PREFIX = "falsify-v1-holdout|20261004|"
ORDER_PREFIX = "falsify-v1-order|"

PREFLIGHT_POINT_IDS = (
    "TT|2.500|27.0",
    "TT|1.200|-40.0",
    "SS|1.200|125.0",
    "FF|3.300|-40.0",
    "FS|1.800|85.0",
)


class ProtocolError(RuntimeError):
    """The frozen inputs are missing, altered or internally inconsistent."""


def point_id(corner: str, vdd: float, temp_c: float) -> str:
    return f"{corner}|{vdd:.3f}|{temp_c:.1f}"


@dataclass(frozen=True, order=True)
class Point:
    """A grid point. Ordering is the protocol tie order: corner, VDD, temperature."""

    corner: str
    vdd: float
    temp_c: float

    @property
    def id(self) -> str:
        return point_id(self.corner, self.vdd, self.temp_c)

    @property
    def pvt(self) -> dict[str, Any]:
        return {"corner": self.corner, "vdd": self.vdd, "temp_c": self.temp_c}

    @property
    def dvtn(self) -> float:
        return CORNERS[self.corner][0]

    @property
    def dvtp(self) -> float:
        return CORNERS[self.corner][2]

    @property
    def coords(self) -> tuple[float, float, float, float]:
        """Four axes (NMOS dVt, PMOS dVt, VDD, temperature), each scaled to 0..1."""
        return (
            (self.dvtn - DVT_SPAN[0]) / (DVT_SPAN[1] - DVT_SPAN[0]),
            (self.dvtp - DVT_SPAN[0]) / (DVT_SPAN[1] - DVT_SPAN[0]),
            (self.vdd - VDD_SPAN[0]) / (VDD_SPAN[1] - VDD_SPAN[0]),
            (self.temp_c - TEMP_SPAN[0]) / (TEMP_SPAN[1] - TEMP_SPAN[0]),
        )

    @classmethod
    def parse(cls, pid: str) -> Point:
        try:
            corner, vdd, temp = str(pid).split("|")
            pt = cls(corner, float(vdd), float(temp))
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"malformed point id {pid!r}") from exc
        if corner not in CORNERS or pt.vdd not in GRID_VDD or pt.temp_c not in GRID_TEMP or pt.id != pid:
            raise ProtocolError(f"point id {pid!r} is not on the frozen grid")
        return pt


def distance(a: Point, b: Point) -> float:
    return math.dist(a.coords, b.coords)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def full_grid() -> list[Point]:
    return sorted(Point(c, v, t) for c in CORNERS for v in GRID_VDD for t in GRID_TEMP)


@dataclass(frozen=True)
class Replicate:
    seed: int
    initial: tuple[Point, ...]
    random_order: tuple[str, ...]


@dataclass(frozen=True)
class Protocol:
    protocol_sha256: str
    manifest_sha256: str
    manifest_canonical_sha256: str
    manifest: dict[str, Any]
    training: tuple[Point, ...]
    prior_excluded: tuple[Point, ...]
    held_out: tuple[Point, ...]
    search_pool: tuple[Point, ...]
    replicates: tuple[Replicate, ...]
    policies: tuple[str, ...]
    clear_threshold: float
    secondary_threshold: float
    preflight_max_rel_diff: float

    def replicate(self, seed: int) -> Replicate:
        for rep in self.replicates:
            if rep.seed == seed:
                return rep
        raise ProtocolError(f"seed {seed} is not a frozen replicate")

    def partition(self, point: Point) -> str:
        pid = point.id
        if pid in self._ids("training"):
            return "training"
        if pid in self._ids("held_out"):
            return "held_out"
        if pid in self._ids("prior_excluded"):
            return "prior_excluded"
        if pid in self._ids("search_pool"):
            return "search_pool"
        raise ProtocolError(f"{pid} is not on the grid")

    def _ids(self, name: str) -> frozenset[str]:
        return frozenset(p.id for p in getattr(self, name))

    @property
    def budget(self) -> dict[str, Any]:
        return self.manifest["budget"]

    @property
    def policy_parameters(self) -> dict[str, Any]:
        return self.manifest["policy_parameters"]

    @property
    def primary_analysis(self) -> dict[str, Any]:
        return self.manifest["primary_analysis"]


def _points(rows: list[dict[str, Any]]) -> tuple[Point, ...]:
    out = []
    for row in rows:
        pt = Point(row["corner"], float(row["vdd"]), float(row["temp_c"]))
        if pt.id != row["id"] or Point.parse(row["id"]) != pt:
            raise ProtocolError(f"manifest row {row} is inconsistent")
        out.append(pt)
    return tuple(out)


def _verify(proto: Protocol) -> None:
    m = proto.manifest
    if proto.protocol_sha256 != m["protocol_sha256"]:
        raise ProtocolError("RESEARCH_PROTOCOL.md hash does not match the manifest")
    grid = full_grid()
    counts = m["dataset_counts"]
    sets = [proto.training, proto.prior_excluded, proto.held_out, proto.search_pool]
    if [len(s) for s in sets] != [counts["training"], counts["prior_observed_excluded"], counts["held_out"], counts["search_pool"]]:
        raise ProtocolError("manifest partition sizes differ from dataset_counts")
    all_ids = [p.id for s in sets for p in s]
    if len(set(all_ids)) != len(all_ids) or set(all_ids) != {p.id for p in grid} or len(grid) != counts["grid"]:
        raise ProtocolError("manifest partitions do not tile the 175-point grid")
    # Held-out split: per corner, sort eligible ids by sha256(prefix+id), first eight.
    excluded = {p.id for p in proto.training} | {p.id for p in proto.prior_excluded}
    expected_held = set()
    for corner in CORNERS:
        eligible = [p.id for p in grid if p.corner == corner and p.id not in excluded]
        eligible.sort(key=lambda pid: sha256_bytes((HOLDOUT_PREFIX + pid).encode("utf-8")))
        expected_held.update(eligible[:8])
    if expected_held != {p.id for p in proto.held_out}:
        raise ProtocolError("held-out partition does not follow the frozen SHA256 split")
    pool_ids = sorted(p.id for p in proto.search_pool)
    seeds = [r.seed for r in proto.replicates]
    if seeds != list(range(1001, 1011)):
        raise ProtocolError("replicate seeds must be 1001..1010")
    for rep in proto.replicates:
        order = sorted(pool_ids, key=lambda pid: sha256_bytes(f"{ORDER_PREFIX}{rep.seed}|{pid}".encode("utf-8")))
        if list(rep.random_order) != order:
            raise ProtocolError(f"random order for seed {rep.seed} does not follow the frozen SHA256 order")
        if [p.id for p in rep.initial] != order[:3]:
            raise ProtocolError(f"initial queries for seed {rep.seed} are not the first three of the order")
    th = m["thresholds"]
    if (th["primary_clear_counterexample_abs_relative_error_strictly_greater_than"], th["secondary_error_strictly_greater_than"]) != (0.11, 0.1):
        raise ProtocolError("thresholds differ from the frozen protocol")
    b = m["budget"]
    if (b["per_policy_replicate"], b["calibration"], b["search"], b["shared_initial_search"], b["subsequent_search"]) != (24, 9, 15, 3, 12):
        raise ProtocolError("budget differs from the frozen protocol")
    pp = m["policy_parameters"]
    if (pp["idw_power"], pp["idw_epsilon"], pp["exploration_weight"], pp["distance_divisor"]) != (2, 1e-9, 0.05, 2):
        raise ProtocolError("policy parameters differ from the frozen protocol")
    pa = m["primary_analysis"]
    if (pa["bootstrap_replicates"], pa["bootstrap_seed"], pa["interval"], pa["quantile_method"], pa["complete_paired_seeds_required"], pa["practical_mean_gain_min"]) != (10000, 20261004, [0.025, 0.975], "linear", 10, 2):
        raise ProtocolError("primary analysis parameters differ from the frozen protocol")
    if not set(PREFLIGHT_POINT_IDS) <= {p.id for p in grid}:
        raise ProtocolError("preflight points are off grid")


@lru_cache(maxsize=4)
def _load(protocol_path: str, manifest_path: str) -> Protocol:
    try:
        proto_bytes = Path(protocol_path).read_bytes()
        manifest_bytes = Path(manifest_path).read_bytes()
    except OSError as exc:
        raise ProtocolError(f"frozen research inputs are missing: {exc}") from exc
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    th = manifest["thresholds"]
    proto = Protocol(
        protocol_sha256=sha256_bytes(proto_bytes),
        manifest_sha256=sha256_bytes(manifest_bytes),
        manifest_canonical_sha256=sha256_bytes(canonical_json(manifest).encode("utf-8")),
        manifest=manifest,
        training=_points(manifest["training_points"]),
        prior_excluded=_points(manifest["prior_observed_excluded"]),
        held_out=_points(manifest["held_out_points"]),
        search_pool=_points(manifest["search_pool"]),
        replicates=tuple(
            Replicate(int(r["seed"]), _points(r["initial_queries"]), tuple(r["random_order_ids"]))
            for r in manifest["replicates"]
        ),
        policies=tuple(manifest["policies"]),
        clear_threshold=float(th["primary_clear_counterexample_abs_relative_error_strictly_greater_than"]),
        secondary_threshold=float(th["secondary_error_strictly_greater_than"]),
        preflight_max_rel_diff=float(th["preflight_delay_relative_difference_max"]),
    )
    _verify(proto)
    return proto


def load_protocol(protocol_path: Path | None = None, manifest_path: Path | None = None) -> Protocol:
    """Load and verify the frozen protocol and manifest (cached per path)."""
    return _load(str(protocol_path or PROTOCOL_PATH), str(manifest_path or MANIFEST_PATH))

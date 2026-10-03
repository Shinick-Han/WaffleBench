"""Deterministic local experiment functions.

Every numeric value an agent sees comes from one of these functions:

* ``simulate_pvt_point`` runs ngspice on a fixed CMOS inverter netlist and
  parses the ``.meas`` output. Results are content-addressed by their inputs and
  cached on disk with provenance (netlist sha256, ngspice banner, raw lines).
* ``evaluate_surrogate`` loads a *stored* simulation result by id and compares
  it with a pre-registered analytic surrogate. It never accepts numbers from the
  caller, so an LLM cannot inject evidence.
* ``propose_next_point`` ranks unexplored PVT points from the stored evaluations
  with a fixed, documented rule and writes a handoff file.
* ``plumbing_smoke`` is a tiny deterministic computation used only to prove the
  tool path works. Its output is labelled PLUMBING_SMOKE and is not science.

Device models are generic textbook SPICE Level-1 cards, not a foundry PDK.

LEGACY CONNECTION-TEST PATH. The voltage-only one-point surrogate and the single
10% judgement below are kept only for the existing smoke/MCP entry points. The
research path (protocol, model, storage, policies, benchmark, reporting, cli)
never imports this module's surrogate; it uses the frozen five-coefficient PVT
model in ``falsify_lab.model``. See CORE_API.md.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RUNS_DIR = Path(os.environ.get("FALSIFY_RUNS_DIR", ROOT / "runs"))
NGSPICE = Path(
    os.environ.get(
        "FALSIFY_NGSPICE",
        ROOT / "tools" / "ngspice-47" / "Spice64" / "bin" / "ngspice_con.exe",
    )
)

SCHEMA_VERSION = 1
NGSPICE_TIMEOUT_S = 60
MAX_SIMULATIONS = 24  # hard budget per runs/ directory

# --- Experiment domain (pre-registered) -------------------------------------

# Corner -> (NMOS dVt [V], NMOS KP scale, PMOS |dVt| [V], PMOS KP scale).
# Positive dVt means a slower (higher |Vt|) device.
CORNERS: dict[str, tuple[float, float, float, float]] = {
    "TT": (0.00, 1.00, 0.00, 1.00),
    "FF": (-0.05, 1.10, -0.05, 1.10),
    "SS": (0.05, 0.90, 0.05, 0.90),
    "FS": (-0.05, 1.10, 0.05, 0.90),
    "SF": (0.05, 0.90, -0.05, 1.10),
}
VDD_RANGE = (1.2, 3.3)
TEMP_RANGE = (-40.0, 125.0)
GRID_VDD = (1.2, 1.5, 1.8, 2.2, 2.5, 2.9, 3.3)
GRID_TEMP = (-40.0, 0.0, 27.0, 85.0, 125.0)

CALIBRATION_POINT = ("TT", 2.5, 27.0)

# Pre-registered surrogate: alpha-power-law delay, temperature- and corner-blind.
#   tpd_hat(VDD) = K * VDD / (VDD - VT_EFF) ** ALPHA
# K is fitted once from the simulated calibration point; nothing else is tuned.
SURROGATE_ALPHA = 1.3
SURROGATE_VT_EFF = 0.55
FAIL_THRESHOLD = 0.10  # |relative error| above this counts as a surrogate failure

NETLIST_TEMPLATE = """* falsify-lab CMOS inverter delay (generic Level-1 models, not a PDK)
.param vdd={vdd}
.param dvtn={dvtn} kpn_s={kpn_s} dvtp={dvtp} kpp_s={kpp_s}
.temp {temp}
.options tnom=27
.model nch nmos level=1 vto={{0.50+dvtn}} kp={{170u*kpn_s}} gamma=0.4 phi=0.7 lambda=0.05
+ tox=7.6n cgso=0.3n cgdo=0.3n cj=0.9m cjsw=0.25n
.model pch pmos level=1 vto={{-0.60-dvtp}} kp={{60u*kpp_s}} gamma=0.5 phi=0.7 lambda=0.08
+ tox=7.6n cgso=0.3n cgdo=0.3n cj=1.0m cjsw=0.3n
vsup vdd 0 {{vdd}}
vin in 0 pulse(0 {{vdd}} 1n 50p 50p 2n 4n)
mn out in 0 0 nch w=1u l=0.35u ad=1p as=1p pd=4u ps=4u
mp out in vdd vdd pch w=2.5u l=0.35u ad=2.5p as=2.5p pd=7u ps=7u
cl out 0 20f
.tran 1p 6n
.control
run
meas tran tphl trig v(in) val={half} rise=1 targ v(out) val={half} fall=1
meas tran tplh trig v(in) val={half} fall=1 targ v(out) val={half} rise=1
echo FALSIFY_DONE
quit
.endc
.end
"""


class ExperimentError(ValueError):
    """Raised for invalid inputs or a failed simulation."""


@dataclass(frozen=True)
class PVT:
    corner: str
    vdd: float
    temp_c: float

    @classmethod
    def checked(cls, corner: str, vdd: float, temp_c: float) -> PVT:
        corner = str(corner).upper().strip()
        if corner not in CORNERS:
            raise ExperimentError(f"corner must be one of {sorted(CORNERS)}, got {corner!r}")
        vdd = round(float(vdd), 3)
        temp_c = round(float(temp_c), 1)
        if not (VDD_RANGE[0] <= vdd <= VDD_RANGE[1]):
            raise ExperimentError(f"vdd must be within {VDD_RANGE} V, got {vdd}")
        if not (TEMP_RANGE[0] <= temp_c <= TEMP_RANGE[1]):
            raise ExperimentError(f"temp_c must be within {TEMP_RANGE} C, got {temp_c}")
        return cls(corner, vdd, temp_c)

    @property
    def result_id(self) -> str:
        """Cache identity: schema + simulator banner + full rendered netlist (which
        embeds the PVT point and the model cards). Editing models, netlist, or the
        simulator binary therefore yields a new id instead of reusing a stale result."""
        key = f"v{SCHEMA_VERSION}|{ngspice_banner()}|{render_netlist(self)}"
        return "sim_" + hashlib.sha256(key.encode()).hexdigest()[:12]


# --- storage helpers ---------------------------------------------------------


def _dir(name: str) -> Path:
    path = RUNS_DIR / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_all(name: str) -> list[dict[str, Any]]:
    return [_read_json(p) for p in sorted(_dir(name).glob("*.json"))]


def load_result(result_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"sim_[0-9a-f]{12}", str(result_id)):
        raise ExperimentError(f"malformed result_id {result_id!r}")
    path = _dir("results") / f"{result_id}.json"
    if not path.exists():
        raise ExperimentError(f"no stored simulation result {result_id!r}; run simulate_pvt_point first")
    return _read_json(path)


# --- plumbing smoke ----------------------------------------------------------


def plumbing_smoke(n: int = 10) -> dict[str, Any]:
    """Sum of squares 1..n. Proves the tool path; NOT a scientific result."""
    n = int(n)
    if not 1 <= n <= 1000:
        raise ExperimentError("n must be in [1, 1000]")
    return {
        "label": "PLUMBING_SMOKE_NOT_SCIENTIFIC",
        "n": n,
        "sum_of_squares": sum(i * i for i in range(1, n + 1)),
        "closed_form": n * (n + 1) * (2 * n + 1) // 6,
    }


# --- simulation ----------------------------------------------------------------


_MEAS_RE = re.compile(r"^\s*(tphl|tplh)\s*=\s*([-+0-9.eE]+)", re.MULTILINE)


_BANNER: str | None = None


def ngspice_banner() -> str:
    """Version line plus binary sha256, read once per process."""
    global _BANNER
    if _BANNER is not None:
        return _BANNER
    if not NGSPICE.exists():
        raise ExperimentError(f"ngspice not found at {NGSPICE}")
    proc = subprocess.run(
        [str(NGSPICE), "--version"], capture_output=True, text=True, timeout=30, check=False
    )
    version = next((l.strip("* ").strip() for l in proc.stdout.splitlines() if "ngspice-" in l), None)
    if version is None:
        raise ExperimentError("could not read ngspice version banner")
    binary_sha = hashlib.sha256(NGSPICE.read_bytes()).hexdigest()
    _BANNER = f"{version} [sha256 {binary_sha[:16]}]"
    return _BANNER


@contextlib.contextmanager
def _runs_lock(timeout_s: float = 120.0) -> Iterator[None]:
    """Cross-process exclusive lock on RUNS_DIR/.lock (several MCP server
    processes may share one runs dir), so the budget check, the simulation and
    the result write happen atomically."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RUNS_DIR / ".lock", "a+b") as fh:
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
                    raise ExperimentError("timed out waiting for the runs-dir lock")
                time.sleep(0.2)
        try:
            yield
        finally:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def render_netlist(pvt: PVT) -> str:
    dvtn, kpn_s, dvtp, kpp_s = CORNERS[pvt.corner]
    return NETLIST_TEMPLATE.format(
        vdd=f"{pvt.vdd:.3f}",
        half=f"{pvt.vdd / 2:.4f}",
        temp=f"{pvt.temp_c:.1f}",
        dvtn=dvtn,
        kpn_s=kpn_s,
        dvtp=dvtp,
        kpp_s=kpp_s,
    )


def simulate_pvt_point(corner: str, vdd: float, temp_c: float) -> dict[str, Any]:
    """Run (or load cached) ngspice simulation for one PVT point."""
    pvt = PVT.checked(corner, vdd, temp_c)
    with _runs_lock():
        return _simulate_locked(pvt)


def _simulate_locked(pvt: PVT) -> dict[str, Any]:
    netlist = render_netlist(pvt)
    out_path = _dir("results") / f"{pvt.result_id}.json"
    if out_path.exists():
        cached = _read_json(out_path)
        if cached["provenance"]["netlist_sha256"] != hashlib.sha256(netlist.encode()).hexdigest():
            raise ExperimentError(f"cache fingerprint mismatch for {pvt.result_id}; refusing stale result")
        cached["cache_hit"] = True
        return cached
    if len(list(_dir("results").glob("sim_*.json"))) >= MAX_SIMULATIONS:
        raise ExperimentError(f"simulation budget exhausted ({MAX_SIMULATIONS}); stop and report")

    net_path = _dir("netlists") / f"{pvt.result_id}.cir"
    net_path.write_text(netlist, encoding="utf-8")
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [str(NGSPICE), "-b", str(net_path)],
            capture_output=True,
            text=True,
            timeout=NGSPICE_TIMEOUT_S,
            check=False,
            cwd=str(net_path.parent),
        )
    except subprocess.TimeoutExpired as exc:
        raise ExperimentError(f"ngspice timed out after {NGSPICE_TIMEOUT_S}s") from exc
    elapsed = time.perf_counter() - started
    text = proc.stdout + "\n" + proc.stderr
    meas = {m.group(1): float(m.group(2)) for m in _MEAS_RE.finditer(text)}
    if proc.returncode != 0 or "FALSIFY_DONE" not in text or set(meas) != {"tphl", "tplh"}:
        tail = "\n".join(text.strip().splitlines()[-15:])
        raise ExperimentError(f"ngspice failed (rc={proc.returncode}); tail:\n{tail}")
    if not all(0 < v < 4e-9 for v in meas.values()):
        raise ExperimentError(f"implausible delay measurement {meas}")

    result = {
        "kind": "ngspice_simulation",
        "schema_version": SCHEMA_VERSION,
        "result_id": pvt.result_id,
        "pvt": {"corner": pvt.corner, "vdd": pvt.vdd, "temp_c": pvt.temp_c},
        "tphl_s": meas["tphl"],
        "tplh_s": meas["tplh"],
        "tpd_s": (meas["tphl"] + meas["tplh"]) / 2,
        "provenance": {
            "simulator": ngspice_banner(),
            "ngspice_path": str(NGSPICE),
            "netlist_file": str(net_path.relative_to(ROOT)) if net_path.is_relative_to(ROOT) else str(net_path),
            "netlist_sha256": hashlib.sha256(netlist.encode()).hexdigest(),
            "models": "generic SPICE Level-1 textbook cards (not a foundry PDK)",
            "raw_meas_lines": [m.group(0).strip() for m in _MEAS_RE.finditer(text)],
            "wall_time_s": round(elapsed, 3),
        },
        "cache_hit": False,
    }
    _write_json(out_path, result)
    return result


# --- surrogate ----------------------------------------------------------------


def _alpha_shape(vdd: float) -> float:
    return vdd / (vdd - SURROGATE_VT_EFF) ** SURROGATE_ALPHA


def surrogate_calibration() -> dict[str, Any]:
    cal_id = PVT.checked(*CALIBRATION_POINT).result_id
    cal = load_result(cal_id)
    k = cal["tpd_s"] / _alpha_shape(cal["pvt"]["vdd"])
    return {"calibration_result_id": cal_id, "K": k}


def evaluate_surrogate(result_id: str) -> dict[str, Any]:
    """Compare the pre-registered surrogate against a stored simulation result."""
    sim = load_result(result_id)
    try:
        cal = surrogate_calibration()
    except ExperimentError as exc:
        c, v, t = CALIBRATION_POINT
        raise ExperimentError(
            f"surrogate not calibrated: simulate the calibration point corner={c} vdd={v} temp_c={t} first"
        ) from exc
    predicted = cal["K"] * _alpha_shape(sim["pvt"]["vdd"])
    rel_err = (predicted - sim["tpd_s"]) / sim["tpd_s"]
    evaluation = {
        "kind": "surrogate_evaluation",
        "result_id": result_id,
        "pvt": sim["pvt"],
        "simulated_tpd_s": sim["tpd_s"],
        "surrogate_tpd_s": predicted,
        "relative_error": rel_err,
        "fails": abs(rel_err) > FAIL_THRESHOLD,
        "fail_threshold": FAIL_THRESHOLD,
        "surrogate": {
            "form": "K*VDD/(VDD-VT_EFF)^ALPHA",
            "alpha": SURROGATE_ALPHA,
            "vt_eff": SURROGATE_VT_EFF,
            **cal,
        },
    }
    _write_json(_dir("evaluations") / f"{result_id}.json", evaluation)
    return evaluation


# --- next decision -------------------------------------------------------------


def _coords(corner: str, vdd: float, temp_c: float) -> tuple[float, float, float, float]:
    dvtn, _, dvtp, _ = CORNERS[corner]
    return (
        dvtn / 0.05,
        dvtp / 0.05,
        (vdd - VDD_RANGE[0]) / (VDD_RANGE[1] - VDD_RANGE[0]),
        (temp_c - TEMP_RANGE[0]) / (TEMP_RANGE[1] - TEMP_RANGE[0]),
    )


def propose_next_point(exploration_weight: float = 0.05) -> dict[str, Any]:
    """Rank unsimulated grid points by IDW-predicted |error| + exploration bonus."""
    sims = {r["result_id"] for r in _load_all("results")}
    evals = _load_all("evaluations")
    cal_pvt = PVT.checked(*CALIBRATION_POINT)
    if cal_pvt.result_id not in sims:
        decision = {
            "action": "simulate",
            "pvt": {"corner": cal_pvt.corner, "vdd": cal_pvt.vdd, "temp_c": cal_pvt.temp_c},
            "reason": "surrogate calibration point has not been simulated",
        }
    elif len(sims) >= MAX_SIMULATIONS:
        decision = {"action": "stop", "reason": f"simulation budget {MAX_SIMULATIONS} exhausted"}
    elif not evals:
        decision = {"action": "evaluate", "reason": "no surrogate evaluations stored yet"}
    else:
        evidence = [(_coords(**e["pvt"]), abs(e["relative_error"])) for e in evals]
        best: tuple[float, tuple[str, float, float], float, float] | None = None
        for corner in sorted(CORNERS):
            for vdd in GRID_VDD:
                for temp in GRID_TEMP:
                    cand = PVT.checked(corner, vdd, temp)
                    if cand.result_id in sims:
                        continue
                    x = _coords(corner, vdd, temp)
                    dists = [math.dist(x, ex) for ex, _ in evidence]
                    weights = [1.0 / (d * d + 1e-9) for d in dists]
                    pred = sum(w * err for w, (_, err) in zip(weights, evidence)) / sum(weights)
                    score = pred + exploration_weight * min(dists)
                    key = (score, (corner, vdd, temp), pred, min(dists))
                    # Deterministic: highest score wins; ties go to the lexically first point.
                    if best is None or score > best[0] + 1e-15:
                        best = key
        if best is None:
            decision = {"action": "stop", "reason": "every grid point has been simulated"}
        else:
            score, (corner, vdd, temp), pred, dmin = best
            decision = {
                "action": "simulate",
                "pvt": {"corner": corner, "vdd": vdd, "temp_c": temp},
                "score": score,
                "idw_predicted_abs_error": pred,
                "min_normalized_distance": dmin,
                "reason": "highest IDW-predicted surrogate |error| plus exploration bonus among unsimulated grid points",
            }
    decision.update(
        {
            "kind": "next_decision",
            "rule": f"IDW(|rel_err|, p=2) + {exploration_weight}*min_dist over {len(CORNERS)}x{len(GRID_VDD)}x{len(GRID_TEMP)} grid",
            "n_simulations": len(sims),
            "n_evaluations": len(evals),
            "n_failures": sum(1 for e in evals if e["fails"]),
        }
    )
    _write_json(_dir("handoff") / "next_decision.json", decision)
    return decision


def ledger_summary() -> dict[str, Any]:
    evals = _load_all("evaluations")
    rows = [
        {
            "result_id": e["result_id"],
            **e["pvt"],
            "simulated_tpd_ps": round(e["simulated_tpd_s"] * 1e12, 3),
            "surrogate_tpd_ps": round(e["surrogate_tpd_s"] * 1e12, 3),
            "relative_error": round(e["relative_error"], 4),
            "fails": e["fails"],
        }
        for e in evals
    ]
    return {
        "kind": "ledger_summary",
        "n_simulations": len(_load_all("results")),
        "n_evaluations": len(rows),
        "fail_threshold": FAIL_THRESHOLD,
        "evaluations": sorted(rows, key=lambda r: -abs(r["relative_error"])),
    }

"""Simulator backends for the research path.

``NgspiceSimulator`` runs the frozen CMOS inverter netlist with one of the three
protocol numerical settings. The ``basic`` setting renders byte-for-byte the
connection-test netlist of ``experiment.py``; ``half`` halves the time step and
``tight`` additionally applies RELTOL=1e-4, VNTOL=1e-7 V, ABSTOL=1e-13 A.

Every result carries a fingerprint (measurement schema, numerical setting,
netlist sha256, simulator version + binary sha256). Cached evidence is reused
only when the whole fingerprint matches.

``FixtureSimulator`` is a deterministic analytic stand-in used only by unit
tests. Its fingerprint and every record it produces are labelled
NON-SCIENTIFIC FIXTURE, so its numbers can never be mistaken for ngspice data.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .protocol import CORNERS, ROOT, Point

MEASUREMENT_SCHEMA = "falsify-meas-v1"  # bump when parsing/acceptance rules change
NGSPICE_TIMEOUT_S = 60

NUMERICAL_SETTINGS: dict[str, dict[str, Any]] = {
    "basic": {"tstep": "1p", "options": ""},
    "half": {"tstep": "0.5p", "options": ""},
    "tight": {"tstep": "0.5p", "options": " reltol=1e-4 vntol=1e-7 abstol=1e-13"},
}
PRIMARY_SETTING = "basic"

NETLIST_TEMPLATE = """* falsify-lab CMOS inverter delay (generic Level-1 models, not a PDK)
.param vdd={vdd}
.param dvtn={dvtn} kpn_s={kpn_s} dvtp={dvtp} kpp_s={kpp_s}
.temp {temp}
.options tnom=27{options}
.model nch nmos level=1 vto={{0.50+dvtn}} kp={{170u*kpn_s}} gamma=0.4 phi=0.7 lambda=0.05
+ tox=7.6n cgso=0.3n cgdo=0.3n cj=0.9m cjsw=0.25n
.model pch pmos level=1 vto={{-0.60-dvtp}} kp={{60u*kpp_s}} gamma=0.5 phi=0.7 lambda=0.08
+ tox=7.6n cgso=0.3n cgdo=0.3n cj=1.0m cjsw=0.3n
vsup vdd 0 {{vdd}}
vin in 0 pulse(0 {{vdd}} 1n 50p 50p 2n 4n)
mn out in 0 0 nch w=1u l=0.35u ad=1p as=1p pd=4u ps=4u
mp out in vdd vdd pch w=2.5u l=0.35u ad=2.5p as=2.5p pd=7u ps=7u
cl out 0 20f
.tran {tstep} 6n
.control
run
meas tran tphl trig v(in) val={half} rise=1 targ v(out) val={half} fall=1
meas tran tplh trig v(in) val={half} fall=1 targ v(out) val={half} rise=1
echo FALSIFY_DONE
quit
.endc
.end
"""

_MEAS_RE = re.compile(r"^\s*(tphl|tplh)\s*=\s*([-+0-9.eE]+)", re.MULTILINE)


class SimulationFailed(RuntimeError):
    def __init__(self, message: str, status: str = "failed", log: str = "", wall_time_s: float = 0.0):
        super().__init__(message)
        self.status = status
        self.log = log
        self.wall_time_s = wall_time_s


def render_netlist(point: Point, setting: str = PRIMARY_SETTING) -> str:
    if setting not in NUMERICAL_SETTINGS:
        raise ValueError(f"unknown numerical setting {setting!r}")
    dvtn, kpn_s, dvtp, kpp_s = CORNERS[point.corner]
    s = NUMERICAL_SETTINGS[setting]
    return NETLIST_TEMPLATE.format(
        vdd=f"{point.vdd:.3f}",
        half=f"{point.vdd / 2:.4f}",
        temp=f"{point.temp_c:.1f}",
        dvtn=dvtn,
        kpn_s=kpn_s,
        dvtp=dvtp,
        kpp_s=kpp_s,
        tstep=s["tstep"],
        options=s["options"],
    )


@dataclass
class Measurement:
    tphl_s: float
    tplh_s: float
    raw_meas_lines: list[str]
    wall_time_s: float

    @property
    def tpd_s(self) -> float:
        return (self.tphl_s + self.tplh_s) / 2


def _accept(meas: dict[str, float]) -> None:
    if set(meas) != {"tphl", "tplh"}:
        raise SimulationFailed(f"missing delay measurement(s): got {sorted(meas)}")
    if not all(math.isfinite(v) and 0 < v < 4e-9 for v in meas.values()):
        raise SimulationFailed(f"non-positive or implausible delay measurement {meas}")


class NgspiceSimulator:
    """Real ngspice 47 backend. The binary is never copied; FALSIFY_NGSPICE points at it."""

    scientific = True

    def __init__(self, binary: Path | str | None = None, timeout_s: float = NGSPICE_TIMEOUT_S):
        self.binary = Path(
            binary
            or os.environ.get("FALSIFY_NGSPICE", ROOT / "tools" / "ngspice-47" / "Spice64" / "bin" / "ngspice_con.exe")
        )
        self.timeout_s = timeout_s
        self._banner: str | None = None

    def banner(self) -> str:
        if self._banner is None:
            if not self.binary.exists():
                raise SimulationFailed(f"ngspice not found at {self.binary}", status="unavailable")
            proc = subprocess.run([str(self.binary), "--version"], capture_output=True, text=True, timeout=30, check=False)
            version = next((ln.strip("* ").strip() for ln in proc.stdout.splitlines() if "ngspice-" in ln), None)
            if version is None:
                raise SimulationFailed("could not read ngspice version banner", status="unavailable")
            self._banner = f"{version} [sha256 {hashlib.sha256(self.binary.read_bytes()).hexdigest()}]"
        return self._banner

    def fingerprint(self, point: Point, setting: str) -> dict[str, Any]:
        netlist = render_netlist(point, setting)
        return {
            "measurement_schema": MEASUREMENT_SCHEMA,
            "setting": setting,
            "netlist_sha256": hashlib.sha256(netlist.encode("utf-8")).hexdigest(),
            "simulator": self.banner(),
            "device_models": "generic SPICE Level-1 textbook cards (not a foundry PDK)",
        }

    def run(self, point: Point, setting: str, workdir: Path) -> Measurement:
        netlist = render_netlist(point, setting)
        workdir.mkdir(parents=True, exist_ok=True)
        net_path = workdir / "netlist.cir"
        net_path.write_text(netlist, encoding="utf-8", newline="\n")
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                [str(self.binary), "-b", str(net_path)],
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
                cwd=str(workdir),
            )
        except subprocess.TimeoutExpired as exc:
            raise SimulationFailed(
                f"ngspice timed out after {self.timeout_s}s", status="timeout", wall_time_s=time.perf_counter() - started
            ) from exc
        elapsed = time.perf_counter() - started
        text = proc.stdout + "\n" + proc.stderr
        (workdir / "ngspice.log").write_text(text, encoding="utf-8", newline="\n")
        meas = {m.group(1): float(m.group(2)) for m in _MEAS_RE.finditer(text)}
        if proc.returncode != 0 or "FALSIFY_DONE" not in text:
            tail = "\n".join(text.strip().splitlines()[-15:])
            raise SimulationFailed(f"ngspice failed (rc={proc.returncode})", log=tail, wall_time_s=elapsed)
        try:
            _accept(meas)
        except SimulationFailed as exc:
            exc.wall_time_s = elapsed
            exc.log = "\n".join(text.strip().splitlines()[-15:])
            raise
        return Measurement(meas["tphl"], meas["tplh"], [m.group(0).strip() for m in _MEAS_RE.finditer(text)], elapsed)


def fixture_delay(point: Point, setting: str = PRIMARY_SETTING) -> tuple[float, float]:
    """Smooth synthetic delays with a PVT interaction the 5-coefficient model cannot express."""
    dvtn, kpn, dvtp, kpp = CORNERS[point.corner]
    v, t = point.vdd, point.temp_c
    mob = ((t + 273.15) / 300.15) ** 1.5
    base_n = 30e-12 * v / ((v - 0.5 - dvtn) ** 1.4) * mob / kpn
    base_p = 40e-12 * v / ((v - 0.6 - dvtp) ** 1.3) * mob / kpp
    inter = 1.0 + 0.35 * max(0.0, 1.9 - v) * (t + 40) / 165
    jitter = {"basic": 1.0, "half": 1.0005, "tight": 1.001}[setting]
    return base_n * inter * jitter, base_p * inter * jitter


@dataclass
class FixtureSimulator:
    """Deterministic NON-SCIENTIFIC stand-in for tests. ``fail`` marks point ids that fail."""

    variant: str = "a"
    fail: set[str] = field(default_factory=set)
    delay_fn: Callable[[Point, str], tuple[float, float]] | None = None
    sleep_s: float = 0.0
    scientific = False

    def banner(self) -> str:
        return f"NON-SCIENTIFIC FIXTURE simulator variant={self.variant}"

    def fingerprint(self, point: Point, setting: str) -> dict[str, Any]:
        netlist = render_netlist(point, setting)
        return {
            "measurement_schema": MEASUREMENT_SCHEMA,
            "setting": setting,
            "netlist_sha256": hashlib.sha256(netlist.encode("utf-8")).hexdigest(),
            "simulator": self.banner(),
            "device_models": "NON-SCIENTIFIC FIXTURE",
        }

    def run(self, point: Point, setting: str, workdir: Path) -> Measurement:
        if self.sleep_s:
            time.sleep(self.sleep_s)
        if point.id in self.fail:
            raise SimulationFailed(f"fixture failure injected at {point.id}", log="fixture")
        tphl, tplh = (self.delay_fn or fixture_delay)(point, setting)
        meas = {"tphl": tphl, "tplh": tplh}
        _accept(meas)
        return Measurement(tphl, tplh, [f"tphl = {tphl:.6e} (fixture)", f"tplh = {tplh:.6e} (fixture)"], 0.0)

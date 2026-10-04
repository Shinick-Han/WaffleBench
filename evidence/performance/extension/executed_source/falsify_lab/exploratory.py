"""Post-M5 exploratory policy and load sensitivity backend; never a primary policy.

Kernel dispersion is a selection heuristic, not calibrated uncertainty. This
module takes only currently revealed evidence. It has no campaign or truth input.
"""
from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path

import numpy as np

from .protocol import Point
from .simulator import (NgspiceSimulator, Measurement, SimulationFailed,
                        render_netlist, _MEAS_RE, _accept)

LENGTH_SCALES = (0.6, 0.6, 0.25, 0.5)
RIDGE = 1e-4
DISPERSION_WEIGHT = 0.05


def kernel_candidates(candidates: list[Point], evidence: list[tuple[Point, float]]) -> list[dict]:
    """Fixed RBF regression of absolute error plus a kernel-dispersion bonus."""
    points = sorted(set(candidates))
    if not points:
        return []
    if not evidence:
        raise ValueError("Revealed evidence required")
    scale = np.asarray(LENGTH_SCALES)
    x = np.asarray([p.coords for p, _ in evidence]) / scale
    y = np.asarray([e for _, e in evidence])
    if not np.all(np.isfinite(y)) or np.any(y < 0):
        raise ValueError("Evidence errors must be finite and nonnegative")
    z = np.asarray([p.coords for p in points]) / scale
    k = np.exp(-0.5 * np.sum((x[:, None] - x[None, :]) ** 2, axis=2))
    k.flat[::len(x) + 1] += RIDGE
    cross = np.exp(-0.5 * np.sum((z[:, None] - x[None, :]) ** 2, axis=2))
    # One factorization, multiple right-hand sides for means and dispersion.
    solution = np.linalg.solve(k, np.column_stack((y, cross.T)))
    mean = cross @ solution[:, 0]
    dispersion = np.sqrt(np.maximum(0.0, 1 - np.sum(cross * solution[:, 1:].T, axis=1)))
    rows = [{"point_id": p.id, "predicted_abs_error": float(m),
             "kernel_dispersion": float(s),
             "score": float(max(0.0, m) + DISPERSION_WEIGHT * s)}
            for p, m, s in zip(points, mean, dispersion)]
    return sorted(rows, key=lambda r: -r["score"])


class LoadSimulator(NgspiceSimulator):
    """Explicit supplementary inverter load; primary simulator remains untouched."""

    def __init__(self, load_ff: int, binary=None):
        if load_ff not in (8, 20, 35):
            raise ValueError("Only the three predeclared sensitivity loads are supported")
        super().__init__(binary)
        self.load_ff = load_ff

    def netlist(self, point: Point, setting: str) -> str:
        text = render_netlist(point, setting)
        marker = "\ncl out 0 20f\n"
        if text.count(marker) != 1:
            raise RuntimeError("Base netlist load marker changed")
        return text.replace(marker, f"\ncl out 0 {self.load_ff}f\n")

    def fingerprint(self, point: Point, setting: str) -> dict:
        fp = super().fingerprint(point, setting)
        fp["netlist_sha256"] = hashlib.sha256(self.netlist(point, setting).encode()).hexdigest()
        fp["supplementary_load_ff"] = self.load_ff
        return fp

    def run(self, point: Point, setting: str, workdir: Path) -> Measurement:
        workdir.mkdir(parents=True, exist_ok=True)
        net = workdir / "netlist.cir"
        net.write_text(self.netlist(point, setting), encoding="utf-8", newline="\n")
        start = time.perf_counter()
        try:
            proc = subprocess.run([str(self.binary), "-b", str(net.resolve())], cwd=workdir,
                                  capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise SimulationFailed("Supplementary ngspice timeout", status="timeout",
                                   wall_time_s=time.perf_counter()-start) from exc
        elapsed = time.perf_counter()-start
        log = proc.stdout + "\n" + proc.stderr
        (workdir / "ngspice.log").write_text(log, encoding="utf-8", newline="\n")
        values = {m.group(1): float(m.group(2)) for m in _MEAS_RE.finditer(log)}
        try:
            if proc.returncode or "FALSIFY_DONE" not in log:
                raise SimulationFailed(f"Supplementary ngspice rc={proc.returncode}")
            _accept(values)
        except SimulationFailed as exc:
            exc.wall_time_s = elapsed
            exc.log = log[-2000:]
            raise
        return Measurement(values["tphl"], values["tplh"],
                           [m.group(0).strip() for m in _MEAS_RE.finditer(log)], elapsed)

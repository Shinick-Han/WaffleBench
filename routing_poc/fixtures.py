"""Synthetic development/benchmark fixtures for the routing PoC (ROUTING_POC_CONTRACT.md).

``make_job(seed, regime)`` returns ``(job, archive)`` in the shared JSON contract. Two
preregistered regimes:

- ``clustered``: candidates gathered in a few clusters per wafer, with expensive wafer
  load, settle and slow stage movement (the regime the movement-aware question is about);
- ``diffuse``: candidates spread over the wafer with low load/settle/movement overhead
  (stress/control regime where movement awareness should matter little).

Each job has 120 candidates on 3 synthetic wafers. Reference DOI is sampled
independently from the public ``prior_p`` (priors are calibrated by construction), and
paid detector reports are imperfect (fixed sensitivity/false-positive rate) with
occasional failed/missing attempts. The generator never looks at any choice policy and
is frozen with the protocol; its latent RNG (seeded from ``seed``/``regime``) is never
exposed to a policy. The job holds only public fields; the seed, truth and detector
outcomes exist only in the archive/reference or the campaign record.

Everything here is invented: these fixtures are never real equipment evidence and say
nothing about commercial throughput.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timedelta, timezone

FIXTURE_VERSION = 1
REGIMES = ("clustered", "diffuse")
N_CANDIDATES = 120
N_WAFERS = 3
CUTOFF_UTC = datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc)
WAFER_RADIUS_UM = 140_000.0
SENSITIVITY = 0.9
FALSE_POSITIVE_RATE = 0.05
P_FAILED = 0.04
P_MISSING = 0.03
REFERENCE_SOURCE = "synthetic_fixture_independent_reference"
SYNTHETIC_LABEL = "synthetic fixture — invented parameters, not equipment evidence"

# Public action-time cost parameters per regime (invented, not measured on any tool).
COSTS = {
    "clustered": {"wafer_load_s": 20.0, "settle_s": 1.0, "move_um_per_s": 10_000.0, "retry_limit": 1},
    "diffuse": {"wafer_load_s": 2.0, "settle_s": 0.2, "move_um_per_s": 200_000.0, "retry_limit": 1},
}
# Declared per-recipe action-time bounds (capture, inference) in seconds.
RECIPES = {"sem-r1": (3.0, 0.5), "sem-r2": (4.5, 0.8)}


def job_id_for(seed: int, regime: str) -> str:
    """Public job id. It is an opaque digest, so the latent seed is not readable from it."""
    digest = hashlib.sha256(f"routing-poc-fixture|v{FIXTURE_VERSION}|{regime}|{seed}".encode()).hexdigest()
    return f"syn-{regime}-{digest[:12]}"


def _iso(t: datetime) -> str:
    return t.isoformat()


def _in_disc(rng: random.Random, radius: float) -> tuple[float, float]:
    r = radius * math.sqrt(rng.random())
    a = rng.uniform(0.0, 2.0 * math.pi)
    return r * math.cos(a), r * math.sin(a)


def _positions_and_priors(rng: random.Random, regime: str, n: int) -> list[tuple[float, float, float]]:
    out = []
    if regime == "clustered":
        k = rng.randint(3, 5)
        centers = [_in_disc(rng, WAFER_RADIUS_UM * 0.85) for _ in range(k)]
        base = [rng.uniform(0.03, 0.45) for _ in range(k)]
        for _ in range(n):
            c = rng.randrange(k)
            x = centers[c][0] + rng.gauss(0.0, 2_500.0)
            y = centers[c][1] + rng.gauss(0.0, 2_500.0)
            p = min(0.95, max(0.01, base[c] + rng.gauss(0.0, 0.08)))
            out.append((x, y, p))
    else:
        for _ in range(n):
            x, y = _in_disc(rng, WAFER_RADIUS_UM)
            p = min(0.95, max(0.01, rng.betavariate(1.3, 4.0)))
            out.append((x, y, p))
    return out


def _attempt(rng: random.Random, doi: bool, recipe: str, t: datetime) -> dict:
    cap_b, inf_b = RECIPES[recipe]
    u = rng.random()
    status = "failed" if u < P_FAILED else "missing" if u < P_FAILED + P_MISSING else "ok"
    capture_s = round(cap_b * rng.uniform(0.6, 1.0), 3)
    if status == "ok":
        reported = rng.random() < (SENSITIVITY if doi else FALSE_POSITIVE_RATE)
        inference_s = round(inf_b * rng.uniform(0.5, 1.0), 3)
    else:
        reported = None
        inference_s = round(inf_b * rng.uniform(0.0, 0.5), 3) if status == "failed" else 0.0
    return {"status": status, "reported_doi": reported, "capture_s": capture_s,
            "inference_s": inference_s, "observed_at": _iso(t)}


def make_job(seed: int, regime: str = "clustered") -> tuple[dict, dict]:
    """Return ``(job, archive)`` for one synthetic lot. Deterministic in ``(seed, regime)``."""
    if regime not in REGIMES:
        raise ValueError(f"unknown regime {regime!r}; expected one of {REGIMES}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    rng = random.Random(f"routing-poc-fixture|v{FIXTURE_VERSION}|{regime}|{seed}")
    job_id = job_id_for(seed, regime)
    per_wafer = N_CANDIDATES // N_WAFERS
    candidates, observations, doi_by_site = [], {}, {}
    t_obs = CUTOFF_UTC
    for w in range(N_WAFERS):
        wafer_id = f"{job_id}-w{w + 1}"
        for i, (x, y, p) in enumerate(_positions_and_priors(rng, regime, per_wafer)):
            site_id = f"{wafer_id}-s{i + 1:03d}"
            recipe = "sem-r1" if rng.random() < 0.7 else "sem-r2"
            cap_b, inf_b = RECIPES[recipe]
            optical_at = CUTOFF_UTC - timedelta(minutes=rng.randint(5, 240))
            candidates.append({
                "site_id": site_id, "wafer_id": wafer_id, "x_um": round(x, 1), "y_um": round(y, 1),
                "prior_p": round(p, 4), "optical_observed_at": _iso(optical_at), "recipe_id": recipe,
                "capture_bound_s": cap_b, "inference_bound_s": inf_b,
            })
            # Reference truth sampled from the rounded public prior: priors are calibrated.
            doi = rng.random() < round(p, 4)
            doi_by_site[site_id] = doi
            attempts = []
            for _ in range(1 + COSTS[regime]["retry_limit"]):
                t_obs += timedelta(seconds=1)
                attempts.append(_attempt(rng, doi, recipe, t_obs))
                if attempts[-1]["status"] == "ok":
                    break
            observations[site_id] = attempts
    job = {
        "schema_version": 1, "job_id": job_id, "data_mode": "synthetic", "cutoff_utc": _iso(CUTOFF_UTC),
        "cost": dict(COSTS[regime]), "candidates": candidates,
        "provenance": {"generator": "routing_poc.fixtures", "fixture_version": FIXTURE_VERSION,
                       "regime": regime, "synthetic": True, "label": SYNTHETIC_LABEL,
                       "commercial_validated": False},
    }
    archive = {"job_id": job_id, "observations": observations,
               "reference": {"complete": True, "source": REFERENCE_SOURCE, "doi_by_site": doi_by_site}}
    return job, archive

"""Shared helpers for core tests (no tests here).

Fixture-based tests are NON-SCIENTIFIC: they validate algorithms and information
flow with a synthetic simulator. Every test campaign lives in a fresh, uniquely
named directory under runs/development/ and is never deleted by the tests.
"""

from __future__ import annotations

import itertools
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from falsify_lab.simulator import FixtureSimulator  # noqa: E402
from falsify_lab.storage import Campaign  # noqa: E402

DEV_ROOT = ROOT / "runs" / "development"
_STAMP = time.strftime("%Y%m%d-%H%M%S")
_COUNTER = itertools.count(1)


def fresh_dir(name: str) -> Path:
    path = DEV_ROOT / f"tests-{_STAMP}-{os.getpid()}" / f"{next(_COUNTER):03d}-{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def fixture_campaign(name: str, sim: FixtureSimulator | None = None, **kw) -> Campaign:
    return Campaign.create(fresh_dir(name), "fixture", sim or FixtureSimulator(), label="NON-SCIENTIFIC fixture test", **kw)

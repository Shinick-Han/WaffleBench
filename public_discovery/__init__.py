"""Bounded public HTTP API that starts fixed WaffleBench discovery diagnostics.

See public_discovery.server for the endpoint contract. The API never serves files
from disk, never accepts commands, prompts, models or paths, and only exposes a
completed result after the runtime's SDK and ledger verification passed.
"""

from public_discovery.server import Config, RunManager, make_server

__all__ = ["Config", "RunManager", "make_server"]

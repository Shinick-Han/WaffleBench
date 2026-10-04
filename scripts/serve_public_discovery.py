"""Serve the bounded public discovery API on 127.0.0.1:8782 only.

The coordinator exposes this port through its own tunnel. Run state, job ledgers and runtime
logs live in the private --state-dir (default <repo>/.public-discovery-runtime); nothing there
is served by path.

Usage:
  python scripts/serve_public_discovery.py [--state-dir DIR] [--max-runs N] [--disabled]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from public_discovery.server import BIND_HOST, MAX_RUNS_LIMIT, PORT, Config, make_server  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", type=Path, default=REPO_ROOT / ".public-discovery-runtime")
    parser.add_argument("--max-runs", type=int, default=MAX_RUNS_LIMIT,
                        help=f"lifetime run quota, 0..{MAX_RUNS_LIMIT}")
    parser.add_argument("--disabled", action="store_true", help="serve status only; POST returns 503")
    args = parser.parse_args(argv)
    if not 0 <= args.max_runs <= MAX_RUNS_LIMIT:
        parser.error(f"--max-runs must be between 0 and {MAX_RUNS_LIMIT}")
    config = Config(state_dir=args.state_dir.resolve(), max_runs=args.max_runs, enabled=not args.disabled)
    server = make_server(config)
    print(f"public discovery API on http://{BIND_HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

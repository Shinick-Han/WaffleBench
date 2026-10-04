"""Inspection route v5 stages: build, dev, freeze, test (see inspection_route_v5.campaign)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inspection_route_v5.campaign import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())

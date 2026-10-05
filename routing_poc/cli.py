"""``python -m routing_poc.cli demo|benchmark|replay`` (post-hackathon development only).

- ``demo --out PATH``: development campaign (seeds 12000..12007).
- ``benchmark --out PATH --confirm-synthetic-test``: the preregistered reserved synthetic
  test; refused without the explicit flag. Coordinator-operated.
- ``replay --job PATH --archive PATH --out PATH --budget-s N --policy NAME``: replay an
  input job/archive (synthetic or historical log). Truth metrics are computed only after
  the loop finishes. Every report carries its evidence mode label.

Prints one JSON object; exits nonzero on failure. Output paths must not exist.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

from . import campaign, metrics


def _replay(args: argparse.Namespace) -> dict:
    out = Path(args.out)
    if out.exists():
        raise campaign.CampaignError(f"output {out} already exists")
    if args.policy not in campaign.PROTOCOL["policies"]:
        raise campaign.CampaignError(f"unknown policy {args.policy!r}")
    contracts = importlib.import_module("routing_poc.contracts")
    replay = importlib.import_module("routing_poc.replay")
    policies = importlib.import_module("routing_poc.policies")
    job = contracts.validate_job(json.loads(Path(args.job).read_text(encoding="utf-8")))
    archive = contracts.validate_archive(job, json.loads(Path(args.archive).read_text(encoding="utf-8")))
    # The selector sees only replay state; the archive reference is untouched until evaluation.
    seed = campaign.policy_seed(job["job_id"], args.policy)
    run = replay.run_loop(job, archive, lambda s, r: policies.select(s, r, policy=args.policy,
                                                                     audit_epsilon=args.audit_epsilon),
                          args.budget_s, seed=seed)
    result = metrics.evaluate(job, archive, run)
    out.mkdir(parents=True)
    campaign._write(out / "run.json", {"policy": args.policy, "policy_seed": seed,
                                       "audit_epsilon": args.audit_epsilon, "run": run})
    campaign._write(out / "metrics.json", result)
    return {"status": "complete", "out": str(out), "evidence_mode": result["data_mode"],
            "evidence_label": result["evidence_label"], "metrics": result, "commercial_validated": False}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m routing_poc.cli")
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("demo", help="development synthetic campaign")
    d.add_argument("--out", required=True)
    b = sub.add_parser("benchmark", help="preregistered reserved synthetic test (coordinator-operated)")
    b.add_argument("--out", required=True)
    b.add_argument("--confirm-synthetic-test", action="store_true")
    r = sub.add_parser("replay", help="replay an input job/archive")
    r.add_argument("--job", required=True)
    r.add_argument("--archive", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--budget-s", type=float, required=True)
    r.add_argument("--policy", required=True)
    r.add_argument("--audit-epsilon", type=float, default=campaign.PROTOCOL["audit_epsilon"])
    args = ap.parse_args(argv)
    try:
        if args.command == "demo":
            result = campaign.run_campaign(args.out, "development")
        elif args.command == "benchmark":
            if not args.confirm_synthetic_test:
                raise campaign.CampaignError("benchmark requires --confirm-synthetic-test")
            result = campaign.run_campaign(args.out, "test", confirm_synthetic_test=True)
        else:
            result = _replay(args)
    except Exception as exc:  # report every failure as JSON with a nonzero exit
        print(json.dumps({"status": "failed", "command": args.command, "error": f"{type(exc).__name__}: {exc}",
                          "commercial_validated": False}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

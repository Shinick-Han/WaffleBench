"""Bounded real-SEM development runner; never selects or evaluates test items."""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
NOTICE = "Post-hackathon development — not part of the submitted version."


def main(argv=None):
    parser = argparse.ArgumentParser(description=NOTICE)
    parser.add_argument("--python", default=sys.executable, help="isolated SEM environment interpreter")
    parser.add_argument("--data-root", required=True, help="already extracted Carinthia-S data")
    parser.add_argument("--output", required=True, help="new development-run directory")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--train-images", type=int, default=24)
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--eval-images", type=int, default=24)
    parser.add_argument("--encoder-weights", help="explicit local pretrained weights; no implicit download")
    args = parser.parse_args(argv)
    if not (1 <= args.train_images <= 64 and 1 <= args.epochs <= 3 and 1 <= args.eval_images <= 24):
        parser.error("development bounds: 1..64 train images, 1..3 epochs, 1..24 calibration images")
    output = pathlib.Path(args.output).resolve()
    data = pathlib.Path(args.data_root).resolve()
    if output == data or data in output.parents:
        parser.error("output must be separate from the read-only data root")
    if output.exists():
        parser.error("output already exists; use a new run directory to preserve previous evidence")
    if not data.is_dir():
        parser.error("data root does not exist; use sem_data.cli extract first")
    output.mkdir(parents=True)
    events = []

    def save(status, error=None):
        result = {"schema_version": 1, "status": status, "notice": NOTICE,
                  "role": "bounded_development_pipeline_check", "data_mode": "real",
                  "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "test_evaluation": "not executed", "events": events,
                  "limitations": ["Not a confirmatory benchmark or an equipment comparison.",
                                  "Calibration examples used here are development evidence.",
                                  "Defect-only SEM frames do not establish clean-frame false-alarm rate."]}
        if error:
            result["error"] = error
        (output / "integration-receipt.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    def command(stage, *parts):
        cmd = [args.python, "-m", *map(str, parts)]
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        result = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=1200)
        (output / (stage + ".stdout.txt")).write_text(result.stdout, encoding="utf-8")
        (output / (stage + ".stderr.txt")).write_text(result.stderr, encoding="utf-8")
        events.append({"stage": stage, "command": cmd, "started_at": started,
                       "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                       "exit_code": result.returncode})
        save("running")
        print(json.dumps({"stage": stage, "exit_code": result.returncode}), flush=True)
        if result.returncode:
            raise RuntimeError(f"{stage} failed; see preserved stdout/stderr")

    try:
        command("audit", "sem_data.cli", "audit", "--data", data, "--out", output / "audit")
        command("split", "sem_data.cli", "split", "--manifest", output / "audit/audit_manifest.json",
                "--out", output / "split", "--seed", "2026100501")
        manifest = output / "split/manifest.json"
        command("validate", "sem_images.cli", "validate", "--manifest", manifest)
        command("backend-configs", "sem_images.cli", "export-backends", "--out", output / "backends")
        if not args.prepare_only:
            train = ["sem_images.cli", "train", "--manifest", manifest, "--root", output / "model",
                     "--max-train-images", str(args.train_images), "--epochs", str(args.epochs),
                     "--threads", "2", "--size", "256", "--overlap", "64"]
            if args.encoder_weights:
                train += ["--encoder-weights", args.encoder_weights]
            command("train", *train)
            command("predict", "sem_images.cli", "predict", "--manifest", manifest,
                    "--root", output / "model", "--split", "calibration",
                    "--max-images", args.eval_images, "--threads", "2")
            command("evaluate", "sem_images.cli", "evaluate", "--manifest", manifest,
                    "--root", output / "model", "--split", "calibration",
                    "--max-images", args.eval_images, "--threads", "2")
        print(json.dumps(save("prepared" if args.prepare_only else "complete"), indent=2))
        return 0
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps(save("failed", str(exc)), indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())

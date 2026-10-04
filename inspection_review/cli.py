"""CLI: python -m inspection_review.cli <prepare|campaign|report|reproduce> --root <new directory>

prepare   generate+persist train/validation lots, train and freeze the model, write freeze.json.
campaign  verify the freeze, refuse any existing campaign, generate test lots AFTER the freeze,
          run 60 lots x 8 policies x 2 modes x 3 independent budgets, save ledgers + metrics.json.
report    build report.json/report.md from stored metrics only (never reruns).
reproduce short development check on stored validation lots; labeled development, never held-out.

Lots are stored as public.npz / oracle.npz / metadata.json (no pickle). Policies only see a
sanitized view of public.npz; oracle.npz is opened by ``ControlledReviewSimulator`` for the
exact selected review and, after the selection loop, by the evaluator.
Prints one JSON summary; nonzero exit on failure; artifacts remain on failure.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import platform
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import harness, reporting
from .policies import POLICIES, sanitize_public

REPO = Path(__file__).resolve().parents[1]
SOURCE_FILES = ("inspection_review/data.py", "inspection_review/model.py", "inspection_review/policies.py",
                "inspection_review/harness.py", "inspection_review/reporting.py", "inspection_review/cli.py",
                "inspection_review/protocol.json", "INSPECTION_PROTOCOL.md")
TRAIN_SCENARIO = "stationary"
METADATA_KEYS = ("seed", "scenario", "metadata")


class HarnessError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_jsonable).encode()


def _jsonable(x: Any) -> Any:
    if isinstance(x, np.ndarray):
        return x.tolist()
    if isinstance(x, np.generic):
        return x.item()
    raise TypeError(f"not JSON serializable: {type(x)}")


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    return sha256_bytes(Path(p).read_bytes())


def atomic_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, path)


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True, default=_jsonable) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _apis(data_api: Any, model_api: Any) -> tuple[Any, Any]:
    if data_api is None:
        data_api = importlib.import_module("inspection_review.data")
    if model_api is None:
        model_api = importlib.import_module("inspection_review.model")
    return data_api, model_api


# ---------- lot storage ----------

def _arrays(d: dict, skip: tuple = ()) -> dict[str, np.ndarray]:
    out = {}
    for k, v in d.items():
        if k in skip:
            continue
        a = np.asarray(v)
        if a.dtype == object:
            try:
                a = a.astype(str)
            except Exception as exc:  # pragma: no cover - defensive
                raise HarnessError(f"array {k} not storable without pickle") from exc
        out[k] = a
    return out


def save_lot(lot_dir: Path, lot: dict, split: str, seed: int, scenario: str) -> dict:
    if lot_dir.exists():
        raise HarnessError(f"lot directory already exists: {lot_dir}")
    lot_dir.mkdir(parents=True)
    public = _arrays(lot["public"], METADATA_KEYS)
    oracle = _arrays(lot["oracle"])
    np.savez(lot_dir / "public.npz", **public)
    np.savez(lot_dir / "oracle.npz", **oracle)
    meta = {"lot_id": str(lot["public"]["lot_id"]), "split": split, "scenario": scenario, "seed": int(seed),
            "generated_at": _now(), "public_sha256": sha256_file(lot_dir / "public.npz"),
            "oracle_sha256": sha256_file(lot_dir / "oracle.npz")}
    write_json(lot_dir / "metadata.json", meta)
    return {**meta, "path": lot_dir.as_posix(), "metadata_sha256": sha256_file(lot_dir / "metadata.json")}


def _load_npz(p: Path) -> dict[str, np.ndarray]:
    with np.load(p, allow_pickle=False) as z:
        return {k: z[k] for k in z.files}


def load_public(lot_dir: Path) -> dict:
    pub = _load_npz(lot_dir / "public.npz")
    pub["lot_id"] = str(pub["lot_id"])
    return pub


def load_lot_for_training(lot_dir: Path) -> dict:
    """Past train/validation lots with their separate annotations (never test lots).

    Privileged model-work loader only: restores the ``scenario``/``seed`` evaluation metadata that
    ``save_lot`` moved to metadata.json, because ``model.train_model`` checks split authority with
    them. ``load_public`` and the sanitized policy view stay metadata-free.
    """
    meta = read_json(lot_dir / "metadata.json")
    if meta.get("split") not in ("train", "validation"):
        raise HarnessError("only train/validation lots may be loaded with oracle for model work")
    public = load_public(lot_dir)
    if public["lot_id"] != meta["lot_id"]:
        raise HarnessError(f"lot id mismatch between public.npz and metadata.json: {lot_dir}")
    public["scenario"] = str(meta["scenario"])
    public["seed"] = int(meta["seed"])
    return {"public": public, "oracle": _load_npz(lot_dir / "oracle.npz")}


class ControlledReviewSimulator:
    """Sole holder of the hidden oracle during selection; returns exactly the requested review."""

    def __init__(self, lot_dir: Path, data_api: Any):
        self._oracle = _load_npz(lot_dir / "oracle.npz")
        self._data = data_api
        self._closed = False
        self.calls: list[tuple[int, int]] = []

    def __call__(self, i: int, attempt: int) -> dict:
        if self._closed:
            raise HarnessError("review simulator closed")
        self.calls.append((int(i), int(attempt)))
        return dict(self._data.review_observation(self._oracle, int(i), int(attempt)))

    def release_for_evaluation(self) -> dict:
        """Called only after the selection loop finished."""
        self._closed = True
        return self._oracle


# ---------- freeze ----------

def source_hashes(repo: Path, files: tuple = SOURCE_FILES) -> dict[str, str]:
    out = {}
    for f in files:
        p = Path(repo) / f
        if not p.is_file():
            raise HarnessError(f"source file missing: {f}")
        out[f] = sha256_file(p)
    return out


def _git(repo: Path, *args: str) -> str | None:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _receipt_hash(receipt: dict) -> str:
    return sha256_bytes(_canon({k: v for k, v in receipt.items() if k != "receipt_sha256"}))


def _train_stats(lots: list[dict]) -> tuple[list[float], list[float]]:
    rows = np.concatenate([np.asarray(l["public"]["features"], float)[np.asarray(l["public"]["candidate"], bool)]
                           for l in lots])
    return np.nanmean(rows, axis=0).tolist(), np.nanstd(rows, axis=0).tolist()


def _all_test_seeds(config: dict) -> list[tuple[str, int]]:
    return [(s, int(x)) for s, xs in config["splits"]["test_seeds_by_scenario"].items() for x in xs]


def check_disjoint(config: dict) -> None:
    sp = config["splits"]
    tr, va = set(map(int, sp["train_seeds"])), set(map(int, sp["validation_seeds"]))
    te = [x for _, x in _all_test_seeds(config)]
    if tr & va or tr & set(te) or va & set(te) or len(te) != len(set(te)):
        raise HarnessError("train/validation/test seeds are not disjoint")


def prepare(root: Path, *, data_api: Any = None, model_api: Any = None, repo: Path = REPO,
            source_files: tuple = SOURCE_FILES, config_path: Any = None) -> dict:
    root = Path(root)
    if (root / "freeze.json").exists() or (root / "campaign").exists() or (root / "lots").exists():
        raise HarnessError("root already prepared; refusing to replace a freeze")
    data_api, model_api = _apis(data_api, model_api)
    config = data_api.load_config(config_path)
    check_disjoint(config)
    root.mkdir(parents=True, exist_ok=True)
    manifest = []
    for split, seeds in (("train", config["splits"]["train_seeds"]), ("validation", config["splits"]["validation_seeds"])):
        for seed in seeds:
            lot = data_api.generate_lot(int(seed), TRAIN_SCENARIO, config)
            manifest.append(save_lot(root / "lots" / split / str(lot["public"]["lot_id"]), lot, split, int(seed),
                                     TRAIN_SCENARIO))
    write_json(root / "dataset_manifest.json", {"lots": manifest})
    train = [load_lot_for_training(Path(m["path"])) for m in manifest if m["split"] == "train"]
    valid = [load_lot_for_training(Path(m["path"])) for m in manifest if m["split"] == "validation"]
    c0, w0 = time.process_time(), time.perf_counter()
    model = model_api.train_model(train, config)
    train_t = {"cpu_s": time.process_time() - c0, "wall_s": time.perf_counter() - w0}
    write_json(root / "model.json", model)
    c0, w0 = time.process_time(), time.perf_counter()
    validation = model_api.evaluate_model(model, valid, config)
    val_t = {"cpu_s": time.process_time() - c0, "wall_s": time.perf_counter() - w0}
    write_json(root / "model_validation.json", {"label": "model classification validation (development lots)",
                                                "metrics": validation, "timing": val_t})
    mean, sd = _train_stats(train)
    head = _git(repo, "rev-parse", "HEAD")
    receipt = {
        "schema_version": 1, "study_id": config.get("study_id"), "frozen_at": _now(),
        "config_sha256": sha256_bytes(_canon(config)), "source_sha256": source_hashes(repo, source_files),
        "source_commit": head, "source_dirty": _git(repo, "status", "--porcelain", "--", *source_files),
        "versions": {"python": platform.python_version(), "numpy": np.__version__},
        "train_seeds": list(config["splits"]["train_seeds"]), "validation_seeds": list(config["splits"]["validation_seeds"]),
        "test_seeds": _all_test_seeds(config), "train_scenario": TRAIN_SCENARIO,
        "model_hash": model_api.hash_model(model), "model_file_sha256": sha256_file(root / "model.json"),
        "model_validation_sha256": sha256_file(root / "model_validation.json"),
        "dataset_manifest_sha256": sha256_file(root / "dataset_manifest.json"),
        "impute_mean": mean, "impute_sd": sd, "model_training_timing": train_t,
    }
    receipt["receipt_sha256"] = _receipt_hash(receipt)
    write_json(root / "freeze.json", receipt)
    return {"command": "prepare", "freeze": str(root / "freeze.json"), "receipt_sha256": receipt["receipt_sha256"],
            "model_hash": receipt["model_hash"], "lots": len(manifest)}


def _lot_problems(manifest: list[dict]) -> list[str]:
    out = []
    for m in manifest:
        d = Path(m["path"])
        try:
            same = (sha256_file(d / "public.npz") == m["public_sha256"] and sha256_file(d / "oracle.npz") == m["oracle_sha256"]
                    and sha256_file(d / "metadata.json") == m["metadata_sha256"])
        except OSError:
            same = False
        if not same:
            out.append(f"stored lot changed or missing: {m['lot_id']}")
    return out


def verify_outputs(out_dir: Path) -> list[str]:
    """Read-only integrity check of stored campaign/development outputs."""
    problems = []
    status = read_json(out_dir / "status.json")
    if sha256_file(out_dir / "metrics.json") != status.get("metrics_sha256"):
        problems.append("metrics.json hash differs from status receipt")
    tm = out_dir / "test_manifest.json"
    if tm.exists() or status.get("test_manifest_sha256"):
        if not tm.exists() or sha256_file(tm) != status.get("test_manifest_sha256"):
            problems.append("test_manifest.json hash differs from status receipt")
        else:
            problems += _lot_problems(read_json(tm)["lots"])
    for r in read_json(out_dir / "metrics.json")["runs"]:
        lp = out_dir / r["ledger"]
        if not lp.exists() or sha256_file(lp) != r["ledger_sha256"]:
            problems.append(f"ledger changed or missing: {r['ledger']}")
    return problems


def verify_freeze(root: Path, config: dict, model_api: Any, repo: Path, source_files: tuple) -> tuple[dict, dict]:
    root = Path(root)
    if not (root / "freeze.json").exists():
        raise HarnessError("no freeze receipt; run prepare first")
    fr = read_json(root / "freeze.json")
    problems = []
    if _receipt_hash(fr) != fr.get("receipt_sha256"):
        problems.append("freeze receipt hash mismatch")
    try:
        if source_hashes(repo, source_files) != fr["source_sha256"]:
            problems.append("source files changed since freeze")
    except HarnessError as exc:
        problems.append(str(exc))
    if sha256_bytes(_canon(config)) != fr["config_sha256"]:
        problems.append("protocol config changed since freeze")
    if sha256_file(root / "model.json") != fr["model_file_sha256"]:
        problems.append("model file changed since freeze")
    model = read_json(root / "model.json")
    if model_api.hash_model(model) != fr["model_hash"]:
        problems.append("model hash mismatch")
    if sha256_file(root / "model_validation.json") != fr["model_validation_sha256"]:
        problems.append("model validation record changed")
    versions = {"python": platform.python_version(), "numpy": np.__version__}
    if versions != fr["versions"]:
        problems.append(f"runtime versions differ from freeze: {versions} vs {fr['versions']}")
    if sha256_file(root / "dataset_manifest.json") != fr["dataset_manifest_sha256"]:
        problems.append("dataset manifest changed")
    else:
        problems += _lot_problems(read_json(root / "dataset_manifest.json")["lots"])
    try:
        check_disjoint(config)
    except HarnessError as exc:
        problems.append(str(exc))
    if [list(x) for x in fr["test_seeds"]] != [list(x) for x in _all_test_seeds(config)]:
        problems.append("test seed list differs from freeze")
    if problems:
        raise HarnessError("; ".join(problems))
    return fr, model


# ---------- campaign ----------

def _run_lot(lot_dir: Path, *, data_api, model_api, config, model, freeze, policies, modes, budgets,
             ledger_root: Path) -> tuple[list[dict], dict]:
    meta = read_json(lot_dir / "metadata.json")
    public = load_public(lot_dir)
    view = sanitize_public(public, freeze["impute_mean"], freeze["impute_sd"])
    c0, w0 = time.process_time(), time.perf_counter()
    frozen_p = np.asarray(model_api.predict(model, view.features_imputed), float)
    initial_inference_timing = {"cpu_s": time.process_time() - c0, "wall_s": time.perf_counter() - w0}
    frozen_p.flags.writeable = False
    frozen_p_hash = sha256_bytes(frozen_p.tobytes())
    out = []
    for mode in modes:
        for pol in policies:
            for b in budgets:
                sim = ControlledReviewSimulator(lot_dir, data_api)
                run = harness.run_selection(view, sim, policy_name=pol, mode=mode, budget=b, frozen_model=model,
                                            frozen_p=frozen_p, model_api=model_api, config=config)
                if model_api.hash_model(model) != freeze["model_hash"] or sha256_bytes(frozen_p.tobytes()) != frozen_p_hash:
                    raise HarnessError("frozen model or baseline predictions mutated during a run")
                oracle = sim.release_for_evaluation()
                metrics = harness.evaluate_run(run, oracle, view.candidate, frozen_p, config)
                lp = ledger_root / meta["lot_id"] / mode / pol / f"b{int(b)}.jsonl"
                atomic_write(lp, "".join(json.dumps(r, default=_jsonable) + "\n" for r in run["rows"]))
                out.append({"lot_id": meta["lot_id"], "scenario": meta["scenario"], "seed": meta["seed"],
                            "policy": pol, "mode": mode, "budget": int(b), "status": "complete",
                            "online_model_hash": run["online_model_hash"], "frozen_model_hash": freeze["model_hash"],
                            "frozen_p_sha256": frozen_p_hash, "ledger": lp.relative_to(ledger_root.parent).as_posix(),
                            "ledger_sha256": sha256_file(lp), "metrics": metrics})
    return out, {"lot_id": meta["lot_id"], "frozen_p_sha256": frozen_p_hash,
                 "initial_inference_timing": initial_inference_timing}


def _checkpoint(out_dir: Path, metrics: dict, status: dict, **fields: Any) -> None:
    write_json(out_dir / "metrics.json", metrics)
    status.update(fields, metrics_sha256=sha256_file(out_dir / "metrics.json"))
    tm = out_dir / "test_manifest.json"
    if tm.exists():
        status["test_manifest_sha256"] = sha256_file(tm)
    write_json(out_dir / "status.json", status)


def _execute(out_dir: Path, kind: str, lot_dirs_fn, *, data_api, model_api, config, model, freeze,
             policies, modes, budgets) -> dict:
    if out_dir.exists():
        raise HarnessError(f"{out_dir} already exists (completed or partial); refusing to replace or resume")
    out_dir.mkdir(parents=True)
    status = {"kind": kind, "state": "running", "started_at": _now(), "freeze_sha256": freeze["receipt_sha256"],
              "policies": list(policies), "modes": list(modes), "budgets": [int(b) for b in budgets]}
    write_json(out_dir / "status.json", status)
    metrics = {"kind": kind, "freeze_sha256": freeze["receipt_sha256"], "runs": [], "lots": []}
    try:
        for lot_dir in lot_dirs_fn():
            runs, lot = _run_lot(lot_dir, data_api=data_api, model_api=model_api, config=config, model=model,
                                 freeze=freeze, policies=policies, modes=modes, budgets=budgets,
                                 ledger_root=out_dir / "ledgers")
            metrics["runs"].extend(runs)
            metrics["lots"].append(lot)
            _checkpoint(out_dir, metrics, status, progress_lots=len(metrics["lots"]), updated_at=_now())
    except BaseException as exc:
        _checkpoint(out_dir, metrics, status, state="failed", error=repr(exc), traceback=traceback.format_exc(),
                    failed_at=_now())
        raise
    _checkpoint(out_dir, metrics, status, state="completed", completed_at=_now())
    return {"out": str(out_dir), "runs": len(metrics["runs"]), "lots": len(metrics["lots"]), "kind": kind}


def campaign(root: Path, *, data_api: Any = None, model_api: Any = None, repo: Path = REPO,
             source_files: tuple = SOURCE_FILES, config_path: Any = None, _plan: list | None = None) -> dict:
    """Held-out campaign. ``_plan`` (tests only) restricts lots and labels the run development."""
    root = Path(root)
    data_api, model_api = _apis(data_api, model_api)
    config = data_api.load_config(config_path)
    if (root / "campaign").exists():
        raise HarnessError("campaign directory already exists (completed or partial); refusing to replace or resume")
    freeze, model = verify_freeze(root, config, model_api, repo, source_files)
    plan = _all_test_seeds(config) if _plan is None else list(_plan)
    kind = "primary_campaign" if _plan is None else "development"
    test_manifest: list[dict] = []

    def lots():
        for scenario, seed in plan:
            lot = data_api.generate_lot(int(seed), scenario, config)
            m = save_lot(root / "lots" / "test" / str(lot["public"]["lot_id"]), lot, "test", int(seed), scenario)
            test_manifest.append(m)
            write_json(root / "campaign" / "test_manifest.json", {"lots": test_manifest})
            yield Path(m["path"])

    res = _execute(root / "campaign", kind, lots, data_api=data_api, model_api=model_api, config=config, model=model,
                   freeze=freeze, policies=config["policies"], modes=config["modes"], budgets=config["budgets"])
    return {"command": "campaign", **res}


def reproduce(root: Path, *, data_api: Any = None, model_api: Any = None, repo: Path = REPO,
              source_files: tuple = SOURCE_FILES, config_path: Any = None, lots: int = 1,
              budgets: list[int] | None = None) -> dict:
    """Development-only quick check on stored validation lots; never generates held-out lots."""
    root = Path(root)
    data_api, model_api = _apis(data_api, model_api)
    config = data_api.load_config(config_path)
    freeze, model = verify_freeze(root, config, model_api, repo, source_files)
    vals = [Path(m["path"]) for m in read_json(root / "dataset_manifest.json")["lots"] if m["split"] == "validation"][:lots]
    out = root / "development" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    res = _execute(out, "development", lambda: iter(vals), data_api=data_api, model_api=model_api, config=config,
                   model=model, freeze=freeze, policies=config["policies"], modes=config["modes"],
                   budgets=budgets or [min(config["budgets"])])
    return {"command": "reproduce", "label": "development (validation lots, not held-out results)", **res}


def report(root: Path, *, data_api: Any = None, model_api: Any = None, repo: Path = REPO,
           source_files: tuple = SOURCE_FILES, source: Path | None = None, config_path: Any = None) -> dict:
    """Read-only: verifies freeze and stored output hashes, then reports; never reruns or recomputes runs."""
    root = Path(root)
    src = Path(source) if source else root / "campaign"
    if not (src / "metrics.json").exists() or not (src / "status.json").exists():
        raise HarnessError(f"no stored metrics/status in {src}")
    data_api, model_api = _apis(data_api, model_api)
    config = data_api.load_config(config_path)
    freeze, _ = verify_freeze(root, config, model_api, repo, source_files)
    problems = verify_outputs(src)
    if problems:
        raise HarnessError("integrity check failed; refusing to report: " + "; ".join(problems))
    metrics = read_json(src / "metrics.json")
    status = read_json(src / "status.json")
    if status.get("freeze_sha256") != freeze["receipt_sha256"] or metrics.get("freeze_sha256") != freeze["receipt_sha256"]:
        raise HarnessError("stored outputs were not produced under the current freeze receipt")
    mv = read_json(root / "model_validation.json")
    rep = reporting.build_report(metrics, config, status, freeze, mv)
    write_json(src / "report.json", rep)
    atomic_write(src / "report.md", reporting.render_markdown(rep))
    return {"command": "report", "report": str(src / "report.json"), "markdown": str(src / "report.md"),
            "kind": rep["kind"], "primary_success": rep["primary"].get("success")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m inspection_review.cli")
    ap.add_argument("command", choices=["prepare", "campaign", "report", "reproduce"])
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--source", type=Path, help="report: metrics directory (default <root>/campaign)")
    ap.add_argument("--lots", type=int, default=1, help="reproduce: number of validation lots")
    ap.add_argument("--budgets", type=int, nargs="*", help="reproduce: budgets (default smallest)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "prepare":
            out = prepare(root)
        elif args.command == "campaign":
            out = campaign(root)
        elif args.command == "reproduce":
            out = reproduce(root, lots=args.lots, budgets=args.budgets)
        else:
            out = report(root, source=args.source)
        print(json.dumps({"ok": True, **out}, indent=2, default=str))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "command": args.command, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())

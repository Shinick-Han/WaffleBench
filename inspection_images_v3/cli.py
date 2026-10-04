"""python -m inspection_images_v3.cli {prepare-data|train|verify|evaluate} ...

Runs in the separate CPU torch environment. ``prepare-data`` reuses the already downloaded
v1 archive and pinned split copy read-only and extracts PCB3 only into the v3 build dir.
``evaluate`` is for the coordinator only.
"""

import argparse
import json
import pathlib
import shutil
import sys

from inspection_images import data
from inspection_images_v3 import pipeline

DEFAULT_V1_BUILD = pathlib.Path(r"C:\Users\user\hacknation7th\output\inspection-image-build")


def _paths(build_dir, v1_build_dir):
    build, v1 = pathlib.Path(build_dir).resolve(), pathlib.Path(v1_build_dir).resolve()
    return {"build": build, "archive": v1 / "downloads" / "VisA_20220922.tar",
            "v1_split_csv": v1 / "sources" / "1cls.csv", "weights": v1 / "torch-weights",
            "visa": build / "visa", "split_csv": build / "sources" / "1cls.csv",
            "manifest": build / "data-manifest.json"}


def _check_root_inside_build(root, build):
    root = pathlib.Path(root).resolve()
    if build not in root.parents:
        raise RuntimeError(f"--root {root} must be inside the v3 build dir {build}")


def prepare_data(paths):
    protocol = pipeline.load_protocol()["dataset"]
    if data.sha256_file(paths["v1_split_csv"]) != protocol["split_csv_sha256"]:
        raise RuntimeError("v1 pinned spot-diff 1cls.csv hash mismatch")
    paths["split_csv"].parent.mkdir(parents=True, exist_ok=True)
    if not paths["split_csv"].exists():
        shutil.copyfile(paths["v1_split_csv"], paths["split_csv"])
    csv_sha = data.sha256_file(paths["split_csv"])
    if csv_sha != protocol["split_csv_sha256"]:
        raise RuntimeError("v3 split copy hash mismatch")
    if paths["manifest"].exists():
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        bad = [n for n, f in manifest["files"].items() if data.sha256_file(paths["visa"] / n) != f["sha256"]]
        if bad:
            raise RuntimeError(f"{len(bad)} extracted files differ from data-manifest.json")
        return {"status": "verified", "manifest": str(paths["manifest"]), "files": len(manifest["files"])}
    if paths["visa"].exists() and any(paths["visa"].iterdir()):
        raise RuntimeError(f"{paths['visa']} exists without a manifest; remove the partial extraction manually")
    archive = paths["archive"]
    if archive.stat().st_size != protocol["archive_bytes"]:
        raise RuntimeError("archive size differs from protocol")
    archive_sha = data.sha256_file(archive)
    if archive_sha != protocol["archive_sha256_observed"]:
        raise RuntimeError("archive sha256 differs from protocol")
    files = data.safe_extract(archive, paths["visa"], protocol["archive_extract_prefixes"])
    stray = [n for n in files if not n.startswith(tuple(protocol["archive_extract_prefixes"]))]
    if stray:
        raise RuntimeError(f"unexpected extracted members: {stray[:3]}")
    train_ids, test_ids = data.split_identities(paths["split_csv"], protocol["category"])
    missing = [i for i in train_ids + test_ids if i not in files]
    if missing:
        raise RuntimeError(f"{len(missing)} split images missing from extraction")
    in_tar_csv = files.get("split_csv/1cls.csv", {}).get("sha256")
    manifest = {"schema_version": 1, "archive": {"path": str(archive), "bytes": archive.stat().st_size,
                                                 "sha256": archive_sha, "url": protocol["archive_url"]},
                "split_csv": {"url": protocol["split_csv_url"], "copied_from": str(paths["v1_split_csv"]),
                              "sha256": csv_sha, "matches_archive_copy": in_tar_csv == csv_sha},
                "license": protocol["license"], "license_source": protocol["license_source"],
                "extract_prefixes": protocol["archive_extract_prefixes"], "files": files}
    data.write_json(paths["manifest"], manifest)
    return {"status": "extracted", "manifest": str(paths["manifest"]), "files": len(files),
            "train_images": len(train_ids), "test_images_unlabeled": len(test_ids)}


def extractors(paths, protocol):
    from inspection_images.features import ResNet18Extractor

    common, bb = protocol["image_common"], protocol["backbone"]
    cfg = {"mean": common["mean"], "std": common["std"]}
    return {name: ResNet18Extractor(paths["weights"], {**cfg, "resize": fs["resize"]}, batch=bb["batch"],
                                    threads=bb["threads"])
            for name, fs in protocol["feature_sets"].items()}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="inspection_images_v3.cli")
    parser.add_argument("command", choices=["prepare-data", "train", "verify", "evaluate"])
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--v1-build-dir", default=str(DEFAULT_V1_BUILD), help="read-only archive/split/weights source")
    parser.add_argument("--root", help="run output root (train requires a new/empty directory)")
    args = parser.parse_args(argv)
    paths = _paths(args.build_dir, args.v1_build_dir)
    try:
        build, v1 = paths["build"], pathlib.Path(args.v1_build_dir).resolve()
        if build == v1 or v1 in build.parents or build in v1.parents:
            raise RuntimeError("--build-dir must be a separate v3 directory, not the read-only v1 build")
        if args.command != "prepare-data" and args.root:
            _check_root_inside_build(args.root, paths["build"])
        if args.command == "prepare-data":
            result = prepare_data(paths)
        else:
            if not args.root:
                raise RuntimeError("--root is required")
            protocol = pipeline.load_protocol()
            ex = extractors(paths, protocol)
            if args.command == "train":
                freeze = pipeline.train(args.root, paths["visa"], paths["split_csv"], ex, protocol)
                result = {"status": "frozen", "freeze_digest": freeze["freeze_digest"],
                          "thresholds": freeze["calibration"]["thresholds"],
                          "note": "no test image was scored and no test label was read"}
            elif args.command == "verify":
                freeze = pipeline.verify_freeze(args.root, paths["visa"], paths["split_csv"], ex)
                result = {"status": "verified", "freeze_digest": freeze["freeze_digest"],
                          "note": "verification only; no test image was scored and no test label was read"}
            else:
                evaluation = pipeline.evaluate(args.root, paths["visa"], paths["split_csv"], ex)
                result = {"status": "evaluated", "path": str(pathlib.Path(args.root) / "evaluation.json"),
                          "primary_decision": evaluation["primary_endpoint"]["decision"]}
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports any failure as JSON
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

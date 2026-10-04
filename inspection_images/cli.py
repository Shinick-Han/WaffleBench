"""python -m inspection_images.cli {prepare-data|train|verify|evaluate} ...

Runs in the separate CPU torch environment, never the research .venv. Prints JSON summaries
and exits nonzero on failure.
"""

import argparse
import json
import pathlib
import sys
import urllib.request

from inspection_images import data, pipeline


def _paths(build_dir):
    build = pathlib.Path(build_dir).resolve()
    return {"build": build, "archive": build / "downloads" / "VisA_20220922.tar",
            "visa": build / "visa", "split_csv": build / "sources" / "1cls.csv",
            "manifest": build / "data-manifest.json", "weights": build / "torch-weights"}


def prepare_data(build_dir):
    protocol = pipeline.load_protocol()["dataset"]
    paths = _paths(build_dir)
    for key in ("archive", "split_csv"):
        paths[key].parent.mkdir(parents=True, exist_ok=True)
    if not paths["split_csv"].exists():
        with urllib.request.urlopen(protocol["split_csv_url"], timeout=60) as response:
            paths["split_csv"].write_bytes(response.read())
    csv_sha = data.sha256_file(paths["split_csv"])
    if csv_sha != protocol["split_csv_sha256"]:
        raise RuntimeError("pinned spot-diff 1cls.csv hash mismatch")
    fetched = data.fetch_archive(protocol["archive_url"], paths["archive"], protocol["archive_bytes"],
                                 protocol["archive_max_bytes"])
    archive_sha = data.sha256_file(paths["archive"])
    if archive_sha != protocol["archive_sha256_observed"]:
        raise RuntimeError("archive sha256 differs from the digest recorded in protocol.json")
    if paths["manifest"].exists():
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        bad = [n for n, f in manifest["files"].items() if data.sha256_file(paths["visa"] / n) != f["sha256"]]
        if bad:
            raise RuntimeError(f"{len(bad)} extracted files differ from data-manifest.json")
        return {"status": "verified", "manifest": str(paths["manifest"]), "files": len(manifest["files"])}
    files = data.safe_extract(paths["archive"], paths["visa"], protocol["archive_extract_prefixes"])
    in_tar_csv = files.get("split_csv/1cls.csv", {}).get("sha256")
    manifest = {"schema_version": 1, "archive": {**fetched, "sha256": archive_sha},
                "split_csv": {"url": protocol["split_csv_url"], "sha256": csv_sha,
                              "matches_archive_copy": in_tar_csv == csv_sha},
                "license": protocol["license"], "license_source": protocol["license_source"],
                "extract_prefixes": protocol["archive_extract_prefixes"], "files": files}
    train_ids, test_ids = data.split_identities(paths["split_csv"], protocol["category"])
    missing = [i for i in train_ids + test_ids if i not in files]
    if missing:
        raise RuntimeError(f"{len(missing)} split images missing from extraction")
    data.write_json(paths["manifest"], manifest)
    return {"status": "extracted", "manifest": str(paths["manifest"]), "files": len(files),
            "train_images": len(train_ids), "test_images_unlabeled": len(test_ids)}


def _extractor(paths, protocol):
    from inspection_images.features import ResNet18Extractor

    return ResNet18Extractor(paths["weights"], protocol["image"])


def main(argv=None):
    parser = argparse.ArgumentParser(prog="inspection_images.cli")
    parser.add_argument("command", choices=["prepare-data", "train", "verify", "evaluate"])
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("--root", help="run output root (train requires a new/empty directory)")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare-data":
            result = prepare_data(args.build_dir)
        else:
            if not args.root:
                raise RuntimeError("--root is required")
            paths = _paths(args.build_dir)
            protocol = pipeline.load_protocol()
            extractor = _extractor(paths, protocol)
            if args.command == "train":
                freeze = pipeline.train(args.root, paths["visa"], paths["split_csv"], extractor, protocol)
                result = {"status": "frozen", "freeze_digest": freeze["freeze_digest"],
                          "thresholds": freeze["calibration"]["thresholds"],
                          "note": "no test image was scored and no test label was read"}
            elif args.command == "verify":
                freeze = pipeline.verify_freeze(args.root, paths["visa"], paths["split_csv"], extractor)
                result = {"status": "verified", "freeze_digest": freeze["freeze_digest"]}
            else:
                evaluation = pipeline.evaluate(args.root, paths["visa"], paths["split_csv"], extractor)
                result = {"status": "evaluated", "path": str(pathlib.Path(args.root) / "evaluation.json"),
                          "methods": {k: {m: v[m] for m in ("image_auroc", "image_average_precision")}
                                      for k, v in evaluation["methods"].items()}}
    except Exception as exc:  # noqa: BLE001 - CLI boundary reports any failure as JSON
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

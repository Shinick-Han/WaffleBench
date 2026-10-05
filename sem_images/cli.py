"""python -m sem_images.cli {validate|train|predict|evaluate|export-backends} ...

Post-hackathon development only. Defaults target the calibration split; the test split needs
``--confirm-test`` and is refused once a test receipt exists. Prints JSON; nonzero on failure.
"""

import argparse
import json
import sys

from sem_images import NOTICE, backends, manifest as mf, pipeline, tiling


def _manifest(args):
    return mf.load(args.manifest, allow_fixture=args.allow_fixture_manifest)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sem_images", description=NOTICE)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--manifest", required=True)
        sp.add_argument("--root", required=True, help="run directory")
        sp.add_argument("--threads", type=int, default=2)
        sp.add_argument("--allow-fixture-manifest", action="store_true",
                        help="accept data_mode='fixture' manifests (tests/development only)")

    v = sub.add_parser("validate")
    v.add_argument("--manifest", required=True)
    v.add_argument("--allow-fixture-manifest", action="store_true")

    t = sub.add_parser("train")
    common(t)
    t.add_argument("--epochs", type=int, default=1)
    t.add_argument("--max-train-images", type=int, default=8)
    t.add_argument("--size", type=int, default=tiling.DEFAULT_TILE, help="native-resolution tile size")
    t.add_argument("--overlap", type=int, default=tiling.DEFAULT_OVERLAP)
    t.add_argument("--model", default="unet", choices=["unet"])
    t.add_argument("--encoder", default="resnet18")
    t.add_argument("--encoder-weights", default=None, help="explicit local encoder weights file (no download)")
    t.add_argument("--seed", type=int, default=pipeline.DEFAULT_SEED)
    t.add_argument("--augment-copies", type=int, default=0,
                   help="train-only synthetic scratch/particle copies per image (provenance recorded)")

    for name in ("predict", "evaluate"):
        sp = sub.add_parser(name)
        common(sp)
        sp.add_argument("--split", default="calibration", choices=["calibration", "test"])
        sp.add_argument("--max-images", type=int, default=None)
        sp.add_argument("--confirm-test", action="store_true", help="explicit opt-in for the test split")

    e = sub.add_parser("export-backends")
    e.add_argument("--out", required=True)
    e.add_argument("--size", type=int, default=tiling.DEFAULT_TILE)
    e.add_argument("--overlap", type=int, default=tiling.DEFAULT_OVERLAP)

    args = p.parse_args(argv)
    try:
        if args.command == "validate":
            m = _manifest(args)
            result = {"status": "valid", "data_mode": m.data_mode, "manifest_sha256": m.file_sha256, **m.report}
        elif args.command == "train":
            frozen = pipeline.train(_manifest(args), args.root, epochs=args.epochs,
                                    max_train_images=args.max_train_images, tile=args.size, overlap=args.overlap,
                                    threads=args.threads, model_name=args.model, encoder=args.encoder,
                                    encoder_weights_path=args.encoder_weights, seed=args.seed,
                                    augment_copies=args.augment_copies)
            result = {"status": "frozen", "model_sha256": frozen["model"]["sha256"],
                      "train_items": len(frozen["training"]["item_ids"]), "data_mode": frozen["data_mode"],
                      "class_counts": frozen["training"]["class_counts"], "tiles": frozen["training"]["tiles"],
                      "wall_seconds": frozen["training"]["wall_seconds"]}
        elif args.command == "predict":
            r = pipeline.predict(_manifest(args), args.root, args.split, args.max_images, args.threads,
                                 args.confirm_test)
            result = {"status": "predicted", "split": args.split, "items": len(r["items"]),
                      "model_sha256": r["model_sha256"], "data_mode": r["data_mode"]}
        elif args.command == "evaluate":
            r = pipeline.evaluate(_manifest(args), args.root, args.split, args.max_images, args.threads,
                                  args.confirm_test)
            result = {"status": "evaluated", "split": args.split, "role": r["role"], "data_mode": r["data_mode"],
                      "pixel": r["summary"]["pixel"], "component_recall": r["summary"]["component_recall"]["overall"],
                      "empty_mask_images": r["summary"]["empty_mask_images"]}
        else:
            result = {"status": "exported", "files": backends.export(args.out, tile=args.size, overlap=args.overlap),
                      "note": "configuration only; no backend was run"}
    except (mf.ManifestError, mf.MaskAccessError, pipeline.ProtocolError, ValueError, FileNotFoundError,
            RuntimeError) as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__, "message": str(exc)}))
        return 1
    print(json.dumps({**result, "notice": NOTICE}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

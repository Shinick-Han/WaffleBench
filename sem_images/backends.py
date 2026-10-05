"""Adapters and launch-config exports for upstream anomaly backends.

These are configuration exports only: nothing here runs PatchCore, EfficientAD or DRAEM, and no
outcome is reported for them. Commands mirror the upstream READMEs at the pinned commits and must
be checked against the root-owned fork before any run. The PatchCore tile stitching adapter is
this package's own code; SAHI is cited only as the slicing reference.
"""

import json
import pathlib

import numpy as np

from sem_images import NOTICE, tiling

SOURCES = {
    "patchcore": {"repo": "amazon-science/patchcore-inspection",
                  "url": "https://github.com/amazon-science/patchcore-inspection",
                  "sha": "fcaa92f124fb1ad74a7acf56726decd4b27cbcad", "license_spdx": "Apache-2.0",
                  "license_sha256": "09e8a9bcec8067104652c168685ab0931e7868f9c8284b66f5ae6edae5f1130b"},
    "anomalib": {"repo": "open-edge-platform/anomalib", "url": "https://github.com/open-edge-platform/anomalib",
                 "sha": "335a6be1eac101030d3085082883dc4c1b861dce", "license_spdx": "Apache-2.0",
                 "license_sha256": "7ae7504c2b7e895c3f61320a389af7fdde68143cd4dfd7941247640b0b62cc76"},
    "draem": {"repo": "VitjanZ/DRAEM", "url": "https://github.com/VitjanZ/DRAEM",
              "sha": "2dbf67397ab5c10a1494e5ae70ab59a25d7c35ef", "license_spdx": "MIT",
              "license_sha256": "b8072bab9e17693331b0a1930027b0280bc7c642607871c4f928064807f8fd42"},
    "sahi": {"repo": "obss/sahi", "url": "https://github.com/obss/sahi",
             "sha": "80ebdb699851facf87def03c0597e1aeb87a9a2d", "license_spdx": "MIT"},
}

NORMAL_ONLY = "Representative real normal (defect-free) training and calibration images"


def _prereq(name, satisfied, detail):
    return {"name": name, "satisfied": satisfied, "detail": detail}


def backend_configs(tile=tiling.DEFAULT_TILE, overlap=tiling.DEFAULT_OVERLAP, normal_images_available=False,
                    fork_root="<root-owned fork of the pinned source>"):
    normal = _prereq(NORMAL_ONLY, normal_images_available,
                     "Carinthia-S is defect-only; normal-only training is not possible on it without "
                     "a separate real normal set." if not normal_images_available else "declared available")
    common = {"status": "not_run", "outcome": None, "notice": NOTICE,
              "tiling": {"tile": tile, "overlap": overlap, "resize": None,
                         "stitching": "sem_images.backends.stitch_patchcore_tile_maps (own code)"}}
    return {
        "patchcore": {**common, "backend": "PatchCore (official author code)", "source": SOURCES["patchcore"],
                      "prerequisites": [normal,
                                        _prereq("Isolated env per upstream requirements.txt (faiss, timm, torch)", False,
                                                "not installed by this package"),
                                        _prereq("ImageNet-pretrained WideResNet50 weights with recorded provenance",
                                                False, "weights have separate provenance; no implicit download")],
                      "launch": {"cwd": fork_root,
                                 "command": ["python", "bin/run_patchcore.py", "--gpu", "-1", "--seed", "0",
                                             "--save_patchcore_model", "--log_group", "sem_tiles", "<results_dir>",
                                             "patch_core", "-b", "wideresnet50", "-le", "layer2", "-le", "layer3",
                                             "--pretrain_embed_dimension", "1024", "--target_embed_dimension", "1024",
                                             "--anomaly_scorer_num_nn", "1", "--patchsize", "3",
                                             "sampler", "-p", "0.1", "approx_greedy_coreset",
                                             "dataset", "--resize", str(tile), "--imagesize", str(tile),
                                             "-d", "sem_tiles", "mvtec", "<mvtec_layout_tile_root>"],
                                 "input_layout": "MVTec-style <class>/train/good (normal tiles only), "
                                                 "<class>/test/<label>; tiles exported at native resolution so "
                                                 "--resize equals --imagesize equals tile (no rescale)"}},
        "efficientad": {**common, "backend": "EfficientAD via Anomalib (not author code)",
                        "source": SOURCES["anomalib"],
                        "prerequisites": [normal,
                                          _prereq("Anomalib installed in an isolated env from the pinned commit", False,
                                                  "not installed by this package"),
                                          _prereq("Teacher weights and ImageNet/Imagenette penalty data with licences",
                                                  False, "separate provenance; no implicit download")],
                        "launch": {"cwd": fork_root,
                                   "command": ["anomalib", "train", "--model", "EfficientAd",
                                               "--data", "anomalib.data.Folder", "--data.name", "sem_tiles",
                                               "--data.root", "<folder_tile_root>", "--data.normal_dir", "normal",
                                               "--data.abnormal_dir", "abnormal", "--data.mask_dir", "masks",
                                               "--data.train_batch_size", "1"],
                                   "note": "EfficientAD requires train batch size 1 upstream; paper GPU timings are "
                                           "not a local CPU guarantee. Fast-stage use goes through sem_images.cascade."}},
        "draem": {**common, "backend": "DRAEM (official author code)", "source": SOURCES["draem"],
                  "prerequisites": [normal,
                                    _prereq("Anomaly source texture set (e.g. DTD) with its own licence", False,
                                            "texture licensing is separate"),
                                    _prereq("Isolated env per upstream README", False, "not installed by this package")],
                  "launch": {"cwd": fork_root,
                             "command": ["python", "train_DRAEM.py", "--gpu_id", "-1", "--obj_id", "-1", "--lr",
                                         "0.0001", "--bs", "8", "--epochs", "700", "--data_path",
                                         "<mvtec_layout_tile_root>", "--anomaly_source_path", "<texture_dir>",
                                         "--checkpoint_path", "<checkpoints>", "--log_path", "<logs>"],
                             "note": "Upstream expects MVTec object folders and a CUDA device; CPU use needs a "
                                     "documented patch in the fork. Evaluation images stay real and unaugmented."}},
        "sahi_reference": {"status": "reference_only", "outcome": None, "source": SOURCES["sahi"],
                           "mapping": {"slice_height": tile, "slice_width": tile,
                                       "overlap_height_ratio": overlap / tile, "overlap_width_ratio": overlap / tile},
                           "note": "SAHI slices for object detection; it is not a PatchCore backend. Anomaly-map "
                                   "stitching here is own code."},
    }


def export(out_dir, **kwargs):
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, cfg in backend_configs(**kwargs).items():
        path = out / f"backend-{name}.json"
        path.write_text(json.dumps(cfg, indent=2, sort_keys=True), encoding="utf-8")
        written.append(str(path))
    return written


def stitch_patchcore_tile_maps(tile_maps, tile_plan):
    """Own adapter: upsample per-tile (possibly low-resolution) anomaly maps to the tile size, then
    stitch with overlap weights. Padding is excluded; output has the original image size."""
    t = tile_plan["tile"]
    if len(tile_maps) != len(tile_plan["tiles"]):
        raise ValueError("one anomaly map per planned tile is required")
    full = np.stack([tiling.resize_bilinear(m, t, t) for m in tile_maps])
    return tiling.stitch(full, tile_plan)

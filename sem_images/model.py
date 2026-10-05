"""Lazy segmentation_models.pytorch (MIT) U-Net backend.

The optional backend is imported only when a model is built. Encoder weights default to None;
pretrained weights are used only from an explicit local file and are never downloaded.
"""

import hashlib
import os

SMP_SOURCE = {"repo": "qubvel-org/segmentation_models.pytorch",
              "url": "https://github.com/qubvel-org/segmentation_models.pytorch",
              "sha": "d2f65c5e3c9a34a02d90e19ad798c5f8f99c21ae", "license_spdx": "MIT"}
SUPPORTED_MODELS = ("unet",)
CLASSIFIER_KEYS = ("fc.weight", "fc.bias")
# BatchNorm step counters are non-learned buffers that older torchvision checkpoints (for example
# resnet18-f37072fd.pth) predate. Only these may be absent; they are filled with integer zero.
BN_COUNTER_SUFFIX = ".num_batches_tracked"
LEGACY_COMPAT = "absent *.num_batches_tracked filled with 0; fc.weight/fc.bias dropped; RGB stem summed"


class BackendUnavailable(RuntimeError):
    pass


def import_torch():
    try:
        import torch
    except ImportError as exc:
        raise BackendUnavailable(
            "PyTorch is not importable. Use the isolated SEM env described in SEM_IMAGE_GUIDE.md "
            "(output/post-hackathon/sem-build-20261005/images-env).") from exc
    return torch


def import_smp():
    # Belt and braces: the hub client must never fetch weights implicitly.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    try:
        import segmentation_models_pytorch as smp
    except ImportError as exc:
        raise BackendUnavailable(
            "segmentation_models_pytorch is not importable. Install the pinned source "
            f"git+{SMP_SOURCE['url']}@{SMP_SOURCE['sha']} into the isolated SEM env "
            "(see sem_images/requirements-sem.txt); it is an optional dependency.") from exc
    return smp


def model_config(name="unet", encoder="resnet18", encoder_weights_path=None, decoder_channels=(128, 64, 32, 16, 8)):
    if name not in SUPPORTED_MODELS:
        raise ValueError(f"unsupported model {name!r}; supported: {SUPPORTED_MODELS}")
    return {"name": name, "encoder": encoder, "encoder_depth": len(decoder_channels),
            "decoder_channels": list(decoder_channels), "in_channels": 1, "classes": 1,
            "encoder_weights_path": str(encoder_weights_path) if encoder_weights_path else None,
            "encoder_weights_sha256": _file_sha256(encoder_weights_path) if encoder_weights_path else None,
            "encoder_weights_compat": LEGACY_COMPAT if encoder_weights_path else None,
            "backend": SMP_SOURCE}


def _file_sha256(path):
    if not os.path.isfile(path):
        raise FileNotFoundError(f"explicit encoder weights file not found: {path} (no download fallback)")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(config):
    torch = import_torch()
    smp = import_smp()
    net = smp.Unet(encoder_name=config["encoder"], encoder_weights=None,
                   encoder_depth=config["encoder_depth"], decoder_channels=config["decoder_channels"],
                   in_channels=config["in_channels"], classes=config["classes"])
    path = config.get("encoder_weights_path")
    if path:
        if _file_sha256(path) != config.get("encoder_weights_sha256"):
            raise ValueError("encoder weights file hash differs from the recorded config hash")
        state = torch.load(path, map_location="cpu", weights_only=True)
        load_encoder_state(net.encoder, state, config["in_channels"])
    return net


def load_encoder_state(encoder, state, in_channels):
    """Strict load of a torchvision-style ResNet state into the SMP encoder.

    Only the known ImageNet classifier keys (fc.weight, fc.bias) may be dropped. Absent BatchNorm
    `*.num_batches_tracked` counters (legacy checkpoints) are filled with integer zero matching the
    encoder buffer; any other missing key or any other unexpected key is rejected. The input mapping
    is not mutated. Returns the sorted list of zero-filled counter keys.
    """
    import torch
    state = {k: v for k, v in state.items() if k not in CLASSIFIER_KEYS}
    conv1 = state.get("conv1.weight")
    if conv1 is not None and conv1.ndim == 4 and conv1.shape[1] == 3 and in_channels == 1:
        state["conv1.weight"] = conv1.sum(dim=1, keepdim=True)  # RGB stem -> grayscale, documented
    reference = encoder.state_dict()
    missing, unexpected = sorted(set(reference) - set(state)), sorted(set(state) - set(reference))
    filled = [k for k in missing if k.endswith(BN_COUNTER_SUFFIX)]
    missing = [k for k in missing if not k.endswith(BN_COUNTER_SUFFIX)]
    if missing or unexpected:
        raise ValueError(f"encoder weights mismatch: missing={missing[:5]} ({len(missing)}), "
                         f"unexpected={unexpected[:5]} ({len(unexpected)})")
    for k in filled:
        state[k] = torch.zeros_like(reference[k])  # same integer dtype, device and shape
    encoder.load_state_dict(state, strict=True)
    return filled

"""CPU ResNet18 layer2/layer3 patch descriptors on a fixed 14x14 grid (torch imported lazily).

Adapted from the PatchCore recipe: ResNet18 instead of WideResNet50, 3x3 average local
aggregation per layer, layer2 average-pooled to the layer3 14x14 grid (PatchCore upsamples
layer3 instead), plain channel concatenation (no adaptive projection to 1024 dims).
"""

import importlib.metadata
import pathlib

import numpy as np

from inspection_images.data import sha256_file

WEIGHTS_URL = "https://download.pytorch.org/models/resnet18-f37072fd.pth"


class ResNet18Extractor:
    def __init__(self, weights_dir, image_cfg, batch=16, threads=4):
        import torch
        import torchvision
        from torchvision.models import resnet18

        torch.manual_seed(0)
        torch.use_deterministic_algorithms(True)
        torch.set_num_threads(threads)
        weights_dir = pathlib.Path(weights_dir)
        weights_dir.mkdir(parents=True, exist_ok=True)
        state = torch.hub.load_state_dict_from_url(
            WEIGHTS_URL, model_dir=str(weights_dir), progress=False, check_hash=True, weights_only=True)
        self.weights_path = weights_dir / pathlib.Path(WEIGHTS_URL).name
        net = resnet18(weights=None)
        net.load_state_dict(state)
        self.net = net.eval()
        self.torch = torch
        self.cfg = image_cfg
        self.batch = batch
        self.threads = threads
        self._versions = {"torch": torch.__version__, "torchvision": torchvision.__version__,
                          "pillow": importlib.metadata.version("pillow"),
                          "numpy": np.__version__}

    def identity(self):
        return {"backbone": "torchvision.resnet18", "weights_url": WEIGHTS_URL,
                "weights_file": self.weights_path.name, "weights_sha256": sha256_file(self.weights_path),
                "threads": self.threads, "batch": self.batch, "packages": self._versions}

    def _load(self, path):
        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGB").resize(tuple(self.cfg["resize"]), Image.BILINEAR)
            x = np.asarray(image, dtype=np.float32) / 255.0
        x = (x - np.asarray(self.cfg["mean"], np.float32)) / np.asarray(self.cfg["std"], np.float32)
        return x.transpose(2, 0, 1)

    def extract(self, paths):
        torch, net = self.torch, self.net
        F = torch.nn.functional
        out = []
        with torch.no_grad():
            for lo in range(0, len(paths), self.batch):
                x = torch.from_numpy(np.stack([self._load(p) for p in paths[lo:lo + self.batch]]))
                x = net.maxpool(net.relu(net.bn1(net.conv1(x))))
                l2 = net.layer2(net.layer1(x))
                l3 = net.layer3(l2)
                l2 = F.avg_pool2d(F.avg_pool2d(l2, 3, 1, 1, count_include_pad=False), 2, 2)
                l3 = F.avg_pool2d(l3, 3, 1, 1, count_include_pad=False)
                feat = torch.cat([l2, l3], dim=1)  # (b, 384, 14, 14)
                out.append(feat.flatten(2).transpose(1, 2).numpy().astype(np.float32))
        result = np.concatenate(out)
        if not np.all(np.isfinite(result)):
            raise ValueError("non-finite descriptors")
        return result

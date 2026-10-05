"""Explicit local frozen-model inference on acquired grayscale frames; no downloads."""
import copy
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

from sem_efficiency.core import digest


class FrozenPerception:
    def __init__(self, model_root, threads=2):
        if type(threads) is not int or not 1 <= threads <= 8:
            raise ValueError('threads must be an integer in 1..8')
        self.root = Path(model_root).resolve()
        self.receipt = self.root / 'frozen.json'
        self.frozen = json.loads(self.receipt.read_text(encoding='utf-8'))
        self.receipt_sha = digest(self.receipt)
        self.path = (self.root / self.frozen['model']['file']).resolve()
        if self.root not in self.path.parents or digest(self.path) != self.frozen['model']['sha256']:
            raise ValueError('model path/hash mismatch')
        from sem_images import model
        self.torch = model.import_torch()
        self.torch.set_num_threads(threads)
        config = copy.deepcopy(self.frozen['model']['config'])
        config['encoder_weights_path'] = None
        config['encoder_weights_sha256'] = None
        start = time.perf_counter()
        self.net = model.build(config)
        self.net.load_state_dict(self.torch.load(self.path, weights_only=True, map_location='cpu'), strict=True)
        self.net.eval()
        self.cold_load_s = time.perf_counter() - start

    def predict(self, image_path, expected_sha256):
        start = time.perf_counter()
        if digest(self.receipt) != self.receipt_sha or digest(self.path) != self.frozen['model']['sha256']:
            raise ValueError('frozen model/receipt changed')
        path = Path(image_path)
        if digest(path) != expected_sha256:
            raise ValueError('acquired image hash mismatch')
        with Image.open(path) as im:
            if im.mode != 'L':
                raise ValueError('model requires an 8-bit grayscale acquired image')
            image = np.asarray(im, dtype=np.float32) / 255.0
        from sem_images import pipeline, tiling
        cfg = self.frozen['tiling']
        layout = tiling.plan(*image.shape, tile=cfg['tile'], overlap=cfg['overlap'])
        tiles, _ = tiling.extract(image, layout, pad_mode=cfg['pad_mode'])
        probability = tiling.stitch(pipeline._tile_batches(self.net, self.torch, tiles, 8), layout).astype(np.float32)
        return probability, {'image_sha256': expected_sha256, 'model_sha256': self.frozen['model']['sha256'],
                             'cold_model_load_s': self.cold_load_s,
                             'image_verification_and_inference_s': time.perf_counter() - start,
                             'tiles': len(tiles), 'native_shape': list(image.shape),
                             'masks_read': False, 'physical_scale': None}

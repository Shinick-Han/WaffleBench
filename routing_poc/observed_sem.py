"""Optional SEM perception AFTER a replay action has been admitted.

This observer never emits pre-acquisition policy features or physical DOI truth.
Only previously declared calibration images are allowed; test images are refused.
The standard routing policies use optical priors and modeled movement costs.
Observer wall time is separate from historical action-time charges.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path


def _hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


class ObservedSEM:
    def __init__(self, model_root, output_root, threads=2):
        if isinstance(threads, bool) or not isinstance(threads, int) or not 1 <= threads <= 8:
            raise ValueError('threads must be an integer in 1..8')
        self.root = Path(model_root).resolve()
        self.frozen_path = self.root / 'frozen.json'
        self.frozen = json.loads(self.frozen_path.read_text(encoding='utf-8'))
        self.model_path = (self.root / self.frozen['model']['file']).resolve()
        if self.root not in self.model_path.parents:
            raise ValueError('model path must remain under model root')
        if _hash(self.model_path) != self.frozen['model']['sha256']:
            raise ValueError('model hash differs from frozen receipt')
        manifest_path = Path(self.frozen['manifest']['path'])
        if _hash(manifest_path) != self.frozen['manifest']['file_sha256']:
            raise ValueError('manifest hash differs from frozen receipt')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('data_mode') != 'real' or self.frozen.get('data_mode') != 'real':
            raise ValueError('observer requires a declared real-image development manifest')
        image_root = Path(manifest['root']).resolve()
        self.allowed = {}
        for item in manifest['items']:
            if item['split'] != 'calibration':
                continue
            image_path = (image_root / item['image']).resolve()
            if image_root not in image_path.parents:
                raise ValueError('manifest image path escapes data root')
            self.allowed[str(image_path).casefold()] = item['image_sha256']
        self.output = Path(output_root).resolve()
        if self.output.exists():
            raise ValueError('observer output exists; use a fresh evidence directory')
        self.output.mkdir(parents=True)
        self.threads = threads
        self.net = None
        self.sequence = 0

    def __call__(self, attempt, candidate):
        if attempt.get('status') != 'ok':
            return {'status': 'not_run', 'reason': 'capture was not valid'}
        image_path = Path(attempt['image_path']).resolve()
        expected = self.allowed.get(str(image_path).casefold())
        if expected is None:
            raise ValueError('image is not an allowed calibration image; test/unregistered access refused')
        if attempt.get('image_sha256') != expected or _hash(image_path) != expected:
            raise ValueError('observed image hash differs from declared calibration content')
        if _hash(self.model_path) != self.frozen['model']['sha256']:
            raise ValueError('frozen model changed before observation')
        from sem_images import model, tiling
        import numpy as np
        from PIL import Image
        torch = model.import_torch()
        if self.net is None:
            torch.set_num_threads(self.threads)
            config = copy.deepcopy(self.frozen['model']['config'])
            # Load the entire frozen trained state, without re-reading initial encoder weights.
            config['encoder_weights_path'] = None
            config['encoder_weights_sha256'] = None
            self.net = model.build(config)
            self.net.load_state_dict(torch.load(self.model_path, map_location='cpu', weights_only=True))
            self.net.eval()
        with Image.open(image_path) as image:
            if image.mode != 'L':
                raise ValueError('expected an 8-bit grayscale SEM image')
            pixels = np.asarray(image, dtype=np.float32) / 255.0
        config = self.frozen['tiling']
        plan = tiling.plan(*pixels.shape, tile=config['tile'], overlap=config['overlap'])
        tiles, _ = tiling.extract(pixels, plan, pad_mode=config['pad_mode'])
        maps = []
        with torch.no_grad():
            for start in range(0, len(tiles), 8):
                batch = torch.from_numpy(np.ascontiguousarray(tiles[start:start + 8]))[:, None]
                maps.append(torch.sigmoid(self.net(batch))[:, 0].numpy())
        probability = tiling.stitch(np.concatenate(maps), plan).astype(np.float32)
        threshold = self.frozen['decision']['threshold']
        if isinstance(threshold, bool) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError('invalid frozen threshold')
        self.sequence += 1
        target = self.output / f'perception-{self.sequence:04d}.npy'
        with target.open('xb') as stream:
            np.save(stream, probability, allow_pickle=False)
        return {
            'status': 'observed', 'role': 'post-acquisition auxiliary localization',
            'site_id': candidate['site_id'], 'image_sha256': expected,
            'model_sha256': self.frozen['model']['sha256'],
            'probability_map': str(target), 'probability_map_sha256': _hash(target),
            'shape': list(probability.shape), 'max_pixel_probability': float(probability.max()),
            'positive_pixels': int((probability >= threshold).sum()), 'threshold': threshold,
            'masks_read': False, 'physical_doi_confirmed': False,
            'commercial_validated': False,
            'limitation': 'Calibration image localization; no physical acquisition or routing-performance evidence. Pixel scores are not calibrated site-level DOI probabilities.',
        }

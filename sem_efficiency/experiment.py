"""Executable development comparison; original test data is never accepted here."""
import argparse
import copy
import io
import json
import math
import tarfile
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image

from sem_images import manifest as mf, metrics, model as backend, pipeline, tiling
from . import NOTICE
from .core import (ScopedReader, balanced_ids, digest, make_partition, now, safe_member,
                   source_freeze, verify_source, write)
from .models import SmallHead, masked_loss


def encode(net, images):
    with torch.no_grad():
        features = net.decoder(net.encoder(images))
    if features.shape[1] != 8:
        raise ValueError('This registered small-head experiment requires eight decoder channels')
    return features


def cache_image(net, image, target, folder, name):
    tp = tiling.plan(*image.shape, tile=256, overlap=64)
    xs, valid = tiling.extract(image, tp)
    features = []
    with torch.no_grad():
        for start in range(0, len(xs), 8):
            x = torch.from_numpy(xs[start:start+8].copy())[:, None]
            features.append(encode(net, x).numpy())
    f = np.concatenate(features)
    path = folder / (name + '.features.npy')
    np.save(path, f, allow_pickle=False)
    np.save(folder / (name + '.valid.npy'), valid, allow_pickle=False)
    if target is not None:
        ys, _ = tiling.extract(target.astype(np.float32), tp, pad_mode='constant')
        np.save(folder / (name + '.target.npy'), ys, allow_pickle=False)
    return {'id': name, 'plan': tp, 'features': str(path), 'feature_sha256': digest(path),
            'valid': str(folder / (name + '.valid.npy')), 'tiles': len(xs),
            'target': str(folder / (name + '.target.npy')) if target is not None else None,
            'dtype': str(f.dtype), 'feature_extraction_only_seconds': None}


def prepare_nffa(root, raw, forbidden, per_category=32):
    """Extract only registered raster bytes; never execute archived code or deserialize models."""
    result = []
    for category in ('Patterned_surface', 'MEMS_devices_and_electrodes'):
        receipts = list((raw / 'receipts').glob('nffa-sem-v2--' + category + '*.json'))
        receipt = next(json.loads(p.read_text()) for p in receipts
                       if json.loads(p.read_text())['status'] == 'downloaded')
        archive = Path(receipt['path'])
        if digest(archive) != receipt['sha256']:
            raise ValueError('NFFA archive changed since verified acquisition')
        with tarfile.open(archive) as tar:
            candidates = [m for m in tar.getmembers() if safe_member(m)]
            candidates.sort(key=lambda m: __import__('hashlib').sha256(m.name.encode()).hexdigest())
            selected = 0
            for member in candidates:
                data = tar.extractfile(member).read()
                sha = __import__('hashlib').sha256(data).hexdigest()
                if sha in forbidden or any(r['sha256'] == sha for r in result):
                    continue
                with Image.open(io.BytesIO(data)) as im:
                    arr = np.asarray(im.convert('L'), dtype=np.float32) / 255
                if min(arr.shape) < 128:
                    continue
                name = 'nffa-' + sha[:24]
                p = root / 'nffa-source' / (name + Path(member.name).suffix.lower())
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                result.append({'id': name, 'path': str(p), 'sha256': sha, 'category': category,
                               'archive_sha256': receipt['sha256'], 'archive_member': member.name,
                               'license': 'CC-BY-4.0', 'role': 'self-supervised training only'})
                selected += 1
                if selected == per_category:
                    break
            if selected != per_category:
                raise ValueError('Insufficient eligible NFFA images')
    write(root / 'nffa-source-manifest.json', result)
    return result


def batch(records, indices, with_target=True):
    f, y, v = [], [], []
    for r, tile in indices:
        f.append(np.load(r['features'], allow_pickle=False, mmap_mode='r')[tile])
        v.append(np.load(r['valid'], allow_pickle=False, mmap_mode='r')[tile])
        if with_target:
            y.append(np.load(r['target'], allow_pickle=False, mmap_mode='r')[tile])
    x = torch.from_numpy(np.stack(f).copy())
    val = torch.from_numpy(np.stack(v).astype(np.float32))[:, None]
    target = torch.from_numpy(np.stack(y).astype(np.float32))[:, None] if with_target else None
    return x, target, val


def sampled_patches(records, rng):
    """Four tiles per parent, with true training foreground and background coverage."""
    indices = []
    for r in records:
        ys = np.load(r['target'], allow_pickle=False, mmap_mode='r')
        valid = np.load(r['valid'], allow_pickle=False, mmap_mode='r')
        fg = np.flatnonzero((ys * valid).sum((1, 2)) > 0)
        bg = np.flatnonzero((ys * valid).sum((1, 2)) == 0)
        for pool in (fg, fg, bg, bg):
            use = pool if len(pool) else np.arange(len(ys))
            indices.append((r, int(rng.choice(use))))
    rng.shuffle(indices)
    return indices


def self_supervise(base_head, records, seed=731):
    torch.manual_seed(seed)
    net = SmallHead(base_head, adapter=True)
    reconstruction = torch.nn.Conv2d(8, 1, 1)
    opt = torch.optim.Adam(list(net.adapter.parameters()) + list(reconstruction.parameters()), lr=1e-3)
    history = []
    rng = np.random.default_rng(seed)
    for epoch in range(8):
        indices = [(r, 0) for r in records]
        rng.shuffle(indices)
        losses = []
        for start in range(0, len(indices), 8):
            x, y, valid = batch(records, indices[start:start+8])
            mask = (torch.rand((x.shape[0], 1, 16, 16)) < .3).float()
            mask = torch.nn.functional.interpolate(mask, x.shape[-2:], mode='nearest')
            prediction = torch.sigmoid(reconstruction(net.adapted(x * (1-mask))))
            weight = valid * mask
            loss = ((prediction-y).square() * weight).sum() / weight.sum().clamp_min(1)
            opt.zero_grad(); loss.backward(); opt.step()
            losses.append(float(loss.detach()))
        history.append({'epoch': epoch+1, 'masked_reconstruction_mse': float(np.mean(losses))})
    return copy.deepcopy(net.adapter.state_dict()), history


def fit_head(base_head, records, seed, adapter_state=None):
    torch.manual_seed(seed)
    net = SmallHead(base_head, adapter=adapter_state is not None)
    if adapter_state is not None:
        net.adapter.load_state_dict(adapter_state)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    rng, history = np.random.default_rng(seed), []
    for epoch in range(6):
        indices = sampled_patches(records, rng)
        losses = []
        for start in range(0, len(indices), 8):
            x, y, valid = batch(records, indices[start:start+8])
            loss = masked_loss(net(x), y, valid)
            opt.zero_grad(); loss.backward(); opt.step()
            losses.append(float(loss.detach()))
        history.append({'epoch': epoch+1, 'loss': float(np.mean(losses))})
    return net.eval(), history


def predict(head, records, out):
    """Persist all probability maps before evaluation enables mask reads."""
    out.mkdir(parents=True, exist_ok=False)
    rows = []
    for r in records:
        t0 = time.perf_counter()
        f = np.load(r['features'], allow_pickle=False, mmap_mode='r')
        probs = []
        with torch.no_grad():
            for start in range(0, len(f), 8):
                probs.append(torch.sigmoid(head(torch.from_numpy(f[start:start+8].copy()))).numpy()[:, 0])
        p = tiling.stitch(np.concatenate(probs), r['plan']).astype(np.float32)
        path = out / (r['id'] + '.npy')
        np.save(path, p, allow_pickle=False)
        rows.append({'id': r['id'], 'file': str(path), 'sha256': digest(path),
                     'head_and_stitch_seconds': time.perf_counter()-t0})
    write(out / 'receipt.json', {'created_at': now(), 'masks_read_for_prediction': False, 'items': rows})
    return rows


def load_predictions(rows):
    result = {}
    for row in rows:
        if digest(row['file']) != row['sha256']:
            raise ValueError('Prediction changed before label access')
        result[row['id']] = np.load(row['file'], allow_pickle=False)
    return result


def choose_threshold(predictions, reader):
    # Only the registered tuning pool is consulted, never development evaluation.
    truth = {i: reader.load('tune', i, 'mask') for i in predictions}
    scores = []
    for threshold in (.1, .2, .3, .4, .5, .6, .7, .8, .9):
        total = dict(tp=0, fp=0, tn=0, fn=0)
        for i, p in predictions.items():
            c = metrics.pixel_counts(p >= threshold, truth[i])
            total = {k: total[k]+c[k] for k in total}
        scores.append({'threshold': threshold, 'dice': metrics.dice_iou(total)['dice']})
    best = max(scores, key=lambda r: (r['dice'] or 0, r['threshold']))
    return best['threshold'], scores


def evaluate(predictions, reader, threshold):
    rows = []
    for i, p in predictions.items():
        truth = reader.load('development_evaluation', i, 'mask')
        counts = metrics.pixel_counts(p >= threshold, truth)
        row = {'id': i, 'defect_class': next(r['defect_class'] for r in reader.manifest.data['items'] if r['id']==i),
               'counts': counts, **metrics.dice_iou(counts),
               **metrics.component_matches(p >= threshold, truth)}
        rows.append(row)
    summary = metrics.aggregate(rows)
    c = summary['pixel']
    summary['pixel']['precision'] = c['tp'] / (c['tp']+c['fp']) if c['tp']+c['fp'] else None
    summary['pixel']['recall'] = c['tp'] / (c['tp']+c['fn']) if c['tp']+c['fn'] else None
    summary['pixel']['background_pixel_fpr'] = c['fp'] / (c['fp']+c['tn'])
    return {'threshold': threshold, 'summary': summary, 'per_image': rows}


def paired_dice(a, b):
    by_id = {r['id']: r for r in b}
    differences = [r['dice']-by_id[r['id']]['dice'] for r in a
                   if r['dice'] is not None and by_id[r['id']]['dice'] is not None]
    if not differences:
        return {'n_image_pairs': 0, 'mean_difference': None, 'ci95': None}
    d = np.asarray(differences)
    rng = np.random.default_rng(811)
    means = np.mean(rng.choice(d, (2000, len(d)), replace=True), axis=1)
    return {'n_image_pairs': len(d), 'mean_difference': float(d.mean()),
            'ci95': np.quantile(means, [.025, .975]).tolist(),
            'resampling_unit': 'image (lot/acquisition groups unavailable)',
            'inference': 'exploratory development interval; unadjusted secondary comparisons'}


def run(args):
    root = Path(args.out)
    root.mkdir(parents=True, exist_ok=False)
    package = Path(__file__).parent
    log = []
    state = {'started_at': now(), 'status': 'running', 'notice': NOTICE,
             'commercial_validated': False, 'original_test_evaluated': False}
    write(root / 'status.json', state)
    try:
        torch.set_num_threads(2)
        manifest = mf.load(args.manifest)
        frozen = pipeline.load_frozen(manifest, args.base_model, 'calibration')
        pools = make_partition(manifest, frozen)
        protocol = {'schema_version': 1, 'created_at': now(), 'notice': NOTICE,
                    'role': 'development comparison only', 'data_mode': 'real',
                    'manifest_sha256': manifest.file_sha256, 'base_model_sha256': frozen['model']['sha256'],
                    'pools': pools, 'original_test_identity': manifest.split_identity('test'),
                    'training_seeds': [7, 17], 'epochs': 6, 'lr': .001,
                    'self_supervised_method': 'own masked-feature reconstruction residual adapter; not SimCLR/BYOL',
                    'self_supervised_seed': 731, 'self_supervised_epochs': 8,
                    'nffa_categories': ['Patterned_surface', 'MEMS_devices_and_electrodes'],
                    'nffa_images_per_category': 32, 'ssl_crop': 'native center 256x256; symmetric pad only',
                    'supervised_patch_sampling': 'four tiles per parent per epoch; two foreground/two background where available',
                    'primary_metric': 'paired native-resolution development pixel Dice at fixed threshold 0.5',
                    'secondary_metrics': 'tune-pool Dice threshold, component recall, pixel precision/recall, false components, latency',
                    'source_hashes': source_freeze(package),
                    'dependency_source_hashes': {m.__file__: digest(m.__file__) for m in (mf, metrics, backend, pipeline, tiling)},
                    'limits': ['No lot/acquisition identifiers', 'No representative independent normal wafer population',
                               'Read log covers registered loaders, not unrestricted OS access',
                               'No actual instrument routing or hardware comparison']}
        write(root / 'protocol.json', protocol)
        reader = ScopedReader(manifest, pools, log)
        net = backend.build(frozen['model']['config'])
        net.load_state_dict(torch.load(Path(args.base_model)/'model.pt', map_location='cpu', weights_only=True))
        net.eval()
        for param in net.parameters():
            param.requires_grad_(False)
        folder = root / 'cache'; folder.mkdir()
        cache = {}
        for role, ids in pools.items():
            cache[role] = []
            for index, i in enumerate(ids):
                t0 = time.perf_counter()
                image = reader.load(role, i, 'image')
                target = reader.load(role, i, 'mask') if role == 'train' else None
                row = cache_image(net, image, target, folder, i)
                row['feature_extraction_only_seconds'] = time.perf_counter()-t0
                cache[role].append(row)
                if (index+1) % 16 == 0:
                    print(json.dumps({'stage': 'feature_cache', 'role': role, 'completed': index+1, 'total': len(ids)}), flush=True)
        write(root / 'cache-manifest.json', cache)
        # Native baseline parity is checked against direct frozen-model execution, before any calibration labels.
        first = pools['tune'][0]
        image = reader.load('tune', first, 'image')
        tp = cache['tune'][0]['plan']; xs, _ = tiling.extract(image, tp)
        with torch.no_grad():
            direct = torch.sigmoid(net(torch.from_numpy(xs.copy())[:, None])).numpy()[:, 0]
            feats = np.load(cache['tune'][0]['features'], allow_pickle=False)
            cached = torch.sigmoid(net.segmentation_head(torch.from_numpy(feats))).numpy()[:, 0]
        parity = float(np.max(np.abs(direct-cached)))
        write(root / 'baseline-parity.json', {'max_absolute_probability_difference': parity, 'tolerance': 1e-5})
        if parity > 1e-5:
            raise ValueError('Cached baseline differs from direct frozen-model prediction')
        sources = prepare_nffa(root, Path(args.acquisition), {r['image_sha256'] for r in manifest.data['items']})
        nffa = []
        for r in sources:
            with Image.open(r['path']) as im:
                arr = np.asarray(im.convert('L'), dtype=np.float32)/255
            y, x = max(0, (arr.shape[0]-256)//2), max(0, (arr.shape[1]-256)//2)
            arr = arr[y:y+256, x:x+256]
            nffa.append(cache_image(net, arr, arr, folder, r['id']))
        write(root / 'nffa-cache.json', nffa)
        adapter, history = self_supervise(net.segmentation_head, nffa)
        write(root / 'self-supervised-history.json', history)
        torch.save(adapter, root / 'self-supervised-adapter.pt')
        models = {'frozen_baseline': SmallHead(net.segmentation_head).eval()}
        training = {}
        for kind in ('head', 'ssl_adapter'):
            for seed in protocol['training_seeds']:
                name = kind + '-seed' + str(seed)
                t0 = time.perf_counter()
                model, history = fit_head(net.segmentation_head, cache['train'], seed,
                                          adapter if kind == 'ssl_adapter' else None)
                models[name] = model
                p = root / (name + '.pt'); torch.save(model.state_dict(), p)
                training[name] = {'seed': seed, 'kind': kind, 'history': history,
                                  'trainable_parameters': sum(p.numel() for p in model.parameters()),
                                  'model_sha256': digest(p), 'training_seconds': time.perf_counter()-t0}
                write(root / 'training.json', training)
                print(json.dumps({'stage': 'fit', 'model': name, 'seconds': training[name]['training_seconds']}), flush=True)
        verify_source(package, protocol['source_hashes'])
        thresholds, predictions = {}, {}
        for name, model in models.items():
            rows = predict(model, cache['tune'], root / 'predictions' / name / 'tune')
            threshold, curve = choose_threshold(load_predictions(rows), reader)
            thresholds[name] = {'threshold': threshold, 'tuning_curve': curve}
            predictions[name] = predict(model, cache['development_evaluation'], root / 'predictions' / name / 'development_evaluation')
        write(root / 'evaluation-freeze.json', {'created_at': now(), 'training': training,
                                               'thresholds': thresholds, 'source_hashes': protocol['source_hashes'],
                                               'development_evaluation_labels_read': False})
        write(root / 'development-evaluation-consumption.json', {'feedback_started_at': now(), 'consumed': True,
                                                                'meaning': 'Future tuning makes this development evaluation adaptive, never a pristine final test.'})
        results = {}
        for name, rows in predictions.items():
            pred = load_predictions(rows)
            results[name] = {'fixed': evaluate(pred, reader, .5),
                             'tuned': evaluate(pred, reader, thresholds[name]['threshold'])}
            write(root / 'results.json', results)
        baseline = results['frozen_baseline']['fixed']['per_image']
        comparisons = {name: paired_dice(result['fixed']['per_image'], baseline)
                       for name, result in results.items() if name != 'frozen_baseline'}
        write(root / 'comparisons.json', comparisons)
        verify_source(package, protocol['source_hashes'])
        if any(digest(p) != h for p,h in protocol['dependency_source_hashes'].items()):
            raise ValueError('Dependency source changed after protocol freeze')
        state.update(status='complete', completed_at=now(), pools={k: len(v) for k,v in pools.items()},
                     original_test_reads_logged=0, nffa_training_images=len(sources),
                     source_verification='pass', baseline_cache_parity=parity)
        report(root, protocol, results, comparisons, cache)
    except Exception as exc:
        state.update(status='failed', failed_at=now(), error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        write(root / 'access-log.json', log)
        write(root / 'status.json', state)


def report(root, protocol, results, comparisons, cache):
    lines = ['# WaffleBench data-efficient SEM development results', '', NOTICE, '',
             f'Created: {now()}', '',
             'Real image development comparison. Original final test was not opened. No commercial equipment claim.', '',
             '| Candidate | Fixed-threshold Dice | Pixel recall | Pixel precision | Component recall | Tuned Dice |',
             '| --- | ---: | ---: | ---: | ---: | ---: |']
    for name, result in results.items():
        s = result['fixed']['summary']; p = s['pixel']
        vals = [p['dice'], p['recall'], p['precision'], s['component_recall']['overall']['recall'], result['tuned']['summary']['pixel']['dice']]
        lines.append('| '+name+' | '+' | '.join('unidentified' if v is None else f'{v:.4f}' for v in vals)+' |')
    lines += ['', '## Paired development contrasts at threshold 0.5', '',
              'Intervals resample images, not lots. They are exploratory and unadjusted for multiple comparisons.', '']
    for name, c in comparisons.items():
        lines.append(f'- {name}: mean per-image Dice difference {c["mean_difference"]}; 95% interval {c["ci95"]}; {c["n_image_pairs"]} defined pairs.')
    timings = [r['feature_extraction_only_seconds'] for r in cache['development_evaluation']]
    lines += ['', '## Interpretation boundaries', '',
              f'- Eligible sources: 128 original training images, 32 tuning images, 64 development evaluation images, 64 NFFA training-only frames. Final test has {protocol["original_test_identity"]["count"]} images and is untouched.',
              '- Frozen backbone and decoder; only a 73-parameter output head or a small residual adapter/head is supervised here. Compare seeds rather than selecting a lucky seed.',
              '- Self-supervision is an own masked-feature reconstruction pretext on frozen features, not an implementation of SimCLR/BYOL or full-backbone adaptation.',
              '- Native-resolution tiles, overlap stitching and padding exclusion are retained. Foreground/background patches come from their original training parents.',
              '- All evaluation frames are from an already established calibration pool. Image-level independence cannot establish unseen-lot generalization.',
              '- Background pixel FPR and false components are identifiable; full-wafer false-alarm rates are not, because a representative normal population is absent.',
              f'- Median cache extraction time: {float(np.median(timings)):.4f} seconds/image. Cached head timings are not end-to-end inference latency; warm full-network latency requires a separate measurement.',
              '- This study does not expose SEM features before paid acquisition or provide optical/SEM paired routing data. Perception gains must not be relabeled as physical routing gains.',
              '- Model hashes, source hashes, sampling pools, access logs and persisted predictions are recorded alongside this report.', '',
              '## Attribution', '',
              '- Carinthia-S: https://zenodo.org/records/16895427 (CC-BY-4.0).',
              '- NFFA SEM collection v2: https://b2share.eudat.eu/records/zja8y-53j14 (CC-BY-4.0).',
              '- segmentation_models.pytorch: pinned MIT source and local weights recorded in the frozen base model.', '']
    (root/'RESULTS.md').write_text('\n'.join(lines), encoding='utf-8')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--manifest', required=True)
    p.add_argument('--base-model', required=True)
    p.add_argument('--acquisition', required=True)
    p.add_argument('--out', required=True)
    run(p.parse_args())


if __name__ == '__main__':
    main()

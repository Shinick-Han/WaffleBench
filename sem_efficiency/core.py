"""Protocol, grouping and read boundaries independent of the optional torch backend."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4 << 20), b''):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(p)


def balanced_ids(items, count, namespace):
    """Metadata-only deterministic sampling; never consult masks or outcomes."""
    groups = {}
    for row in items:
        groups.setdefault(str(row['defect_class']), []).append(row['id'])
    for c in groups:
        groups[c].sort(key=lambda i: hashlib.sha256((namespace + i).encode()).hexdigest())
    out = []
    while len(out) < count and any(groups.values()):
        for c in sorted(groups):
            if groups[c] and len(out) < count:
                out.append(groups[c].pop(0))
    return out


def make_partition(manifest, frozen, train_count=128, tune_count=32, evaluation_count=64):
    by_id = {r['id']: r for r in manifest.data['items']}
    original = list(frozen['training']['item_ids'])
    if any(by_id[i]['split'] != 'train' for i in original):
        raise ValueError('Encoder training includes non-training records')
    if train_count < len(original):
        raise ValueError('Training pool must include all original encoder training records')
    extra = [r for r in manifest.items('train') if r['id'] not in original]
    train = original + balanced_ids(extra, train_count - len(original), 'efficiency-train-v1')
    tune = balanced_ids(manifest.items('calibration'), tune_count, 'efficiency-tune-v1')
    tune_set = set(tune)
    evaluation = balanced_ids([r for r in manifest.items('calibration') if r['id'] not in tune_set],
                              evaluation_count, 'efficiency-development-eval-v1')
    if [len(train), len(tune), len(evaluation)] != [train_count, tune_count, evaluation_count]:
        raise ValueError('Insufficient eligible development records')
    pools = {'train': train, 'tune': tune, 'development_evaluation': evaluation}
    assigned = {}
    for role, ids in pools.items():
        for i in ids:
            row = by_id[i]
            keys = [('id', i), ('duplicate', row['duplicate_group']),
                    ('image', row['image_sha256'])]
            if row.get('source_group') is not None:
                keys.append(('source', str(row['source_group'])))
            for key in keys:
                if key in assigned and assigned[key] != role:
                    raise ValueError(f'Group crosses development roles: {key}')
                assigned[key] = role
    return pools


class ScopedReader:
    """Log exact file reads; intentionally provides no original-test reader."""
    def __init__(self, manifest, pools, log):
        self.manifest, self.pools, self.log = manifest, pools, log
        self.readers = {s: manifest.reader(s, masks_allowed=True, access_log=log)
                        for s in ('train', 'calibration')}

    def load(self, role, iid, kind):
        if role not in self.pools or iid not in self.pools[role]:
            raise ValueError('Read outside registered development pool')
        if kind not in ('image', 'mask'):
            raise ValueError('Unsupported read kind')
        split = 'train' if role == 'train' else 'calibration'
        self.log.append({'role': role, 'kind': kind, 'id': iid, 'at': now()})
        return getattr(self.readers[split], 'load_' + kind)(iid)


def source_freeze(package):
    return {str(p.relative_to(package)): digest(p) for p in sorted(package.rglob('*.py'))}


def verify_source(package, hashes):
    if source_freeze(package) != hashes:
        raise ValueError('Experiment source changed after protocol freeze')


def safe_member(member):
    from pathlib import PurePosixPath
    p = PurePosixPath(member.name.replace('\\', '/'))
    return (member.isfile() and not p.is_absolute() and '..' not in p.parts
            and ':' not in str(p) and 0 < member.size < 16 << 20
            and p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.tif', '.tiff'))

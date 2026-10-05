"""Standalone interactive evidence review; images stay local, no external assets."""
import argparse
import base64
import json
from pathlib import Path

from sem_efficiency.core import digest


def build(study_root, manifest_path, output):
    root, output = Path(study_root), Path(output)
    manifest = json.loads(Path(manifest_path).read_text())
    protocol = json.loads((root/'protocol.json').read_text())
    if digest(manifest_path) != protocol['manifest_sha256']:
        raise ValueError('report source manifest hash mismatch')
    by_id = {r['id']:r for r in manifest['items']}
    summary = json.loads((root/'summary.json').read_text())
    records, images = [], {}
    for row in json.loads((root/'prospective-results-freeze.json').read_text()):
        path = Path(row['result_path'])
        if digest(path) != row['result_sha256']:
            raise ValueError('analysis changed after prospective freeze')
        result = json.loads(path.read_text())
        iid = row['id']
        item = by_id[iid]
        if item['split'] != 'calibration':
            raise ValueError('report refuses non-development images')
        if iid not in images:
            source = (Path(manifest['root'])/item['image']).resolve()
            if Path(manifest['root']).resolve() not in source.parents or digest(source) != item['image_sha256']:
                raise ValueError('report image path/hash mismatch')
            ext = source.suffix.lower()
            mime = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png'}.get(ext.lstrip('.'))
            if mime is None:
                raise ValueError('standalone preview supports PNG/JPEG source frames')
            images[iid] = 'data:' + mime + ';base64,' + base64.b64encode(source.read_bytes()).decode()
        evaluation = json.loads(path.with_name(iid+'-evaluation.json').read_text())
        records.append({'id':iid, 'proxy_threshold':row['proxy_threshold'], 'analysis':result,
                        'evaluation':evaluation})
    payload = json.dumps({'summary':summary,'records':records,'images':images}, allow_nan=False).replace('<','\\u003c')
    template = (Path(__file__).parent/'review.html').read_text(encoding='utf-8')
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as f:
        f.write(template.replace('__EVIDENCE_JSON__',payload))
    return {'path':str(output.resolve()), 'records':len(records), 'images':len(images), 'sha256':digest(output)}


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--study',required=True)
    p.add_argument('--manifest',required=True)
    p.add_argument('--out',required=True)
    a=p.parse_args()
    print(json.dumps(build(a.study,a.manifest,a.out)))

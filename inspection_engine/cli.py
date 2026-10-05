"""Executable local image-to-review engine; immutable evidence directories."""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

from sem_efficiency.core import digest, now, write
from . import NOTICE
from .core import Config, analyze, apply_reviews, plan, validate_frame


def main(argv=None):
    parser = argparse.ArgumentParser(description='WaffleBench local inspection augmentation')
    sub = parser.add_subparsers(dest='command', required=True)
    inspect = sub.add_parser('analyze')
    inspect.add_argument('--frame', required=True, help='Prospective frame JSON with incumbent pixel boxes')
    inputs = inspect.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--image', help='Acquired 8-bit grayscale frame; requires model-root')
    inputs.add_argument('--probability', help='Acquired native probability .npy; requires its receipt hash')
    inspect.add_argument('--probability-sha256')
    inspect.add_argument('--model-root')
    inspect.add_argument('--coverage', help='Optional bool .npy acquired-pixel coverage')
    inspect.add_argument('--coverage-sha256')
    inspect.add_argument('--budget-s', type=float, default=120.0)
    inspect.add_argument('--threads', type=int, default=2)
    inspect.add_argument('--config', help='Explicit frozen development Config JSON; never qualifies automatic suppression')
    inspect.add_argument('--out', required=True)
    review = sub.add_parser('reviews')
    review.add_argument('--plan', required=True)
    review.add_argument('--events', required=True)
    review.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    out = Path(args.out)
    try:
        out.mkdir(parents=True, exist_ok=False)
        write(out / 'status.json', {'status': 'started', 'created_at': now(), 'notice': NOTICE})
        if args.command == 'analyze':
            frame = validate_frame(json.loads(Path(args.frame).read_text(encoding='utf-8')))
            receipt = {'frame_file_sha256': digest(args.frame), 'created_at': now()}
            if args.image:
                if not args.model_root:
                    raise ValueError('--image requires --model-root')
                from .perception import FrozenPerception
                backend = FrozenPerception(args.model_root, args.threads)
                if backend.frozen['model']['sha256'] != frame['model_sha256']:
                    raise ValueError('frame model hash differs from selected frozen model')
                p, details = backend.predict(args.image, frame['image_sha256'])
                receipt.update(details)
            else:
                if not args.probability_sha256 or digest(args.probability) != args.probability_sha256:
                    raise ValueError('cached probability hash mismatch or missing explicit receipt hash')
                p = np.load(args.probability, allow_pickle=False)
                receipt['cached_probability_sha256'] = args.probability_sha256
                receipt['mode'] = 'caller-declared acquired score map; model association not independently verified'
            coverage = None
            if args.coverage:
                if not args.coverage_sha256 or digest(args.coverage) != args.coverage_sha256:
                    raise ValueError('coverage hash mismatch or missing explicit receipt hash')
                coverage = np.load(args.coverage, allow_pickle=False)
                receipt['coverage_sha256'] = args.coverage_sha256
            config = Config()
            if args.config:
                config = Config(**json.loads(Path(args.config).read_text(encoding='utf-8'))).validate()
                receipt['config_sha256'] = digest(args.config)
            result = analyze(frame, p, coverage, config)
            scheduled = plan(result, args.budget_s)
            with (out / 'probability.npy').open('xb') as f:
                np.save(f, p, allow_pickle=False)
            receipt['output_probability_sha256'] = digest(out / 'probability.npy')
            write(out / 'analysis.json', result)
            write(out / 'plan.json', scheduled)
            write(out / 'inference-receipt.json', receipt)
            detail = {'existing': len(result['existing']), 'additional': len(result['additional']),
                      'admitted': len(scheduled['admitted']), 'deferred': len(scheduled['deferred']),
                      'unknown': scheduled['unknown_count']}
        else:
            scheduled = json.loads(Path(args.plan).read_text(encoding='utf-8'))
            events = json.loads(Path(args.events).read_text(encoding='utf-8'))
            result = apply_reviews(scheduled, events)
            write(out / 'reviewed.json', result)
            write(out / 'input-receipt.json', {'plan_sha256': digest(args.plan),
                                             'events_sha256': digest(args.events)})
            detail = {k: result[k] for k in ('review_positive', 'review_rejected', 'unknown_count', 'charged_s')}
        write(out / 'status.json', {'status': 'complete', 'created_at': now(), 'notice': NOTICE,
                                   'hardware_connected': False, 'commercial_validated': False})
        print(json.dumps({'status': 'complete', 'out': str(out.resolve()), **detail}))
        return 0
    except Exception as exc:
        # Existing evidence is never overwritten, including its status file.
        if out.exists() and 'detail' not in locals() and not isinstance(exc, FileExistsError):
            write(out / 'failure.json', {'status': 'failed', 'created_at': now(),
                                         'error': f'{type(exc).__name__}: {exc}'})
        print(json.dumps({'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Seven-parameter residual routing policy over public, affordable actions only."""
import math
import random

import numpy as np
import torch

from routing_poc.policies import sanitize_state


def features(state):
    clean = sanitize_state(state)
    candidates = sorted([c for c in clean['candidates'] if c['cost']['reserved_s'] <= clean['remaining_s']],
                        key=lambda c: c['site_id'])
    rows, base = [], []
    counts = dict(positive=0, negative=0, unknown=0)
    for h in clean['history']:
        key = 'positive' if h['reported_doi'] is True else 'negative' if h['reported_doi'] is False else 'unknown'
        counts[key] += 1
    observed = counts['positive']+counts['negative']
    rate = counts['positive']/observed if observed else .5
    for c in candidates:
        cost = c['cost']; current = clean['current']
        p, seconds = c['prior_p'], cost['reserved_s']
        base.append(math.log(max(p, 1e-6))-math.log(seconds))
        rows.append([p, math.log(seconds)/5, cost['move_s']/seconds,
                     float(current is not None and c['wafer_id']==current['wafer_id']),
                     p*min(clean['remaining_s']/240, 1), p*(rate-.5),
                     p*counts['unknown']/max(len(clean['history']), 1)])
    values = [min(clean['remaining_s']/240,1), len(candidates)/120, rate,
              sum(c['prior_p'] for c in candidates)/max(len(candidates),1)]
    return candidates, torch.tensor(rows, dtype=torch.float32).reshape(-1,7), torch.tensor(base), torch.tensor(values)


class Actor(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.residual = torch.nn.Parameter(torch.zeros(7))

    def scores(self, rows, base):
        # Bound the residual correction; the physical budget guard remains independent.
        return base + rows @ (2*torch.tanh(self.residual))


class Selector:
    def __init__(self, actor, training=False, critic=None):
        self.actor, self.training, self.critic = actor, training, critic
        self.trace = []

    def __call__(self, state, rng):
        candidates, rows, base, context = features(state)
        if not candidates:
            return None
        n = len(candidates)
        scores = self.actor.scores(rows, base)
        if not bool(torch.isfinite(scores).all()):
            from routing_poc.policies import select
            return select(state, rng, policy='risk_per_second', audit_epsilon=.1)
        if self.training:
            probabilities = .8*torch.softmax(scores/.7, dim=0)+.2/n
            draw = rng.random(); cumulative = 0; index = n-1
            for j, p in enumerate(probabilities.detach().tolist()):
                cumulative += p
                if draw < cumulative:
                    index = j; break
            self.trace.append((torch.log(probabilities[index]), self.critic(context).squeeze()))
            propensity, audit = float(probabilities[index].detach()), False
        else:
            best = int(torch.argmax(scores).item())
            audit = rng.random() < .1
            index = rng.randrange(n) if audit else best
            propensity = .1/n + (.9 if index == best else 0)
        return {'site_id': candidates[index]['site_id'], 'propensity': propensity, 'audit': audit,
                'reason': 'learned_residual_training' if self.training else 'learned_residual_with_uniform_audit',
                'components': {'learned_parameters': 7, 'score': float(scores[index].detach()),
                               'training': self.training, 'evidence_mode': 'synthetic development'}}

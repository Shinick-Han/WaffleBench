"""New synthetic-only actor-critic/Monte-Carlo policy-gradient development study."""
import argparse
import copy
import hashlib
import json
import random
import time
from pathlib import Path

import numpy as np
import torch

from routing_poc import fixtures, metrics, policies, replay
from sem_efficiency import NOTICE
from sem_efficiency.core import digest, now, source_freeze, verify_source, write
from .policy import Actor, Selector


def train(seed, root):
    torch.manual_seed(seed)
    actor = Actor()
    critic = torch.nn.Sequential(torch.nn.Linear(4,16), torch.nn.Tanh(), torch.nn.Linear(16,1))
    opt = torch.optim.Adam(list(actor.parameters())+list(critic.parameters()), lr=.01)
    history = []
    jobs = [(s,r,b) for s in range(40000,40096) for r in ('clustered','diffuse') for b in (120,240)]
    # These are new training episodes; repeat the same training pool, not the consumed old test.
    for epoch in range(2):
        random.Random(seed*10+epoch).shuffle(jobs)
        rewards, losses = [], []
        for step, (s, regime, budget) in enumerate(jobs):
            job, archive = fixtures.make_job(s, regime)
            selector = Selector(actor, training=True, critic=critic)
            run = replay.run_loop(job, archive, selector, budget,
                                  seed=seed*100000+epoch*1000+step)
            reference = archive['reference']['doi_by_site']
            immediate = [float(row['reported_doi'] is True and reference[row['site_id']])
                         -.25*float(row['reported_doi'] is True and not reference[row['site_id']])
                         for row in run['rows']]
            returns, cumulative = [], 0.
            for reward in reversed(immediate):
                cumulative += reward; returns.append(cumulative)
            returns.reverse()
            if selector.trace:
                logp = torch.stack([x[0] for x in selector.trace])
                value = torch.stack([x[1] for x in selector.trace])
                target = torch.tensor(returns, dtype=torch.float32)
                loss = -(logp*(target-value.detach())).mean() + .5*(value-target).square().mean()
                loss += .01*actor.residual.square().mean()
                opt.zero_grad(); loss.backward()
                torch.nn.utils.clip_grad_norm_(list(actor.parameters())+list(critic.parameters()), 5)
                opt.step()
                losses.append(float(loss.detach()))
            rewards.append(sum(immediate))
        history.append({'epoch':epoch+1, 'episodes':len(jobs), 'mean_reward':float(np.mean(rewards)),
                        'mean_loss':float(np.mean(losses)), 'residual':actor.residual.detach().tolist()})
        print(json.dumps({'stage':'routing_train','seed':seed, **history[-1]}), flush=True)
        write(root / f'training-seed{seed}.json', history)
    path = root / f'actor-seed{seed}.pt'; torch.save(actor.state_dict(), path)
    return actor.eval(), {'seed':seed,'file':str(path),'sha256':digest(path),'history':history}


def reward_summary(records):
    return {k: float(np.mean([r['metrics'][k] for r in records])) for k in
            ('confirmed_reference_doi','false_confirmations','escapes','spent_s')}


def main():
    p=argparse.ArgumentParser(); p.add_argument('--out', required=True); args=p.parse_args()
    root=Path(args.out); root.mkdir(parents=True, exist_ok=False)
    package=Path(__file__).parent
    torch.set_num_threads(1)
    protocol={'created_at':now(),'notice':NOTICE,'data_mode':'synthetic',
              'commercial_validated':False,'role':'new development study, not final confirmatory evaluation',
              'method':'Monte Carlo policy gradient with learned value baseline, not CQL/PPO',
              'train_seeds':[7,17],'training_episode_namespace':[40000,40095],
              'development_evaluation_namespace':[45000,45031],
              'old_consumed_test_namespace':[22000,22039], 'epochs':2,'budgets_s':[120,240],
              'regimes':['clustered','diffuse'],'reward':'confirmed synthetic DOI - 0.25 false confirmations',
              'policy_state':'public candidate/cost features and already-paid detector history; no truth/seed/images',
              'action_constraints':'existing deterministic admission and full retry reserve',
              'source_hashes':source_freeze(package),
              'dependencies':{m.__file__:digest(m.__file__) for m in (fixtures,metrics,policies,replay)},
              'limits':['No real logged transitions','No image-to-optical prior wiring','Same generator across splits',
                        'New seeds demonstrate same-generator development only; no new physical wafers',
                        'Training reference is synthetic reward, never policy input']}
    write(root/'protocol.json',protocol)
    state={'status':'running','started_at':now(),'commercial_validated':False}
    write(root/'status.json',state)
    try:
        actors, receipt = {}, {}
        for seed in (7,17):
            actors[f'learned_seed{seed}'], receipt[f'learned_seed{seed}']=train(seed,root)
        verify_source(package, protocol['source_hashes'])
        write(root/'evaluation-freeze.json',{'created_at':now(),'actors':receipt,'protocol_sha256':digest(root/'protocol.json')})
        all_records=[]; cells=[]
        for regime in ('clustered','diffuse'):
            for budget in (120,240):
                grouped={name:[] for name in ('risk_only','risk_per_second','beam_route',*actors)}
                for seed in range(45000,45032):
                    job, archive=fixtures.make_job(seed,regime)
                    write(root/'jobs'/f'{regime}-{seed}.json',{'job':job,'archive':archive})
                    for name in grouped:
                        selector=Selector(actors[name]) if name in actors else policies.make_selector(name,.1)
                        # Common stream yields paired audit RNGs for equal affordable populations.
                        with torch.no_grad():
                            run=replay.run_loop(job,archive,selector,budget,seed=seed+101)
                        result=metrics.evaluate(job,archive,run)
                        if run['spent_s'] > budget+1e-9:
                            raise ValueError('Budget violation')
                        record={'regime':regime,'budget_s':budget,'lot':seed,'policy':name,'run':run,'metrics':result}
                        write(root/'runs'/f'{regime}-{budget}-{seed}-{name}.json',record)
                        grouped[name].append(record); all_records.append(record)
                summaries={name:reward_summary(rs) for name,rs in grouped.items()}
                comparisons={}
                baseline=grouped['risk_per_second']
                for name in actors:
                    d=np.array([a['metrics']['confirmed_reference_doi']-b['metrics']['confirmed_reference_doi']
                                for a,b in zip(grouped[name],baseline)])
                    rng=np.random.default_rng(816)
                    bs=rng.choice(d,(2000,len(d)),replace=True).mean(1)
                    comparisons[name]={'difference':float(d.mean()),'ci95':np.quantile(bs,[.025,.975]).tolist(),
                                       'n_lots':len(d),'metric':'confirmed reference DOI',
                                       'inference':'exploratory development, unadjusted comparisons'}
                cells.append({'regime':regime,'budget_s':budget,'policies':summaries,'contrasts':comparisons})
                print(json.dumps({'stage':'routing_development','regime':regime,'budget':budget,'contrasts':comparisons}),flush=True)
        verify_source(package,protocol['source_hashes'])
        if any(digest(path)!=sha for path,sha in protocol['dependencies'].items()):
            raise ValueError('Dependency source changed during study')
        write(root/'summary.json',{'cells':cells,'runs':len(all_records),'commercial_validated':False,
                                  'data_mode':'synthetic','created_at':now()})
        lines=['# WaffleBench learned routing development results','',NOTICE,'',
               'Synthetic trajectories only. Real SEM learning is a separate study. No hardware or offline-real-log claim.','',
               '| Regime / budget | Risk only | Risk/second | Beam | Learned seed 7 | Learned seed 17 |',
               '| --- | ---: | ---: | ---: | ---: | ---: |']
        for cell in cells:
            lines.append('| '+cell['regime']+' / '+str(cell['budget_s'])+' | '+' | '.join(
                f'{cell["policies"][name]["confirmed_reference_doi"]:.3f}' for name in
                ('risk_only','risk_per_second','beam_route','learned_seed7','learned_seed17'))+' |')
        lines+=['','Both training seeds and all contrasts are retained; no winning seed is selected.',
                'The learned actor has seven parameters. Budget/unknown/retry behavior remains in the original admission layer.',
                'This generator has priors calibrated by construction and invented physical costs. New seeds do not establish new-generator or real-instrument generalization.',
                'All gains are development evidence. The existing strong baseline remains the default unless independent future evaluation supports replacement.','']
        (root/'RESULTS.md').write_text('\n'.join(lines),encoding='utf-8')
        state.update(status='complete',completed_at=now(),runs=len(all_records),source_verification='pass')
    except Exception as e:
        state.update(status='failed',error=f'{type(e).__name__}: {e}'); raise
    finally:
        write(root/'status.json',state)


if __name__=='__main__':
    main()

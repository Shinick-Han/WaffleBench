"""Coordinator: one genuine Omnigent posthoc diagnostic cycle, then fail-closed verification.

The new cycle is separate from every frozen benchmark. SDK records establish
actual delegation/tool provenance; actor strings alone are insufficient.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import run_inspection_live as sdk

ORDER = ['discovery_analyst', 'discovery_experimenter', 'discovery_analyst',
         'discovery_experimenter', 'discovery_analyst']
ROLE_TOOLS = {'discovery_analyst': {'read_context', 'record_plan', 'record_update'},
              'discovery_experimenter': {'run_experiment'}}
PROMPT = ('Complete the one prepared WaffleBench posthoc discovery cycle as configured. '
          'Let the analyst choose diagnostic hypotheses and competing tests without prescribing '
          'a conclusion. Execute two read-only diagnostics, interpret each result, propose the '
          'next prospective experiment, finalize, then stop. Report actual IDs and limitations.')


def write(path, value):
    sdk._write_json(Path(path), value)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unwrap(text):
    value = sdk._json(text) or {}
    if set(value) == {'result'} and isinstance(value['result'], str):
        return sdk._json(value['result']) or {}
    return value


def verify(root: Path, capture: dict) -> dict:
    from discovery_cycle.core import DiscoveryCycle, COMPUTE, INPUT_NAME, _canon
    core = DiscoveryCycle(root).status()  # full ledger, input and head verification
    events = [json.loads(x) for x in (root / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    doc = json.loads((root / 'inputs' / INPUT_NAME).read_text(encoding='utf-8'))
    plans = [e['data'] for e in events if e['type'] == 'plan_recorded']
    results = [e['data'] for e in events if e['type'] == 'experiment_completed']
    updates = [e['data'] for e in events if e['type'] == 'update_recorded']
    failures, checks = [], []

    def check(condition, name):
        checks.append({'name': name, 'passed': bool(condition)})
        if not condition:
            failures.append(name)

    check(core['finalized'] and not core['blocked'], 'core finalized without blocked admission')
    check(core['budget'] == {'limit': 2, 'used': 2, 'remaining': 0}, 'two-experiment cap')
    check(len(plans) == len(results) == len(updates) == 2, 'two plans, results and updates')
    for i, (plan, result, update) in enumerate(zip(plans, results, updates), 1):
        check(len(set(plan['candidates'])) >= 2 and plan['selected_test_id'] in plan['candidates'], f'plan {i} competing tests')
        prior = [r['result_id'] for r in results[:i-1]]
        check(plan['evidence_result_ids'] == prior, f'plan {i} observed evidence only')
        check(result['plan_id'] == plan['plan_id'] and result['test_id'] == plan['selected_test_id'], f'result {i} selected test')
        expected = COMPUTE[result['test_id']](doc)
        check(result['values'] == expected and result['values_sha256'] == hashlib.sha256(_canon(expected)).hexdigest(), f'result {i} recomputed values')
        check(result['privileged_posthoc_oracle'] and not result['production_selection'] and not result['new_heldout_gain'], f'result {i} scope disclosure')
        check(update['result_id'] == result['result_id'] and update['result_sha256'] == result['values_sha256'], f'update {i} binds real result')
        check(update['next_experiment']['status'] == 'PROPOSED' and update['next_experiment']['executed'] is False, f'update {i} proposal is unexecuted')

    check(capture.get('outcome') == 'completed', 'SDK workflow completed')
    root_id = capture.get('session_id')
    calls, collapsed = sdk.sdk_calls(sdk.tool_records(capture['items']))
    dispatches = []
    root_final_calls = []
    for call in calls:
        if call['name'] in sdk.DISCOVERY_TOOLS:
            continue
        if call['name'] in sdk.DISPATCH_TOOLS:
            out = unwrap(call['out'])
            dispatches.append({'index': len(dispatches)+1, 'agent': call['arguments'].get('agent'),
                               'output_agent': out.get('agent'), 'child_session_id': out.get('conversation_id'),
                               'title': out.get('title'), '_message': call['arguments'].get('args'),
                               'accepted': bool(out.get('conversation_id')) and not out.get('error'),
                               'sdk_call_id': call['sdk_call_id'], 'record_item_ids': call['record_item_ids']})
        elif sdk._tool_name(call['name']) == ('discovery', 'finalize'):
            root_final_calls.append(call)
        elif sdk._tool_name(call['name']) == ('discovery', 'read_context'):
            check(call['has_output'] and unwrap(call['out']).get('status') != 'error', 'supervisor context read completed')
        elif call['name'] != 'sys_read_inbox':
            check(False, 'unexpected supervisor tool '+call['name'])
    check([d['agent'] for d in dispatches] == ORDER and all(d['accepted'] for d in dispatches), 'five accepted ordered delegations')
    check(all(d['output_agent'] in (None, d['agent']) for d in dispatches), 'dispatch identities match')
    check(len(root_final_calls) == 1 and root_final_calls[0]['has_output'], 'one supervisor finalize tool call')

    children = capture['children']
    check(set(children) == {d['child_session_id'] for d in dispatches}, 'exact direct child subtree')
    matched = []
    for cid, child in children.items():
        mine = [d for d in dispatches if d['child_session_id'] == cid]
        if not mine:
            continue
        agent = mine[0]['agent']
        summary = child['summary']
        check(all(d['agent'] == agent for d in mine), 'same-agent conversation reuse '+cid)
        check((summary.get('parent_id') or summary.get('parent_session_id')) == root_id, 'direct child '+cid)
        declared = [summary.get('agent_name'), summary.get('tool')]
        if ':' in str(summary.get('title', '')):
            declared.append(summary['title'].split(':', 1)[0])
        check(all(v in (None, agent) for v in declared), 'child metadata identity '+cid)
        turns, artifacts, problems = sdk.child_turns(cid, child['items'], mine)
        collapsed += artifacts
        check(not problems, 'ordered child turn mapping '+cid)
        failures += problems
        for step, turn_calls in turns:
            for call in turn_calls:
                if call['name'] in sdk.DISCOVERY_TOOLS:
                    continue
                server, tool = sdk._tool_name(call['name'])
                check(server == 'discovery' and tool in ROLE_TOOLS.get(agent, set()) and step is not None and call['has_output'], 'allowed child tool '+call['name'])
                output = unwrap(call['out'])
                check(output.get('status') != 'error' and not output.get('code'), 'child tool success '+call['name'])
                matched.append({'agent': agent, 'delegation': step, 'tool': tool,
                                'arguments': call['arguments'], 'output': output,
                                'sdk_call_id': call['sdk_call_id'], 'record_item_ids': call['record_item_ids']})

    for i, (plan, result, update) in enumerate(zip(plans, results, updates)):
        ps = [c for c in matched if c['tool'] == 'record_plan' and c['delegation'] == 1+2*i]
        rs = [c for c in matched if c['tool'] == 'run_experiment' and c['delegation'] == 2+2*i]
        us = [c for c in matched if c['tool'] == 'record_update' and c['delegation'] == 3+2*i]
        check(len(ps) == len(rs) == len(us) == 1, f'cycle {i+1} exact specialist calls')
        if len(ps) == len(rs) == len(us) == 1:
            check(all(ps[0]['arguments'].get(k) == plan[k] for k in ('candidates', 'selected_test_id', 'hypothesis', 'expected_learning', 'reason', 'evidence_result_ids')), f'cycle {i+1} SDK plan arguments match ledger')
            check(ps[0]['output'].get('plan') == plan, f'cycle {i+1} SDK plan matches ledger')
            check(rs[0]['arguments'].get('plan_id') == plan['plan_id'] and rs[0]['output'].get('result') == result and not rs[0]['output'].get('replayed'), f'cycle {i+1} SDK experiment matches ledger')
            check(us[0]['arguments'].get('result_id') == result['result_id'] and us[0]['output'].get('update') == update and not us[0]['output'].get('replayed'), f'cycle {i+1} SDK update matches ledger')
            check(us[0]['arguments'].get('interpretation') == update['interpretation'] and us[0]['arguments'].get('next_hypothesis') == update['next_hypothesis']['text'] and us[0]['arguments'].get('next_experiment') == update['next_experiment']['text'], f'cycle {i+1} SDK update arguments match ledger')
    final = next((e['data'] for e in events if e['type'] == 'finalized'), None)
    if len(root_final_calls) == 1:
        check(unwrap(root_final_calls[0]['out']).get('final') == final, 'SDK finalization matches ledger')
    mutation_calls = [c for c in matched if c['tool'] != 'read_context']
    check(len(mutation_calls) == 6, 'no extra specialist mutations')
    check(len(results) == 2 and results[0]['test_id'] != results[1]['test_id'], 'distinct diagnostic studies')
    return {'status': 'passed' if not failures else 'failed', 'checked_at': sdk._now(), 'checks': checks,
            'failure_reasons': failures, 'head_hash': core['head_hash'], 'input_sha256': core['input_sha256'],
            'events_sha256': digest(root/'events.jsonl'), 'delegations': [{k:v for k,v in d.items() if k != '_message'} for d in dispatches],
            'specialist_calls': matched, 'collapsed_record_artifacts': collapsed,
            'verifier_sha256': digest(__file__), 'sdk_helper_sha256': digest(sdk.__file__)}


def export(root: Path, capture: dict, receipt: dict, public_run_id: str | None = None) -> dict:
    if receipt['status'] != 'passed':
        raise RuntimeError('unverified cycle cannot be exported')
    events = [json.loads(x) for x in (root/'events.jsonl').read_text(encoding='utf-8').splitlines()]
    rows = lambda kind: [e['data'] for e in events if e['type'] == kind]
    event_source = f'public-runs/{public_run_id}/cycle/events.jsonl' if public_run_id else 'evidence/discovery-cycle/events.jsonl'
    return {'schema_version': 1, 'project': 'WaffleBench',
            **({'public_run_id': public_run_id} if public_run_id else {}),
            'label': ('Fresh public Omnigent diagnostic session over existing authored synthetic evidence; no new held-out gain or physical experiment.' if public_run_id else 'Actual Omnigent posthoc diagnostics of authored synthetic evidence; recorded replay, no new held-out gain or physical experiment.'),
            'question': 'What limits confirmed defect discovery under the same precision-review budget, and which experiment should we run next?',
            'sdk': {'session_id': capture['session_id'], 'status': 'completed', 'model': 'claude-opus-5-5', 'harness': 'claude-sdk'},
            'verification': {k: receipt[k] for k in ('status', 'checked_at', 'checks')},
            'sources': [{'path': 'evidence/inspection-improvements-v3/quality-diagnostics.json', 'sha256': receipt['input_sha256']},
                        {'path': event_source, 'sha256': receipt['events_sha256']}],
            'plans': rows('plan_recorded'),
            'results': [{**r, 'elapsed_seconds': r['elapsed_s'], 'source_sha256': r['input_sha256']} for r in rows('experiment_completed')],
            'updates': [{**u, 'next_hypothesis': u['next_hypothesis']['text'], 'next_experiment': u['next_experiment']['text']} for u in rows('update_recorded')],
            'closed': {'status': 'finalized'},
            'limits': ['Diagnostic catalog and budget are human-authorized; hypotheses, study choices and interpretations are agent-authored.',
                       'Two preauthorized read-only computations; outside-catalog actions are denied and require approval.',
                       'Privileged posthoc oracle evaluation, never a production policy input or a new held-out test.',
                       'The proposed prospective experiment is unexecuted. No discovery-speed or commercial-superiority gain is measured.',
                       'LLM coordination and computation consume time; zero inspection CU does not mean zero total cost.']}


async def run(args):
    from omnigent_client import OmnigentClient, SessionsChat
    root = args.root.resolve()
    proof_path = root/'omnigent/session-proof.json'
    if proof_path.exists() and not args.recover:
        raise RuntimeError('one SDK session per prepared cycle; use --recover to collect the same session')
    local = args.proof_dir or ROOT/'.discovery-live-runtime/proof'
    local.mkdir(parents=True, exist_ok=True)
    raw_file = local/f'{root.name}-stream.jsonl'
    async with OmnigentClient(base_url=args.server) as client:
        if args.recover:
            sid = json.loads(proof_path.read_text(encoding='utf-8'))['session_id']
            chat = None
        else:
            health = await client._http.get(args.server+'/health')
            health.raise_for_status()
            agents = (await client._http.get(args.server+'/v1/agents',params={'limit':1000})).json()['data']
            agents = [a for a in agents if a.get('name') == 'discovery_supervisor']
            hosts = (await client._http.get(args.server+'/v1/hosts')).json()['hosts']
            hosts = [h for h in hosts if h.get('status') == 'online' and not h.get('sandbox_provider')]
            if len(agents) != 1 or len(hosts) != 1:
                raise RuntimeError('expected one discovery supervisor and one online local host')
            response = await client._http.post(args.server+'/v1/sessions',json={'agent_id':agents[0]['id'],
                 'host_id':hosts[0]['host_id'],'workspace':str(ROOT),'reasoning_effort':'medium','title':'WaffleBench evidence-driven diagnostic cycle'})
            response.raise_for_status()
            sid = response.json()['id']
            write(proof_path, {'status':'running','session_id':sid,'agent':'discovery_supervisor'})
            session = await client.sessions.get(sid)
            chat = SessionsChat(namespace=client.sessions,files_uploader=None,files_getter=None,session=session)
        with raw_file.open('a',encoding='utf-8') as handle:
            recorder = sdk.Recorder(handle,sid)
            prior_capture = root/'omnigent/sdk-records.json'
            if args.recover and prior_capture.exists():
                prior = json.loads(prior_capture.read_text(encoding='utf-8'))
                if prior.get('session_id') != sid:
                    raise RuntimeError('recovery capture names another session')
                recorder.responses.extend(sdk.prior_responses(prior))
            outcome = await sdk.drive(client.sessions,sid,recorder,deadline=time.monotonic()+args.timeout,
                         poll_interval=3,settle_grace=15,chat=chat,prompt=PROMPT,interrupt_on_timeout=True)
        snapshot = await sdk.collect_snapshot(client.sessions,sid)
        capture = {'session_id':sid,'outcome':outcome['outcome'],'error':outcome['error'],
                   'responses':recorder.responses,'items':snapshot['items'],'children':snapshot['raw_children']}
        write(local/f'{root.name}-sdk-raw.json',capture)
        write(root/'omnigent/sdk-records.json',sdk.sanitize(capture))
        write(proof_path, {'status':capture['outcome'],'session_id':sid,'agent':'discovery_supervisor',
                          'finished_at':sdk._now(),'error':capture['error'],'raw_local_only':True})
    proc = subprocess.run([str(ROOT/'.venv/Scripts/python.exe'),__file__,'--root',str(root),'--verify'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=60)
    print(proc.stdout)
    if proc.returncode:
        print(proc.stderr,file=sys.stderr)
    return proc.returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--server',default='http://127.0.0.1:6773')
    parser.add_argument('--timeout',type=float,default=900)
    parser.add_argument('--verify',action='store_true')
    parser.add_argument('--recover',action='store_true')
    parser.add_argument('--proof-dir',type=Path,help='private raw proof directory (default: project-local runtime proof)')
    args = parser.parse_args()
    if args.server not in ('http://127.0.0.1:6773', 'http://127.0.0.1:6775'):
        parser.error('only isolated project-local port 6773 or public-runtime port 6775 is supported')
    if args.verify:
        root = args.root.resolve()
        capture = json.loads((root/'omnigent/sdk-records.json').read_text(encoding='utf-8'))
        receipt = verify(root,capture)
        write(root/'verification.json',receipt)
        proof = json.loads((root/'omnigent/session-proof.json').read_text(encoding='utf-8'))
        proof['status'] = 'completed' if receipt['status']=='passed' else 'failed'
        proof['verification_status'] = receipt['status']
        write(root/'omnigent/session-proof.json',proof)
        if receipt['status']=='passed':
            write(root/'export.json',export(root,capture,receipt))
        print(json.dumps({'status':receipt['status'],'session_id':capture['session_id'],'checks':len(receipt['checks']),
                          'failure_reasons':receipt['failure_reasons']}))
        return 0 if receipt['status']=='passed' else 1
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())

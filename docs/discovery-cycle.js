// Recorded post-hoc diagnostic discovery cycle (web/data/discovery-cycle.json).
// Read-only: nothing runs here. The record is shown only when the session
// completed and verification passed. All data is inserted with textContent;
// missing values render "—" rather than a fabricated number.
(() => {
  'use strict';
  const DATA_URL = './data/discovery-cycle.json';
  const DATA_PATH = 'web/data/discovery-cycle.json';
  const RUN_CAP = 2;
  const $ = id => document.getElementById(id);
  const state = {bytes:null, data:null};

  // ---------- null-safe helpers ----------
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const arr = v => Array.isArray(v) ? v : [];
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const str = v => typeof v === 'string' && v.trim() ? v : null;
  const show = v => v == null || v === '' ? '—' : String(v);
  const time = iso => { const t = Date.parse(iso); return Number.isFinite(t) ? new Date(t).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC') : show(iso); };
  const json = v => { try { return JSON.stringify(v, null, 2) ?? '—'; } catch { return '—'; } };

  // DOM builder: strings become text nodes, never HTML.
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k === 'on') for (const [ev, fn] of Object.entries(v)) el.addEventListener(ev, fn);
      else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  const code = v => h('code', {class:'mono-id'}, show(v));
  const mark = ok => ok == null ? h('span', null, '—') : h('span', {class: ok ? 'ok' : 'bad'}, ok ? 'match' : 'mismatch');
  const who = (kind, text) => h('span', {class:`who ${kind}`}, text);
  const statusOk = s => typeof s === 'string' && /^(passed|pass|ok|completed|match)$/i.test(s);
  const statusBad = s => typeof s === 'string' && /^(failed|fail|error|mismatch)$/i.test(s);
  const receipt = rows => h('dl', {class:'receipt'}, rows.map(([k, v]) => h('div', null, h('dt', null, k), h('dd', null, v instanceof Node ? v : show(v)))));

  // ---------- load ----------
  function setLoad(title, body, retry) {
    const ls = $('loadState'); ls.hidden = false; ls.replaceChildren();
    if (title) ls.append(h('h2', null, title));
    ls.append(...[body].flat());
    if (retry) ls.append(h('button', {type:'button', class:'control', on:{click:load}}, 'Retry'));
  }
  const pathP = (...parts) => h('p', null, parts.map(x => x === '@path' ? h('code', null, DATA_PATH) : x));

  async function load() {
    $('app').hidden = true; $('download').disabled = true; state.bytes = null; state.data = null;
    setLoad(null, [h('div', {class:'skeleton', 'aria-hidden':'true'}, h('span'), h('span'), h('span')), h('p', null, 'Loading stored cycle record…')]);
    let response;
    try { response = await fetch(DATA_URL, {cache:'no-store'}); }
    catch (error) {
      console.error('Discovery cycle fetch:', error);
      return setLoad('Could not load the record', pathP('A network error prevented reading ', '@path', '. Check your connection and try again.'), true);
    }
    if (response.status === 404) {
      return setLoad('Pending — no record yet', pathP('@path', ' does not exist yet (HTTP 404). The diagnostic cycle record is pending export and verification. This page never shows placeholder numbers.'), true);
    }
    if (!response.ok) return setLoad('Could not load the record', pathP('The request for ', '@path', ` failed with HTTP ${response.status}.`), true);
    let bytes, data;
    try {
      bytes = await response.arrayBuffer();
      data = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes));
    } catch (error) {
      console.error('Discovery cycle parse:', error);
      return setLoad('Could not parse the record', pathP('@path', ' is not valid UTF-8 JSON. The file may be mid-update; try again.'), true);
    }
    const d = obj(data);
    if (!d || d.schema_version !== 1 || d.project !== 'WaffleBench') {
      return setLoad('Unsupported record format', pathP('@path', ` requires schema_version 1 and project WaffleBench. Received schema_version ${show(d?.schema_version)}, project ${show(d?.project)}.`), true);
    }
    // Validation gate: nothing from the record is rendered unless both hold.
    const sdkStatus = obj(d.sdk)?.status, verStatus = obj(d.verification)?.status;
    if (verStatus !== 'passed' || sdkStatus !== 'completed') {
      return setLoad('Not yet validated', [
        pathP('@path', ' exists, but it is not shown until the agent session has completed and verification has passed.'),
        receipt([['Session status', code(sdkStatus ?? 'missing')], ['Verification status', code(verStatus ?? 'missing')]]),
      ], true);
    }
    state.bytes = bytes; state.data = d;
    try { render(d); }
    catch (error) {
      console.error('Discovery cycle render:', error);
      state.bytes = null;
      return setLoad('Could not display the record', pathP('@path', ' does not have the expected structure.'), true);
    }
    $('loadState').hidden = true; $('app').hidden = false; $('download').disabled = false;
  }

  // ---------- render ----------
  function render(d) {
    const sdk = obj(d.sdk) || {};
    $('studyMeta').replaceChildren(...[
      ['Project', d.project], ['Session', sdk.session_id], ['Model', sdk.model], ['Harness', sdk.harness],
      ['Cycle', obj(d.closed)?.status ? `closed · ${obj(d.closed).status}` : null],
    ].map(([k, v]) => h('div', null, h('dt', null, k), h('dd', null, show(v)))));
    $('questionText').textContent = show(str(d.question));
    $('scopeLabel').textContent = str(d.label) ? `Record label: ${d.label}` : 'Record label: —';
    renderExperiments(d);
    renderRoles(d);
    renderVerification(d);
    renderLimits(d);
  }

  function renderExperiments(d) {
    const plans = arr(d.plans).filter(obj), results = arr(d.results).filter(obj), updates = arr(d.updates).filter(obj);
    const sourceShas = new Set(arr(d.sources).map(s => obj(s)?.sha256).filter(str));
    const out = [];
    if (plans.length !== 2) out.push(h('p', {class:'warn', role:'note'}, `The record contains ${plans.length} plan(s); this cycle is expected to contain exactly 2. All recorded plans are shown.`));
    if (!plans.length) out.push(h('div', {class:'empty'}, h('strong', null, 'No plans recorded'), 'The record contains no experiment plans.'));
    plans.forEach((p, i) => {
      const r = results.find(x => x.plan_id != null && x.plan_id === p.plan_id) || null;
      const u = r ? updates.find(x => x.result_id != null && x.result_id === r.result_id) || null : null;
      out.push(card(p, r, u, i, sourceShas));
    });
    const orphan = results.filter(r => !plans.some(p => p.plan_id === r.plan_id));
    if (orphan.length) out.push(h('p', {class:'warn', role:'note'}, `${orphan.length} recorded result(s) reference no plan in this file: `, orphan.map(r => show(r.result_id)).join(', ')));
    $('experimentCards').replaceChildren(...out);
  }

  function step(title, tag, body, extraClass) {
    return h('div', {class:`exp-step${extraClass ? ` ${extraClass}` : ''}`}, h('h4', null, title, tag), h('div', null, body));
  }
  const para = (label, v) => h('p', null, label ? h('span', {class:'sub'}, `${label}: `) : null, show(str(v) ?? v));

  function card(p, r, u, i, sourceShas) {
    const cands = arr(p.candidates).filter(x => typeof x === 'string');
    const sel = p.selected_test_id;
    const selInCands = str(sel) ? cands.includes(sel) : null;
    const steps = [];

    steps.push(step('Initial hypothesis', who('agent', 'Agent-authored'), [
      para(null, p.hypothesis),
      arr(p.evidence_result_ids).length ? h('p', {class:'sub'}, 'Evidence cited: ', arr(p.evidence_result_ids).map(show).join(', ')) : h('p', {class:'sub'}, 'Evidence cited: none (first plan or none recorded)'),
    ]));

    steps.push(step('Competing tests', who('agent', 'Agent-authored from fixed catalog'), [
      cands.length ? h('ul', {class:'cand-list', 'aria-label':'Candidate catalog test IDs'}, cands.map(c => h('li', {class: c === sel ? 'is-chosen' : null}, c, c === sel ? h('small', null, 'chosen') : null))) : h('p', null, '—'),
      cands.length < 2 ? h('p', {class:'warn', role:'note'}, `Only ${cands.length} candidate test(s) recorded; at least 2 competing tests are expected.`) : null,
    ]));

    steps.push(step('Chosen study and rationale', who('agent', 'Agent-authored'), [
      h('p', null, h('span', {class:'sub'}, 'Selected test: '), code(sel)),
      para('Reason', p.reason),
      para('Expected learning', p.expected_learning),
      h('p', {class:'integrity'}, 'Selected test is among candidates: ', mark(selInCands)),
    ]));

    if (r) {
      const values = obj(r.values) || {};
      const numeric = Object.entries(values).filter(([, v]) => finite(v));
      const rest = Object.fromEntries(Object.entries(values).filter(([, v]) => !finite(v)));
      const restN = Object.keys(rest).length;
      steps.push(step('Measured result', who('det', 'Deterministic computation'), [
        numeric.length ? h('dl', {class:'kv'}, numeric.map(([k, v]) => h('div', null, h('dt', null, k), h('dd', null, String(v))))) : h('p', null, 'No numeric fields recorded.'),
        h('details', {class:'raw'}, h('summary', null, restN ? `Remaining fields (${restN}) as JSON` : 'Remaining fields (none)'), h('pre', null, restN ? json(rest) : '{}')),
        h('p', {class:'integrity'},
          'Result ', code(r.result_id), ' · test ', code(r.test_id),
          ' · elapsed ', finite(r.elapsed_seconds) ? `${r.elapsed_seconds} s (recorded wall-clock, not a speed claim)` : '—'),
        h('p', {class:'integrity'}, 'Test matches selected study: ', mark(str(r.test_id) && str(sel) ? r.test_id === sel : null),
          ' · Source hash listed in sources: ', mark(str(r.source_sha256) ? sourceShas.has(r.source_sha256) : null)),
      ]));
    } else {
      steps.push(step('Measured result', who('det', 'Deterministic computation'), h('div', {class:'empty'}, h('strong', null, 'No result recorded for this plan'), 'No measured values are shown.')));
    }

    if (u) {
      steps.push(step('Result-bound interpretation', who('agent', 'Agent-authored'), [
        para(null, u.interpretation),
        para('Next hypothesis', u.next_hypothesis),
      ]));
      steps.push(step('Next proposed experiment', who('prop', 'Proposed · not executed'), [
        para(null, u.next_experiment),
        h('p', {class:'sub'}, 'This is a proposal only. It was not run and has no measured result.'),
      ], 'proposed'));
    } else {
      steps.push(step('Result-bound interpretation', who('agent', 'Agent-authored'), h('p', null, '— No interpretation recorded for this result.')));
    }

    return h('article', {class:'exp-card', 'aria-labelledby':`exp${i}`},
      h('header', null, h('h3', {id:`exp${i}`}, `Experiment ${i + 1}`), h('span', {class:'ids'}, 'Plan ', code(p.plan_id))),
      steps);
  }

  function renderRoles(d) {
    const used = arr(d.results).filter(obj).length;
    const bar = h('div', {class:'bar', style:`--cap:${Math.max(RUN_CAP, used)}`, 'aria-hidden':'true'},
      Array.from({length:Math.max(RUN_CAP, used)}, (_, k) => h('i', {class: k < used ? (k < RUN_CAP ? 'used' : 'over') : null})));
    $('rolesBody').replaceChildren(
      h('dl', {class:'roles'},
        h('div', null, h('dt', null, 'Omnigent agent (agent-authored)'), h('dd', null, 'Writes hypotheses, chooses one test from the fixed catalog among competing candidates, states the reason and expected learning, interprets the measured result and proposes the next experiment.')),
        h('div', null, h('dt', null, 'Deterministic runner (computation)'), h('dd', null, 'Executes only the selected catalog test and records the values, elapsed time and source hash. The agent cannot edit these numbers.')),
        h('div', null, h('dt', null, 'Controls'), h('dd', null, 'No free-form code or arbitrary commands: tests are catalog IDs. Proposed next experiments are recorded as text and never executed in this cycle.'))),
      h('div', {class:'budget'},
        h('p', null, h('strong', null, `Budget cap: ${RUN_CAP} diagnostic runs`), ` · runs recorded: ${used}`),
        bar,
        used > RUN_CAP ? h('p', {class:'warn', role:'note'}, `The record contains ${used} results, more than the cap of ${RUN_CAP}.`) : null));
  }

  function checkRows(checks) {
    if (Array.isArray(checks)) return checks.map((c, i) => {
      if (typeof c === 'string') return [c, null, null];
      const o = obj(c) || {};
      const st = o.status ?? (o.passed === true || o.ok === true ? 'passed' : o.passed === false || o.ok === false ? 'failed' : null);
      return [o.name ?? o.id ?? o.check ?? `Check ${i + 1}`, st, o.detail ?? o.message ?? null];
    });
    const o = obj(checks);
    if (!o) return [];
    return Object.entries(o).map(([k, v]) => {
      if (typeof v === 'boolean') return [k, v ? 'passed' : 'failed', null];
      const vo = obj(v);
      if (vo) return [k, vo.status ?? (vo.passed === true ? 'passed' : vo.passed === false ? 'failed' : null), vo.detail ?? vo.message ?? json(vo)];
      return [k, typeof v === 'string' ? v : null, typeof v === 'string' ? null : show(v)];
    });
  }

  function renderVerification(d) {
    const sdk = obj(d.sdk) || {}, ver = obj(d.verification) || {};
    const rows = checkRows(ver.checks);
    const sources = arr(d.sources).filter(obj);
    const cell = v => typeof v === 'object' && v !== null ? json(v) : show(v);
    $('verificationBody').replaceChildren(
      receipt([
        ['Session ID', code(sdk.session_id)], ['Session status', code(sdk.status)], ['Model', show(sdk.model)], ['Harness', show(sdk.harness)],
        ['Verification status', code(ver.status)], ['Checked at', time(ver.checked_at)],
      ]),
      h('div', {class:'ver-tables'},
        h('div', null, h('h3', null, 'Verification checks'),
          rows.length ? h('div', {class:'table-scroll', tabindex:'0', role:'region', 'aria-label':'Verification checks'},
            h('table', {class:'data-table'}, h('thead', null, h('tr', null, h('th', {scope:'col'}, 'Check'), h('th', {scope:'col'}, 'Status'), h('th', {scope:'col'}, 'Detail'))),
              h('tbody', null, rows.map(([n, s, det]) => h('tr', null, h('th', {scope:'row'}, cell(n)), h('td', {class: statusOk(s) ? 'ok' : statusBad(s) ? 'bad' : null}, cell(s)), h('td', {class:'reason'}, cell(det)))))))
          : h('div', {class:'empty'}, h('strong', null, 'No individual checks listed'), 'Only the overall verification status is recorded.')),
        h('div', null, h('h3', null, 'Sources'),
          sources.length ? h('div', {class:'table-scroll', tabindex:'0', role:'region', 'aria-label':'Source files and hashes'},
            h('table', {class:'data-table'}, h('thead', null, h('tr', null, h('th', {scope:'col'}, 'Path'), h('th', {scope:'col'}, 'SHA-256'))),
              h('tbody', null, sources.map(s => h('tr', null, h('th', {scope:'row'}, show(s.path)), h('td', {class:'mono-id'}, show(s.sha256)))))))
          : h('div', {class:'empty'}, h('strong', null, 'No sources listed'), 'The record names no source files.'))));
  }

  function renderLimits(d) {
    const own = [
      'This page only re-reads a recorded, completed cycle. It does not run experiments in the browser and has no replay or live execution.',
      'Diagnostics are post-hoc and authored synthetic. They are not a new hold-out gain, a factory result or a physical measurement.',
      'Verification confirms session completion and source integrity only; it says nothing about inspection accuracy or discovery speed.',
      'Next experiments are proposals and were not executed.',
    ];
    const list = [...arr(d.limits).filter(x => typeof x === 'string'), ...own];
    $('limitationsList').replaceChildren(...list.map(x => h('li', null, x)));
  }

  function download() {
    if (!state.bytes) return;
    const url = URL.createObjectURL(new Blob([state.bytes], {type:'application/json'}));
    const a = h('a', {href:url, download:'discovery-cycle.json'}); document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  $('download').addEventListener('click', download);
  window.__discoveryCycle = {state};
  load();
})();

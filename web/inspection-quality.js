(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const DATA_URL = './data/inspection-quality.json';
  const DATA_FILE = 'web/data/inspection-quality.json';
  const KIND = 'inspection_v3_quality_diagnostics';
  const SCOPE = 'posthoc_diagnostic_authored_synthetic';
  const SCEN_NAME = {stationary:'Stationary', novel_cluster:'Novel cluster', nuisance_heavy:'Nuisance-heavy', process_shift:'Process shift', low_contrast:'Low contrast'};
  const SCEN_ORDER = ['stationary', 'novel_cluster', 'nuisance_heavy', 'process_shift', 'low_contrast'];
  // Reading rule of this page, stated next to every label it produces; not a stored result.
  const CEILING_LOW = 0.6, RANK_HIGH = 0.05;
  const SMALL_N = 30;
  const state = {data:null, bytes:null, scenarios:[], scen:'all', err:'all', cal:'candidates', sha:null};

  // ---------- value guards: only finite values in their valid range reach the page ----------
  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const arr = v => Array.isArray(v) ? v : [];
  const nn = v => finite(v) && v >= 0 ? v : null;
  const pos = v => finite(v) && v > 0 ? v : null;
  const prob = v => finite(v) && v >= 0 && v <= 1 ? v : null;
  const ratio = (a, b) => nn(a) != null && pos(b) != null ? a / b : null;
  const allOk = (...v) => v.every(x => x != null);
  const text = v => v == null || v === '' ? '—' : String(v);
  const e = v => text(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (n, d = 2) => finite(n) ? n.toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
  const fmtT = (n, d = 2) => finite(n) ? n.toLocaleString('en-US', {maximumFractionDigits:d}) : '—';
  const count = n => finite(n) ? n.toLocaleString('en-US') : '—';
  const pct = (n, d = 1) => finite(n) ? fmt(n * 100, d) + '%' : '—';
  const p3 = n => finite(n) ? n.toFixed(3).replace(/^0\./, '.').replace(/^-0\./, '−.') : '—';
  const scenName = k => SCEN_NAME[k] || k;
  const sum = (list, f) => { let t = 0; for (const x of list) { const v = nn(f(x)); if (v == null) return null; t += v; } return t; };
  const empty = (title, paths) => `<div class="empty"><strong>${e(title)}</strong>Required fields ${paths.map(p => `<code>${e(p)}</code>`).join(', ')} are missing or out of valid range · file <code>${DATA_FILE}</code></div>`;
  const check = ok => ok == null ? '<span class="chk">cannot check</span>' : ok ? '<span class="chk ok">match</span>' : '<span class="chk bad">mismatch</span>';
  const near = (a, b, tol = 1e-6) => finite(a) && finite(b) ? Math.abs(a - b) <= tol : null;

  // ---------- loading ----------
  function showState(html) { const box = $('loadState'); box.hidden = false; box.innerHTML = html; }
  function retryButton() { const b = document.createElement('button'); b.type = 'button'; b.className = 'control'; b.textContent = 'Retry'; b.addEventListener('click', load); $('loadState').append(b); }
  async function load() {
    $('app').hidden = true; $('download').disabled = true;
    showState('<div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div><p>Loading stored post-hoc diagnostics…</p>');
    let response;
    try { response = await fetch(DATA_URL, {cache:'no-store'}); }
    catch (error) {
      console.error('Inspection quality fetch:', error);
      showState(`<h2>Cannot access the diagnostics file</h2><p>Could not read <code>${DATA_FILE}</code> because of a network error or a local file-access restriction. Serve <code>web/</code> with a static server and try again.</p>`);
      return retryButton();
    }
    if (response.status === 404) {
      showState(`<h2>No verified diagnostics yet</h2><p>The post-hoc quality diagnostics have not been exported to <code>${DATA_FILE}</code> yet. This page fills in once diagnostics that passed audit are exported. No example values are shown.</p>`);
      return retryButton();
    }
    try {
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const bytes = await response.arrayBuffer();
      const data = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes));
      if (!obj(data)) throw Error('Top-level value is not an object');
      if (data.schema_version !== 1) throw Error(`Unsupported schema_version ${text(data.schema_version)}`);
      if (data.kind !== KIND) throw Error(`kind is not ${KIND} (${text(data.kind)})`);
      state.data = data; state.bytes = bytes; state.sha = null;
      const ps = obj(data.per_scenario) || {};
      state.scenarios = Object.keys(ps).filter(k => obj(ps[k]))
        .sort((a, b) => (SCEN_ORDER.indexOf(a) + 1 || 99) - (SCEN_ORDER.indexOf(b) + 1 || 99) || a.localeCompare(b));
      fillSelects();
      $('loadState').hidden = true; $('app').hidden = false; $('download').disabled = false;
      renderAll();
      digest(bytes);
    } catch (error) {
      console.error('Inspection quality parse:', error);
      showState(`<h2>Cannot parse the diagnostics file</h2><p>${e(error.message)}. Check the export format of <code>${DATA_FILE}</code> and try again.</p>`);
      retryButton();
    }
  }

  async function digest(bytes) {
    try {
      if (!globalThis.crypto?.subtle) throw Error('crypto.subtle unavailable');
      const h = await crypto.subtle.digest('SHA-256', bytes);
      state.sha = Array.from(new Uint8Array(h), b => b.toString(16).padStart(2, '0')).join('');
    } catch (error) { state.sha = false; }
    renderReceipt();
  }

  function fillSelects() {
    const opts = state.scenarios.map(k => `<option value="${e(k)}">${e(scenName(k))}</option>`).join('');
    $('scenSelect').innerHTML = '<option value="all">Compare all</option>' + opts;
    $('errSelect').innerHTML = '<option value="all">All 5 scenarios pooled</option>' + opts;
    if (state.scenarios.length !== 5) $('errSelect').options[0].textContent = `All ${state.scenarios.length} scenarios pooled`;
    if (state.scen !== 'all' && !state.scenarios.includes(state.scen)) state.scen = 'all';
    if (state.err !== 'all' && !state.scenarios.includes(state.err)) state.err = 'all';
    $('scenSelect').value = state.scen; $('errSelect').value = state.err; $('calSelect').value = state.cal;
  }

  function renderAll() {
    renderMeta(); renderBudget(); renderBalance(); renderScenarios(); renderErrors(); renderCalibration(); renderReceipt(); renderLimitations();
  }

  const scen = k => obj(obj(state.data.per_scenario)?.[k]);
  const lotsOf = k => pos(scen(k)?.lots);
  const mean = (k, f) => nn(obj(scen(k)?.mean_per_lot)?.[f]);

  // ---------- meta ----------
  function renderMeta() {
    const d = state.data;
    let generated = text(d.generated_at);
    if (typeof d.generated_at === 'string' && !Number.isNaN(Date.parse(d.generated_at))) generated = new Date(d.generated_at).toLocaleString('en-US', {dateStyle:'medium', timeStyle:'short'});
    const items = [['Policy', d.candidate, true], ['Model', d.model, true], ['Mode', d.mode, true], ['Budget per lot', pos(d.budget_cu) != null ? `${count(d.budget_cu)} CU` : null],
      ['Lots', pos(d.lots) != null ? count(d.lots) : null], ['Audit', d.audit_status], ['Generated', generated]];
    $('studyMeta').innerHTML = items.map(([k, v, mono]) => `<div><dt>${e(k)}</dt><dd${mono ? ' class="mono"' : ''}>${e(v)}</dd></div>`).join('');
    const warn = [];
    if (d.scope !== SCOPE) warn.push(`scope is not <code>${SCOPE}</code> (<code>${e(d.scope)}</code>).`);
    if (d.replaces_primary !== false) warn.push('<code>replaces_primary</code> is not recorded as false. Check how this relates to the primary result.');
    if (d.audit_status !== 'passed') warn.push(`Audit status is not passed (<code>${e(d.audit_status)}</code>). Treat the values below as unverified.`);
    $('scopeWarn').hidden = !warn.length; $('scopeWarn').innerHTML = warn.join(' ');
  }

  // ---------- 1. budget ----------
  function hbar(rows, max, unit) {
    return `<ul class="hbars">${rows.map(r => `<li class="${r.cls || ''}"><span class="hb-name">${r.name}</span><span class="hb-track"><i style="width:${Math.max(0, Math.min(100, r.value / max * 100))}%"></i></span><span class="hb-val">${count(r.value)} ${unit}</span></li>`).join('')}</ul>`;
  }

  function visitRows() {
    return state.scenarios.map(k => {
      const v = obj(scen(k)?.visits) || {};
      return {k, lots:lotsOf(k), visits:nn(v.mean_per_lot), short:finite(v.shortfall_vs_bound) ? v.shortfall_vs_bound : null,
        stage:nn(v.mean_stage_movement_cu_above_minimum), load:nn(v.mean_extra_wafer_loads_cu), retry:nn(v.mean_retry_cu), unspent:nn(v.mean_unspent_cu),
        stops:obj(v.stop_reasons) || {}};
    });
  }

  function renderBudget() {
    const d = state.data, vb = obj(d.visit_bound) || {}, o = obj(d.overall) || {}, m = obj(o.mean_per_lot) || {};
    const B = pos(d.budget_cu), f = pos(vb.first_review_min_cu), l = pos(vb.later_review_min_cu), r = nn(vb.retry_reserve_cu);
    const dies = pos(m.dies), visits = nn(o.mean_visits_per_lot), maxV = nn(vb.max_visits);
    const title = dies != null && visits != null ? `Why only ${fmtT(visits)} of ${count(dies)} dies are reviewed on average` : 'Why only some dies are reviewed';
    $('budgetTitle').textContent = title;
    if (!allOk(B, f, l, r) || B < f + r) { $('budgetBody').innerHTML = empty('Cannot compute the visit bound',['budget_cu', 'visit_bound.first_review_min_cu', 'visit_bound.later_review_min_cu', 'visit_bound.retry_reserve_cu']); return; }
    const n = 1 + Math.floor((B - f - r) / l + 1e-9);
    const charged = f + l * (n - 1), unspent = B - charged;
    const match = maxV == null ? null : n === maxV && near(charged, vb.charged_at_bound) !== false && near(unspent, vb.unspent_at_bound) !== false;
    const allDie = pos(o.min_cu_to_review_every_die_per_lot), allCand = pos(o.min_cu_to_review_every_candidate_per_lot), cands = nn(m.optical_candidates);
    const overBound = prob(o.visits_over_optimistic_bound), reviewed = prob(o.dies_reviewed_fraction);

    const statement = dies != null && visits != null
      ? `A lot has <span class="num">${count(dies)}</span> dies (three wafers), but on average only <span class="num">${fmtT(visits)}</span> per lot actually received a paid SEM review. Even assuming only minimum fees, no failures and no movement, the bound is <span class="num">${n}</span> reviews.`
      : `Even assuming only minimum fees, no failures and no movement, the bound is <span class="num">${n}</span> paid reviews per lot.`;

    const formula = `<div class="formula" role="group" aria-label="Derivation of the visit bound">
      <p class="f-line"><span>first-review minimum</span> <b>${fmtT(f)}</b> + <span>later-review minimum</span> <b>${fmtT(l)}</b> × (n − 1) + <span>retry reserve</span> <b>${fmtT(r)}</b> ≤ <span>budget</span> <b>${fmtT(B)}</b> CU</p>
      <p class="f-line">n ≤ 1 + ⌊(${fmtT(B)} − ${fmtT(f)} − ${fmtT(r)}) ÷ ${fmtT(l)}⌋ = 1 + ⌊${fmtT(B - f - r)} ÷ ${fmtT(l)}⌋ = <b>${n}</b> reviews</p>
      <p class="f-line f-sub">This charges ${fmtT(f)} + ${fmtT(l)} × ${n - 1} = <b>${fmtT(charged)} CU</b>; the remaining ${fmtT(unspent)} CU is less than the ${fmtT(l)} + ${fmtT(r)} = ${fmtT(l + r)} CU the next review needs, so it cannot be used.</p>
      <p class="f-check">JSON <code>visit_bound.max_visits</code> ${maxV == null ? '—' : count(maxV)}, <code>charged_at_bound</code> ${fmtT(nn(vb.charged_at_bound))}, <code>unspent_at_bound</code> ${fmtT(nn(vb.unspent_at_bound))} vs this page's calculation: ${check(match)}</p>
    </div>`;

    const assumptions = arr(vb.assumptions).filter(x => typeof x === 'string');
    const bound = `<p class="prose">These ${n} reviews are a <strong>loose, optimistic upper bound</strong>. It assumes one wafer load, zero stage movement after the first review, and zero failures or missing results. It is neither an achievable optimal (oracle) plan nor fab throughput. Keeping a ${fmtT(r)} CU retry reserve before every review matches how the harness actually reserves budget.</p>
      ${assumptions.length ? `<details class="raw"><summary>${assumptions.length} assumptions recorded in the JSON</summary><ul lang="en">${assumptions.map(x => `<li>${e(x)}</li>`).join('')}</ul></details>` : ''}`;

    const costRows = [];
    if (allDie != null) costRows.push({name:`Review all ${dies != null ? count(dies) + ' ' : ''}dies`, value:allDie});
    if (allCand != null) costRows.push({name:`Review all ${cands != null ? fmtT(cands, 0) + ' ' : ''}optical candidates`, value:allCand});
    costRows.push({name:'Budget per lot', value:B, cls:'is-budget'});
    const costMax = Math.max(...costRows.map(x => x.value));
    const cost = `<figure class="figure-block"><figcaption class="fig-title"><strong>Lower bound on charged CU</strong> needed to review everything (linear axis from 0)</figcaption>${hbar(costRows, costMax, 'CU')}
      <p class="footnote">Both values are lower bounds computed from the frozen minimum fees, not budget proposals. Actual charges are higher because of movement and retries.${allDie != null ? ` Reviewing every die would need about ${fmt(allDie / B, 1)}× the budget.` : ''}</p></figure>`;

    const rows = visitRows();
    const W = sum(rows, x => x.lots);
    const wmean = key => W && rows.every(x => x[key] != null && x.lots != null) ? rows.reduce((t, x) => t + x[key] * x.lots, 0) / W : null;
    const stopTotal = {}; rows.forEach(x => Object.entries(x.stops).forEach(([k, v]) => { if (nn(v) != null) stopTotal[k] = (stopTotal[k] || 0) + v; }));
    const stopText = Object.entries(stopTotal).map(([k, v]) => `<code>${e(k)}</code> ${count(v)}${W ? ` / ${count(W)}` : ''}`).join(', ');
    const gap = visits != null && maxV != null ? maxV - visits : null;
    const gStage = wmean('stage'), gLoad = wmean('load'), gRetry = wmean('retry'), gUnspent = wmean('unspent');
    const extra = allOk(gStage, gLoad, gRetry, gUnspent) ? gStage + gLoad + gRetry - (unspent - gUnspent) : null;

    const figures = [['Observed mean reviews', visits != null ? `${fmtT(visits)}<small>/lot</small>` : '—'], ['Optimistic bound', `${n}<small>/lot</small>`],
      ['Share of bound', pct(overBound)], ['Share of dies reviewed', pct(reviewed, 2)]];

    const gapProse = gap != null && extra != null
      ? `<p class="prose">The observed mean is <strong>${fmt(gap)} reviews</strong> per lot below the bound. The gap comes from stage movement above the minimum (${fmt(gStage, 1)} CU), extra wafer loads (${fmt(gLoad, 1)} CU) and charged retries (${fmt(gRetry, 1)} CU). Unusable CU left at the end averages ${fmt(gUnspent, 1)} CU, ${fmt(unspent - gUnspent, 1)} CU less than the ${fmtT(unspent)} CU in the bound calculation. The extra ${fmt(extra, 1)} CU equals ${fmt(extra / l, 2)} later reviews (÷ ${fmtT(l)} CU), which ${Math.abs(extra / l - gap) < 0.05 ? 'matches' : `differs by ${fmt(Math.abs(extra / l - gap))} reviews from`} the observed gap of ${fmt(gap)}. Stop reasons across all lots: ${stopText || '—'}.</p>`
      : '';

    const table = rows.length ? `<div class="table-scroll" role="region" tabindex="0" aria-label="Reviews and extra cost by scenario table · scrollable"><table class="data-table visit-table"><caption>Mean per lot · the total row is weighted by lot count · CU columns are amounts above the minimum fees</caption>
      <thead><tr><th scope="col">Scenario</th><th scope="col" class="num">Lots</th><th scope="col" class="num">Mean reviews</th><th scope="col" class="num">Shortfall vs bound</th><th scope="col" class="num">Excess movement CU</th><th scope="col" class="num">Extra load CU</th><th scope="col" class="num">Retry CU</th><th scope="col" class="num">Unspent CU</th></tr></thead>
      <tbody>${rows.map(x => `<tr><th scope="row">${e(scenName(x.k))}</th><td class="num">${count(x.lots)}</td><td class="num">${fmt(x.visits)}</td><td class="num">${fmt(x.short)}</td><td class="num">${fmt(x.stage, 1)}</td><td class="num">${fmt(x.load, 1)}</td><td class="num">${fmt(x.retry, 1)}</td><td class="num">${fmt(x.unspent, 1)}</td></tr>`).join('')}
      <tr class="total"><th scope="row">All</th><td class="num">${count(W)}</td><td class="num">${fmt(visits)}</td><td class="num">${fmt(gap)}</td><td class="num">${fmt(gStage, 1)}</td><td class="num">${fmt(gLoad, 1)}</td><td class="num">${fmt(gRetry, 1)}</td><td class="num">${fmt(gUnspent, 1)}</td></tr></tbody></table></div>` : '';

    $('budgetBody').innerHTML = `<p class="statement wide">${statement}</p>
      <dl class="figures">${figures.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>
      <div class="two-col"><div>${formula}${bound}</div>${cost}</div>
      <h3 class="sub-head">Gap between the bound and observed reviews</h3>${gapProse}${table}
      <p class="footnote">The die count ${dies != null ? count(dies) : '—'} is the total over three wafers (Inspection v3 protocol <code>wafers_per_lot: 3</code>). This JSON has no wafer-count field, so only this value was taken from the protocol.</p>`;
  }

  // ---------- 2. balance ----------
  function renderBalance() {
    const o = obj(state.data.overall) || {}, m = obj(o.mean_per_lot) || {}, c = obj(o.counts) || {};
    const need = ['latent_doi', 'never_optically_admitted_doi', 'missed_candidate_doi', 'selected_latent_doi', 'tp_reported_positive', 'sensor_missed_doi', 'unresolved_doi', 'selected', 'selected_non_doi', 'reported_negative_non_doi', 'fp_reported_positive', 'unresolved_non_doi'];
    if (!need.every(k => nn(m[k]) != null && nn(c[k]) != null)) { $('balanceBody').innerHTML = empty('Cannot build the conservation table', need.map(k => `overall.mean_per_lot.${k}`)); return; }
    const L = m.latent_doi;
    const lots = pos(state.data.lots);
    const segA = [
      {k:'never', name:'Never offered as optical candidate', note:'out of reach for any candidate-only policy', f:'never_optically_admitted_doi'},
      {k:'unsel', name:'Candidate but not selected', note:'not picked within budget', f:'missed_candidate_doi'},
      {k:'sel', name:'Selected for paid review', note:'split by outcome below', f:'selected_latent_doi'}];
    const segB = [
      {k:'tp', name:'Confirmed (review positive)', note:'latent DOI reported positive by review', f:'tp_reported_positive'},
      {k:'sneg', name:'Sensor reported negative', note:'latent DOI but the review response was negative', f:'sensor_missed_doi'},
      {k:'unk', name:'Unresolved · outcome unknown', note:'all attempts failed or missing; not treated as good', f:'unresolved_doi'},
      {k:'nondoi', name:'Non-DOI selected', note:'', f:'selected_non_doi'}];
    const S = m.selected;
    const stack = (segs, total, label) => !(total > 0) ? '<p class="footnote">The denominator is 0, so no proportion chart is shown.</p>' : `<div class="stack" role="img" aria-label="${e(label)}">${segs.map(s => `<i class="seg ${s.k}" style="width:${m[s.f] / total * 100}%"></i>`).join('')}</div>`;
    const legend = (segs, total, denomName) => `<ul class="seg-legend">${segs.map(s => `<li><i class="sw ${s.k}" aria-hidden="true"></i><span class="sl-name">${s.name}${s.note ? `<small>${s.note}</small>` : ''}</span><span class="sl-val"><b>${fmt(m[s.f])}</b><small>${pct(prob(ratio(m[s.f], total)))} · ${denomName}</small></span></li>`).join('')}</ul>`;
    const nonNote = `Non-DOI ${fmt(m.selected_non_doi)} = review negative ${fmt(m.reported_negative_non_doi)} + false positive ${fmt(m.fp_reported_positive)} + unresolved ${fmt(m.unresolved_non_doi)}`;

    const chk1 = c.never_optically_admitted_doi + c.missed_candidate_doi + c.selected_latent_doi === c.latent_doi;
    const chk2 = c.tp_reported_positive + c.sensor_missed_doi + c.unresolved_doi === c.selected_latent_doi;
    const chk3 = c.selected_latent_doi + c.selected_non_doi === c.selected;
    const chk4 = c.reported_negative_non_doi + c.fp_reported_positive + c.unresolved_non_doi === c.selected_non_doi;

    const rows = [
      ['Latent DOI (post-hoc truth)', 'latent_doi', 0],
      ['└ Never offered as optical candidate', 'never_optically_admitted_doi', 1],
      ['└ Candidate but not selected', 'missed_candidate_doi', 1],
      ['└ Selected latent DOI', 'selected_latent_doi', 1],
      ['　└ Confirmed (review positive)', 'tp_reported_positive', 2],
      ['　└ Sensor reported negative', 'sensor_missed_doi', 2],
      ['　└ Unresolved · outcome unknown', 'unresolved_doi', 2],
      ['Selected non-DOI', 'selected_non_doi', 0],
      ['└ Review negative', 'reported_negative_non_doi', 1],
      ['└ False positive', 'fp_reported_positive', 1],
      ['└ Unresolved', 'unresolved_non_doi', 1],
      ['Total paid review sites', 'selected', 0]];

    $('balanceBody').innerHTML = `<p class="statement wide">Of <span class="num">${fmt(L)}</span> latent DOI per lot, <span class="num">${fmt(m.tp_reported_positive)}</span> are confirmed. The largest share (<span class="num">${fmt(m.missed_candidate_doi)}</span>) were optical candidates not picked within budget, and <span class="num">${fmt(m.never_optically_admitted_doi)}</span> were never offered as candidates.</p>
      <p class="prose truth-note"><strong>Latent DOI is post-hoc truth that exists only in the offline synthetic evaluation.</strong> It was read only to classify outcomes with values the generator knows, and never used for policy selection, scoring or replay. In a real process the state of unreviewed dies is unknown, and this page never marks unreviewed dies as good.</p>
      <figure class="figure-block balance-fig">
        <figcaption class="fig-title">Where ${fmt(L)} latent DOI per lot go (linear, 0 to ${fmt(L)})</figcaption>
        ${stack(segA, L, `Of ${fmt(L)} latent DOI: never offered ${fmt(m.never_optically_admitted_doi)}, candidate not selected ${fmt(m.missed_candidate_doi)}, selected ${fmt(m.selected_latent_doi)}`)}
        ${legend(segA, L, 'of latent DOI')}
        <figcaption class="fig-title">Outcomes of ${fmt(S)} paid review sites per lot (linear, 0 to ${fmt(S)})</figcaption>
        ${stack(segB, S, `Of ${fmt(S)} reviewed sites: confirmed ${fmt(m.tp_reported_positive)}, sensor negative ${fmt(m.sensor_missed_doi)}, unresolved ${fmt(m.unresolved_doi)}, non-DOI ${fmt(m.selected_non_doi)}`)}
        ${legend(segB, S, 'of review sites')}
        <p class="footnote">${nonNote}.</p>
      </figure>
      <div class="table-scroll" role="region" tabindex="0" aria-label="Latent DOI conservation table · scrollable"><table class="data-table balance-table"><caption>Per-lot means are totals over ${lots ? count(lots) + ' lots' : 'all lots'} divided by the lot count. Values are rounded to two decimals, so sub-items may differ from their parent by ±0.01. Conservation checks use the unrounded integer totals.</caption>
        <thead><tr><th scope="col">Category</th><th scope="col" class="num">Mean per lot</th><th scope="col" class="num">Total</th></tr></thead>
        <tbody>${rows.map(([name, f, lv]) => `<tr class="lv${lv}"><th scope="row">${name}</th><td class="num">${fmt(m[f])}</td><td class="num">${count(c[f])}</td></tr>`).join('')}</tbody></table></div>
      <ul class="checks">
        <li>never offered ${count(c.never_optically_admitted_doi)} + candidate not selected ${count(c.missed_candidate_doi)} + selected ${count(c.selected_latent_doi)} = latent DOI ${count(c.latent_doi)} ${check(chk1)}</li>
        <li>confirmed ${count(c.tp_reported_positive)} + sensor negative ${count(c.sensor_missed_doi)} + unresolved ${count(c.unresolved_doi)} = selected latent DOI ${count(c.selected_latent_doi)} ${check(chk2)}</li>
        <li>selected latent DOI ${count(c.selected_latent_doi)} + non-DOI ${count(c.selected_non_doi)} = paid reviews ${count(c.selected)} ${check(chk3)}</li>
        <li>non-DOI: review negative ${count(c.reported_negative_non_doi)} + false positive ${count(c.fp_reported_positive)} + unresolved ${count(c.unresolved_non_doi)} = ${count(c.selected_non_doi)} ${check(chk4)}</li>
      </ul>`;
  }

  // ---------- 3. scenarios ----------
  function scenStats(k) {
    const s = scen(k) || {};
    const ceil = prob(s.optical_recall_ceiling);
    const latent = mean(k, 'latent_doi'), cand = mean(k, 'candidate_doi'), sel = mean(k, 'selected'), selDoi = mean(k, 'selected_latent_doi');
    const nondoi = mean(k, 'selected_non_doi');
    const reached = prob(ratio(selDoi, latent));
    const rank = prob(ratio(nondoi, sel));
    return {k, lots:lotsOf(k), ceil, latent, cand, sel, selDoi, reached, rank, nondoi,
      visits:nn(obj(s.visits)?.mean_per_lot), tp:mean(k, 'tp_reported_positive'), sneg:mean(k, 'sensor_missed_doi'), unk:mean(k, 'unresolved_doi'),
      never:mean(k, 'never_optically_admitted_doi'), unsel:mean(k, 'missed_candidate_doi'), p99:mean(k, 'candidates_p_ge_099'), unselP99:mean(k, 'unselected_candidate_doi_p_ge_099')};
  }
  function bottleneck(x) {
    if (x.ceil == null) return {label:'Undetermined', cls:''};
    const main = x.ceil < CEILING_LOW ? {label:'Optical ceiling', cls:'b-optic'} : {label:'Review count (budget)', cls:'b-visit'};
    if (x.rank != null && x.rank >= RANK_HIGH) { main.label += ' + ranking error'; main.rankFlag = true; }
    return main;
  }

  function renderScenarios() {
    if (!state.scenarios.length) { $('scenBody').innerHTML = empty('No per-scenario diagnostics', ['per_scenario']); return; }
    const list = state.scenarios.map(scenStats);
    const pick = state.scen;
    const ticks = [0, .25, .5, .75, 1];
    const chart = `<figure class="figure-block"><figcaption class="fig-title">Optical candidate ceiling and share of latent DOI actually selected (both relative to latent DOI, 0–1 axis)</figcaption>
      <ul class="rbars">${list.map(x => `<li class="${pick === x.k ? 'is-pick' : ''}${pick !== 'all' && pick !== x.k ? ' is-dim' : ''}"><span class="rb-name">${e(scenName(x.k))}</span><span class="rb-track">${x.ceil != null ? `<i class="ceil" style="width:${x.ceil * 100}%"></i>` : ''}${x.reached != null ? `<i class="reach" style="width:${x.reached * 100}%"></i>` : ''}</span><span class="rb-val">${p3(x.ceil)} <small>/ ${p3(x.reached)}</small></span></li>`).join('')}</ul>
      <div class="rb-axis" aria-hidden="true"><span></span><span>${ticks.map(t => `<b>${t === 0 ? '0' : t === 1 ? '1' : String(t).replace(/^0/, '')}</b>`).join('')}</span><span></span></div>
      <p class="seg-key"><span><i class="sw ceil" aria-hidden="true"></i>Optical candidate ceiling = candidate DOI ÷ latent DOI</span><span><i class="sw reach" aria-hidden="true"></i>Selected latent DOI ÷ latent DOI</span></p></figure>`;

    let detail;
    if (pick === 'all') {
      const optic = list.filter(x => x.ceil != null && x.ceil < CEILING_LOW), visit = list.filter(x => x.ceil != null && x.ceil >= CEILING_LOW);
      const names = xs => xs.map(x => `${e(scenName(x.k))} ${p3(x.ceil)}`).join(', ');
      detail = `<p class="prose">The bottleneck splits two ways. In scenarios with a low <strong>optical candidate ceiling</strong> (${names(optic) || 'none'}), most latent DOI are never offered as candidates, so they are missed however well the policy chooses among candidates. In scenarios with a high ceiling (${names(visit) || 'none'}), candidates contain enough DOI, but the number of reviews per lot is the bottleneck. Pick a scenario to see its explanation.</p>`;
    } else {
      const x = list.find(y => y.k === pick), b = bottleneck(x);
      const parts = [];
      if (x.ceil != null) parts.push(`Of ${fmt(x.latent)} latent DOI per lot, ${fmt(x.cand)} were offered as optical candidates (ceiling ${p3(x.ceil)}). The other ${fmt(x.never)} were never offered, so no candidate-only policy can reach them.`);
      if (x.visits != null) parts.push(`Mean reviews are ${fmt(x.visits)}, while candidates with p ≥ 0.99 alone number ${fmt(x.p99)}. Of those, ${fmt(x.unselP99)} latent DOI were left unselected.`);
      if (x.rank != null) parts.push(`Of ${fmt(x.sel)} selected sites, ${fmt(x.nondoi)} (${pct(x.rank)}) were non-DOI${b.rankFlag ? ', so ranking error adds to the bottleneck' : ', so ranking error is small'}.`);
      if (x.sneg != null) parts.push(`Among selected latent DOI, the sensor reported ${fmt(x.sneg)} as negative, and ${fmt(x.unk)} are unresolved with unknown outcome.`);
      detail = `<p class="prose"><span class="bneck ${b.cls}">${e(b.label)}</span> ${parts.join(' ')}</p>`;
    }

    const table = `<div class="table-scroll" role="region" tabindex="0" aria-label="Bottleneck by scenario table · scrollable"><table class="data-table scen-table"><caption>Mean per lot · the bottleneck label is this page's reading rule: optical ceiling if ceiling &lt; ${CEILING_LOW}, otherwise review count. Ranking error is appended when the non-DOI selection share is ≥ ${pct(RANK_HIGH, 0)}.</caption>
      <thead><tr><th scope="col">Scenario</th><th scope="col" class="num">Optical ceiling</th><th scope="col" class="num">Candidate DOI</th><th scope="col" class="num">Latent DOI</th><th scope="col" class="num">Mean reviews</th><th scope="col" class="num">Confirmed</th><th scope="col" class="num">Selected non-DOI</th><th scope="col" class="num">Sensor negative</th><th scope="col" class="num">Unresolved</th><th scope="col">Bottleneck</th></tr></thead>
      <tbody>${list.map(x => { const b = bottleneck(x); return `<tr class="${pick === x.k ? 'is-current' : ''}"><th scope="row">${e(scenName(x.k))}</th><td class="num">${p3(x.ceil)}</td><td class="num">${fmt(x.cand)}</td><td class="num">${fmt(x.latent)}</td><td class="num">${fmt(x.visits)}</td><td class="num">${fmt(x.tp)}</td><td class="num">${fmt(x.nondoi)}</td><td class="num">${fmt(x.sneg)}</td><td class="num">${fmt(x.unk)}</td><td><span class="bneck ${b.cls}">${e(b.label)}</span></td></tr>`; }).join('')}</tbody></table></div>`;

    $('scenBody').innerHTML = `${detail}${chart}${table}`;
  }

  // ---------- 4. errors ----------
  function errData(k) {
    const keys = k === 'all' ? state.scenarios : [k];
    const sep = keys.map(x => obj(scen(x)?.error_separation));
    if (!sep.length || sep.some(s => !s)) return null;
    const th = sep.map(s => obj(s.threshold_classification_candidates));
    const rk = sep.map(s => obj(s.ranking)), sn = sep.map(s => obj(s.sensor));
    const add = (list, f) => list.every(Boolean) ? sum(list, f) : null;
    const t = {tp:add(th, x => x.tp), fp:add(th, x => x.fp), fn:add(th, x => x.fn), tn:add(th, x => x.tn)};
    const thr = th.every(Boolean) && th.every(x => x.threshold === th[0].threshold) ? prob(th[0].threshold) : null;
    const r = {};
    ['selected_latent_doi', 'selected_non_doi', 'candidates_p_ge_099', 'selected_p_ge_099', 'selected_p_ge_099_doi', 'unselected_candidate_doi_p_ge_099'].forEach(f => { r[f] = add(rk, x => x[f]); });
    const s = {};
    ['sensor_missed_doi', 'fp_reported_positive', 'unresolved_doi', 'unresolved_non_doi', 'retried_reviews'].forEach(f => { s[f] = add(sn, x => x[f]); });
    ['ok', 'failure', 'missing', 'other'].forEach(f => { s['att_' + f] = add(sn, x => obj(x.attempts)?.[f]); });
    const lots = sum(keys, lotsOf);
    return {t, thr, r, s, lots, n:keys.length};
  }

  function renderErrors() {
    if (!state.scenarios.length) { $('errBody').innerHTML = empty('No error-separation diagnostics', ['per_scenario.*.error_separation']); return; }
    const d = errData(state.err);
    if (!d) { $('errBody').innerHTML = empty('No error-separation diagnostics',[`per_scenario.${state.err === 'all' ? '*' : state.err}.error_separation`]); return; }
    const per = v => d.lots && v != null ? fmt(v / d.lots) : '—';
    const {t, r, s} = d;
    const precision = prob(ratio(t.tp, t.tp + t.fp)), recall = prob(ratio(t.tp, t.tp + t.fn));
    const scope = state.err === 'all' ? `${d.n} scenarios pooled, ${count(d.lots)} lots` : `${e(scenName(state.err))}, ${count(d.lots)} lots`;
    const thrText = d.thr != null ? fmtT(d.thr) : '—';

    const sel = allOk(r.selected_latent_doi, r.selected_non_doi) ? r.selected_latent_doi + r.selected_non_doi : null;
    const rankRate = prob(ratio(r.selected_non_doi, sel));

    $('errBody').innerHTML = `<p class="err-scope">Scope: ${scope}. Totals are exact integers; per-lot values are total ÷ lot count.</p>
      <div class="err-block">
        <h3>① Threshold classification error <span class="tag">Not used for route ranking</span></h3>
        <p class="prose">Classification obtained by cutting the frozen probabilities at p ≥ ${thrText}. It covers all original optical candidates, and the policy that chooses review routes does not use this threshold. So the FP and FN here are not errors on sites that actually received a review.</p>
        <div class="table-scroll" role="region" tabindex="0" aria-label="Threshold confusion matrix · scrollable"><table class="data-table conf-table"><caption>Confusion matrix · candidate counts (truth is post-hoc latent DOI)</caption>
          <thead><tr><th scope="col">Latent DOI</th><th scope="col" class="num">p ≥ ${thrText} (judged positive)</th><th scope="col" class="num">p &lt; ${thrText} (judged negative)</th></tr></thead>
          <tbody><tr><th scope="row">Yes</th><td class="num">TP ${count(t.tp)}</td><td class="num">FN ${count(t.fn)}</td></tr><tr><th scope="row">No</th><td class="num">FP ${count(t.fp)}</td><td class="num">TN ${count(t.tn)}</td></tr></tbody></table></div>
        <p class="kv">Precision <b>${p3(precision)}</b> · candidate recall <b>${p3(recall)}</b> · FP ${per(t.fp)}/lot · FN ${per(t.fn)}/lot</p>
      </div>
      <div class="err-block">
        <h3>② Selection ranking error</h3>
        <p class="prose">Sites that actually received a paid review but were not latent DOI: wrong picks under the frozen probabilities and route costs.</p>
        <dl class="state-list"><dt>Selected non-DOI / selected sites</dt><dd>${count(r.selected_non_doi)} / ${count(sel)} (${pct(rankRate)})</dd>
          <dt>Selected non-DOI per lot</dt><dd>${per(r.selected_non_doi)}</dd>
          <dt>Candidates with p ≥ 0.99 (per lot)</dt><dd>${count(r.candidates_p_ge_099)} (${per(r.candidates_p_ge_099)})</dd>
          <dt>Of those selected / latent DOI among selected</dt><dd>${count(r.selected_p_ge_099)} / ${count(r.selected_p_ge_099_doi)}</dd>
          <dt>Unselected p ≥ 0.99 latent DOI (per lot)</dt><dd>${count(r.unselected_candidate_doi_p_ge_099)} (${per(r.unselected_candidate_doi_p_ge_099)})</dd></dl>
        <p class="scope-note">If candidates contain more real latent DOI than there are affordable reviews, unreviewed DOI remain even with perfect ranking. A high predicted probability does not by itself guarantee a real DOI; read ranking error together with the review budget.</p>
      </div>
      <div class="err-block">
        <h3>③ Sensor response error</h3>
        <p class="prose">Sites that were reviewed but whose response was wrong or never arrived. Failed or missing results are not physical negatives; they are kept as unknown.</p>
        <dl class="state-list"><dt>Latent DOI reported negative</dt><dd>${count(s.sensor_missed_doi)} (${per(s.sensor_missed_doi)}/lot)</dd>
          <dt>Non-DOI reported positive</dt><dd>${count(s.fp_reported_positive)} (${per(s.fp_reported_positive)}/lot)</dd>
          <dt>Unresolved latent DOI / non-DOI</dt><dd>${count(s.unresolved_doi)} / ${count(s.unresolved_non_doi)}</dd>
          <dt>Reviews that needed a retry</dt><dd>${count(s.retried_reviews)} (${per(s.retried_reviews)}/lot)</dd>
          <dt>Attempts: ok / failed / missing / other</dt><dd>${count(s.att_ok)} / ${count(s.att_failure)} / ${count(s.att_missing)} / ${count(s.att_other)}</dd></dl>
      </div>`;
  }

  function calData(k, pop) {
    const keys = k === 'all' ? state.scenarios : [k];
    const field = pop === 'selected' ? 'calibration_selected' : 'calibration_candidates';
    const cals = keys.map(x => obj(scen(x)?.[field]));
    if (!cals.length || cals.some(c => !c || !Array.isArray(c.bins))) return null;
    const merge = bins => {
      const n = sum(bins, b => b.n), latent = sum(bins, b => b.latent_doi);
      if (n == null || latent == null) return {bin:bins[0]?.bin, n:null};
      let pw = 0, ok = true;
      for (const b of bins) { if (!b.n) continue; const mp = prob(b.mean_p); if (mp == null) { ok = false; break; } pw += mp * b.n; }
      const mp = n > 0 && ok ? pw / n : null, rate = n > 0 ? latent / n : null;
      return {bin:bins[0].bin, n, latent, mean_p:mp, rate, gap:mp != null && rate != null ? mp - rate : null};
    };
    const labels = cals[0].bins.map(b => obj(b)?.bin);
    if (cals.some(c => c.bins.length !== labels.length || c.bins.some((b, i) => obj(b)?.bin !== labels[i]))) return null;
    const bins = labels.map((_, i) => merge(cals.map(c => c.bins[i])));
    const hi = cals.every(c => obj(c['p_ge_0.99'])) ? merge(cals.map(c => c['p_ge_0.99'])) : null;
    return {bins, hi, n:sum(cals, c => c.n), latent:sum(cals, c => c.latent_doi)};
  }

  function renderCalibration() {
    const pop = state.cal, k = state.err;
    const d = state.scenarios.length ? calData(k, pop) : null;
    const field = pop === 'selected' ? 'calibration_selected' : 'calibration_candidates';
    if (!d) { $('calBody').innerHTML = empty('Cannot build the calibration table', [`per_scenario.${k === 'all' ? '*' : k}.${field}.bins`]); return; }
    const scope = `${k === 'all' ? `${state.scenarios.length} scenarios pooled (n-weighted mean p)` : e(scenName(k))} · ${pop === 'selected' ? 'selected review sites' : 'all optical candidates'} n = ${count(d.n)}`;
    const row = (b, cls = '') => {
      const nOk = nn(b.n);
      const small = nOk != null && nOk > 0 && nOk < SMALL_N;
      const mp = nOk ? prob(b.mean_p) : null, rt = nOk ? prob(b.rate) : null, gap = mp != null && rt != null ? b.gap : null;
      return `<tr class="${cls}"><th scope="row" class="mono">${e(b.bin)}</th><td class="num">${count(nOk)}${small ? ' <span class="small-n">small sample</span>' : ''}</td><td class="num">${nOk ? p3(mp) : '<span class="null">— no samples</span>'}</td><td class="num">${nOk ? p3(rt) : '—'}</td><td class="num">${gap == null ? '—' : (gap > 0 ? '+' : gap < 0 ? '−' : '') + p3(Math.abs(gap))}</td></tr>`;
    };
    const pts = d.bins.filter(b => nn(b.n) && prob(b.mean_p) != null && prob(b.rate) != null);
    const W = 280, P = 36, S = W - P - 10;
    const X = v => P + v * S, Y = v => 10 + (1 - v) * S;
    const grid = [0, .25, .5, .75, 1].map(t => `<line x1="${X(t)}" y1="${Y(0)}" x2="${X(t)}" y2="${Y(1)}" class="grid"/><line x1="${X(0)}" y1="${Y(t)}" x2="${X(1)}" y2="${Y(t)}" class="grid"/><text x="${X(t)}" y="${Y(0) + 16}" text-anchor="middle">${t}</text><text x="${X(0) - 6}" y="${Y(t) + 4}" text-anchor="end">${t}</text>`).join('');
    const svg = `<svg viewBox="0 0 ${W} ${W + 8}" role="img" aria-labelledby="calFigCap"><g>${grid}<line x1="${X(0)}" y1="${Y(0)}" x2="${X(1)}" y2="${Y(1)}" class="diag"/>${pts.map(b => `<circle cx="${X(b.mean_p)}" cy="${Y(b.rate)}" r="5" class="pt${nn(b.n) < SMALL_N ? ' small' : ''}"/>`).join('')}${d.hi && nn(d.hi.n) && prob(d.hi.mean_p) != null && prob(d.hi.rate) != null ? `<rect x="${X(d.hi.mean_p) - 4}" y="${Y(d.hi.rate) - 4}" width="8" height="8" class="pt hi"/>` : ''}</g></svg>`;
    const over = pts.filter(b => b.gap > 0.05).map(b => e(b.bin));
    const sentence = pts.length ? (over.length ? `Bins where mean p exceeds the actual rate by more than 0.05 (overconfident): ${over.join(', ')}.` : 'No sampled bin has mean p more than 0.05 above the actual rate. Check negative differences in the table for underprediction.') : 'No bin has samples.';
    $('calBody').innerHTML = `<p class="err-scope">Scope: ${scope}. ${sentence}</p>
      <div class="cal-grid"><figure class="cal-fig">${svg}<figcaption id="calFigCap">Calibration plot: x-axis mean predicted p, y-axis actual latent DOI rate, both 0–1. The diagonal is perfect calibration; points below it are overconfident. The square is the p ≥ 0.99 bin; hollow points have n &lt; ${SMALL_N}. The same values are in the calibration table.</figcaption></figure>
      <div class="table-scroll" role="region" tabindex="0" aria-label="Calibration table · scrollable"><table class="data-table cal-table"><caption>Fixed bins · actual rate = latent DOI ÷ n · difference = mean p − actual rate</caption>
        <thead><tr><th scope="col">p bin</th><th scope="col" class="num">n</th><th scope="col" class="num">Mean p</th><th scope="col" class="num">Actual rate</th><th scope="col" class="num">Difference</th></tr></thead>
        <tbody>${d.bins.map(b => row(b)).join('')}${d.hi ? row(d.hi, 'hi-row') : ''}</tbody></table></div></div>`;
  }

  // ---------- 5. receipt ----------
  function renderReceipt() {
    const d = state.data; if (!d) return;
    const size = state.bytes ? state.bytes.byteLength : null;
    const sha = state.sha === null ? 'Computing…' : state.sha === false ? 'Cannot be computed in this browser (no crypto.subtle)' : state.sha;
    const okAudit = d.audit_status === 'passed';
    const rows = [
      ['File', `<code>${DATA_FILE}</code>`],
      ['Bytes', size != null ? `${count(size)} bytes` : '—'],
      ['SHA-256 of this file (computed in browser)', `<code class="hash">${e(sha)}</code>`],
      ['Audit status', `<span class="${okAudit ? 'ok' : 'bad'}">${e(d.audit_status)}</span>`],
      ['Audit record SHA-256', `<code class="hash">${e(d.audit_sha256)}</code>`],
      ['Freeze receipt SHA-256', `<code class="hash">${e(d.freeze_sha256)}</code>`],
      ['Diagnostic script SHA-256', `<code class="hash">${e(d.diagnostic_script_sha256)}</code>`],
      ['kind · schema', `<code>${e(d.kind)}</code> · ${e(d.schema_version)}`],
      ['scope', `<code>${e(d.scope)}</code>`],
      ['Replaces primary result', d.replaces_primary === false ? 'No (preregistered primary result kept)' : `<span class="bad">${e(d.replaces_primary)}</span>`],
      ['Generated at', `<code>${e(d.generated_at)}</code>`]];
    const defs = obj(d.definitions) || {};
    const defList = Object.entries(defs).filter(([, v]) => typeof v === 'string');
    $('receiptBody').innerHTML = `<dl class="receipt">${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>
      <div class="receipt-actions"><button type="button" class="control primary-action" id="download2">Download raw JSON (${size != null ? count(size) + ' bytes' : '—'})</button><a class="control" href="./inspection-evidence.html">Inspection v3 evidence page</a></div>
      <p class="scope-note">The SHA-256 of this file is for checking the downloaded bytes. The audit and freeze hashes point to <code>audit.json</code> and the freeze receipt; this page does not re-verify those originals. The diagnostic script verified them before export.</p>
      ${defList.length ? `<details class="raw"><summary>${defList.length} term definitions from the JSON (verbatim)</summary><dl class="defs" lang="en">${defList.map(([k, v]) => `<div><dt><code>${e(k)}</code></dt><dd>${e(v)}</dd></div>`).join('')}</dl></details>` : ''}`;
    $('download2').addEventListener('click', download);
  }

  function renderLimitations() {
    const list = arr(state.data.limitations).filter(x => typeof x === 'string');
    const fixed = ['This page claims no improvement effect. It does not imply real SEM image judgment, a guarantee of high confidence, or use of Jev.',
      'Latent DOI and every rate derived from it are computed only from the synthetic generator\'s post-hoc truth. In a real process, unreviewed dies have an unknown outcome.'];
    $('limitationsList').innerHTML = fixed.map(x => `<li>${e(x)}</li>`).join('') + list.map(x => `<li lang="en">${e(x)}</li>`).join('');
  }

  function download() {
    if (!state.bytes) return;
    const url = URL.createObjectURL(new Blob([state.bytes], {type:'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = 'inspection-quality.json'; document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // ---------- events ----------
  $('download').addEventListener('click', download);
  $('scenSelect').addEventListener('change', ev => { state.scen = ev.target.value; renderScenarios(); });
  $('errSelect').addEventListener('change', ev => { state.err = ev.target.value; renderErrors(); renderCalibration(); });
  $('calSelect').addEventListener('change', ev => { state.cal = ev.target.value; renderCalibration(); });
  load();
})();

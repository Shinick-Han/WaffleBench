(() => {
  'use strict';
  // Post-hackathon recorded-spend explainer. Reads the original frozen export once; runs and writes nothing.
  const $ = id => document.getElementById(id);
  const Core = window.BudgetCore;
  const DATA_URL = './data/inspection-v3.json';
  const DATA_FILE = 'web/data/inspection-v3.json';
  const OUTCOME = {positive:'Review positive', negative:'Observed negative', failed:'Failed or missing'};
  const STATUS = {failure:'Measurement failed', missing:'Measurement missing'};
  const state = {data:null, ledger:null, cap:0, result:null, loading:false};

  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const arr = v => Array.isArray(v) ? v : [];
  const text = v => v == null || v === '' ? '—' : String(v);
  const e = v => text(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (n, d=2) => finite(n) ? n.toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
  const signed = (n, d=2) => finite(n) ? (n > 0 ? '+' : n < 0 ? '−' : '') + fmt(Math.abs(n), d) : '—';
  const pct = (n, d=1) => finite(n) ? signed(n * 100, d) + '%' : '—';
  const plainPct = (n, d=1) => finite(n) ? fmt(n * 100, d) + '%' : '—';
  const count = n => finite(n) ? n.toLocaleString('en-US') : '—';
  const waferName = w => w == null ? '—' : typeof w === 'number' ? `W${w}` : String(w);
  const cu = (n, d=2) => finite(n) ? `${fmt(n, d)} CU` : 'unavailable';
  // Rounds a recorded threshold up so a displayed/typed value never falls just below it.
  const ceil4 = n => Math.ceil(n * 1e4 - 1e-7) / 1e4;
  const outcomeText = row => row.outcome === 'failed' ? (STATUS[row.status] || 'No result') : OUTCOME[row.outcome] || 'No result';
  const badge = row => `<span class="result ${e(row.outcome)}"><i aria-hidden="true"></i>${e(outcomeText(row))}</span>`;

  // ---------- loading ----------
  function showState(html, retry) {
    const box = $('loadState'); box.hidden = false; box.innerHTML = html;
    if (retry) { const b = document.createElement('button'); b.type = 'button'; b.className = 'control'; b.textContent = 'Retry'; b.addEventListener('click', load); box.append(b); }
  }
  async function load() {
    if (state.loading) return;
    state.loading = true; $('app').hidden = true;
    showState('<div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div><p>Loading stored Inspection v3 evidence…</p>');
    try {
      if (!Core) return showState('<h2>Page logic did not load</h2><p><code>post-hackathon-budget-core.js</code> is missing, so no figures are calculated.</p>', true);
      let response;
      try { response = await fetch(DATA_URL, {cache:'no-store'}); }
      catch (error) {
        console.error('Inspection v3 fetch:', error);
        return showState(`<h2>Cannot access the evidence file</h2><p>Could not read <code>${DATA_FILE}</code> because of a network error or a local file-access restriction. Serve <code>web/</code> with a static server and try again. Nothing is shown in place of the evidence.</p>`, true);
      }
      if (response.status === 404) return showState(`<h2>Evidence file not found</h2><p><code>${DATA_FILE}</code> returned 404. This page shows only the stored export; no example or provisional values are substituted.</p>`, true);
      if (!response.ok) return showState(`<h2>Cannot load the evidence file</h2><p>The server answered HTTP ${e(response.status)} for <code>${DATA_FILE}</code>.</p>`, true);
      let data;
      try {
        data = JSON.parse(await response.text());
        if (!obj(data)) throw Error('Top-level value is not an object');
        if (data.schema_version != null && data.schema_version !== 1) throw Error(`Unsupported schema_version ${data.schema_version}`);
      } catch (error) {
        console.error('Inspection v3 parse:', error);
        return showState(`<h2>Cannot parse the evidence file</h2><p>${e(error.message)}. Check the export format of <code>${DATA_FILE}</code> and try again.</p>`, true);
      }
      state.data = data;
      state.ledger = Core.prepareLedger(data.replay);
      $('loadState').hidden = true; $('app').hidden = false;
      renderStatic(); renderPrimary(); renderFoot();
      if (finite(state.ledger.budget)) setCap(state.ledger.budget);
    } finally { state.loading = false; }
  }

  // ---------- static parts ----------
  function renderStatic() {
    const L = state.ledger;
    $('caveatBudget').textContent = finite(L.budget) ? `${fmt(L.budget, 0)}-CU` : 'exported-budget';
    $('ledgerMeta').innerHTML = `Recorded lot <code>${e(L.lot_id)}</code>, policy <code>${e(L.variant)}</code>: ${count(L.totalSites)} care sites, ${count(L.rows.length)} recorded paid reviews, ${cu(L.exportedSpent, 1)} of ${cu(L.budget, 0)} spent in the original run.`;
    const issues = $('ledgerIssues');
    issues.hidden = !L.issues.length;
    issues.innerHTML = L.issues.length ? `<div class="empty"><strong>Parts of the recorded ledger are unavailable</strong><ul>${L.issues.map(x => `<li>${e(x)}</li>`).join('')}</ul>No cost or outcome is filled in for unavailable rows; the cut stops before them.</div>` : '';
    const usable = finite(L.budget);
    for (const id of ['capControls', 'capResults']) $(id).hidden = !usable;
    if (!usable) return;
    for (const id of ['capRange', 'capNumber']) $(id).max = String(L.budget);
  }

  // ---------- cap ----------
  function thresholds() { return [0, ...state.ledger.rows.slice(0, state.ledger.validCount).map(r => r.cumulative_spend)].filter(t => t <= state.ledger.budget); }
  function setCap(value, source) {
    const error = Core.validateCap(value, state.ledger.budget);
    $('capError').textContent = error ? `${error} The figures below still show the last valid cap (${fmt(state.cap, 2)} CU).` : '';
    $('capNumber').setAttribute('aria-invalid', error ? 'true' : 'false');
    if (error) return;
    state.cap = value; state.result = Core.capPrefix(state.ledger, value);
    if (source !== 'range') $('capRange').value = String(value);
    if (source !== 'number') $('capNumber').value = String(Number(value.toFixed(4)) === value ? value : ceil4(value));
    $('capRange').setAttribute('aria-valuetext', `${fmt(value, 2)} CU, ${state.result.paidReviews} recorded paid reviews retained`);
    const t = thresholds();
    $('snapPrev').disabled = !t.some(x => x < value - 1e-9);
    $('snapNext').disabled = !t.some(x => x > value + 1e-9);
    $('snapFull').disabled = value === state.ledger.budget;
    renderSummary(); renderChart(); renderTables();
  }

  // ---------- summary ----------
  function renderSummary() {
    const r = state.result, L = state.ledger;
    const repeat = r.repeatedRecords > 0 ? ` · ${count(r.uniqueSites)} unique sites (${count(r.repeatedRecords)} repeat record${r.repeatedRecords === 1 ? '' : 's'})` : '';
    let next;
    if (r.stop === 'cap') next = `<dd><span class="big">${cu(r.next.charged)}</span><small>step ${e(r.next.step ?? r.next.position)} · <span class="mono">${e(r.next.site_id)}</span> · ${e(waferName(r.next.wafer))}. Needs a cap of at least the recorded cumulative ${cu(r.next.cumulative_spend, 4)}. Its outcome is not shown.</small></dd>`;
    else if (r.stop === 'unavailable') next = `<dd><span class="big">Unavailable</span><small>The next recorded row is malformed, so its charge and threshold are not known. Nothing beyond it is used.</small></dd>`;
    else next = `<dd><span class="big">None</span><small>The stored record ends here: all ${count(r.recordedTotal)} recorded reviews fit under this cap.</small></dd>`;
    $('summary').innerHTML = `<dl class="tiles">
      <div class="tile"><dt>Retained paid reviews</dt><dd><span class="big">${count(r.paidReviews)}</span><small>of ${count(r.recordedTotal)} recorded${repeat}</small></dd></div>
      <div class="tile"><dt>Recorded observations</dt><dd class="outcomes"><span class="result positive"><i aria-hidden="true"></i>${count(r.counts.positive)} review positive</span><span class="result negative"><i aria-hidden="true"></i>${count(r.counts.negative)} observed negative</span><span class="result failed"><i aria-hidden="true"></i>${count(r.counts.failed)} failed or missing</span></dd></div>
      <div class="tile"><dt>Actually charged</dt><dd><span class="big">${cu(r.charged)}</span><small>unused cap ${cu(r.unusedCap)}</small></dd></div>
      <div class="tile"><dt>Care sites reviewed</dt><dd><span class="big">${plainPct(r.fractionReviewed, 2)}</span><small>${count(r.uniqueSites)} of ${count(r.totalSites)} sites; every other site is unmeasured and its outcome unknown</small></dd></div>
      <div class="tile wide"><dt>Next recorded review</dt>${next}</div>
    </dl>
    <p class="footnote">Counts are per paid record as observed by the review; they are not truth-confirmed DOI and say nothing about defects at unmeasured sites. A failed or missing review is unknown, never a good die.${L.invalidAt != null ? ' The cut cannot extend past the first malformed row.' : ''}</p>`;
  }

  // ---------- chart ----------
  function niceStep(max, target) {
    const raw = max / target, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
  }
  function renderChart() {
    const L = state.ledger, r = state.result, rows = L.rows.slice(0, L.validCount), n = r.paidReviews;
    const W = 760, H = 330, ml = 58, mr = 18, mt = 22, mb = 44, pw = W - ml - mr, ph = H - mt - mb;
    const xmax = Math.max(1, rows.length), ymax = L.budget;
    const x = k => ml + k / xmax * pw, y = v => mt + ph - v / ymax * ph;
    const path = (from, to) => { let d = `M${x(from).toFixed(1)},${y(from ? rows[from - 1].cumulative_spend : 0).toFixed(1)}`; for (let k = from + 1; k <= to; k++) d += `H${x(k).toFixed(1)}V${y(rows[k - 1].cumulative_spend).toFixed(1)}`; return d; };
    const ys = niceStep(ymax, 6), xs = Math.max(1, niceStep(xmax, 8));
    let grid = '';
    for (let v = 0; v <= ymax + 1e-9; v += ys) grid += `<line class="grid" x1="${ml}" x2="${ml + pw}" y1="${y(v)}" y2="${y(v)}"/><text x="${ml - 8}" y="${y(v) + 4}" text-anchor="end">${fmt(v, 0)}</text>`;
    for (let k = 0; k <= xmax; k += xs) grid += `<text x="${x(k)}" y="${mt + ph + 18}" text-anchor="middle">${k}</text>`;
    const dots = rows.map((row, i) => {
      const k = i + 1, kept = k <= n, cx = x(k).toFixed(1), cy = y(row.cumulative_spend).toFixed(1);
      const label = `Step ${text(row.step ?? row.position)} · ${text(row.site_id)} · charged ${fmt(row.charged, 2)} CU · cumulative ${fmt(row.cumulative_spend, 2)} CU${kept ? ` · ${outcomeText(row)}` : ' · beyond the cap, outcome not shown'}`;
      return `<g class="pt"><title>${e(label)}</title><circle class="hit" cx="${cx}" cy="${cy}" r="9"/><circle class="mark ${kept ? e(row.outcome) : 'beyond'}" cx="${cx}" cy="${cy}" r="4.5"/></g>`;
    }).join('');
    const capY = y(state.cap), cutX = x(n);
    const capLabelY = capY < mt + 14 ? capY + 15 : capY - 6;
    $('chart').innerHTML = `<svg class="spend-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Step chart of recorded cumulative charge against paid review count. ${n} of ${rows.length} recorded reviews fit under a cap of ${fmt(state.cap, 2)} CU, charging ${fmt(r.charged, 2)} CU. The original budget is ${fmt(ymax, 0)} CU.">
      ${grid}
      <line class="axis" x1="${ml}" x2="${ml + pw}" y1="${mt + ph}" y2="${mt + ph}"/>
      <text x="${ml + pw / 2}" y="${H - 6}" text-anchor="middle">Paid reviews in recorded order</text>
      <text x="14" y="${mt + ph / 2}" text-anchor="middle" transform="rotate(-90 14 ${mt + ph / 2})">Cumulative CU charged</text>
      <text class="ink" x="${ml + pw}" y="${y(ymax) - 6}" text-anchor="end">Original budget ${fmt(ymax, 0)} CU</text>
      ${n < rows.length ? `<path class="beyond-path" d="${path(n, rows.length)}"/>` : ''}
      ${n ? `<path class="retained-path" d="${path(0, n)}"/>` : ''}
      <line class="cap-line" x1="${ml}" x2="${ml + pw}" y1="${capY}" y2="${capY}"/>
      <text class="cap-text" x="${ml + 6}" y="${capLabelY}">Cap ${fmt(state.cap, 2)} CU</text>
      <line class="cut-line" x1="${cutX}" x2="${cutX}" y1="${mt}" y2="${mt + ph}"/>
      <text class="ink" x="${Math.min(cutX + 6, ml + pw - 4)}" y="${mt + ph - 8}" text-anchor="${cutX + 120 > ml + pw ? 'end' : 'start'}">${n} retained</text>
      ${dots}
    </svg>`;
  }

  // ---------- tables ----------
  function renderTables() {
    const r = state.result;
    $('retainedTable').innerHTML = r.retained.length ? `<table><thead><tr><th scope="col">Step</th><th scope="col">Site</th><th scope="col">Wafer</th><th scope="col" class="n">Charged</th><th scope="col" class="n">Cumulative</th><th scope="col">Observation</th></tr></thead><tbody>${
      r.retained.map(row => `<tr><td>${e(row.step ?? row.position)}</td><td class="mono">${e(row.site_id)}</td><td>${e(waferName(row.wafer))}</td><td class="n">${fmt(row.charged, 2)}</td><td class="n">${fmt(row.cumulative_spend, 2)}</td><td>${badge(row)}</td></tr>`).join('')
    }</tbody></table>` : `<div class="empty"><strong>No recorded review fits under this cap</strong>${r.next && finite(r.next.cumulative_spend) ? `The first recorded review charged ${cu(r.next.charged)}.` : 'Nothing was charged.'}</div>`;
    $('beyondBox').hidden = !r.beyond.length;
    $('beyondSummary').textContent = `Recorded beyond the cap (${count(r.beyond.length)})`;
    $('beyondTable').innerHTML = r.beyond.length ? `<table><thead><tr><th scope="col">Step</th><th scope="col">Site</th><th scope="col">Wafer</th><th scope="col" class="n">Charged</th><th scope="col" class="n">Recorded threshold</th></tr></thead><tbody>${
      r.beyond.map(row => `<tr><td>${e(row.step ?? row.position)}</td><td class="mono">${e(row.site_id)}</td><td>${e(waferName(row.wafer))}</td><td class="n">${row.valid ? fmt(row.charged, 2) : 'unavailable'}</td><td class="n">${row.valid ? fmt(row.cumulative_spend, 2) : 'unavailable'}</td></tr>`).join('')
    }</tbody></table>` : '';
  }

  // ---------- original primary outcome ----------
  function renderPrimary() {
    const p = obj(state.data.primary), L = state.ledger;
    if (!p) { $('verdict').innerHTML = ''; $('primary').innerHTML = `<div class="empty"><strong>No primary result exported</strong>the export has no primary field · file <code>${DATA_FILE}</code></div>`; return; }
    const met = p.success === true, missed = p.success === false;
    $('verdict').innerHTML = `<span class="verdict ${met ? 'met' : missed ? 'missed' : 'none'}">${met ? 'Preregistered target met' : missed ? 'Preregistered target missed' : 'No exported verdict'}</span>`;
    const ci = arr(p.ci95);
    const same = L.variant != null && p.candidate != null && String(p.candidate) === L.variant;
    $('primary').innerHTML = `<p class="statement">At ${cu(p.budget, 0)} per lot, <code>${e(p.candidate)}</code> confirmed <span class="num">${fmt(p.mean_candidate)}</span> DOI per lot and <code>${e(p.comparator)}</code> confirmed <span class="num">${fmt(p.mean_comparator)}</span>, paired over ${count(p.pairs)} lots (${e(p.mode)}).</p>
      <dl class="figures"><div><dt>Paired difference</dt><dd>${signed(p.mean_difference)}</dd></div><div><dt>95% CI</dt><dd>${finite(ci[0]) && finite(ci[1]) ? `${signed(ci[0])} to ${signed(ci[1])}` : '—'}</dd></div><div><dt>Relative gain</dt><dd>${pct(p.relative_gain)} <small>target ${plainPct(p.target)}</small></dd></div></dl>
      <p class="footnote">Valid only at the exported ${cu(p.budget, 0)} setting; not rescaled, interpolated or recomputed for the cap above. The verdict is the exported <code>success</code> flag. ${same ? `The capped sequence above is one recorded lot of the same candidate policy, not this 100-lot mean.` : `The capped sequence above is policy <code>${e(L.variant)}</code> on one lot and is not this comparison.`}</p>`;
  }

  function renderFoot() {
    const s = obj(state.data.study) || {};
    const sha = typeof s.freeze_sha256 === 'string' ? s.freeze_sha256.slice(0, 12) + '…' : '—';
    $('foot').innerHTML = `Post-hackathon recorded spend explainer · created 5 October 2026 · branch <code>post-hackathon/budget-20261005</code> · separate from the submitted pages, which are unchanged.<br>Evidence: ${e(s.name)} export generated ${e(s.generated_at)} · freeze <span class="mono" title="${e(s.freeze_sha256)}">${e(sha)}</span> · source commit <span class="mono">${e(typeof s.source_commit === 'string' ? s.source_commit.slice(0, 10) : null)}</span>.`;
  }

  // ---------- events ----------
  $('capRange').addEventListener('input', ev => setCap(Number(ev.target.value), 'range'));
  $('capNumber').addEventListener('input', ev => {
    const raw = ev.target.value.trim();
    setCap(raw === '' ? NaN : Number(raw), 'number');
  });
  $('snapPrev').addEventListener('click', () => { const t = thresholds().filter(x => x < state.cap - 1e-9); if (t.length) setCap(t[t.length - 1]); });
  $('snapNext').addEventListener('click', () => { const t = thresholds().find(x => x > state.cap + 1e-9); if (t != null) setCap(t); });
  $('snapFull').addEventListener('click', () => setCap(state.ledger.budget));
  load();
})();

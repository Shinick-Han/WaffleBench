(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const DATA_URL = './data/inspection-images-v2.json';
  const DATA_FILE = 'web/data/inspection-images-v2.json';
  const ROLE_ORDER = ['baseline', 'primary', 'secondary_descriptive'];
  const ROLE = {baseline:'Baseline', primary:'Primary · preregistered', secondary_descriptive:'Secondary · descriptive'};
  // Labels only; pass/fail always comes from evaluation.primary_endpoint.decision.
  const CONDITIONS = [
    ['recall_gain_at_least_10pp', 'Defect-recall gain point estimate ≥ +10 pp'],
    ['bootstrap_ci_lower_above_zero', 'Lower bound of recall-gain 95% CI > 0'],
    ['primary_far_at_most_10pct', 'Primary-method normal false-alarm rate ≤ 10%'],
  ];
  const KIND = {fn:'Missed defect · FN', fp:'False alarm · FP'};
  // Presentation-only English for stored Korean free text; the stored JSON and its download stay byte-identical.
  const STORED_TEXT_EN = {
    'PCB2 결과를 보고 선택한 복합 설계를 PCB3에서 고정한 별도 검증. PCB3 테스트로 설계·임계값을 고르거나 다시 튜닝하지 않았다.':
      'A separate validation on PCB3 of the composite design, which was chosen after seeing the PCB2 results and then frozen. The PCB3 test was not used to choose or re-tune the design or thresholds.',
  };
  const storedText = v => typeof v === 'string' && Object.hasOwn(STORED_TEXT_EN, v) ? STORED_TEXT_EN[v] : v;
  const state = {data:null, bytes:null, sha:null, methods:[], method:null, highlight:null,
    errKind:'all', errType:'all', errSort:'near'};

  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const arr = v => Array.isArray(v) ? v : [];
  const text = v => v == null || v === '' ? '—' : String(v);
  const e = v => text(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (n, d=2) => finite(n) ? n.toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
  const signed = (n, d=2) => finite(n) ? (n > 0 ? '+' : n < 0 ? '−' : '') + fmt(Math.abs(n), d) : '—';
  const count = n => finite(n) ? n.toLocaleString('en-US') : '—';
  // Rates are counts over 100 images per class: whole percentage points only.
  const pp = n => finite(n) ? signed(Math.round(n * 100), 0) : '—';
  const rate = n => finite(n) && n >= 0 && n <= 1 ? `${Math.round(n * 100)}%` : '—';
  const ofN = (k, n) => finite(k) && finite(n) && n > 0 ? `${count(k)} / ${count(n)}` : '—';
  const ci = c => Array.isArray(c) && c.length === 2 && finite(c[0]) && finite(c[1]) ? `${pp(c[0])} to ${pp(c[1])} pp` : '—';
  const shortId = p => typeof p === 'string' ? p.split('/').slice(-2).join('/') : '—';
  const safeUrl = u => typeof u === 'string' && /^https:\/\/[^\s"'<>]+$/.test(u) ? u : null;
  const empty = (title, detail='') => `<div class="empty"><strong>${e(title)}</strong>${detail ? e(detail) + ' · ' : ''}This entry in <code>${DATA_FILE}</code> is empty or not verified.</div>`;
  const ICON = {
    met:'<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M2.5 7.4 5.6 10.4 11.5 3.8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    missed:'<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3.5 3.5l7 7M10.5 3.5l-7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    none:'<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3 7h8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  };
  const tri = v => v === true ? 'met' : v === false ? 'missed' : 'none';

  // ---------- loading ----------
  function showState(html) { const box = $('loadState'); box.hidden = false; box.innerHTML = html; }
  function retryButton() { const b = document.createElement('button'); b.type = 'button'; b.className = 'control'; b.textContent = 'Retry'; b.addEventListener('click', load); $('loadState').append(b); }
  async function load() {
    $('app').hidden = true; $('download').disabled = true;
    showState('<div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div><p>Loading stored PCB photo inspection evidence…</p>');
    let response;
    try { response = await fetch(DATA_URL, {cache:'no-store'}); }
    catch (error) {
      console.error('Inspection images fetch:', error);
      showState(`<h2>Cannot access the evidence file</h2><p>Could not read <code>${DATA_FILE}</code> because of a network error or a local file-access restriction. Serve <code>web/</code> with a static server and try again.</p>`);
      return retryButton();
    }
    if (response.status === 404) {
      showState(`<h2>No verified result yet</h2><p>PCB photo inspection evidence has not been exported yet. This page fills in once the coordinator exports the audited result to <code>${DATA_FILE}</code>. No example values are shown.</p>`);
      return retryButton();
    }
    try {
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      const data = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes));
      if (!obj(data)) throw Error('Top-level value is not an object');
      if (data.schema_version !== 1) throw Error(`Unsupported schema_version ${data.schema_version}`);
      if (data.kind !== 'inspection_images_v2_evidence' || data.scope !== 'real_pcb_photography_not_wafer_sem') throw Error('PCB photo evidence kind or scope does not match');
      state.data = data; state.bytes = bytes; state.sha = null;
      prepare(data);
      $('loadState').hidden = true; $('app').hidden = false; $('download').disabled = false;
      $('download').textContent = `Download raw JSON · ${count(bytes.byteLength)} B`;
      renderAll();
      digest(bytes);
    } catch (error) {
      console.error('Inspection images parse:', error);
      showState(`<h2>Cannot parse the evidence file</h2><p>${e(error.message)}. Check the export format of <code>${DATA_FILE}</code> and try again.</p>`);
      retryButton();
    }
  }
  async function digest(bytes) {
    try {
      if (!globalThis.crypto?.subtle) throw Error('unavailable');
      const hash = await crypto.subtle.digest('SHA-256', bytes);
      state.sha = [...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, '0')).join('');
    } catch { state.sha = false; }
    const out = $('fileSha');
    if (out) out.textContent = state.sha || 'Cannot be computed in this browser (secure context required)';
  }

  function prepare(data) {
    const methods = obj(data.evaluation?.methods) || {};
    state.methods = Object.entries(methods).filter(([, m]) => obj(m)).map(([id, m]) => ({id, ...m}))
      .sort((a, b) => rank(a.role) - rank(b.role));
    const primary = state.methods.find(m => m.role === 'primary');
    state.method = (primary || state.methods[0])?.id ?? null;
  }
  const rank = r => { const i = ROLE_ORDER.indexOf(r); return i < 0 ? ROLE_ORDER.length : i; };
  const byRole = r => state.methods.find(m => m.role === r) || null;
  const ft = m => obj(m?.frozen_threshold) || {};
  const defects = m => { const t = ft(m); return finite(t.true_positive) && finite(t.false_negative) ? t.true_positive + t.false_negative : null; };
  const normals = m => { const t = ft(m); return finite(t.false_positive) && finite(t.true_negative) ? t.false_positive + t.true_negative : null; };

  function renderAll() {
    renderMeta(); renderPrimary(); renderCompare(); renderSwitch(); renderScores();
    renderErrors(); renderPredecessor(); renderReplication(); renderProvenance(); renderSources(); renderLimitations();
  }

  // ---------- header meta ----------
  function renderMeta() {
    const ev = obj(state.data.evaluation) || {}, c = obj(ev.counts) || {};
    const when = ev.evaluated_at && !Number.isNaN(Date.parse(ev.evaluated_at)) ? new Date(ev.evaluated_at).toLocaleString('en-US') : null;
    const items = [
      ['Study', ev.study], ['Scope', state.data.scope === 'real_pcb_photography_not_wafer_sem' ? 'Real PCB photos · not wafer SEM' : state.data.scope],
      ['Official test', finite(c.test_images) ? `${count(c.test_images)} images (${count(c.normal)} normal · ${count(c.anomaly)} defective)` : null],
      ['Data', ev.data_mode === 'real' ? 'Real-photo evaluation' : ev.data_mode], ['Evaluated', when],
      ['Audit', state.data.audit?.status === 'pass' ? 'Passed' : state.data.audit?.status],
    ];
    $('studyMeta').innerHTML = items.map(([k, v]) => `<div><dt>${e(k)}</dt><dd>${e(v)}</dd></div>`).join('');
  }

  // ---------- primary ----------
  function renderPrimary() {
    const pe = obj(state.data.evaluation?.primary_endpoint);
    const dec = obj(pe?.decision), def = obj(pe?.definition);
    const P = byRole('primary'), B = byRole('baseline');
    const v = $('primaryVerdict');
    const s = dec?.success;
    v.className = `verdict ${tri(s)}`;
    v.innerHTML = `${ICON[tri(s)]}${s === true ? 'All three preregistered criteria met' : s === false ? 'Preregistered criteria not met' : 'No recorded verdict'}`;
    if (!pe || !P || !B) { $('primaryBody').innerHTML = empty('No primary result', 'primary_endpoint or the primary/baseline method is missing'); return; }
    const gain = obj(pe.primary_vs_baseline_bootstrap?.defect_recall_gain) || {};
    const far = obj(pe.primary_vs_baseline_bootstrap?.normal_far_difference) || {};
    const pt = ft(P), bt = ft(B);
    const nd = defects(P), nn = normals(P);
    const conds = CONDITIONS.map(([key, label]) => {
      const st = tri(dec?.[key]);
      return `<li class="cond ${st}">${ICON[st]}<span>${e(label)}</span><b>${st === 'met' ? 'Met' : st === 'missed' ? 'Not met' : 'Not recorded'}</b></li>`;
    }).join('');
    const reqs = arr(def?.success_requires_all).filter(x => typeof x === 'string');
    const bs = obj(def?.bootstrap) || {};
    $('primaryBody').innerHTML = `
      <div class="primary-grid">
        <div>
          <p class="statement">Of <span class="num">${count(nd)}</span> defective photos, the primary method caught <span class="num hit">${count(pt.true_positive)}</span> and the baseline caught <span class="num">${count(bt.true_positive)}</span>.</p>
          <p class="statement second">Of <span class="num">${count(nn)}</span> normal photos, wrongly rejected ones fell from <span class="num">${count(bt.false_positive)}</span> to <span class="num hit">${count(pt.false_positive)}</span>.</p>
          <dl class="figures">
            <div><dt>Defect recall</dt><dd>${rate(bt.defect_recall)} → ${rate(pt.defect_recall)}</dd></div>
            <div><dt>Recall gain · primary metric</dt><dd>${pp(gain.estimate)} pp <small>95% CI ${ci(gain.ci95)}</small></dd></div>
            <div><dt>Normal false-alarm rate</dt><dd>${rate(bt.normal_false_alarm_rate)} → ${rate(pt.normal_false_alarm_rate)}</dd></div>
            <div><dt>False-alarm rate difference · secondary</dt><dd>${pp(far.estimate)} pp <small>95% CI ${ci(far.ci95)}</small></dd></div>
          </dl>
        </div>
        <figure class="ci-figure">${ciChart(gain)}<figcaption>Point estimate and 95% percentile interval of the defect-recall gain (primary − baseline) · ${count(gain.resamples ?? bs.resamples)} resamples · dashed line is the preregistered +10 pp criterion</figcaption></figure>
      </div>
      <div class="conditions">
        <h3>Preregistered success criteria <span class="tag">Exported verdict as recorded</span></h3>
        <ul class="cond-list">${conds}</ul>
        ${reqs.length ? `<p class="footnote">Registered text: ${reqs.map(r => `<span class="quote">${e(r)}</span>`).join(' · ')}</p>` : ''}
      </div>
      <p class="caveat"><strong>Not a resolution-only effect.</strong> Relative to the baseline, the primary method changed input resolution, patch grid, coreset size and score aggregation all at once (see each configuration in the comparison table below). The difference is a composite effect of the whole pipeline and cannot be attributed to any single factor.${def?.attribution ? ` <span class="quote">${e(def.attribution)}</span>` : ''}</p>`;
  }

  function ciChart(g) {
    const c = Array.isArray(g.ci95) ? g.ci95 : [];
    if (!finite(g.estimate) || !finite(c[0]) || !finite(c[1])) return `<div class="empty"><strong>No confidence interval recorded</strong>defect_recall_gain.ci95 is empty.</div>`;
    const lo = Math.min(-0.05, c[0] - 0.05), hi = Math.max(0.35, c[1] + 0.05);
    const W = 560, L = 18, R = 18, x = v => L + (v - lo) / (hi - lo) * (W - L - R);
    const ticks = []; for (let t = Math.ceil(lo * 20) / 20; t <= hi + 1e-9; t += 0.05) ticks.push(Math.round(t * 100) / 100);
    const axis = ticks.map(t => `<line x1="${x(t)}" x2="${x(t)}" y1="34" y2="96" class="grid"/><text x="${x(t)}" y="116" text-anchor="middle">${pp(t)}</text>`).join('');
    return `<svg viewBox="0 0 ${W} 124" role="img" aria-label="Recall gain ${pp(g.estimate)} pp, 95% CI ${ci(c)}, criterion +10 pp, zero reference line">
      ${axis}
      <line x1="${x(0)}" x2="${x(0)}" y1="22" y2="100" class="zero"/><text x="${x(0)}" y="16" text-anchor="middle">0</text>
      <line x1="${x(0.10)}" x2="${x(0.10)}" y1="22" y2="100" class="bar10"/><text x="${x(0.10)}" y="16" text-anchor="middle">Criterion +10</text>
      <line x1="${x(c[0])}" x2="${x(c[1])}" y1="65" y2="65" class="ci"/>
      <line x1="${x(c[0])}" x2="${x(c[0])}" y1="55" y2="75" class="ci"/><line x1="${x(c[1])}" x2="${x(c[1])}" y1="55" y2="75" class="ci"/>
      <circle cx="${x(g.estimate)}" cy="65" r="7" class="est"/>
      <text x="${x(g.estimate)}" y="46" text-anchor="middle" class="ink">${pp(g.estimate)} pp</text>
      <text x="${x(c[0])}" y="92" text-anchor="middle">${pp(c[0])}</text><text x="${x(c[1])}" y="92" text-anchor="middle">${pp(c[1])}</text>
    </svg>`;
  }

  // ---------- comparison table ----------
  function renderCompare() {
    const ms = state.methods;
    if (!ms.length) { $('compareBody').innerHTML = empty('No method records', 'evaluation.methods is missing'); return; }
    const col = (m, html) => `<td class="${m.role === 'primary' ? 'is-primary ' : ''}num">${html}</td>`;
    const row = (label, f, cls='') => `<tr class="${cls}"><th scope="row">${label}</th>${ms.map(m => col(m, f(m))).join('')}</tr>`;
    const sec = obj(state.data.evaluation?.secondary_descriptive?.highres448max_vs_baseline224_bootstrap);
    $('compareBody').innerHTML = `
      <div class="table-scroll" role="region" tabindex="0" aria-label="Three-method comparison table · scrolls horizontally">
        <table class="compare-table">
          <caption>Each method uses its own threshold set on normal training photos (95th percentile of normal calibration scores). Score &gt; threshold means defective.</caption>
          <thead><tr><th scope="col">Item</th>${ms.map(m => `<th scope="col" class="num${m.role === 'primary' ? ' is-primary' : ''}"><span class="mname">${e(m.id)}</span><span class="role ${e(m.role)}">${e(ROLE[m.role] || m.role)}</span></th>`).join('')}</tr></thead>
          <tbody>
            ${row('Configuration', m => `<span class="cfg">${e(m.label)}</span>`, 'cfg-row')}
            ${row('Defect recall <small>primary metric</small>', m => `<b>${ofN(ft(m).true_positive, defects(m))}</b> <small>${rate(ft(m).defect_recall)}</small>`, 'key')}
            ${row('Defects caught · TP', m => count(ft(m).true_positive))}
            ${row('Defects missed · FN', m => count(ft(m).false_negative))}
            ${row('False alarms · FP', m => count(ft(m).false_positive))}
            ${row('Normals passed · TN', m => count(ft(m).true_negative))}
            ${row('Normal false-alarm rate', m => `<b>${ofN(ft(m).false_positive, normals(m))}</b> <small>${rate(ft(m).normal_false_alarm_rate)}</small>`, 'key')}
            ${row('Fixed threshold <small>method-specific units</small>', m => fmt(ft(m).threshold, 2))}
            ${row('Image AUROC <small>ranking metric · secondary</small>', m => fmt(m.image_auroc_secondary, 3), 'rank-row')}
            ${row('Average precision AP <small>ranking metric · secondary</small>', m => fmt(m.image_average_precision_secondary, 3), 'rank-row')}
          </tbody>
        </table>
      </div>
      <p class="footnote">AUROC and AP evaluate only the ordering of scores; they do not say how many images were judged correctly at the fixed threshold. Accuracy claims rest only on the decision counts above.${sec ? ` The recall difference between 448max (secondary, descriptive) and the baseline is recorded as ${pp(sec.defect_recall_gain?.estimate)} pp (95% CI ${ci(sec.defect_recall_gain?.ci95)}) but is not used for the success verdict.` : ''}</p>`;
  }

  // ---------- score strip ----------
  function renderSwitch() {
    const box = $('methodSwitch');
    box.querySelectorAll('label').forEach(n => n.remove());
    for (const m of state.methods) {
      const l = document.createElement('label');
      l.className = 'seg';
      const i = document.createElement('input');
      i.type = 'radio'; i.name = 'scoreMethod'; i.value = m.id; i.checked = m.id === state.method;
      i.addEventListener('change', () => { state.method = m.id; renderScores(); });
      const s = document.createElement('span');
      s.textContent = `${m.id} · ${ROLE[m.role] || m.role || 'no role'}`;
      l.append(i, s); box.append(l);
    }
    box.hidden = !state.methods.length;
  }

  function scoreRows(id) {
    const m = state.methods.find(x => x.id === id);
    const thr = ft(m).threshold;
    const rows = [], skipped = {n:0};
    for (const r of arr(state.data.evaluation?.images)) {
      const s = obj(r)?.[`${id}_score`];
      if (!finite(s) || !finite(thr) || (r.label !== 0 && r.label !== 1)) { skipped.n++; continue; }
      const margin = s - thr, flagged = s > thr;
      rows.push({image:r.image, label:r.label, score:s, margin, flagged,
        cls: r.label === 1 ? (flagged ? 'TP' : 'FN') : (flagged ? 'FP' : 'TN')});
    }
    return {m, thr, rows, skipped:skipped.n};
  }

  function renderScores() {
    const id = state.method;
    if (!id) { $('scoresBody').innerHTML = empty('No score records', 'evaluation.methods is missing'); return; }
    const {m, thr, rows, skipped} = scoreRows(id);
    if (!rows.length) { $('scoresBody').innerHTML = empty('No per-image scores for this method', `evaluation.images[].${id}_score is missing`); return; }
    const tally = {TP:0, FN:0, FP:0, TN:0}; rows.forEach(r => tally[r.cls]++);
    const t = ft(m);
    const agrees = tally.TP === t.true_positive && tally.FN === t.false_negative && tally.FP === t.false_positive && tally.TN === t.true_negative;
    const sorted = [...rows].sort((a, b) => b.margin - a.margin);
    $('scoresBody').innerHTML = `
      <figure class="strip-figure">
        <div class="strip" id="strip"></div>
        <figcaption>
          <span class="key"><i class="dot defect" aria-hidden="true"></i>Defective photo (filled)</span>
          <span class="key"><i class="dot normal" aria-hidden="true"></i>Normal photo (outlined)</span>
          <span class="key"><i class="dot err" aria-hidden="true"></i>Wrong decision (dark outline)</span>
          <span class="tally">Decisions recounted from this chart · defective ${count(tally.TP)} caught / ${count(tally.FN)} missed · normal ${count(tally.FP)} false alarms / ${count(tally.TN)} passed
          ${agrees ? '<b class="ok">Matches exported counts</b>' : '<b class="bad">Differs from exported counts — check the raw file</b>'}${skipped ? ` · ${count(skipped)} rows without scores excluded` : ''}</span>
        </figcaption>
      </figure>
      <p class="footnote">The horizontal axis is the <code>${e(id)}</code> score minus that method's fixed threshold ${fmt(thr, 2)} (method-specific units). Points right of 0 are judged defective.</p>
      <details class="fallback"><summary>View as score table · ${count(rows.length)} rows</summary>
        <div class="table-scroll record-scroll" role="region" tabindex="0" aria-label="${e(id)} per-image score table · scrollable">
          <table class="score-table"><caption>Sorted by margin over threshold, largest first · ${e(id)}</caption>
            <thead><tr><th scope="col">Image</th><th scope="col">Truth</th><th scope="col" class="num">Score</th><th scope="col" class="num">Margin vs threshold</th><th scope="col">Decision</th></tr></thead>
            <tbody>${sorted.map(r => `<tr class="${r.cls === 'FN' || r.cls === 'FP' ? 'is-error' : ''}"><th scope="row" class="img">${e(shortId(r.image))}</th><td>${r.label === 1 ? 'Defective' : 'Normal'}</td><td class="num">${fmt(r.score, 2)}</td><td class="num">${signed(r.margin, 2)}</td><td>${verdictText(r.cls)}</td></tr>`).join('')}</tbody>
          </table>
        </div>
      </details>`;
    drawStrip(id, rows);
  }
  const verdictText = c => ({TP:'Defective · caught TP', FN:'Defective · missed FN', FP:'Normal · false alarm FP', TN:'Normal · passed TN'})[c];

  function drawStrip(id, rows) {
    const host = $('strip');
    if (!host) return;
    const W = Math.max(300, Math.round(host.clientWidth || 900));
    const small = W < 560, r = small ? 3.4 : 4.6, gap = 0.6;
    const L = small ? 56 : 70, R = 12, top = 26, laneH = small ? 104 : 128, H = top + laneH * 2 + 34;
    let lo = Math.min(0, ...rows.map(d => d.margin)), hi = Math.max(0, ...rows.map(d => d.margin));
    const pad = (hi - lo) * 0.04 || 1; lo -= pad; hi += pad;
    const x = v => L + (v - lo) / (hi - lo) * (W - L - R);
    const lanes = [{label:'Defect', key:1, cy: top + laneH / 2}, {label:'Normal', key:0, cy: top + laneH * 1.5}];
    const marks = [];
    for (const lane of lanes) {
      const pts = rows.filter(d => d.label === lane.key).sort((a, b) => a.margin - b.margin);
      const placed = [], half = laneH / 2 - r - 2;
      for (const d of pts) {
        const px = x(d.margin); let dy = 0;
        for (let k = 0; k < 200; k++) {
          dy = (k % 2 ? 1 : -1) * Math.ceil(k / 2) * (r * 0.9);
          if (Math.abs(dy) > half) { dy = Math.max(-half, Math.min(half, dy)); break; }
          if (!placed.some(p => (p.x - px) ** 2 + (p.y - dy) ** 2 < (2 * r + gap) ** 2)) break;
        }
        placed.push({x:px, y:dy});
        marks.push({d, cx:px, cy:lane.cy + dy});
      }
    }
    const step = niceStep((hi - lo) / (small ? 4 : 8));
    const ticks = []; for (let t = Math.ceil(lo / step) * step; t <= hi; t += step) ticks.push(t);
    const err = c => c === 'FN' || c === 'FP';
    const hl = marks.find(k => k.d.image === state.highlight);
    const svg = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${e(id)} score distribution: ${rows.filter(d => d.label === 1).length} defective, ${rows.filter(d => d.label === 0).length} normal images. Exact values are in the score table below.">
      <rect x="${x(0)}" y="${top - 4}" width="${W - R - x(0)}" height="${laneH * 2 + 8}" class="flag-zone"/>
      <text x="${x(0) + 6}" y="${top - 10}" class="zone-label">Flagged defective →</text>
      <text x="${x(0) - 6}" y="${top - 10}" text-anchor="end" class="zone-label">← Passed as normal</text>
      ${ticks.map(t => `<line x1="${x(t)}" x2="${x(t)}" y1="${top}" y2="${top + laneH * 2}" class="grid"/><text x="${x(t)}" y="${H - 12}" text-anchor="middle">${signed(t, step < 1 ? 1 : 0)}</text>`).join('')}
      <line x1="${L}" x2="${W - R}" y1="${top + laneH}" y2="${top + laneH}" class="lane-rule"/>
      ${lanes.map(l => `<text x="${L - 10}" y="${l.cy + 4}" text-anchor="end" class="ink">${l.label}</text>`).join('')}
      <line x1="${x(0)}" x2="${x(0)}" y1="${top - 4}" y2="${top + laneH * 2 + 4}" class="thr"/>
      ${marks.map(k => `<circle cx="${k.cx.toFixed(1)}" cy="${k.cy.toFixed(1)}" r="${r}" class="pt ${k.d.label === 1 ? 'defect' : 'normal'}${err(k.d.cls) ? ' err' : ''}"><title>${e(shortId(k.d.image))} · ${verdictText(k.d.cls)} · margin vs threshold ${signed(k.d.margin, 2)}</title></circle>`).join('')}
      ${hl ? `<circle cx="${hl.cx.toFixed(1)}" cy="${hl.cy.toFixed(1)}" r="${r + 5}" class="hl"/><text x="${Math.min(W - R - 4, Math.max(L + 4, hl.cx))}" y="${hl.d.label === 1 ? top + 12 : top + laneH * 2 - 6}" text-anchor="${hl.cx > W * 0.7 ? 'end' : 'start'}" class="ink">${e(shortId(hl.d.image))}</text>` : ''}
    </svg>`;
    host.innerHTML = svg;
  }
  function niceStep(raw) {
    const p = 10 ** Math.floor(Math.log10(raw || 1)), f = raw / p;
    return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
  }

  // ---------- error review ----------
  function errorRows() {
    const pe = obj(state.data.posthoc_errors) || {};
    const fn = arr(pe.primary448_missed_defects).filter(obj).map(r => ({...r, kind:'fn'}));
    const fp = arr(pe.primary448_false_alarms).filter(obj).map(r => ({...r, kind:'fp'}));
    return {pe, all:[...fn, ...fp], fn:fn.length, fp:fp.length};
  }
  function renderErrors() {
    const {pe, all, fn, fp} = errorRows();
    if (!obj(state.data.posthoc_errors)) { $('errorsBody').innerHTML = empty('No post-hoc error review', 'posthoc_errors is missing'); return; }
    const types = [...new Set(all.map(r => r.defect_type).filter(t => typeof t === 'string'))].sort();
    const P = byRole('primary');
    const mismatch = P && (fn !== ft(P).false_negative || fp !== ft(P).false_positive);
    const byType = Object.entries(obj(pe.anomaly_by_defect_type) || {}).filter(([, v]) => obj(v));
    const flips = obj(pe.paired_flips_vs_baseline224);
    $('errorsBody').innerHTML = `
      <p class="err-lead">Missed defects <b class="num">${count(fn)}</b>, false alarms <b class="num">${count(fp)}</b>${mismatch ? ' <b class="bad">· row count differs from the decision counts</b>' : ''}</p>
      <div class="table-tools">
        <label class="field">Kind<select id="errKind"><option value="all">All</option><option value="fn">Missed defect · FN</option><option value="fp">False alarm · FP</option></select></label>
        <label class="field">Defect annotation<select id="errType"><option value="all">All annotations</option>${types.map(t => `<option value="${e(t)}">${e(t)}</option>`).join('')}</select></label>
        <label class="field">Sort<select id="errSort"><option value="near">Closest to threshold</option><option value="far">Farthest from threshold</option><option value="id">Image ID</option></select></label>
        <span id="errCount" class="count-line" role="status" aria-live="polite"></span>
      </div>
      <div class="table-scroll record-scroll" role="region" tabindex="0" aria-label="Primary-method error photos table · scrollable">
        <table class="err-table"><caption>Margin = primary-method score − primary-method threshold. Defects with a negative margin were missed; normals with a positive margin are false alarms.</caption>
          <thead><tr><th scope="col">Image</th><th scope="col">Kind</th><th scope="col">Defect annotation</th><th scope="col" class="num">Primary margin</th><th scope="col">Baseline decision</th><th scope="col">448max decision</th><th scope="col"><span class="sr">Show in distribution</span></th></tr></thead>
          <tbody id="errRows"></tbody>
        </table>
      </div>
      <div class="err-aside">
        <div>
          <h3>Misses by defect annotation <span class="tag">Descriptive · small sample</span></h3>
          ${byType.length ? `<div class="table-scroll" role="region" tabindex="0" aria-label="Misses by defect annotation table"><table class="type-table"><thead><tr><th scope="col">Annotation</th><th scope="col" class="num">Test images</th><th scope="col" class="num">Primary misses</th></tr></thead><tbody>${byType.map(([t, v]) => `<tr><th scope="row">${e(t)}</th><td class="num">${count(v.test)}</td><td class="num">${count(v.primary448_missed)} / ${count(v.test)}</td></tr>`).join('')}</tbody></table></div>
          <p class="footnote">For example, the missing annotation is ${typeOf(byType, 'missing')}; with so few images it is not generalized as a rate. This is a post-hoc review, not a preregistered metric.</p>` : empty('No per-annotation counts')}
        </div>
        <div>
          <h3>Photos whose decision changed vs baseline</h3>
          ${flips ? `<dl class="state-list">
            <dt>Defects caught only by primary</dt><dd>${count(flips.defect_caught_only_by_primary)}</dd>
            <dt>Defects caught only by baseline</dt><dd>${count(flips.defect_caught_only_by_baseline)}</dd>
            <dt>Normals flagged only by baseline</dt><dd>${count(flips.normal_flagged_only_by_baseline)}</dd>
            <dt>Normals flagged only by primary</dt><dd>${count(flips.normal_flagged_only_by_primary)}</dd></dl>` : empty('No paired comparison record')}
        </div>
      </div>`;
    const kind = $('errKind'), type = $('errType'), sort = $('errSort');
    kind.value = state.errKind; type.value = types.includes(state.errType) ? state.errType : 'all'; sort.value = state.errSort;
    kind.addEventListener('change', () => { state.errKind = kind.value; fillErrors(); });
    type.addEventListener('change', () => { state.errType = type.value; fillErrors(); });
    sort.addEventListener('change', () => { state.errSort = sort.value; fillErrors(); });
    fillErrors();
  }
  function typeOf(byType, t) { const v = byType.find(([k]) => k === t)?.[1]; return v ? `${count(v.primary448_missed)} missed of ${count(v.test)}` : 'not recorded'; }
  const flag = (v, kind) => v === true ? `<span class="flag yes">${kind === 'fn' ? 'Caught' : 'False alarm'}</span>` : v === false ? `<span class="flag no">${kind === 'fn' ? 'Missed' : 'Passed'}</span>` : '—';
  function fillErrors() {
    const {all} = errorRows();
    let rows = all.filter(r => (state.errKind === 'all' || r.kind === state.errKind) && (state.errType === 'all' || r.defect_type === state.errType));
    const mg = r => finite(r.primary448_margin) ? r.primary448_margin : null;
    rows.sort((a, b) => {
      if (state.errSort === 'id') return String(a.image).localeCompare(String(b.image));
      const A = mg(a), B = mg(b);
      if (A == null || B == null) return (A == null) - (B == null);
      return state.errSort === 'near' ? Math.abs(A) - Math.abs(B) : Math.abs(B) - Math.abs(A);
    });
    $('errCount').textContent = `Showing ${count(rows.length)} of ${count(all.length)}`;
    const P = byRole('primary');
    $('errRows').innerHTML = rows.length ? rows.map(r => `<tr class="${r.image === state.highlight ? 'is-current' : ''}">
      <th scope="row" class="img">${e(shortId(r.image))}</th>
      <td><span class="kind ${r.kind}"><i aria-hidden="true"></i>${KIND[r.kind]}</span></td>
      <td>${r.kind === 'fp' ? '<span class="muted">Normal</span>' : e(r.defect_type)}</td>
      <td class="num">${signed(mg(r), 2)}</td>
      <td>${flag(r.baseline224_flagged, r.kind)}</td>
      <td>${flag(r.highres448max_flagged, r.kind)}</td>
      <td>${P ? `<button type="button" class="step-link" data-image="${e(r.image)}" aria-label="Show ${e(shortId(r.image))} in score distribution">Show in distribution</button>` : ''}</td></tr>`).join('')
      : `<tr><td colspan="7"><div class="empty"><strong>No photos match these filters</strong><button type="button" class="control" id="errReset">Reset filters</button></div></td></tr>`;
    $('errReset')?.addEventListener('click', () => { state.errKind = 'all'; state.errType = 'all'; renderErrors(); $('errKind').focus(); });
  }
  function locate(image) {
    const P = byRole('primary');
    if (!P) return;
    state.highlight = image; state.method = P.id;
    document.querySelectorAll('input[name=scoreMethod]').forEach(i => { i.checked = i.value === P.id; });
    renderScores(); fillErrors();
    const fig = $('scores');
    fig.scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block:'start'});
    const heading = $('scoresTitle'); heading.tabIndex = -1; heading.focus({preventScroll:true});
    const live = $('scoreLive'); if (live) live.textContent = `Marked ${shortId(image)} in the ${P.id} score distribution.`;
  }

  // ---------- predecessor ----------
  function renderPredecessor() {
    const p = obj(state.data.predecessor);
    if (!p) { $('predBody').innerHTML = empty('No earlier PCB1 record', 'predecessor is missing'); return; }
    const ms = Object.entries(obj(p.methods) || {}).filter(([, m]) => obj(m));
    const g = obj(p.methods?.global), pa = obj(p.methods?.patch);
    const gt = ft(g), pt = ft(pa);
    $('predBody').innerHTML = `
      ${g && pa ? `<p class="statement small">On PCB1 the patch method caught fewer defects (<span class="num">${count(gt.true_positive)} → ${count(pt.true_positive)}</span>) and raised normal false alarms (<span class="num">${count(gt.false_positive)} → ${count(pt.false_positive)}</span>). The PCB2 success does not overturn this negative result.</p>` : ''}
      ${ms.length ? `<div class="table-scroll" role="region" tabindex="0" aria-label="Earlier PCB1 methods table · scrolls horizontally"><table class="pred-table">
        <caption>${e(p.study)} · ${count(p.counts?.test_images)} test images (${count(p.counts?.normal)} normal · ${count(p.counts?.anomaly)} defective) · no confidence intervals</caption>
        <thead><tr><th scope="col">Method</th><th scope="col" class="num">Defect recall</th><th scope="col" class="num">TP</th><th scope="col" class="num">FN</th><th scope="col" class="num">FP</th><th scope="col" class="num">TN</th><th scope="col" class="num">Normal false-alarm rate</th><th scope="col" class="num">AUROC <small>ranking</small></th></tr></thead>
        <tbody>${ms.map(([id, m]) => { const t = ft(m); return `<tr><th scope="row"><span class="mname">${e(id)}</span><span class="cfg">${e(m.label)}</span></th><td class="num"><b>${ofN(t.true_positive, defects(m))}</b> <small>${rate(t.defect_recall)}</small></td><td class="num">${count(t.true_positive)}</td><td class="num">${count(t.false_negative)}</td><td class="num">${count(t.false_positive)}</td><td class="num">${count(t.true_negative)}</td><td class="num"><b>${ofN(t.false_positive, normals(m))}</b> <small>${rate(t.normal_false_alarm_rate)}</small></td><td class="num">${fmt(m.image_auroc, 3)}</td></tr>`; }).join('')}</tbody>
      </table></div>` : empty('No PCB1 method records')}
      ${arr(p.limitations).length ? `<ul class="plain">${arr(p.limitations).map(l => `<li>${e(l)}</li>`).join('')}</ul>` : ''}`;
  }

  function renderReplication() {
    const r = obj(state.data.replication), ev = obj(r?.evaluation), au = obj(r?.audit);
    if (!r || r.scope !== 'separate_real_pcb3_replication' || ev?.study !== 'inspection-image-replication-v3-pcb3' || au?.status !== 'pass' || au.freeze_digest !== ev.freeze_digest) {
      $('replicationBody').innerHTML = empty('No verified PCB3 record yet', 'A separate result whose independent audit matches its freeze record is required.'); return;
    }
    const g = obj(ev.primary_endpoint?.primary_vs_baseline_bootstrap?.defect_recall_gain) || {};
    const ms = Object.entries(obj(ev.methods) || {}).filter(([,m]) => obj(m));
    const primary = ft(ev.methods?.primary448), passed = ev.primary_endpoint?.decision?.success === true;
    $('replicationBody').innerHTML = `<p class="statement small">Gain on PCB3 <span class="num">${signed(finite(g.estimate) ? g.estimate*100 : null,0)} pp</span> · 95% CI [${arr(g.ci95).map(v=>signed(finite(v)?v*100:null,0)).join(', ')}] pp. The three preregistered criteria were ${passed ? 'met' : 'not met'} · the primary method still missed ${count(primary.false_negative)} of ${count(ev.counts?.anomaly)} defects.</p>
      <p class="caveat">${e(String(storedText(r.selection_disclosure) ?? '').replace(/\.+$/, ''))}. The pass criteria are a gain of ≥10 pp, a CI lower bound >0, and false alarms ≤10%. The +11 pp gain cleared the gain criterion by a single image, and the CI lower bound was only +1 pp. This does not show that the effect size equals PCB2's.</p>
      <div class="table-scroll" role="region" tabindex="0" aria-label="Separate PCB3 validation table · scrolls horizontally"><table class="pred-table"><caption>${e(ev.study)} · ${count(ev.counts?.anomaly)} defective · ${count(ev.counts?.normal)} normal</caption><thead><tr><th scope="col">Method</th><th scope="col">Role</th><th scope="col" class="num">Defects detected</th><th scope="col" class="num">Missed FN</th><th scope="col" class="num">Normal false alarms</th><th scope="col" class="num">False-alarm rate</th></tr></thead><tbody>${ms.map(([id,m])=>{const t=ft(m);return `<tr><th scope="row">${e(id)}</th><td>${e(({baseline:'Baseline',primary:'Primary',secondary_descriptive:'Secondary · descriptive'})[m.role] ?? m.role)}</td><td class="num">${count(t.true_positive)} / ${count(ev.counts?.anomaly)} · ${rate(t.defect_recall)}</td><td class="num">${count(t.false_negative)}</td><td class="num">${count(t.false_positive)} / ${count(ev.counts?.normal)}</td><td class="num">${rate(t.normal_false_alarm_rate)}</td></tr>`}).join('')}</tbody></table></div>
      <p class="caveat">The secondary method stays descriptive; its higher 57% detection rate is not used to switch the primary method. The limits of the composite change, single seed and imaging conditions still apply. Real wafer, SEM or fab performance was not measured.</p>
      ${r.software_profile?.scope === 'posthoc_calibration_only_cpu_software_profile' && r.software_profile?.freeze_digest === ev.freeze_digest ? `<p class="caveat"><strong>Compute cost of the gain.</strong> In a CPU batch measurement on 16 fixed normal calibration photos, the 224 method took ${fmt(r.software_profile.methods?.baseline224?.warm_batch_median_ms,2)} ms and the 448 method ${fmt(r.software_profile.methods?.primary448?.warm_batch_median_ms,2)} ms, about ${fmt(r.software_profile.primary_over_baseline_warm_batch_ratio,2)}×. The feature bank grows from 1.50 to 3.00 MiB. One warmup run, then 6 measured runs on 4 CPU threads; test photos were not re-inferred. This is neither single-photo latency nor fab tool throughput.</p>` : ''}
      <dl class="hash-list"><dt>Separate freeze digest · matches audit</dt><dd><code>${e(ev.freeze_digest)}</code></dd><dt>Separate test-scores SHA-256</dt><dd><code>${e(ev.test_scores_sha256)}</code></dd></dl>`;
  }

  // ---------- provenance ----------
  function renderProvenance() {
    const ev = obj(state.data.evaluation) || {}, au = obj(state.data.audit) || {};
    const bs = obj(ev.primary_endpoint?.definition?.bootstrap) || {};
    const inputs = Object.entries(obj(au.report_input_sha256) || {});
    const freezeMatch = typeof au.freeze_digest === 'string' && au.freeze_digest === ev.freeze_digest;
    const scoresMatch = typeof ev.test_scores_sha256 === 'string' && au.report_input_sha256?.['test-scores.json'] === ev.test_scores_sha256;
    const seeds = arr(bs.seed_stream).map(text).join(', ');
    $('provBody').innerHTML = `
      <div class="prov-grid">
        <div>
          <h3>Freeze and audit</h3>
          <dl class="hash-list">
            <dt>Audit status</dt><dd>${au.status === 'pass' ? '<b class="ok">Passed</b>' : e(au.status)}</dd>
            <dt>freeze digest</dt><dd><code>${e(ev.freeze_digest)}</code>${freezeMatch ? '<span class="ok"> · matches audit record</span>' : au.freeze_digest ? '<span class="bad"> · differs from audit record</span>' : ''}</dd>
            <dt>test-scores SHA-256</dt><dd><code>${e(ev.test_scores_sha256)}</code>${scoresMatch ? '<span class="ok"> · matches audit input</span>' : ''}</dd>
            ${inputs.map(([k, v]) => `<dt>Audit input · ${e(k)}</dt><dd><code>${e(v)}</code></dd>`).join('')}
          </dl>
        </div>
        <div>
          <h3>Bootstrap and downloaded file</h3>
          <dl class="hash-list">
            <dt>Resamples</dt><dd>${count(bs.resamples)} · ${e(bs.interval)}</dd>
            <dt>Resampling unit</dt><dd>${e(bs.unit)}</dd>
            <dt>seed stream</dt><dd><code>${e(seeds || null)}</code></dd>
            <dt>File</dt><dd><code>${DATA_FILE}</code></dd>
            <dt>Exact size</dt><dd>${count(state.bytes?.byteLength)} bytes</dd>
            <dt>File SHA-256 <small>computed in browser</small></dt><dd><code id="fileSha">${state.sha ? e(state.sha) : state.sha === false ? 'Cannot be computed in this browser (secure context required)' : 'Computing…'}</code></dd>
          </dl>
        </div>
      </div>
      <p class="caveat"><strong>What the confidence intervals do not cover.</strong> Intervals come from resampling test images. The primary trial's interval above covers one PCB2 category and one seed. The separate PCB3 validation is a single result on another category; neither interval measures variation across categories, cameras, lighting or production lines.</p>`;
  }

  // ---------- sources ----------
  function renderSources() {
    const a = obj(state.data.attribution);
    if (!a) { $('sourcesBody').innerHTML = empty('No source record', 'attribution is missing'); return; }
    const link = (u, label) => { const s = safeUrl(u); return s ? `<a href="${e(s)}" rel="noopener noreferrer" target="_blank">${e(label)}<span class="sr"> (opens in new window)</span></a>` : e(label); };
    $('sourcesBody').innerHTML = `<ul class="refs">
      <li><span class="ref-k">Data</span><span>${link(a.url, a.dataset)} · license <b>${e(a.license)}</b>. Photos are not included or redistributed on this page; only image IDs are shown.</span></li>
      <li><span class="ref-k">Method</span><span>${e(a.method)} · reference implementation ${link(a.method_url, 'PatchCore (amazon-science/patchcore-inspection)')}. Not an official reproduction, and not compared with the paper's numbers.</span></li>
    </ul>`;
  }

  function renderLimitations() {
    const ev = obj(state.data.evaluation) || {};
    const items = [...new Set([...arr(state.data.limitations), ...arr(ev.limitations), ...arr(ev.metric_notes)].filter(x => typeof x === 'string'))];
    $('limitationsList').innerHTML = items.length ? items.map(l => `<li>${e(l)}</li>`).join('') : `<li>No limitations were exported · <code>${DATA_FILE}</code></li>`;
  }

  // ---------- events ----------
  document.addEventListener('click', ev => {
    const b = ev.target.closest?.('button[data-image]');
    if (b) locate(b.dataset.image);
  });
  $('download').addEventListener('click', () => {
    if (!state.bytes) return;
    const url = URL.createObjectURL(new Blob([state.bytes], {type:'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = 'inspection-images-v2.json'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  let resizeTimer = null, lastW = 0;
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(entries => {
    const w = Math.round(entries[0].contentRect.width);
    if (w === lastW) return; lastW = w;
    clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (state.data && state.method) drawStrip(state.method, scoreRows(state.method).rows); }, 120);
  }) : null;
  ro?.observe($('scores'));
  load();
})();

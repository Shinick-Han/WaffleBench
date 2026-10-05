(() => {
  'use strict';
  // Post-hackathon overview. Reads the original frozen export only; runs and writes nothing.
  const $ = id => document.getElementById(id);
  const DATA_URL = './data/inspection-v3.json';
  const DATA_FILE = 'web/data/inspection-v3.json';
  // Drawing fallback only (same contract values as inspection-evidence.js), named in the UI when used.
  const CONTRACT_GEOMETRY = Object.freeze({wafer_diameter_mm:300,die_width_mm:8,die_height_mm:6,scribe_mm:0.08,edge_exclusion_mm:3});
  const OUTCOME = {positive:'Review positive',negative:'Observed negative',failed:'Failed or missing'};
  const STATUS = {failure:'Measurement failed',missing:'Measurement missing',ok:'OK'};
  const state = {data:null,rows:[],sites:[],siteById:new Map(),wafers:[],geometry:CONTRACT_GEOMETRY,geometryFallback:false,cursor:0,loading:false};

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
  const empty = (title, detail) => `<div class="empty"><strong>${e(title)}</strong>${e(detail)} · file <code>${DATA_FILE}</code></div>`;

  // Same outcome semantics as inspection-evidence.js: a failed/missing review is never a negative.
  function outcome(row) {
    if (row.status != null && row.status !== 'ok') return 'failed';
    if (row.label === true) return 'positive';
    if (row.label === false) return 'negative';
    if (row.status === 'ok' && typeof row.reported_positive === 'boolean') return row.reported_positive ? 'positive' : 'negative';
    return 'failed';
  }
  const outcomeText = row => {
    const o = outcome(row);
    return o === 'failed' ? (row.status != null && row.status !== 'ok' && STATUS[row.status] ? STATUS[row.status] : 'No result') : OUTCOME[o];
  };
  const badge = row => `<span class="result ${outcome(row)}"><i aria-hidden="true"></i>${e(outcomeText(row))}</span>`;

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
      let response;
      try { response = await fetch(DATA_URL, {cache:'no-store'}); }
      catch (error) {
        console.error('Inspection v3 fetch:', error);
        return showState(`<h2>Cannot access the evidence file</h2><p>Could not read <code>${DATA_FILE}</code> because of a network error or a local file-access restriction. Serve <code>web/</code> with a static server and try again. Nothing is shown in place of the evidence.</p>`, true);
      }
      if (response.status === 404) return showState(`<h2>Evidence file not found</h2><p><code>${DATA_FILE}</code> returned 404. This overview shows only the stored export; no example or provisional values are substituted.</p>`, true);
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
      prepare(obj(data.replay));
      $('loadState').hidden = true; $('app').hidden = false;
      renderOutcome(); renderChain(); renderReplayStatic(); renderReplay(); renderLimitations(); renderFoot();
    } finally { state.loading = false; }
  }

  // Same row ordering and site filtering as inspection-evidence.js.
  function prepare(replay) {
    const rows = arr(replay?.rows).filter(obj);
    if (rows.every(r => finite(r.step))) rows.sort((a, b) => a.step - b.step);
    state.rows = rows;
    state.sites = arr(replay?.sites).filter(s => obj(s) && finite(s.x_mm) && finite(s.y_mm) && s.id != null);
    state.siteById = new Map(state.sites.map(s => [String(s.id), s]));
    const keys = new Map();
    for (const item of [...state.sites, ...rows]) if (item.wafer != null && !keys.has(String(item.wafer))) keys.set(String(item.wafer), item.wafer);
    state.wafers = [...keys.values()].sort((a, b) => finite(a) && finite(b) ? a - b : String(a).localeCompare(String(b)));
    const g = obj(replay?.geometry);
    const complete = g && Object.keys(CONTRACT_GEOMETRY).every(k => finite(g[k]));
    state.geometry = complete ? g : CONTRACT_GEOMETRY; state.geometryFallback = !complete;
    state.cursor = rows.length ? 1 : 0;
  }

  // ---------- outcome ----------
  function verdictChip(success) {
    const met = success === true, missed = success === false;
    const icon = met ? '<path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
      : missed ? '<path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
      : '<path d="M4 8h8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>';
    return `<span class="verdict ${met ? 'met' : missed ? 'missed' : 'none'}"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">${icon}</svg>${met ? 'Preregistered target met' : missed ? 'Preregistered target missed' : 'No exported verdict'}</span>`;
  }
  function ciStrip(p) {
    const ci = arr(p.ci95), lo = ci[0], hi = ci[1], mid = p.mean_difference;
    if (!finite(lo) || !finite(hi)) return '<p class="footnote">No 95% CI was exported.</p>';
    const vals = [0, lo, hi].concat(finite(mid) ? [mid] : []);
    let min = Math.min(...vals), max = Math.max(...vals); const pad = (max - min || 1) * .12; min -= pad; max += pad;
    const L = 12, R = 408, x = v => L + (v - min) / (max - min) * (R - L), Y = 40;
    return `<svg class="ci" viewBox="0 0 420 78" role="img" aria-label="Paired mean difference ${signed(mid)} confirmed DOI per lot, 95% confidence interval ${signed(lo)} to ${signed(hi)}. The dashed line is no difference.">
      <line x1="${L}" x2="${R}" y1="66" y2="66" stroke="#b9c5bd"/>
      <line x1="${x(0)}" x2="${x(0)}" y1="14" y2="66" stroke="#596860" stroke-dasharray="3 3"/><text x="${x(0)}" y="10" text-anchor="middle">0</text>
      <line x1="${x(lo)}" x2="${x(hi)}" y1="${Y}" y2="${Y}" stroke="#202b28" stroke-width="2"/>
      <line x1="${x(lo)}" x2="${x(lo)}" y1="${Y - 7}" y2="${Y + 7}" stroke="#202b28" stroke-width="2"/><line x1="${x(hi)}" x2="${x(hi)}" y1="${Y - 7}" y2="${Y + 7}" stroke="#202b28" stroke-width="2"/>
      <text x="${x(lo)}" y="${Y + 22}" text-anchor="middle">${signed(lo)}</text><text x="${x(hi)}" y="${Y + 22}" text-anchor="middle">${signed(hi)}</text>
      ${finite(mid) ? `<circle cx="${x(mid)}" cy="${Y}" r="6" fill="#276449" stroke="#fff" stroke-width="2"/><text class="ink" x="${x(mid)}" y="${Y - 12}" text-anchor="middle">${signed(mid)}</text>` : ''}
    </svg>`;
  }
  function renderOutcome() {
    const p = obj(state.data.primary);
    if (!p) { $('verdict').innerHTML = ''; $('outcome').innerHTML = empty('No primary result exported', 'the export has no primary field'); return; }
    $('verdict').innerHTML = verdictChip(p.success);
    const ci = arr(p.ci95);
    $('outcome').innerHTML = `<div class="outcome-grid">
      <div><p class="statement"><code>${e(p.candidate)}</code> confirmed <span class="num">${fmt(p.mean_candidate)}</span> DOI per lot; the comparator <code>${e(p.comparator)}</code> confirmed <span class="num">${fmt(p.mean_comparator)}</span>.</p>
      <p class="statement-sub">Same ${fmt(p.budget, 0)} CU budget per lot · ${e(p.mode)} · paired over ${count(p.pairs)} lots. Relative gain ${pct(p.relative_gain)} against a preregistered target of ${plainPct(p.target)}. The verdict above is the exported <code>success</code> flag, not recomputed here.</p></div>
      <div><dl class="figures"><div><dt>Paired difference</dt><dd>${signed(p.mean_difference)}</dd></div><div><dt>95% CI</dt><dd>${finite(ci[0]) && finite(ci[1]) ? `${signed(ci[0])} to ${signed(ci[1])}` : '—'}</dd></div><div><dt>Relative gain</dt><dd>${pct(p.relative_gain)} <small>target ${plainPct(p.target)}</small></dd></div></dl>${ciStrip(p)}<p class="footnote">Candidate − comparator, confirmed DOI per lot, paired bootstrap 95% CI.</p></div>
    </div>`;
  }

  function renderChain() {
    const p = obj(state.data.primary);
    if (!p) { $('chainResult').innerHTML = 'The export has no primary result, so no recorded outcome is shown.'; return; }
    $('chainHypothesis').innerHTML = `Before the test campaign, <code>${e(p.candidate)}</code> was chosen and a relative gain of at least ${plainPct(p.target)} over <code>${e(p.comparator)}</code> at ${fmt(p.budget, 0)} CU was fixed as the target.`;
    const verdict = p.success === true ? 'met the target' : p.success === false ? 'missed the target' : 'has no exported verdict';
    $('chainResult').innerHTML = `On ${count(p.pairs)} paired synthetic lots the candidate ${verdict}: ${pct(p.relative_gain)} relative gain, paired difference ${signed(p.mean_difference)} confirmed DOI per lot. Full record on the <a href="./inspection-evidence.html">Inspection v3 evidence</a> page.`;
  }

  // ---------- replay ----------
  function renderReplayStatic() {
    const replay = obj(state.data.replay) || {};
    const hasAny = !!(state.rows.length || state.sites.length);
    $('replayEmpty').hidden = hasAny; $('replayBody').hidden = !hasAny;
    if (!hasAny) {
      $('replayEmpty').innerHTML = empty('No recorded measurements exported', 'the export has no replay.rows or replay.sites');
      $('replayMeta').textContent = 'Steps through the stored paid reviews in their recorded order.';
      return;
    }
    const perWafer = state.wafers.map(w => state.sites.filter(s => s.wafer === w).length);
    $('replayMeta').innerHTML = `Recorded lot <code>${e(replay.lot_id)}</code>, policy <code>${e(replay.variant)}</code>: ${count(state.sites.length)} care sites on ${count(state.wafers.length)} wafers (${perWafer.map(count).join(' / ') || '—'}), ${count(state.rows.length)} paid reviews, ${fmt(replay.spent, 1)} of ${fmt(replay.budget, 0)} CU spent. Lot choice: ${e(replay.selection_rule)} This replays the record; nothing is re-run.${state.geometryFallback ? ' replay.geometry is incomplete, so the contract geometry is used for drawing only.' : ''}`;
    const range = $('stepRange'); range.max = String(state.rows.length); range.disabled = !state.rows.length;
    if (!state.sites.length) { $('wafers').innerHTML = empty('No die coordinates exported', 'replay.sites is missing, so no wafer map is drawn'); return; }
    $('wafers').innerHTML = state.wafers.map((w, i) => `<figure class="wafer"><figcaption><strong>${e(waferName(w))}</strong><span id="waferCount${i}"></span></figcaption><canvas width="600" height="600" data-wafer-index="${i}" role="img"></canvas></figure>`).join('');
    $('wafers').querySelectorAll('canvas').forEach(c => c.addEventListener('click', pickDie));
  }

  function measuredSoFar() {
    const map = new Map();
    for (let i = 0; i < state.cursor; i++) { const r = state.rows[i]; map.set(String(r.site_id), outcome(r)); }
    return map;
  }

  function renderReplay() {
    if ($('replayBody').hidden) return;
    const n = state.rows.length, cur = state.cursor, row = cur ? state.rows[cur - 1] : null, replay = obj(state.data.replay) || {};
    $('stepRange').value = String(cur);
    $('stepOut').textContent = `${count(cur)} / ${count(n)}`;
    $('stepRange').setAttribute('aria-valuetext', row ? `Step ${cur} of ${n}, ${text(row.site_id)}, ${outcomeText(row)}` : `Before the first review, ${n} steps in total`);
    $('stepFirst').disabled = $('stepPrev').disabled = cur === 0;
    $('stepNext').disabled = $('stepLast').disabled = cur >= n;
    const spentNow = row && finite(row.cumulative_spend) ? row.cumulative_spend : 0;
    const width = finite(replay.budget) && replay.budget > 0 ? Math.min(100, spentNow / replay.budget * 100) : 0;
    $('spend').innerHTML = `Cumulative spend <strong class="num">${fmt(spentNow, 1)}</strong> / budget ${fmt(replay.budget, 0)} CU<div class="bar" role="img" aria-label="Cumulative spend ${fmt(width, 0)}% of budget"><i style="width:${width}%"></i></div>`;
    const measured = measuredSoFar();
    state.wafers.forEach((w, i) => {
      const own = state.rows.slice(0, cur).filter(r => r.wafer === w).map(outcome);
      const pos = own.filter(o => o === 'positive').length, neg = own.filter(o => o === 'negative').length, fail = own.length - pos - neg;
      const label = $(`waferCount${i}`); if (label) label.textContent = own.length ? `Reviewed ${own.length} · positive ${pos}` : 'No review yet · all unknown';
      const canvas = $('wafers').querySelector(`canvas[data-wafer-index="${i}"]`);
      if (canvas) { canvas.setAttribute('aria-label', `Wafer ${waferName(w)}: ${own.length} dies reviewed up to this step, ${pos} review positive, ${neg} observed negative, ${fail} failed or missing. Every other die was not measured, so its outcome is unknown.`); draw(canvas, w, measured, row); }
    });
    renderStepDetail(row, n, replay);
  }

  function renderStepDetail(row, n, replay) {
    const next = state.rows[state.cursor];
    const nextLine = next ? `<p class="next-step"><span>Next recorded review</span> step ${e(next.step ?? state.cursor + 1)} · <span class="mono">${e(next.site_id)}</span> · ${e(waferName(next.wafer))} · ${fmt(next.charged, 1)} CU. Its outcome appears when you step to it.</p>`
      : n ? '<p class="next-step"><span>Next recorded review</span> none. The stored record ends at this step.</p>' : '';
    if (!row) { $('stepDetail').innerHTML = `<p class="hint">Before the first review. Every die is unknown.</p>${nextLine}`; return; }
    const left = finite(replay.budget) && finite(row.cumulative_spend) ? replay.budget - row.cumulative_spend : null;
    $('stepDetail').innerHTML = `<p class="step-lead">Step ${e(row.step ?? state.cursor)} ${badge(row)}</p><p class="mono site-id">${e(row.site_id)}</p>
      <dl class="detail-list"><dt>Wafer</dt><dd>${e(waferName(row.wafer))}</dd><dt>Observation</dt><dd>${e(outcomeText(row))}</dd><dt>Charged for this review</dt><dd>${fmt(row.charged, 1)} CU</dd><dt>Cumulative spend</dt><dd>${fmt(row.cumulative_spend, 1)} CU</dd><dt>Budget left</dt><dd>${fmt(left, 1)} CU</dd><dt>Classifier score at selection</dt><dd>${fmt(row.baseline_p, 3)}</dd></dl>
      <p class="hint">The classifier score is a frozen model prediction used to choose the die, not a measurement. ${outcome(row) === 'failed' ? 'A failed or missing review is unknown, never counted as a good die.' : ''}</p>${nextLine}`;
  }

  function draw(canvas, waferKey, measured, currentRow) {
    const ctx = canvas.getContext('2d'); if (!ctx) return;
    const size = 600, ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== size * ratio) { canvas.width = size * ratio; canvas.height = size * ratio; }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, size, size);
    const g = state.geometry, k = size / g.wafer_diameter_mm, dw = g.die_width_mm * k, dh = g.die_height_mm * k, c = size / 2;
    // Projection only: positions from replay.sites, outcomes only from recorded rows up to the cursor.
    ctx.save(); ctx.beginPath(); ctx.arc(c, c, c, 0, Math.PI * 2); ctx.clip();
    ctx.fillStyle = '#d7ded7'; ctx.fillRect(0, 0, size, size);
    const film = ctx.createLinearGradient(45, 20, 555, 575);
    [[0,'#d9d1eb'],[.16,'#a9c5e2'],[.33,'#b7dce1'],[.5,'#c6ddbf'],[.69,'#e9d9ac'],[.85,'#e3c9cf'],[1,'#b9bed5']].forEach(([s, col]) => film.addColorStop(s, col));
    ctx.globalAlpha = .7; ctx.fillStyle = film; ctx.fillRect(0, 0, size, size); ctx.globalAlpha = 1;
    for (const s of state.sites) {
      if (s.wafer !== waferKey) continue;
      const x = c + s.x_mm * k - dw / 2 + .5, y = c + s.y_mm * k - dh / 2 + .5, w = dw - 1, h = dh - 1, o = measured.get(String(s.id));
      if (!o) { ctx.fillStyle = 'rgba(248,250,246,.38)'; ctx.fillRect(x, y, w, h); }
      else if (o === 'positive') { ctx.fillStyle = '#963f34'; ctx.fillRect(x, y, w, h); }
      else if (o === 'negative') { ctx.fillStyle = '#eef5ee'; ctx.fillRect(x, y, w, h); ctx.strokeStyle = '#276449'; ctx.lineWidth = 2; ctx.strokeRect(x + 1, y + 1, w - 2, h - 2); }
      else {
        ctx.fillStyle = '#f5ecd6'; ctx.fillRect(x, y, w, h);
        ctx.save(); ctx.beginPath(); ctx.rect(x, y, w, h); ctx.clip(); ctx.strokeStyle = '#916414'; ctx.lineWidth = 1.6;
        for (let d = -h; d < w; d += 4.5) { ctx.beginPath(); ctx.moveTo(x + d, y + h); ctx.lineTo(x + d + h, y); ctx.stroke(); }
        ctx.restore(); ctx.strokeStyle = '#916414'; ctx.lineWidth = 1; ctx.strokeRect(x + .5, y + .5, w - 1, h - 1);
      }
    }
    ctx.restore();
    ctx.beginPath(); ctx.arc(c, c, (g.wafer_diameter_mm / 2 - g.edge_exclusion_mm) * k, 0, Math.PI * 2); ctx.setLineDash([3, 4]); ctx.strokeStyle = '#8c9c93'; ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(c, c, c - 1, 0, Math.PI * 2); ctx.strokeStyle = '#adbbb3'; ctx.lineWidth = 2; ctx.stroke();
    ctx.beginPath(); ctx.arc(c, size, 6, Math.PI, 0); ctx.fillStyle = '#f3f5f0'; ctx.fill(); ctx.strokeStyle = '#778b83'; ctx.lineWidth = 1; ctx.stroke();
    const cur = currentRow && currentRow.wafer === waferKey ? state.siteById.get(String(currentRow.site_id)) : null;
    if (cur) {
      const x = c + cur.x_mm * k, y = c + cur.y_mm * k;
      ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 4; ctx.strokeRect(x - dw / 2 - 3, y - dh / 2 - 3, dw + 6, dh + 6);
      ctx.strokeStyle = '#202b28'; ctx.lineWidth = 2; ctx.strokeRect(x - dw / 2 - 3, y - dh / 2 - 3, dw + 6, dh + 6);
    }
  }

  function pickDie(event) {
    const canvas = event.currentTarget, w = state.wafers[Number(canvas.dataset.waferIndex)], r = canvas.getBoundingClientRect();
    const g = state.geometry, k = 600 / g.wafer_diameter_mm;
    const xmm = ((event.clientX - r.left) / r.width * 600 - 300) / k, ymm = ((event.clientY - r.top) / r.height * 600 - 300) / k;
    const site = state.sites.find(s => s.wafer === w && Math.abs(s.x_mm - xmm) <= g.die_width_mm / 2 && Math.abs(s.y_mm - ymm) <= g.die_height_mm / 2);
    if (!site) { $('pickNote').textContent = ''; return; }
    const index = state.rows.findIndex(row => String(row.site_id) === String(site.id));
    if (index < 0) { $('pickNote').textContent = `${site.id}: never reviewed in this record, so its outcome is unknown.`; return; }
    $('pickNote').textContent = `${site.id}: moved to step ${state.rows[index].step ?? index + 1}.`;
    setCursor(index + 1);
  }

  function setCursor(value) { state.cursor = Math.max(0, Math.min(state.rows.length, value)); renderReplay(); }

  function renderLimitations() {
    const list = arr(obj(state.data.study)?.limitations).filter(x => x != null);
    $('limitations').innerHTML = list.length ? list.map(x => `<li>${e(x)}</li>`).join('') : '<li>No limitations were exported. Read the results only within the synthetic study scope stated above.</li>';
  }

  function renderFoot() {
    const s = obj(state.data.study) || {};
    const sha = typeof s.freeze_sha256 === 'string' ? s.freeze_sha256.slice(0, 12) + '…' : '—';
    $('foot').innerHTML = `Post-hackathon overview · created 5 October 2026 · branch <code>post-hackathon/roi-20261005</code> · separate from the submitted pages, which are unchanged.<br>Evidence: ${e(s.name)} export generated ${e(s.generated_at)} · freeze <span class="mono" title="${e(s.freeze_sha256)}">${e(sha)}</span> · source commit <span class="mono">${e(typeof s.source_commit === 'string' ? s.source_commit.slice(0, 10) : null)}</span>.`;
  }

  // ---------- events ----------
  $('stepFirst').addEventListener('click', () => setCursor(0));
  $('stepPrev').addEventListener('click', () => setCursor(state.cursor - 1));
  $('stepNext').addEventListener('click', () => setCursor(state.cursor + 1));
  $('stepLast').addEventListener('click', () => setCursor(state.rows.length));
  $('stepRange').addEventListener('input', event => setCursor(Number(event.target.value)));
  let resizeFrame = 0;
  window.addEventListener('resize', () => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(() => { if (state.data) renderReplay(); }); });
  load();
})();

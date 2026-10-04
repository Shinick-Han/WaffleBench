(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const DATA_URL = './data/inspection-v3.json';
  const DATA_FILE = 'web/data/inspection-v3.json';
  // Drawing fallback only, named in the UI when used; never reported as a result.
  const CONTRACT_GEOMETRY = Object.freeze({wafer_diameter_mm:300,die_width_mm:8,die_height_mm:6,scribe_mm:0.08,edge_exclusion_mm:3});
  const OUTCOME = {positive:'Review positive',negative:'Observed negative',failed:'Failed or missing'};
  const STATUS = {failure:'Measurement failed',missing:'Measurement missing',ok:'OK'};
  const REWARD = {reported_review_positive:'Reported review-positive probability',latent_doi_probability:'Latent DOI probability'};
  const PLAY_MS = 520;
  const state = {data:null,raw:'',rows:[],sites:[],wafers:[],geometry:null,geometryFallback:false,cursor:0,timer:null,
    wafer:'all',result:'all',until:true,showCandidates:false,variantSort:'doi'};

  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const arr = v => Array.isArray(v) ? v : [];
  const text = v => v == null || v === '' ? '—' : String(v);
  const e = v => text(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (n, d=2) => finite(n) ? n.toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
  const signed = (n, d=2) => finite(n) ? (n > 0 ? '+' : n < 0 ? '−' : '') + fmt(Math.abs(n), d) : '—';
  const pct = (n, d=1) => finite(n) ? signed(n * 100, d) + '%' : '—';
  const count = n => finite(n) ? n.toLocaleString('en-US') : '—';
  const yesNo = v => v === true ? 'Yes' : v === false ? 'No' : '—';
  const waferName = w => w == null ? '—' : typeof w === 'number' ? `W${w}` : String(w);
  const empty = (title, detail) => `<div class="empty"><strong>${e(title)}</strong>${detail} · file <code>${DATA_FILE}</code></div>`;

  function outcome(row) {
    if (row.status != null && row.status !== 'ok') return 'failed';
    if (row.label === true) return 'positive';
    if (row.label === false) return 'negative';
    if (row.status === 'ok' && typeof row.reported_positive === 'boolean') return row.reported_positive ? 'positive' : 'negative';
    return 'failed';
  }
  const outcomeText = row => {
    const o = outcome(row);
    return o === 'failed' ? (STATUS[row.status] && row.status !== 'ok' ? STATUS[row.status] : 'No result') : OUTCOME[o];
  };
  const badge = row => `<span class="result ${outcome(row)}"><i aria-hidden="true"></i>${e(outcomeText(row))}</span>`;

  // ---------- loading ----------
  function showState(html) { const box = $('loadState'); box.hidden = false; box.innerHTML = html; }
  function retryButton() { const b = document.createElement('button'); b.type = 'button'; b.className = 'control'; b.textContent = 'Retry'; b.addEventListener('click', load); $('loadState').append(b); }
  async function load() {
    stop(); $('app').hidden = true; $('download').disabled = true;
    showState('<div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div><p>Loading stored Inspection v3 evidence…</p>');
    let response;
    try { response = await fetch(DATA_URL, {cache:'no-store'}); }
    catch (error) {
      console.error('Inspection v3 fetch:', error);
      showState(`<h2>Cannot access the evidence file</h2><p>Could not read <code>${DATA_FILE}</code> because of a network error or a local file-access restriction. Serve <code>web/</code> with a static server and try again.</p>`);
      return retryButton();
    }
    if (response.status === 404) {
      showState(`<h2>No verified result yet</h2><p>Inspection v3 evidence has not been exported yet. This page fills in once the coordinator exports the stored result to <code>${DATA_FILE}</code>. No example values or provisional benchmarks are shown.</p>`);
      return retryButton();
    }
    try {
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const raw = await response.text();
      const data = JSON.parse(raw);
      if (!obj(data)) throw Error('Top-level value is not an object');
      if (data.schema_version != null && data.schema_version !== 1) throw Error(`Unsupported schema_version ${data.schema_version}`);
      state.raw = raw; state.data = data;
      prepareReplay(obj(data.replay));
      $('loadState').hidden = true; $('app').hidden = false; $('download').disabled = false;
      renderAll();
    } catch (error) {
      console.error('Inspection v3 parse:', error);
      showState(`<h2>Cannot parse the evidence file</h2><p>${e(error.message)}. Check the export format of <code>${DATA_FILE}</code> and try again.</p>`);
      retryButton();
    }
  }

  function prepareReplay(replay) {
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
    state.cursor = 0; state.wafer = 'all'; state.result = 'all';
    $('waferFilter').innerHTML = '<option value="all">All wafers</option>' + state.wafers.map(w => `<option value="${e(w)}">${e(waferName(w))}</option>`).join('');
    $('resultFilter').value = 'all';
  }

  function renderAll() {
    renderStudy(); renderPrimary(); renderReplayStatic(); renderReplay(); renderVariants(); renderClassification(); renderAudit(); renderInference(); renderLimitations();
  }

  // ---------- study ----------
  function renderStudy() {
    const s = obj(state.data.study) || {};
    let generated = text(s.generated_at);
    if (typeof s.generated_at === 'string' && !Number.isNaN(Date.parse(s.generated_at))) generated = new Date(s.generated_at).toLocaleString('en-US', {dateStyle:'medium', timeStyle:'short'});
    const items = [['Study', s.name], ['Kind', s.kind], ['Generated', generated], ['freeze SHA-256', s.freeze_sha256 ? String(s.freeze_sha256).slice(0, 12) + '…' : null, s.freeze_sha256], ['source commit', s.source_commit ? String(s.source_commit).slice(0, 10) : null, s.source_commit]];
    $('studyMeta').innerHTML = items.map(([k, v, full]) => `<div><dt>${e(k)}</dt><dd${full ? ` class="mono" title="${e(full)}"` : ''}>${e(v)}</dd></div>`).join('');
  }

  // ---------- primary ----------
  function verdictChip(success) {
    const met = success === true, missed = success === false;
    const icon = met ? '<path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
      : missed ? '<path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
      : '<path d="M4 8h8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>';
    return `<span class="verdict ${met ? 'met' : missed ? 'missed' : 'none'}"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">${icon}</svg>${met ? 'Preregistered target met' : missed ? 'Preregistered target missed' : 'No verdict'}</span>`;
  }
  function niceTicks(lo, hi, n=5) {
    const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
    const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    return {ticks: out, step};
  }
  function ciChart(p) {
    const ci = arr(p.ci95), lo = ci[0], hi = ci[1], mid = p.mean_difference;
    if (!finite(lo) || !finite(hi)) return `<figure class="ci-figure">${empty('No 95% CI yet', 'primary.ci95 was not exported')}</figure>`;
    const vals = [0, lo, hi].concat(finite(mid) ? [mid] : []);
    let min = Math.min(...vals), max = Math.max(...vals); const pad = (max - min || 1) * 0.14; min -= pad; max += pad;
    const {ticks, step} = niceTicks(min, max); const digits = Math.max(0, Math.min(3, -Math.floor(Math.log10(step)) + (step / 10 ** Math.floor(Math.log10(step)) === 2.5 ? 1 : 0)));
    const W = 560, L = 16, R = 544, x = v => L + (v - min) / (max - min) * (R - L), Y = 58;
    const tickSvg = ticks.map(t => `<line x1="${x(t)}" x2="${x(t)}" y1="96" y2="101" stroke="#b9c5bd"/><text x="${x(t)}" y="116" text-anchor="middle">${fmt(t, digits)}</text>`).join('');
    return `<figure class="ci-figure"><svg viewBox="0 0 ${W} 124" role="img" aria-label="Mean difference ${signed(mid)}, 95% confidence interval ${signed(lo)} to ${signed(hi)}. 0 means no difference.">
      <line x1="${L}" x2="${R}" y1="96" y2="96" stroke="#b9c5bd"/>${tickSvg}
      <line x1="${x(0)}" x2="${x(0)}" y1="18" y2="96" stroke="#596860" stroke-dasharray="3 3"/><text x="${x(0)}" y="12" text-anchor="middle">No difference</text>
      <line x1="${x(lo)}" x2="${x(hi)}" y1="${Y}" y2="${Y}" stroke="#202b28" stroke-width="2"/>
      <line x1="${x(lo)}" x2="${x(lo)}" y1="${Y - 8}" y2="${Y + 8}" stroke="#202b28" stroke-width="2"/><line x1="${x(hi)}" x2="${x(hi)}" y1="${Y - 8}" y2="${Y + 8}" stroke="#202b28" stroke-width="2"/>
      <text x="${x(lo)}" y="${Y + 24}" text-anchor="middle">${signed(lo)}</text><text x="${x(hi)}" y="${Y + 24}" text-anchor="middle">${signed(hi)}</text>
      ${finite(mid) ? `<circle cx="${x(mid)}" cy="${Y}" r="7" fill="#276449" stroke="#fff" stroke-width="2"><title>Mean difference ${signed(mid)}</title></circle><text class="ink" x="${x(mid)}" y="${Y - 14}" text-anchor="middle">${signed(mid)}</text>` : ''}
    </svg><figcaption>Paired mean difference (candidate − comparator, confirmed DOI count) with 95% CI. If the interval includes the zero line, the difference is uncertain.</figcaption></figure>`;
  }
  function renderPrimary() {
    const p = obj(state.data.primary);
    if (!p) { $('primaryVerdict').innerHTML = ''; $('primaryBody').innerHTML = empty('Primary result not yet verified', 'the export has no primary field'); return; }
    $('primaryVerdict').innerHTML = verdictChip(p.success);
    const ci = arr(p.ci95);
    const statement = `Mean confirmed DOI is <span class="num">${fmt(p.mean_candidate)}</span> for <code>${e(p.candidate)}</code> and <span class="num">${fmt(p.mean_comparator)}</span> for the comparator <code>${e(p.comparator)}</code>.`;
    const sub = `Paired bootstrap over ${count(p.pairs)} lot pairs · same budget of ${fmt(p.budget, 0)} CU · ${e(p.mode)}. Relative gain ${pct(p.relative_gain)}; preregistered target ${finite(p.target) ? fmt(p.target * 100, 1) + '%' : '—'}.`;
    const figures = [['Candidate mean', fmt(p.mean_candidate)], ['Comparator mean', fmt(p.mean_comparator)], ['Mean difference', signed(p.mean_difference)],
      ['95% CI', finite(ci[0]) && finite(ci[1]) ? `${signed(ci[0])} to ${signed(ci[1])}` : '—'], ['Relative gain', `${pct(p.relative_gain)} <small>target ${finite(p.target) ? fmt(p.target * 100, 1) + '%' : '—'}</small>`], ['Lot pairs', count(p.pairs)]];
    $('primaryBody').innerHTML = `<div class="primary-grid"><div><p class="statement">${statement}</p><p class="statement-sub">${sub}</p><dl class="figures">${figures.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl></div>${ciChart(p)}</div>`;
  }

  // ---------- replay ----------
  function renderReplayStatic() {
    const replay = obj(state.data.replay);
    const hasAny = state.rows.length || state.sites.length;
    $('replayEmpty').hidden = !!hasAny; $('replayContent').hidden = !hasAny;
    if (!hasAny) { $('replayEmpty').innerHTML = empty('No measurement replay records yet', 'the export has no replay.rows or replay.sites'); $('replayMeta').textContent = 'Replays the stored paid-measurement records in order.'; return; }
    $('replayMeta').textContent = `Lot ${text(replay.lot_id)} · variant ${text(replay.variant)} · budget ${fmt(replay.budget, 0)} CU · spent ${fmt(replay.spent, 1)} CU · ${count(state.rows.length)} paid measurements. This only replays the record; the study result does not change.`;
    const range = $('stepRange'); range.max = String(state.rows.length); range.value = '0'; range.disabled = !state.rows.length;
    const g = state.geometry, per = state.wafers.map(w => state.sites.filter(s => s.wafer === w).length);
    $('geometryNote').textContent = `⌀${fmt(g.wafer_diameter_mm, 0)} mm · die ${fmt(g.die_width_mm, 0)} × ${fmt(g.die_height_mm, 0)} mm · scribe ${fmt(g.scribe_mm * 1000, 0)} μm · edge exclusion ${fmt(g.edge_exclusion_mm, 0)} mm · sites per wafer ${per.map(count).join(' / ') || '—'}${state.geometryFallback ? ' · replay.geometry is missing, so the contract geometry was used for drawing' : ''}. Outcomes are also distinguished by fill, outline and hatching, not only by color.`;
    $('pickNote').textContent = '';
    if (!state.sites.length) { $('wafers').innerHTML = empty('No die coordinates yet', 'replay.sites is missing, so no wafer map was drawn. The measurement records table below is still available'); return; }
    $('wafers').innerHTML = state.wafers.map((w, i) => `<figure class="wafer"><figcaption><strong>${e(waferName(w))}</strong><span id="waferCount${i}"></span></figcaption><canvas width="600" height="600" data-wafer-index="${i}" role="img"></canvas></figure>`).join('');
    $('wafers').querySelectorAll('canvas').forEach(c => c.addEventListener('click', pickDie));
  }

  function observedMap() {
    const map = new Map();
    for (let i = 0; i < state.cursor; i++) { const r = state.rows[i]; map.set(String(r.site_id), {row:r, index:i, outcome:outcome(r)}); }
    return map;
  }

  function renderReplay() {
    if ($('replayContent').hidden) return;
    const n = state.rows.length, cur = state.cursor, row = cur ? state.rows[cur - 1] : null, replay = obj(state.data.replay) || {};
    $('stepRange').value = String(cur);
    $('stepOut').textContent = `${count(cur)} / ${count(n)}`;
    $('stepRange').setAttribute('aria-valuetext', cur ? `Step ${cur} of ${n}, ${text(row.site_id)}, ${outcomeText(row)}` : `Before replay, ${n} steps in total`);
    $('stepFirst').disabled = $('stepPrev').disabled = cur === 0;
    $('stepNext').disabled = $('stepLast').disabled = cur >= n;
    $('stepPlay').disabled = !n;
    const spentNow = row && finite(row.cumulative_spend) ? row.cumulative_spend : 0;
    const width = finite(replay.budget) && replay.budget > 0 ? Math.min(100, spentNow / replay.budget * 100) : 0;
    $('spend').innerHTML = `Cumulative spend <strong class="num">${fmt(spentNow, 1)}</strong> / budget ${fmt(replay.budget, 0)} CU<div class="bar" role="img" aria-label="Cumulative spend ${fmt(width, 0)}% of budget"><i style="width:${width}%"></i></div>`;
    const obs = observedMap();
    state.wafers.forEach((w, i) => {
      const values = [...obs.values()].filter(o => o.row.wafer === w);
      const pos = values.filter(o => o.outcome === 'positive').length, neg = values.filter(o => o.outcome === 'negative').length, fail = values.length - pos - neg;
      const label = $(`waferCount${i}`); if (label) label.textContent = `Measured ${values.length} · positive ${pos}`;
      const canvas = $('wafers').querySelector(`canvas[data-wafer-index="${i}"]`);
      if (canvas) { canvas.setAttribute('aria-label', `Wafer map ${waferName(w)}. ${values.length} dies measured: ${pos} review positive, ${neg} observed negative, ${fail} failed or missing. All other dies were not measured, so their outcome is unknown. The same information is in the table below.`); draw(canvas, w, obs, row); }
    });
    renderStepDetail(row, n);
    renderRows();
  }

  function renderStepDetail(row, n) {
    if (!row) { $('stepDetail').innerHTML = `<p class="hint">Before replay. Press Next or Play to reveal the ${count(n)} stored paid measurements in order. Dies that were never measured stay unknown to the end.</p>`; return; }
    const semantics = REWARD[row.selection_reward_semantics] || text(row.selection_reward_semantics);
    $('stepDetail').innerHTML = `<p class="step-lead">Step ${e(row.step)} ${badge(row)}</p><p class="mono">${e(row.site_id)}</p>
      <dl class="detail-list"><dt>Wafer</dt><dd>${e(waferName(row.wafer))}</dd><dt>Measurement status</dt><dd>${e(STATUS[row.status] || row.status)}</dd><dt>Charged cost</dt><dd>${fmt(row.charged, 1)} CU</dd><dt>Cumulative spend</dt><dd>${fmt(row.cumulative_spend, 1)} CU</dd><dt>baseline p</dt><dd>${fmt(row.baseline_p, 3)}</dd><dt>selection reward</dt><dd>${fmt(row.selection_reward, 3)}</dd><dt>Reward meaning</dt><dd>${e(semantics)}</dd></dl>
      <p class="hint">Selection reason <code>${e(row.reason)}</code>. baseline p is a frozen DOI-classifier score, not a measurement result.</p>`;
  }

  function draw(canvas, waferKey, obs, currentRow) {
    const ctx = canvas.getContext('2d'); if (!ctx) return;
    const size = 600, ratio = Math.min(window.devicePixelRatio || 1, 2);
    if (canvas.width !== size * ratio) { canvas.width = size * ratio; canvas.height = size * ratio; }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, size, size);
    const g = state.geometry, k = size / g.wafer_diameter_mm, dw = g.die_width_mm * k, dh = g.die_height_mm * k, c = size / 2;
    // Canvas is a projection only: positions and outcomes come from replay.sites and replay.rows.
    ctx.save(); ctx.beginPath(); ctx.arc(c, c, c, 0, Math.PI * 2); ctx.clip();
    ctx.fillStyle = '#d7ded7'; ctx.fillRect(0, 0, size, size);
    const film = ctx.createLinearGradient(45, 20, 555, 575);
    [[0,'#d9d1eb'],[.16,'#a9c5e2'],[.33,'#b7dce1'],[.5,'#c6ddbf'],[.69,'#e9d9ac'],[.85,'#e3c9cf'],[1,'#b9bed5']].forEach(([s, col]) => film.addColorStop(s, col));
    ctx.globalAlpha = .7; ctx.fillStyle = film; ctx.fillRect(0, 0, size, size);
    const glint = ctx.createLinearGradient(100, 30, 410, 400); glint.addColorStop(0, '#ffffff99'); glint.addColorStop(.35, '#ffffff0a'); glint.addColorStop(.62, '#ffffff55'); glint.addColorStop(1, '#ffffff08');
    ctx.globalAlpha = 1; ctx.fillStyle = glint; ctx.fillRect(0, 0, size, size);
    for (const s of state.sites) {
      if (s.wafer !== waferKey) continue;
      const x = c + s.x_mm * k - dw / 2 + .5, y = c + s.y_mm * k - dh / 2 + .5, w = dw - 1, h = dh - 1, o = obs.get(String(s.id));
      if (!o) {
        ctx.fillStyle = 'rgba(248,250,246,.38)'; ctx.fillRect(x, y, w, h);
        if (state.showCandidates && s.candidate === true) { ctx.strokeStyle = '#202b28'; ctx.lineWidth = 1; ctx.strokeRect(x + 1.5, y + 1.5, w - 3, h - 3); }
      } else if (o.outcome === 'positive') { ctx.fillStyle = '#963f34'; ctx.fillRect(x, y, w, h); }
      else if (o.outcome === 'negative') { ctx.fillStyle = '#eef5ee'; ctx.fillRect(x, y, w, h); ctx.strokeStyle = '#276449'; ctx.lineWidth = 2; ctx.strokeRect(x + 1, y + 1, w - 2, h - 2); }
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
    if (index < 0) { $('pickNote').textContent = `${site.id}: this die was not measured in this record. Its outcome is unknown.`; return; }
    $('pickNote').textContent = `${site.id}: moved to step ${state.rows[index].step ?? index + 1}.`;
    stop(); setCursor(index + 1);
  }

  function filteredRows() {
    return state.rows.map((row, index) => ({row, index})).filter(({row, index}) =>
      (!state.until || index < state.cursor) && (state.wafer === 'all' || String(row.wafer) === state.wafer) && (state.result === 'all' || outcome(row) === state.result));
  }
  function renderRows() {
    const list = filteredRows(), focused = document.activeElement?.dataset?.step;
    $('rowCount').textContent = `Showing ${count(list.length)} of ${count(state.rows.length)}`;
    $('obsRows').innerHTML = list.map(({row, index}) => {
      const cls = index === state.cursor - 1 ? 'is-current' : index >= state.cursor ? 'is-future' : '';
      return `<tr class="${cls}"><th scope="row"><button type="button" class="step-link" data-step="${index + 1}" aria-label="Go to step ${e(row.step ?? index + 1)}"${cls === 'is-current' ? ' aria-current="step"' : ''}>${e(row.step ?? index + 1)}</button></th><td class="site">${e(row.site_id)}</td><td>${e(waferName(row.wafer))}</td><td>${badge(row)}${index >= state.cursor ? ' <span class="footnote">not yet replayed</span>' : ''}</td><td class="num">${fmt(row.charged, 1)}</td><td class="num">${fmt(row.cumulative_spend, 1)}</td><td class="num">${fmt(row.baseline_p, 3)}</td><td class="num" title="${e(REWARD[row.selection_reward_semantics] || row.selection_reward_semantics)}">${fmt(row.selection_reward, 3)}</td><td class="reason">${e(row.reason)}</td></tr>`;
    }).join('') || `<tr><td colspan="9">${state.until && state.cursor === 0 ? 'No steps replayed yet. Press Next or Play, or clear “Up to current step only”.' : 'No measurement records match. Try changing the wafer or outcome filter.'}</td></tr>`;
    if (focused) $('obsRows').querySelector(`[data-step="${focused}"]`)?.focus({preventScroll:true});
    const tr = $('obsRows').querySelector('tr.is-current'), box = tr?.closest('.record-scroll');
    if (tr && box) {
      const top = tr.offsetTop + tr.closest('table').offsetTop, head = box.querySelector('thead')?.offsetHeight || 0;
      if (top - head < box.scrollTop || top + tr.offsetHeight > box.scrollTop + box.clientHeight) box.scrollTop = Math.max(0, top - box.clientHeight / 2);
    }
  }

  function setCursor(value) { state.cursor = Math.max(0, Math.min(state.rows.length, value)); renderReplay(); }
  function stop() {
    if (state.timer) clearInterval(state.timer); state.timer = null;
    const b = $('stepPlay'); b.textContent = 'Play'; b.setAttribute('aria-pressed', 'false'); $('stepDetail').setAttribute('aria-live', 'polite');
  }
  function play() {
    if (state.timer) return stop();
    if (state.cursor >= state.rows.length) state.cursor = 0;
    const b = $('stepPlay'); b.textContent = 'Pause'; b.setAttribute('aria-pressed', 'true'); $('stepDetail').setAttribute('aria-live', 'off');
    state.timer = setInterval(() => { if (state.cursor >= state.rows.length) return stop(); setCursor(state.cursor + 1); if (state.cursor >= state.rows.length) stop(); }, PLAY_MS);
    setCursor(state.cursor + (state.cursor === 0 ? 1 : 0));
  }

  // ---------- secondary sections ----------
  function renderVariants() {
    const all = arr(state.data.variants).filter(obj), p = obj(state.data.primary) || {};
    $('variantSort').disabled = !all.length;
    if (!all.length) { $('variantsBody').innerHTML = empty('No variant comparison yet', 'the variants array is empty or missing'); return; }
    const sorted = [...all].sort(state.variantSort === 'id' ? (a, b) => String(a.id).localeCompare(String(b.id))
      : state.variantSort === 'spent' ? (a, b) => (finite(a.mean_spent) ? a.mean_spent : Infinity) - (finite(b.mean_spent) ? b.mean_spent : Infinity)
      : (a, b) => (finite(b.mean_doi) ? b.mean_doi : -Infinity) - (finite(a.mean_doi) ? a.mean_doi : -Infinity));
    const role = v => v.id === p.candidate ? '<span class="role">candidate</span>' : v.id === p.comparator ? '<span class="role comparator">comparator</span>' : '';
    const values = all.map(v => v.mean_doi).filter(finite), top = values.length ? Math.max(...values) : 0;
    const {ticks} = niceTicks(0, top > 0 ? top : 1, 4);
    const tickStep = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
    const axisMax = Math.max(1, Math.ceil(top / tickStep) * tickStep);
    const dots = sorted.map(v => `<li class="${v.id === p.candidate ? 'is-candidate' : v.id === p.comparator ? 'is-comparator' : ''}" title="${e(v.id)} · mean DOI ${fmt(v.mean_doi)}"><span class="name">${e(v.id)}${role(v)}</span><span class="track">${finite(v.mean_doi) ? `<i style="left:${Math.max(0, v.mean_doi / axisMax * 100)}%"></i>` : ''}</span><span class="val">${fmt(v.mean_doi)}</span></li>`).join('');
    const rows = sorted.map(v => `<tr><th scope="row">${e(v.id)}${role(v)}</th><td class="num">${fmt(v.mean_doi)}</td><td class="num">${fmt(v.mean_spent, 1)}</td><td class="num">${fmt(v.mean_policy_wall_s, 4)}</td></tr>`).join('');
    $('variantsBody').innerHTML = `<div class="variant-grid"><figure class="dot-chart" aria-label="Dot chart of mean confirmed DOI by variant"><ol class="dots">${dots}</ol><div class="dots-axis" aria-hidden="true"><span></span><span><span>0</span><span>${fmt(axisMax, axisMax < 10 ? 1 : 0)}</span></span><span></span></div><figcaption>Mean confirmed DOI · green circle = candidate, square = comparator policy</figcaption></figure>
      <div class="table-scroll" role="region" tabindex="0" aria-label="Policy variant table"><table class="data-table"><caption>All variants · no post-hoc replacement · policy time is the sum of all selection calls in a lot</caption><thead><tr><th scope="col">Variant</th><th scope="col" class="num">Mean DOI</th><th scope="col" class="num">Mean spend CU</th><th scope="col" class="num">Mean policy s/lot</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }

  function renderClassification() {
    const list = arr(state.data.classification).filter(obj);
    if (!list.length) { $('classBody').innerHTML = empty('No classification metrics yet', 'the classification array is empty or missing'); return; }
    $('classBody').innerHTML = `<div class="table-scroll" role="region" tabindex="0" aria-label="Classification performance table"><table class="data-table class-table"><caption>Held-out classification metrics · a different result from confirmed DOI (operational finding)</caption><thead><tr><th scope="col">Model</th><th scope="col" class="num">precision</th><th scope="col" class="num">recall</th><th scope="col" class="num">AP</th><th scope="col" class="num">Brier</th><th scope="col" class="num">ECE</th><th scope="col" class="num grp">TP</th><th scope="col" class="num">False alarm FP</th><th scope="col" class="num">Miss FN</th><th scope="col" class="num">TN</th></tr></thead><tbody>${
      list.map(c => `<tr><th scope="row">${e(c.id)}</th><td class="num">${fmt(c.precision, 3)}</td><td class="num">${fmt(c.recall, 3)}</td><td class="num">${fmt(c.average_precision, 3)}</td><td class="num">${fmt(c.brier, 4)}</td><td class="num">${fmt(c.ece, 4)}</td><td class="num grp">${count(c.tp)}</td><td class="num">${count(c.fp)}</td><td class="num">${count(c.fn)}</td><td class="num">${count(c.tn)}</td></tr>`).join('')}</tbody></table></div>`;
  }

  function renderAudit() {
    const a = obj(state.data.audit);
    if (!a) { $('auditBody').innerHTML = empty('No audit result yet', 'the audit field is missing'); return; }
    const okStatus = ['pass', 'passed', 'ok', 'complete'].includes(String(a.status).toLowerCase());
    const hidden = Array.isArray(a.hidden_truth_fields_in_components) ? a.hidden_truth_fields_in_components.length : a.hidden_truth_fields_in_components;
    const cls = (good, v) => v == null ? '' : good ? 'ok' : 'bad';
    const protectedValue = finite(a.protected_files_preserved) ? `${count(a.protected_files_preserved)} files` : yesNo(a.protected_files_preserved);
    const protectedOK = a.protected_files_preserved === true || (finite(a.protected_files_preserved) && a.protected_files_preserved > 0);
    $('auditBody').innerHTML = `<dl class="state-list"><dt>Status</dt><dd class="${a.status == null ? '' : okStatus ? 'ok' : 'bad'}">${e(a.status)}</dd><dt>Runs checked</dt><dd>${count(a.runs_checked)}</dd><dt>Budget violations</dt><dd class="${cls(a.budget_violations === 0, a.budget_violations)}">${count(a.budget_violations)}</dd><dt>Hidden-truth fields in policy inputs</dt><dd class="${cls(hidden === 0, hidden)}">${count(hidden)}</dd><dt>Protected files preserved</dt><dd class="${cls(protectedOK, a.protected_files_preserved)}">${protectedValue}</dd></dl>`;
  }

  function renderInference() {
    const i = obj(state.data.inference);
    if (!i) { $('inferBody').innerHTML = empty('No inference timing yet', 'the inference field is missing'); return; }
    const ratio = finite(i.baseline_ms) && finite(i.compiled_ms) && i.compiled_ms > 0 ? i.baseline_ms / i.compiled_ms : null;
    $('inferBody').innerHTML = `<dl class="state-list"><dt>baseline</dt><dd>${fmt(i.baseline_ms, 2)} ms</dd><dt>compiled</dt><dd>${fmt(i.compiled_ms, 2)} ms</dd><dt>Speedup (computed from both values)</dt><dd>${ratio == null ? '—' : fmt(ratio, 1) + '×'}</dd></dl><p class="scope-note">Measurement scope: ${e(i.scope)}</p>`;
  }

  function renderLimitations() {
    const list = arr(obj(state.data.study)?.limitations).filter(x => x != null);
    $('limitationsList').innerHTML = list.length ? list.map(x => `<li>${e(x)}</li>`).join('') : '<li>No limitations were exported. Interpret the results within the synthetic study scope stated above.</li>';
  }

  // ---------- events ----------
  $('stepFirst').addEventListener('click', () => { stop(); setCursor(0); });
  $('stepPrev').addEventListener('click', () => { stop(); setCursor(state.cursor - 1); });
  $('stepNext').addEventListener('click', () => { stop(); setCursor(state.cursor + 1); });
  $('stepLast').addEventListener('click', () => { stop(); setCursor(state.rows.length); });
  $('stepPlay').addEventListener('click', play);
  $('stepRange').addEventListener('input', event => { stop(); setCursor(Number(event.target.value)); });
  $('obsRows').addEventListener('click', event => { const b = event.target.closest('[data-step]'); if (b) { stop(); setCursor(Number(b.dataset.step)); } });
  $('waferFilter').addEventListener('change', event => { state.wafer = event.target.value; renderRows(); });
  $('resultFilter').addEventListener('change', event => { state.result = event.target.value; renderRows(); });
  $('untilCursor').addEventListener('change', event => { state.until = event.target.checked; renderRows(); });
  $('showCandidates').addEventListener('change', event => { state.showCandidates = event.target.checked; renderReplay(); });
  $('variantSort').addEventListener('change', event => { state.variantSort = event.target.value; renderVariants(); });
  $('download').addEventListener('click', () => {
    if (!state.raw) return;
    const url = URL.createObjectURL(new Blob([state.raw], {type:'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = 'inspection-v3.json'; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  let resizeFrame = 0;
  window.addEventListener('resize', () => { cancelAnimationFrame(resizeFrame); resizeFrame = requestAnimationFrame(() => { if (state.data) renderReplay(); }); });
  load();
})();

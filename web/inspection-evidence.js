(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const DATA_URL = './data/inspection-v3.json';
  const DATA_FILE = 'web/data/inspection-v3.json';
  // Drawing fallback only, named in the UI when used; never reported as a result.
  const CONTRACT_GEOMETRY = Object.freeze({wafer_diameter_mm:300,die_width_mm:8,die_height_mm:6,scribe_mm:0.08,edge_exclusion_mm:3});
  const OUTCOME = {positive:'검토 양성',negative:'관측 음성',failed:'측정 실패·누락'};
  const STATUS = {failure:'측정 실패',missing:'측정 누락',ok:'정상'};
  const REWARD = {reported_review_positive:'보고된 검토 양성 확률',latent_doi_probability:'잠재 DOI 확률'};
  const PLAY_MS = 520;
  const state = {data:null,raw:'',rows:[],sites:[],wafers:[],geometry:null,geometryFallback:false,cursor:0,timer:null,
    wafer:'all',result:'all',until:true,showCandidates:false,variantSort:'doi'};

  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const arr = v => Array.isArray(v) ? v : [];
  const text = v => v == null || v === '' ? '—' : String(v);
  const e = v => text(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (n, d=2) => finite(n) ? n.toLocaleString('ko-KR', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
  const signed = (n, d=2) => finite(n) ? (n > 0 ? '+' : n < 0 ? '−' : '') + fmt(Math.abs(n), d) : '—';
  const pct = (n, d=1) => finite(n) ? signed(n * 100, d) + '%' : '—';
  const count = n => finite(n) ? n.toLocaleString('ko-KR') : '—';
  const yesNo = v => v === true ? '예' : v === false ? '아니오' : '—';
  const waferName = w => w == null ? '—' : typeof w === 'number' ? `W${w}` : String(w);
  const empty = (title, detail) => `<div class="empty"><strong>${e(title)}</strong>${detail} · 파일 <code>${DATA_FILE}</code></div>`;

  function outcome(row) {
    if (row.status != null && row.status !== 'ok') return 'failed';
    if (row.label === true) return 'positive';
    if (row.label === false) return 'negative';
    if (row.status === 'ok' && typeof row.reported_positive === 'boolean') return row.reported_positive ? 'positive' : 'negative';
    return 'failed';
  }
  const outcomeText = row => {
    const o = outcome(row);
    return o === 'failed' ? (STATUS[row.status] && row.status !== 'ok' ? STATUS[row.status] : '결과 없음') : OUTCOME[o];
  };
  const badge = row => `<span class="result ${outcome(row)}"><i aria-hidden="true"></i>${e(outcomeText(row))}</span>`;

  // ---------- loading ----------
  function showState(html) { const box = $('loadState'); box.hidden = false; box.innerHTML = html; }
  function retryButton() { const b = document.createElement('button'); b.type = 'button'; b.className = 'control'; b.textContent = '다시 불러오기'; b.addEventListener('click', load); $('loadState').append(b); }
  async function load() {
    stop(); $('app').hidden = true; $('download').disabled = true;
    showState('<div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div><p>저장된 검사 v3 근거를 불러오는 중…</p>');
    let response;
    try { response = await fetch(DATA_URL, {cache:'no-store'}); }
    catch (error) {
      console.error('Inspection v3 fetch:', error);
      showState(`<h2>근거 파일에 접근할 수 없습니다</h2><p>네트워크 또는 로컬 파일 열기 제한으로 <code>${DATA_FILE}</code> 파일을 읽지 못했습니다. 정적 서버로 <code>web/</code>을 연 뒤 다시 시도하세요.</p>`);
      return retryButton();
    }
    if (response.status === 404) {
      showState(`<h2>아직 검증된 결과가 없습니다</h2><p>검사 v3 근거가 아직 내보내지지 않았습니다. 코디네이터가 저장된 결과를 <code>${DATA_FILE}</code> 파일로 내보내면 이 화면이 채워집니다. 예시 수치나 임시 벤치마크는 표시하지 않습니다.</p>`);
      return retryButton();
    }
    try {
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const raw = await response.text();
      const data = JSON.parse(raw);
      if (!obj(data)) throw Error('최상위 값이 객체가 아닙니다');
      if (data.schema_version != null && data.schema_version !== 1) throw Error(`지원하지 않는 schema_version ${data.schema_version}`);
      state.raw = raw; state.data = data;
      prepareReplay(obj(data.replay));
      $('loadState').hidden = true; $('app').hidden = false; $('download').disabled = false;
      renderAll();
    } catch (error) {
      console.error('Inspection v3 parse:', error);
      showState(`<h2>근거 파일을 해석할 수 없습니다</h2><p>${e(error.message)}. <code>${DATA_FILE}</code>의 내보내기 형식을 확인한 뒤 다시 시도하세요.</p>`);
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
    $('waferFilter').innerHTML = '<option value="all">전체 웨이퍼</option>' + state.wafers.map(w => `<option value="${e(w)}">${e(waferName(w))}</option>`).join('');
    $('resultFilter').value = 'all';
  }

  function renderAll() {
    renderStudy(); renderPrimary(); renderReplayStatic(); renderReplay(); renderVariants(); renderClassification(); renderAudit(); renderInference(); renderLimitations();
  }

  // ---------- study ----------
  function renderStudy() {
    const s = obj(state.data.study) || {};
    let generated = text(s.generated_at);
    if (typeof s.generated_at === 'string' && !Number.isNaN(Date.parse(s.generated_at))) generated = new Date(s.generated_at).toLocaleString('ko-KR', {dateStyle:'medium', timeStyle:'short'});
    const items = [['연구', s.name], ['유형', s.kind], ['생성', generated], ['freeze SHA-256', s.freeze_sha256 ? String(s.freeze_sha256).slice(0, 12) + '…' : null, s.freeze_sha256], ['source commit', s.source_commit ? String(s.source_commit).slice(0, 10) : null, s.source_commit]];
    $('studyMeta').innerHTML = items.map(([k, v, full]) => `<div><dt>${e(k)}</dt><dd${full ? ` class="mono" title="${e(full)}"` : ''}>${e(v)}</dd></div>`).join('');
  }

  // ---------- primary ----------
  function verdictChip(success) {
    const met = success === true, missed = success === false;
    const icon = met ? '<path d="M3 8.5l3.2 3L13 4.5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
      : missed ? '<path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
      : '<path d="M4 8h8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>';
    return `<span class="verdict ${met ? 'met' : missed ? 'missed' : 'none'}"><svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">${icon}</svg>${met ? '사전 목표 달성' : missed ? '사전 목표 미달' : '판정 없음'}</span>`;
  }
  function niceTicks(lo, hi, n=5) {
    const span = hi - lo || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
    const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    return {ticks: out, step};
  }
  function ciChart(p) {
    const ci = arr(p.ci95), lo = ci[0], hi = ci[1], mid = p.mean_difference;
    if (!finite(lo) || !finite(hi)) return `<figure class="ci-figure">${empty('95% CI가 아직 없습니다', 'primary.ci95가 내보내지지 않았습니다')}</figure>`;
    const vals = [0, lo, hi].concat(finite(mid) ? [mid] : []);
    let min = Math.min(...vals), max = Math.max(...vals); const pad = (max - min || 1) * 0.14; min -= pad; max += pad;
    const {ticks, step} = niceTicks(min, max); const digits = Math.max(0, Math.min(3, -Math.floor(Math.log10(step)) + (step / 10 ** Math.floor(Math.log10(step)) === 2.5 ? 1 : 0)));
    const W = 560, L = 16, R = 544, x = v => L + (v - min) / (max - min) * (R - L), Y = 58;
    const tickSvg = ticks.map(t => `<line x1="${x(t)}" x2="${x(t)}" y1="96" y2="101" stroke="#b9c5bd"/><text x="${x(t)}" y="116" text-anchor="middle">${fmt(t, digits)}</text>`).join('');
    return `<figure class="ci-figure"><svg viewBox="0 0 ${W} 124" role="img" aria-label="평균 차이 ${signed(mid)}, 95% 신뢰구간 ${signed(lo)}에서 ${signed(hi)}. 0은 차이 없음.">
      <line x1="${L}" x2="${R}" y1="96" y2="96" stroke="#b9c5bd"/>${tickSvg}
      <line x1="${x(0)}" x2="${x(0)}" y1="18" y2="96" stroke="#596860" stroke-dasharray="3 3"/><text x="${x(0)}" y="12" text-anchor="middle">차이 0</text>
      <line x1="${x(lo)}" x2="${x(hi)}" y1="${Y}" y2="${Y}" stroke="#202b28" stroke-width="2"/>
      <line x1="${x(lo)}" x2="${x(lo)}" y1="${Y - 8}" y2="${Y + 8}" stroke="#202b28" stroke-width="2"/><line x1="${x(hi)}" x2="${x(hi)}" y1="${Y - 8}" y2="${Y + 8}" stroke="#202b28" stroke-width="2"/>
      <text x="${x(lo)}" y="${Y + 24}" text-anchor="middle">${signed(lo)}</text><text x="${x(hi)}" y="${Y + 24}" text-anchor="middle">${signed(hi)}</text>
      ${finite(mid) ? `<circle cx="${x(mid)}" cy="${Y}" r="7" fill="#276449" stroke="#fff" stroke-width="2"><title>평균 차이 ${signed(mid)}</title></circle><text class="ink" x="${x(mid)}" y="${Y - 14}" text-anchor="middle">${signed(mid)}</text>` : ''}
    </svg><figcaption>쌍별 평균 차이(후보 − 비교, 확인 DOI 개수)와 95% CI. 구간이 0선을 포함하면 차이가 불확실합니다.</figcaption></figure>`;
  }
  function renderPrimary() {
    const p = obj(state.data.primary);
    if (!p) { $('primaryVerdict').innerHTML = ''; $('primaryBody').innerHTML = empty('주 결과가 아직 검증되지 않았습니다', 'primary 필드가 내보내기에 없습니다'); return; }
    $('primaryVerdict').innerHTML = verdictChip(p.success);
    const ci = arr(p.ci95);
    const statement = `<code>${e(p.candidate)}</code>의 평균 확인 DOI는 <span class="num">${fmt(p.mean_candidate)}</span>, 비교 <code>${e(p.comparator)}</code>는 <span class="num">${fmt(p.mean_comparator)}</span>입니다.`;
    const sub = `동일 예산 ${fmt(p.budget, 0)} CU · ${e(p.mode)} · lot 쌍 ${count(p.pairs)}개의 paired bootstrap 결과입니다. 상대 이득 ${pct(p.relative_gain)}, 사전 목표 ${finite(p.target) ? fmt(p.target * 100, 1) + '%' : '—'}.`;
    const figures = [['후보 평균', fmt(p.mean_candidate)], ['비교 평균', fmt(p.mean_comparator)], ['평균 차이', signed(p.mean_difference)],
      ['95% CI', finite(ci[0]) && finite(ci[1]) ? `${signed(ci[0])} ~ ${signed(ci[1])}` : '—'], ['상대 이득', `${pct(p.relative_gain)} <small>목표 ${finite(p.target) ? fmt(p.target * 100, 1) + '%' : '—'}</small>`], ['lot 쌍', count(p.pairs)]];
    $('primaryBody').innerHTML = `<div class="primary-grid"><div><p class="statement">${statement}</p><p class="statement-sub">${sub}</p><dl class="figures">${figures.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl></div>${ciChart(p)}</div>`;
  }

  // ---------- replay ----------
  function renderReplayStatic() {
    const replay = obj(state.data.replay);
    const hasAny = state.rows.length || state.sites.length;
    $('replayEmpty').hidden = !!hasAny; $('replayContent').hidden = !hasAny;
    if (!hasAny) { $('replayEmpty').innerHTML = empty('측정 재생 기록이 아직 없습니다', 'replay.rows와 replay.sites가 내보내기에 없습니다'); $('replayMeta').textContent = '저장된 유료 측정 기록을 순서대로 다시 보여 줍니다.'; return; }
    $('replayMeta').textContent = `lot ${text(replay.lot_id)} · 변형 ${text(replay.variant)} · 예산 ${fmt(replay.budget, 0)} CU · 지출 ${fmt(replay.spent, 1)} CU · 유료 측정 ${count(state.rows.length)}건. 기록을 다시 보여 줄 뿐 연구 결과는 바뀌지 않습니다.`;
    const range = $('stepRange'); range.max = String(state.rows.length); range.value = '0'; range.disabled = !state.rows.length;
    const g = state.geometry, per = state.wafers.map(w => state.sites.filter(s => s.wafer === w).length);
    $('geometryNote').textContent = `⌀${fmt(g.wafer_diameter_mm, 0)} mm · 다이 ${fmt(g.die_width_mm, 0)} × ${fmt(g.die_height_mm, 0)} mm · 스크라이브 ${fmt(g.scribe_mm * 1000, 0)} μm · 가장자리 제외 ${fmt(g.edge_exclusion_mm, 0)} mm · 웨이퍼별 사이트 ${per.map(count).join(' / ') || '—'}개${state.geometryFallback ? ' · replay.geometry가 없어 계약 형상으로 그렸습니다' : ''}. 색 대신 채움·테두리·빗금으로도 결과를 구분합니다.`;
    $('pickNote').textContent = '';
    if (!state.sites.length) { $('wafers').innerHTML = empty('다이 좌표가 아직 없습니다', 'replay.sites가 없어 웨이퍼 지도를 그리지 않았습니다. 아래 측정 기록 표는 사용할 수 있습니다'); return; }
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
    $('stepRange').setAttribute('aria-valuetext', cur ? `단계 ${cur} / ${n}, ${text(row.site_id)}, ${outcomeText(row)}` : `재생 전, 총 ${n}단계`);
    $('stepFirst').disabled = $('stepPrev').disabled = cur === 0;
    $('stepNext').disabled = $('stepLast').disabled = cur >= n;
    $('stepPlay').disabled = !n;
    const spentNow = row && finite(row.cumulative_spend) ? row.cumulative_spend : 0;
    const width = finite(replay.budget) && replay.budget > 0 ? Math.min(100, spentNow / replay.budget * 100) : 0;
    $('spend').innerHTML = `누적 지출 <strong class="num">${fmt(spentNow, 1)}</strong> / 예산 ${fmt(replay.budget, 0)} CU<div class="bar" role="img" aria-label="예산 대비 누적 지출 ${fmt(width, 0)}%"><i style="width:${width}%"></i></div>`;
    const obs = observedMap();
    state.wafers.forEach((w, i) => {
      const values = [...obs.values()].filter(o => o.row.wafer === w);
      const pos = values.filter(o => o.outcome === 'positive').length, neg = values.filter(o => o.outcome === 'negative').length, fail = values.length - pos - neg;
      const label = $(`waferCount${i}`); if (label) label.textContent = `측정 ${values.length} · 양성 ${pos}`;
      const canvas = $('wafers').querySelector(`canvas[data-wafer-index="${i}"]`);
      if (canvas) { canvas.setAttribute('aria-label', `${waferName(w)} 웨이퍼 지도. 측정한 다이 ${values.length}개: 검토 양성 ${pos}, 관측 음성 ${neg}, 실패·누락 ${fail}. 나머지 다이는 측정하지 않아 결과를 모릅니다. 같은 내용을 아래 표로 확인할 수 있습니다.`); draw(canvas, w, obs, row); }
    });
    renderStepDetail(row, n);
    renderRows();
  }

  function renderStepDetail(row, n) {
    if (!row) { $('stepDetail').innerHTML = `<p class="hint">재생 전입니다. 다음 또는 재생을 누르면 저장된 ${count(n)}건의 유료 측정이 순서대로 나타납니다. 측정하지 않은 다이는 끝까지 결과를 모르는 상태로 남습니다.</p>`; return; }
    const semantics = REWARD[row.selection_reward_semantics] || text(row.selection_reward_semantics);
    $('stepDetail').innerHTML = `<p class="step-lead">단계 ${e(row.step)} ${badge(row)}</p><p class="mono">${e(row.site_id)}</p>
      <dl class="detail-list"><dt>웨이퍼</dt><dd>${e(waferName(row.wafer))}</dd><dt>측정 상태</dt><dd>${e(STATUS[row.status] || row.status)}</dd><dt>청구 비용</dt><dd>${fmt(row.charged, 1)} CU</dd><dt>누적 지출</dt><dd>${fmt(row.cumulative_spend, 1)} CU</dd><dt>baseline p</dt><dd>${fmt(row.baseline_p, 3)}</dd><dt>selection reward</dt><dd>${fmt(row.selection_reward, 3)}</dd><dt>reward 의미</dt><dd>${e(semantics)}</dd></dl>
      <p class="hint">선택 사유 <code>${e(row.reason)}</code>. baseline p는 고정된 DOI 분류기 점수이며 측정 결과가 아닙니다.</p>`;
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
    if (index < 0) { $('pickNote').textContent = `${site.id}: 이 기록에서 측정하지 않은 다이입니다. 결과를 알 수 없습니다.`; return; }
    $('pickNote').textContent = `${site.id}: 단계 ${state.rows[index].step ?? index + 1}로 이동했습니다.`;
    stop(); setCursor(index + 1);
  }

  function filteredRows() {
    return state.rows.map((row, index) => ({row, index})).filter(({row, index}) =>
      (!state.until || index < state.cursor) && (state.wafer === 'all' || String(row.wafer) === state.wafer) && (state.result === 'all' || outcome(row) === state.result));
  }
  function renderRows() {
    const list = filteredRows(), focused = document.activeElement?.dataset?.step;
    $('rowCount').textContent = `표시 ${count(list.length)} / ${count(state.rows.length)}건`;
    $('obsRows').innerHTML = list.map(({row, index}) => {
      const cls = index === state.cursor - 1 ? 'is-current' : index >= state.cursor ? 'is-future' : '';
      return `<tr class="${cls}"><th scope="row"><button type="button" class="step-link" data-step="${index + 1}" aria-label="단계 ${e(row.step ?? index + 1)}로 이동"${cls === 'is-current' ? ' aria-current="step"' : ''}>${e(row.step ?? index + 1)}</button></th><td class="site">${e(row.site_id)}</td><td>${e(waferName(row.wafer))}</td><td>${badge(row)}${index >= state.cursor ? ' <span class="footnote">미재생</span>' : ''}</td><td class="num">${fmt(row.charged, 1)}</td><td class="num">${fmt(row.cumulative_spend, 1)}</td><td class="num">${fmt(row.baseline_p, 3)}</td><td class="num" title="${e(REWARD[row.selection_reward_semantics] || row.selection_reward_semantics)}">${fmt(row.selection_reward, 3)}</td><td class="reason">${e(row.reason)}</td></tr>`;
    }).join('') || `<tr><td colspan="9">${state.until && state.cursor === 0 ? '아직 재생한 단계가 없습니다. 다음 또는 재생을 누르거나 “현재 단계까지만”을 해제하세요.' : '조건에 맞는 측정 기록이 없습니다. 웨이퍼·결과 필터를 바꿔 보세요.'}</td></tr>`;
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
    const b = $('stepPlay'); b.textContent = '재생'; b.setAttribute('aria-pressed', 'false'); $('stepDetail').setAttribute('aria-live', 'polite');
  }
  function play() {
    if (state.timer) return stop();
    if (state.cursor >= state.rows.length) state.cursor = 0;
    const b = $('stepPlay'); b.textContent = '일시정지'; b.setAttribute('aria-pressed', 'true'); $('stepDetail').setAttribute('aria-live', 'off');
    state.timer = setInterval(() => { if (state.cursor >= state.rows.length) return stop(); setCursor(state.cursor + 1); if (state.cursor >= state.rows.length) stop(); }, PLAY_MS);
    setCursor(state.cursor + (state.cursor === 0 ? 1 : 0));
  }

  // ---------- secondary sections ----------
  function renderVariants() {
    const all = arr(state.data.variants).filter(obj), p = obj(state.data.primary) || {};
    $('variantSort').disabled = !all.length;
    if (!all.length) { $('variantsBody').innerHTML = empty('변형 비교가 아직 없습니다', 'variants 배열이 비어 있거나 없습니다'); return; }
    const sorted = [...all].sort(state.variantSort === 'id' ? (a, b) => String(a.id).localeCompare(String(b.id))
      : state.variantSort === 'spent' ? (a, b) => (finite(a.mean_spent) ? a.mean_spent : Infinity) - (finite(b.mean_spent) ? b.mean_spent : Infinity)
      : (a, b) => (finite(b.mean_doi) ? b.mean_doi : -Infinity) - (finite(a.mean_doi) ? a.mean_doi : -Infinity));
    const role = v => v.id === p.candidate ? '<span class="role">후보</span>' : v.id === p.comparator ? '<span class="role comparator">비교</span>' : '';
    const values = all.map(v => v.mean_doi).filter(finite), top = values.length ? Math.max(...values) : 0;
    const {ticks} = niceTicks(0, top > 0 ? top : 1, 4);
    const tickStep = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
    const axisMax = Math.max(1, Math.ceil(top / tickStep) * tickStep);
    const dots = sorted.map(v => `<li class="${v.id === p.candidate ? 'is-candidate' : v.id === p.comparator ? 'is-comparator' : ''}" title="${e(v.id)} · 평균 DOI ${fmt(v.mean_doi)}"><span class="name">${e(v.id)}${role(v)}</span><span class="track">${finite(v.mean_doi) ? `<i style="left:${Math.max(0, v.mean_doi / axisMax * 100)}%"></i>` : ''}</span><span class="val">${fmt(v.mean_doi)}</span></li>`).join('');
    const rows = sorted.map(v => `<tr><th scope="row">${e(v.id)}${role(v)}</th><td class="num">${fmt(v.mean_doi)}</td><td class="num">${fmt(v.mean_spent, 1)}</td><td class="num">${fmt(v.mean_policy_wall_s, 4)}</td></tr>`).join('');
    $('variantsBody').innerHTML = `<div class="variant-grid"><figure class="dot-chart" aria-label="변형별 평균 확인 DOI 점 그래프"><ol class="dots">${dots}</ol><div class="dots-axis" aria-hidden="true"><span></span><span><span>0</span><span>${fmt(axisMax, axisMax < 10 ? 1 : 0)}</span></span><span></span></div><figcaption>평균 확인 DOI · 초록 원은 후보, 사각형은 비교 정책</figcaption></figure>
      <div class="table-scroll" role="region" tabindex="0" aria-label="정책 변형 표"><table class="data-table"><caption>모든 변형 · 사후 교체 없음 · 정책 시간은 lot 전체 선택 호출의 합계</caption><thead><tr><th scope="col">변형</th><th scope="col" class="num">평균 DOI</th><th scope="col" class="num">평균 지출 CU</th><th scope="col" class="num">평균 정책 s/lot</th></tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }

  function renderClassification() {
    const list = arr(state.data.classification).filter(obj);
    if (!list.length) { $('classBody').innerHTML = empty('분류 지표가 아직 없습니다', 'classification 배열이 비어 있거나 없습니다'); return; }
    $('classBody').innerHTML = `<div class="table-scroll" role="region" tabindex="0" aria-label="분류 성능 표"><table class="data-table class-table"><caption>held-out 분류 지표 · 확인 DOI(운영 발견)와 다른 결과입니다</caption><thead><tr><th scope="col">모델</th><th scope="col" class="num">precision</th><th scope="col" class="num">recall</th><th scope="col" class="num">AP</th><th scope="col" class="num">Brier</th><th scope="col" class="num">ECE</th><th scope="col" class="num grp">TP</th><th scope="col" class="num">오탐 FP</th><th scope="col" class="num">미탐 FN</th><th scope="col" class="num">TN</th></tr></thead><tbody>${
      list.map(c => `<tr><th scope="row">${e(c.id)}</th><td class="num">${fmt(c.precision, 3)}</td><td class="num">${fmt(c.recall, 3)}</td><td class="num">${fmt(c.average_precision, 3)}</td><td class="num">${fmt(c.brier, 4)}</td><td class="num">${fmt(c.ece, 4)}</td><td class="num grp">${count(c.tp)}</td><td class="num">${count(c.fp)}</td><td class="num">${count(c.fn)}</td><td class="num">${count(c.tn)}</td></tr>`).join('')}</tbody></table></div>`;
  }

  function renderAudit() {
    const a = obj(state.data.audit);
    if (!a) { $('auditBody').innerHTML = empty('감사 결과가 아직 없습니다', 'audit 필드가 없습니다'); return; }
    const okStatus = ['pass', 'passed', 'ok', 'complete'].includes(String(a.status).toLowerCase());
    const hidden = Array.isArray(a.hidden_truth_fields_in_components) ? a.hidden_truth_fields_in_components.length : a.hidden_truth_fields_in_components;
    const cls = (good, v) => v == null ? '' : good ? 'ok' : 'bad';
    const protectedValue = finite(a.protected_files_preserved) ? `${count(a.protected_files_preserved)}개 파일` : yesNo(a.protected_files_preserved);
    const protectedOK = a.protected_files_preserved === true || (finite(a.protected_files_preserved) && a.protected_files_preserved > 0);
    $('auditBody').innerHTML = `<dl class="state-list"><dt>상태</dt><dd class="${a.status == null ? '' : okStatus ? 'ok' : 'bad'}">${e(a.status)}</dd><dt>검사한 run</dt><dd>${count(a.runs_checked)}</dd><dt>예산 위반</dt><dd class="${cls(a.budget_violations === 0, a.budget_violations)}">${count(a.budget_violations)}</dd><dt>정책 입력의 숨은 정답 필드</dt><dd class="${cls(hidden === 0, hidden)}">${count(hidden)}</dd><dt>보호 파일 보존</dt><dd class="${cls(protectedOK, a.protected_files_preserved)}">${protectedValue}</dd></dl>`;
  }

  function renderInference() {
    const i = obj(state.data.inference);
    if (!i) { $('inferBody').innerHTML = empty('추론 시간이 아직 없습니다', 'inference 필드가 없습니다'); return; }
    const ratio = finite(i.baseline_ms) && finite(i.compiled_ms) && i.compiled_ms > 0 ? i.baseline_ms / i.compiled_ms : null;
    $('inferBody').innerHTML = `<dl class="state-list"><dt>baseline</dt><dd>${fmt(i.baseline_ms, 2)} ms</dd><dt>compiled</dt><dd>${fmt(i.compiled_ms, 2)} ms</dd><dt>배율 (두 값에서 계산)</dt><dd>${ratio == null ? '—' : fmt(ratio, 1) + '×'}</dd></dl><p class="scope-note">측정 범위: ${e(i.scope)}</p>`;
  }

  function renderLimitations() {
    const list = arr(obj(state.data.study)?.limitations).filter(x => x != null);
    $('limitationsList').innerHTML = list.length ? list.map(x => `<li>${e(x)}</li>`).join('') : '<li>내보낸 한계 항목이 없습니다. 위의 합성 연구 범위 안내를 기준으로 해석하세요.</li>';
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

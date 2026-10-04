// Recorded replay of one actual Omnigent run (web/data/inspection-live.json).
// Read-only: there is no run button. Every value is read from the file; missing
// values render "—" rather than a fabricated number.
(() => {
  'use strict';
  const DATA_URL = './data/inspection-live.json';
  const DATA_PATH = 'web/data/inspection-live.json';
  const KIND = 'inspection_live_recorded_evidence';
  const SCOPE = 'actual_omnigent_authored_numeric_synthetic';
  const $ = id => document.getElementById(id);
  const state = {bytes:null, data:null, stages:[], stage:0, sites:new Map(), geometry:null, canvases:[], observer:null};

  // ---------- null-safe helpers ----------
  const finite = v => typeof v === 'number' && Number.isFinite(v);
  const arr = v => Array.isArray(v) ? v : [];
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const e = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (v, d = 2) => finite(v) ? v.toLocaleString('ko-KR', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
  const count = v => finite(v) ? Math.round(v).toLocaleString('ko-KR') : '—';
  const cu = (v, d = 4) => finite(v) ? `${fmt(v, d)} CU` : '—';
  const shortHash = h => typeof h === 'string' && h.length > 16 ? `${h.slice(0, 10)}…${h.slice(-6)}` : (h ?? '—');
  const siteShort = id => typeof id === 'string' ? id.replace(/^lot-[0-9a-f]+:/, '') : '—';
  const near = (a, b) => finite(a) && finite(b) && Math.abs(a - b) < 1e-6;
  const time = iso => { const t = Date.parse(iso); return Number.isFinite(t) ? new Date(t).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC') : '—'; };
  const okMark = ok => ok == null ? '<span>—</span>' : ok ? '<span class="ok">일치</span>' : '<span class="bad">불일치</span>';

  // ---------- load ----------
  function setLoad(html, retry) {
    const ls = $('loadState'); ls.hidden = false; ls.innerHTML = html;
    if (retry) { const b = document.createElement('button'); b.type = 'button'; b.className = 'control'; b.textContent = '다시 불러오기'; b.addEventListener('click', load); ls.append(b); }
  }
  async function load() {
    $('app').hidden = true; $('download').disabled = true; state.bytes = null;
    setLoad('<div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div><p>저장된 실행 기록을 불러오는 중…</p>');
    let response;
    try { response = await fetch(DATA_URL, {cache:'no-store'}); }
    catch (error) {
      console.error('Inspection live fetch:', error);
      return setLoad(`<h2>기록을 불러오지 못했습니다</h2><p>네트워크 오류로 <code>${e(DATA_PATH)}</code>를 읽지 못했습니다. 연결을 확인한 뒤 다시 시도하세요.</p>`, true);
    }
    if (response.status === 404) {
      return setLoad(`<h2>아직 검증된 기록이 없습니다</h2><p><code>${e(DATA_PATH)}</code> 파일이 없습니다 (HTTP 404). 실행 기록이 아직 내보내지지 않았거나 검증되지 않은 상태입니다. 이 화면은 수치를 지어내지 않습니다.</p>`, true);
    }
    if (!response.ok) return setLoad(`<h2>기록을 불러오지 못했습니다</h2><p><code>${e(DATA_PATH)}</code> 요청이 HTTP ${e(response.status)}로 실패했습니다.</p>`, true);
    let data;
    try {
      state.bytes = await response.arrayBuffer();
      data = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(state.bytes));
    } catch (error) {
      console.error('Inspection live parse:', error);
      state.bytes = null;
      return setLoad(`<h2>기록을 해석하지 못했습니다</h2><p><code>${e(DATA_PATH)}</code>가 올바른 UTF-8 JSON이 아닙니다. 파일이 갱신되는 중일 수 있으니 다시 시도하세요.</p>`, true);
    }
    const d = obj(data);
    if (!d || d.schema_version !== 1 || d.kind !== KIND || d.scope !== SCOPE) {
      state.bytes = null;
      return setLoad(`<h2>지원하지 않는 기록 형식입니다</h2><p><code>${e(DATA_PATH)}</code>에 <code>schema_version: 1</code>과 <code>kind: ${e(KIND)}</code>가 필요합니다. 받은 값: schema_version ${e(d?.schema_version ?? '없음')}, kind ${e(d?.kind ?? '없음')}.</p>`, true);
    }
    state.data = d;
    try { render(); }
    catch (error) {
      console.error('Inspection live render:', error);
      return setLoad(`<h2>기록을 표시하지 못했습니다</h2><p><code>${e(DATA_PATH)}</code>의 구조가 예상과 다릅니다.</p>`, true);
    }
    $('loadState').hidden = true; $('app').hidden = false; $('download').disabled = false;
    drawAll();
  }

  // ---------- derive stages from events ----------
  function buildStages(d) {
    const events = arr(d.events).filter(obj).slice().sort((a, b) => (finite(a.index) ? a.index : 0) - (finite(b.index) ? b.index : 0));
    const stages = []; let pendingUpdate = null; let reviewN = 0;
    for (const ev of events) {
      const p = obj(ev.payload) || {};
      if (ev.type === 'decision') {
        stages.push({kind:'plan', title: stages.length === 0 ? '초기 계획' : '다음 계획', update:pendingUpdate, decision:ev, events: pendingUpdate ? [pendingUpdate, ev] : [ev]});
        pendingUpdate = null;
      } else if (ev.type === 'admission') {
        reviewN += 1;
        stages.push({kind:'review', title: reviewN === 1 ? '첫 검토' : reviewN === 2 ? '두 번째 검토' : `${reviewN}번째 검토`, admission:ev, observation:null, seq:p.decision_sequence, events:[ev]});
      } else if (ev.type === 'observation') {
        const st = [...stages].reverse().find(s => s.kind === 'review' && s.seq === p.decision_sequence && !s.observation);
        if (st) { st.observation = ev; st.events.push(ev); }
        else stages.push({kind:'review', title:'검토', admission:null, observation:ev, seq:p.decision_sequence, events:[ev]});
      } else if (ev.type === 'analysis_update') {
        pendingUpdate = ev;
      } else if (ev.type === 'closed') {
        stages.push({kind:'final', title:'최종 갱신', update:pendingUpdate, closed:ev, events: pendingUpdate ? [pendingUpdate, ev] : [ev]});
        pendingUpdate = null;
      }
    }
    if (pendingUpdate) stages.push({kind:'final', title:'분석 갱신 (마감 기록 없음)', update:pendingUpdate, closed:null, events:[pendingUpdate]});
    const limit = budgetLimit(d);
    let lastSpent = 0;
    stages.forEach((s, i) => {
      s.lastIndex = Math.max(...s.events.map(x => finite(x.index) ? x.index : -1));
      s.at = s.events[s.events.length - 1]?.at;
      s.delegation = arr(d.orchestration?.delegations)[i] || null;
      s.toolCall = arr(d.orchestration?.specialist_tool_calls).find(c => c?.delegation === i + 1) || null;
      // Budget as stated by this stage's own events.
      let spent = null, remaining = null;
      if (s.kind === 'plan') { remaining = s.decision.payload?.remaining_budget; if (finite(remaining) && finite(limit)) spent = limit - remaining; }
      if (s.kind === 'review') { spent = s.observation?.payload?.cumulative_spend; if (finite(spent) && finite(limit)) remaining = limit - spent; }
      if (s.kind === 'final') { remaining = s.update?.payload?.remaining_budget; spent = s.closed?.payload?.spent; if (!finite(spent) && finite(remaining) && finite(limit)) spent = limit - remaining; }
      s.spent = finite(spent) ? spent : null; s.remaining = finite(remaining) ? remaining : null;
      s.budgetAgrees = finite(limit) && finite(s.spent) && finite(s.remaining) ? near(s.spent + s.remaining, limit) : null;
      if (finite(s.spent)) { s.monotone = s.spent + 1e-9 >= lastSpent; lastSpent = s.spent; }
    });
    return stages;
  }
  const budgetLimit = d => finite(d.core?.budget?.limit) ? d.core.budget.limit : finite(d.verification?.budget_cu) ? d.verification.budget_cu : null;
  const observationsUpTo = (d, lastIndex) => arr(d.events).filter(ev => ev?.type === 'observation' && finite(ev.index) && ev.index <= lastIndex).map(ev => ev.payload).filter(obj);

  // ---------- render ----------
  function render() {
    const d = state.data;
    state.geometry = obj(d.geometry);
    state.sites = new Map(arr(d.sites).filter(s => obj(s) && s.id != null && finite(s.x_mm) && finite(s.y_mm) && finite(s.wafer)).map(s => [String(s.id), s]));
    state.stages = buildStages(d);
    state.stage = 0;
    renderMeta(d); renderSummary(d); renderRoles(d); renderChain(d); renderReceipt(d); renderLimits(d);
    setupReplay();
  }

  function renderMeta(d) {
    const c = obj(d.core) || {};
    const items = [['lot', c.lot_id], ['사이트', count(c.n_sites ?? state.sites.size)], ['정책', c.policy], ['변형', c.variant], ['상태', d.status], ['범위', d.scope]];
    $('studyMeta').innerHTML = items.map(([k, v]) => `<div><dt>${e(k)}</dt><dd>${e(v ?? '—')}</dd></div>`).join('');
    const warn = [];
    if (d.scope !== SCOPE) warn.push(`scope가 ${SCOPE}가 아닙니다 (받은 값: ${d.scope ?? '없음'}).`);
    if (d.verification?.primary_benchmark === true) warn.push('verification.primary_benchmark가 true로 기록되어 있습니다. 이 화면은 별도 시연 기록으로만 해석합니다.');
    if (d.verification && d.verification.status !== 'passed') warn.push(`기록된 검증 상태가 passed가 아닙니다 (${d.verification.status ?? '없음'}).`);
    $('scopeWarn').hidden = !warn.length; $('scopeWarn').textContent = warn.join(' ');
  }

  function renderSummary(d) {
    const v = obj(d.verification) || {}, c = obj(d.core) || {};
    const delegations = arr(d.orchestration?.delegations).length;
    const events = arr(d.events);
    const reviews = events.filter(x => x?.type === 'observation').length;
    const updates = events.filter(x => x?.type === 'analysis_update').length;
    const charged = events.filter(x => x?.type === 'observation').reduce((s, x) => s + (finite(x.payload?.charged) ? x.payload.charged : NaN), 0);
    const limit = budgetLimit(d);
    const closed = obj(c.closed);
    const chk = (a, b, nearMode) => (finite(a) && finite(b)) ? (nearMode ? near(a, b) : a === b) : null;
    $('summaryBody').innerHTML = `
      <dl class="figures">
        <div><dt>SDK 위임</dt><dd class="num">${count(delegations)}<small> 회</small></dd></div>
        <div><dt>유료 센서 검토</dt><dd class="num">${count(reviews)}<small> 회</small></dd></div>
        <div><dt>분석 갱신</dt><dd class="num">${count(updates)}<small> 회</small></dd></div>
        <div><dt>청구 합계 / 예산</dt><dd class="num">${fmt(charged, 4)}<small> / ${fmt(limit, 0)} CU</small></dd></div>
      </dl>
      <p class="check-line">기록된 검증 요약과 비교 — 위임 ${okMark(chk(delegations, v.delegations))} · 검토 ${okMark(chk(reviews, v.paid_reviews))} · 갱신 ${okMark(chk(updates, v.analysis_updates))} · 청구 ${okMark(chk(charged, v.charged_cu, true))} · 예산 ${okMark(chk(limit, v.budget_cu, true))}. 검토 상한 ${count(c.max_reviews)}회 중 ${count(c.reviews_executed)}회 실행.</p>
      ${closed ? `<p class="check-line">마감 요약<span class="final-tag">최종</span> 상태 <strong>${e(closed.status ?? '—')}</strong> · 검토 ${count(closed.reviews_executed)}회 · 갱신 ${count(closed.updates)}회 · 사용 ${cu(closed.spent)} · 남은 예산 ${cu(c.budget?.remaining)}. 이 값은 실행이 끝난 뒤의 값이며, 아래 재생의 각 단계 예산은 그 단계 이벤트에서 따로 읽습니다.</p>` : '<p class="check-line">core.closed 마감 요약이 없습니다.</p>'}`;
  }

  function renderRoles(d) {
    const p0 = obj(arr(d.events).find(x => x?.type === 'decision')?.payload) || {};
    const sp = obj(p0.score_parts) || {};
    const sem = arr(d.events).find(x => x?.type === 'observation')?.payload?.selection_reward_semantics;
    $('rolesBody').innerHTML = `
      <dl class="roles">
        <div><dt>LLM 에이전트 (조정)</dt><dd>Omnigent 코디네이터가 SDK로 <code>inspection_analyst</code>와 <code>inspection_experimenter</code>에게 작업을 위임합니다. 분석가는 계획 도구를 호출하고, 실험자는 유료 검토 도구를 호출합니다. 어느 사이트를 볼지는 정하지 않습니다.</dd></div>
        <div><dt>동결된 계획기 (선택)</dt><dd>정책 <code>${e(d.core?.policy ?? '—')}</code>가 고정된 확률(<code>reward_source: ${e(sp.reward_source ?? '—')}</code>)과 예약 비용으로 다음 사이트를 고릅니다. 확률은 실행 중 온라인으로 학습되거나 갱신되지 않습니다.</dd></div>
        <div><dt>합성 센서 (응답)</dt><dd>검토 결과(보고된 결함 종류, 품질, 청구 CU)는 작성된 수치 합성 응답입니다. 보고 결과는 실제 결함 여부의 정답이 아닙니다.</dd></div>
      </dl>
      <div class="rule"><p>계획기 보상은 <strong>잠재 DOI 확률</strong>${sem ? ` (<code>selection_reward_semantics: ${e(sem)}</code>)` : ''}입니다. 표에 함께 나오는 <code>reported_yield_score</code>는 진단용 수치일 뿐 실제 보상이 아닙니다.</p>
      ${sp.rule ? `<p style="margin-top:6px">기록된 규칙: <code>${e(sp.rule)}</code></p>` : ''}</div>`;
  }

  // ---------- replay ----------
  function setupReplay() {
    const n = state.stages.length;
    const range = $('stageRange');
    range.max = String(Math.max(1, n)); range.disabled = n < 2;
    $('stageList').innerHTML = state.stages.map((s, i) => `<li><button type="button" data-stage="${i}"><strong>${i + 1}. ${e(s.title)}</strong><small>${e(time(s.at))}</small></button></li>`).join('');
    $('stageList').onclick = ev => { const b = ev.target.closest('button[data-stage]'); if (b) go(Number(b.dataset.stage)); };
    $('firstBtn').onclick = () => go(0);
    $('prevBtn').onclick = () => go(state.stage - 1);
    $('nextBtn').onclick = () => go(state.stage + 1);
    $('lastBtn').onclick = () => go(n - 1);
    range.oninput = () => go(Number(range.value) - 1);
    // Wafer canvases, one per wafer present in the site list.
    const wafers = [...new Set([...state.sites.values()].map(s => s.wafer))].sort((a, b) => a - b);
    $('wafers').innerHTML = wafers.map(w => `<figure class="wafer"><figcaption><strong>웨이퍼 ${e(w)}</strong><span id="wcap${e(w)}">—</span></figcaption><canvas data-wafer="${e(w)}" role="img" aria-label="웨이퍼 ${e(w)} 지도"></canvas></figure>`).join('') || '<p class="empty"><strong>사이트 좌표가 없습니다</strong>sites 배열이 비어 있어 지도를 그릴 수 없습니다.</p>';
    state.canvases = [...$('wafers').querySelectorAll('canvas')];
    const g = state.geometry;
    $('geometryNote').textContent = g ? `공개 형상만 사용: ⌀${fmt(g.wafer_diameter_mm, 0)} mm · 다이 ${fmt(g.die_width_mm, 0)} × ${fmt(g.die_height_mm, 0)} mm · 가장자리 제외 ${fmt(g.edge_exclusion_mm, 0)} mm. 각 단계에서 아직 검토되지 않은 사이트는 모두 '알 수 없음'으로 그리며, 뒤 단계의 관측은 미리 보이지 않습니다. 색 외에 채움·테두리·점선으로도 구분합니다.` : 'geometry가 없어 지도를 그릴 수 없습니다.';
    if (n === 0) {
      $('stageDetail').innerHTML = '<div class="empty"><strong>재생할 이벤트가 없습니다</strong><code>events</code>에 decision·admission·observation·analysis_update·closed 이벤트가 없습니다.</div>';
      ['firstBtn', 'prevBtn', 'nextBtn', 'lastBtn'].forEach(id => { $(id).disabled = true; });
      return;
    }
    state.observer?.disconnect();
    if (window.ResizeObserver) { state.observer = new ResizeObserver(() => drawAll()); state.observer.observe($('wafers')); }
    go(0, true);
  }

  function go(i, silent) {
    const n = state.stages.length; if (!n) return;
    state.stage = Math.min(n - 1, Math.max(0, Number.isFinite(i) ? i : 0));
    const s = state.stages[state.stage];
    $('stageRange').value = String(state.stage + 1);
    $('stageOut').textContent = `${state.stage + 1} / ${n} · ${s.title}`;
    $('firstBtn').disabled = $('prevBtn').disabled = state.stage === 0;
    $('nextBtn').disabled = $('lastBtn').disabled = state.stage === n - 1;
    $('stageList').querySelectorAll('button').forEach((b, k) => { if (k === state.stage) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current'); b.classList.toggle('is-future', k > state.stage); });
    if (!silent) $('stageAnnounce').textContent = `${state.stage + 1}단계 ${s.title}. 사용 ${fmt(s.spent, 4)} CU, 남은 예산 ${fmt(s.remaining, 4)} CU.`;
    renderBudget(s); renderDetail(s); renderStageTables(s); renderSiteTable(s); drawAll();
  }

  function renderBudget(s) {
    const limit = budgetLimit(state.data);
    const pct = finite(s.spent) && finite(limit) && limit > 0 ? Math.min(100, Math.max(0, s.spent / limit * 100)) : 0;
    $('stageBudget').innerHTML = `이 단계 이벤트 기준 사용 <strong class="num">${fmt(s.spent, 4)}</strong> / ${fmt(limit, 0)} CU · 남은 예산 <strong class="num">${fmt(s.remaining, 4)}</strong> CU · 사용+남음=예산 ${okMark(s.budgetAgrees)}${s.monotone === false ? ' · <span class="bad">사용액이 이전 단계보다 줄었습니다</span>' : ''}
      <div class="bar" role="img" aria-label="예산 ${fmt(pct, 1)}% 사용"><i style="width:${pct}%"></i></div>`;
  }

  // Map state for a stage: measured (observations up to this stage) + this stage's highlighted sites.
  function stageMarks(s) {
    const marks = new Map();
    const obs = observationsUpTo(state.data, s.lastIndex);
    for (const o of obs) marks.set(String(o.site_id), {kind: o.status !== 'ok' ? 'failed' : o.reported_doi === true ? 'positive' : o.reported_doi === false ? 'negative' : 'failed', obs:o});
    const hl = new Map();
    if (s.kind === 'plan') for (const c of arr(s.decision.payload?.candidates)) if (c?.site_id) hl.set(String(c.site_id), c.site_id === s.decision.payload.selected_site_id ? 'selected' : 'alt');
    if (s.kind === 'review') { const id = s.admission?.payload?.site_id ?? s.observation?.payload?.site_id; if (id) hl.set(String(id), 'selected'); }
    if (s.kind === 'final') arr(s.update?.payload?.next_candidate_site_ids).forEach(id => hl.set(String(id), id === s.update.payload.next_selected_site_id ? 'selected' : 'alt'));
    return {marks, hl};
  }

  function drawAll() {
    const s = state.stages[state.stage]; const g = state.geometry;
    if (!s || !g || !finite(g.wafer_diameter_mm)) return;
    const {marks, hl} = stageMarks(s);
    for (const cv of state.canvases) {
      const w = Number(cv.dataset.wafer);
      const css = cv.clientWidth || 300, dpr = Math.min(3, window.devicePixelRatio || 1);
      if (cv.width !== Math.round(css * dpr)) { cv.width = Math.round(css * dpr); cv.height = Math.round(css * dpr); }
      const ctx = cv.getContext('2d'); if (!ctx) continue;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, css, css);
      const size = css, k = size / g.wafer_diameter_mm, c = size / 2;
      const dw = (finite(g.die_width_mm) ? g.die_width_mm : 8) * k, dh = (finite(g.die_height_mm) ? g.die_height_mm : 6) * k;
      ctx.save(); ctx.beginPath(); ctx.arc(c, c, c, 0, Math.PI * 2); const iridescence = ctx.createLinearGradient(0, 0, css, css);
      for (const [at, color] of [[0,'#e7e0f1'],[.23,'#e2eff3'],[.46,'#e7efdf'],[.7,'#f2e8d8'],[1,'#e8e0f0']]) iridescence.addColorStop(at, color);
      ctx.fillStyle = iridescence; ctx.fill(); ctx.clip();
      let measured = 0, total = 0;
      for (const site of state.sites.values()) {
        if (site.wafer !== w) continue; total++;
        const x = c + site.x_mm * k - dw / 2 + .5, y = c + site.y_mm * k - dh / 2 + .5, ww = Math.max(1, dw - 1), hh = Math.max(1, dh - 1);
        const m = marks.get(String(site.id));
        if (m) {
          measured++;
          if (m.kind === 'positive') { ctx.fillStyle = '#963f34'; ctx.fillRect(x, y, ww, hh); }
          else if (m.kind === 'negative') { ctx.fillStyle = '#eef5ee'; ctx.fillRect(x, y, ww, hh); ctx.strokeStyle = '#276449'; ctx.lineWidth = 1.5; ctx.strokeRect(x + .75, y + .75, ww - 1.5, hh - 1.5); }
          else { ctx.fillStyle = '#f5ecd6'; ctx.fillRect(x, y, ww, hh); ctx.strokeStyle = '#916414'; ctx.lineWidth = 1; ctx.strokeRect(x, y, ww, hh); }
        } else {
          ctx.fillStyle = '#536d6018'; ctx.fillRect(x, y, ww, hh);
          if (site.candidate === true) { ctx.strokeStyle = '#7d8f86'; ctx.lineWidth = 1; ctx.strokeRect(x + .5, y + .5, ww - 1, hh - 1); }
        }
      }
      ctx.restore();
      if (finite(g.edge_exclusion_mm)) { ctx.beginPath(); ctx.arc(c, c, (g.wafer_diameter_mm / 2 - g.edge_exclusion_mm) * k, 0, Math.PI * 2); ctx.setLineDash([3, 4]); ctx.strokeStyle = '#8c9c93'; ctx.lineWidth = 1; ctx.stroke(); ctx.setLineDash([]); }
      ctx.beginPath(); ctx.arc(c, c, c - 1, 0, Math.PI * 2); ctx.strokeStyle = '#adbbb3'; ctx.lineWidth = 2; ctx.stroke();
      ctx.beginPath(); ctx.arc(c, size, 6, Math.PI, 0); ctx.fillStyle = '#f3f5f0'; ctx.fill(); ctx.strokeStyle = '#778b83'; ctx.lineWidth = 1; ctx.stroke();
      // Highlights drawn last, outside the clip, so they stay visible at the edge.
      for (const [id, role] of hl) {
        const site = state.sites.get(id); if (!site || site.wafer !== w) continue;
        const x = c + site.x_mm * k, y = c + site.y_mm * k, r = Math.max(7, dw * 1.1);
        ctx.strokeStyle = '#202b28';
        if (role === 'selected') { ctx.lineWidth = 2.5; ctx.setLineDash([]); ctx.strokeRect(x - r, y - r * .8, r * 2, r * 1.6); }
        else { ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]); ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]); }
      }
      const cap = $(`wcap${w}`); if (cap) cap.textContent = `검토 ${count(measured)} · 미측정 ${count(total - measured)}`;
      cv.setAttribute('aria-label', `웨이퍼 ${w} 지도: 사이트 ${total}개 중 이 단계까지 검토 ${measured}개, 나머지는 알 수 없음. 같은 내용이 아래 표에 있습니다.`);
    }
  }

  function renderSiteTable(s) {
    const {marks, hl} = stageMarks(s);
    const ids = [...new Set([...hl.keys(), ...marks.keys()])];
    const label = id => {
      const parts = [];
      const m = marks.get(id);
      if (m) parts.push(m.kind === 'positive' ? `검토됨: 센서가 DOI(${m.obs.reported_kind ?? '종류 없음'})로 보고` : m.kind === 'negative' ? '검토됨: 센서가 DOI 아님으로 보고' : `검토 실패 (${m.obs.status ?? '—'})`);
      const h = hl.get(id);
      if (h === 'selected') parts.push(s.kind === 'review' ? '이 단계에서 검토' : s.kind === 'final' ? '다음 선택 (기록되지 않음·마감)' : '이 단계의 선택');
      if (h === 'alt') parts.push(s.kind === 'final' ? '다음 후보 (실행 안 됨)' : '이 단계의 대안');
      if (!m) parts.push('아직 미측정');
      return parts.join(' · ');
    };
    $('siteTable').querySelector('tbody').innerHTML = ids.length ? ids.map(id => {
      const site = state.sites.get(id);
      return `<tr class="${hl.get(id) === 'selected' ? 'is-selected' : ''}"><td class="site">${e(siteShort(id))}</td><td>${e(site?.wafer ?? '—')}</td><td class="num">${fmt(site?.x_mm, 2)}</td><td class="num">${fmt(site?.y_mm, 2)}</td><td>${e(label(id))}</td></tr>`;
    }).join('') : '<tr><td colspan="5">표시할 사이트가 없습니다.</td></tr>';
  }

  function delegationText(s) {
    const dl = s.delegation, tc = s.toolCall;
    if (!dl && !tc) return '<p class="hint">이 단계와 짝지은 SDK 위임 기록이 없습니다.</p>';
    return `<dl class="detail-list">
      <dt>SDK 위임 대상</dt><dd>${e(dl?.agent ?? '—')}</dd>
      <dt>호출한 도구</dt><dd>${e(tc?.tool ?? '—')}${tc?.arguments && 'finalize' in tc.arguments ? ` (finalize ${e(tc.arguments.finalize)})` : ''}</dd>
      <dt>도구 출력 상태</dt><dd>${e(tc?.output_status ?? '—')}</dd>
      <dt>세션 재사용</dt><dd>${dl?.reuses_child_of_delegation != null ? `위임 ${e(dl.reuses_child_of_delegation)}의 세션` : '새 세션'}</dd>
    </dl>`;
  }

  function renderDetail(s) {
    const p = s.kind === 'plan' ? obj(s.decision.payload) || {} : null;
    let body = '';
    if (s.kind === 'plan') {
      const sel = arr(p.candidates).find(c => c?.site_id === p.selected_site_id);
      const up = obj(s.update?.payload);
      body = `${up ? `<p class="hint" style="margin:0 0 8px">직전 분석 갱신: 결과 <code>${e(up.observed_result_id ?? '—')}</code> (사이트 <code>${e(siteShort(up.observed_site_id))}</code>)를 증거로 넘겨받았습니다. 이전 계획의 다음 후보를 그대로 따랐는가: ${up.lookahead_followed === true ? '예' : up.lookahead_followed === false ? '아니요' : '—'}.</p>` : ''}
        <dl class="detail-list">
          <dt>결정 순번</dt><dd>${e(p.decision_sequence ?? '—')}</dd>
          <dt>선택한 사이트</dt><dd class="mono-id">${e(siteShort(p.selected_site_id))}</dd>
          <dt>보고된 잠재 DOI 확률</dt><dd>${fmt(sel?.selection_reward, 4)}</dd>
          <dt>예약 비용</dt><dd>${cu(sel?.reserved_cost)}</dd>
          <dt>2단계 경로 가치</dt><dd>${fmt(p.score, 4)}</dd>
          <dt>예산 안의 후보</dt><dd>${count(p.affordable_sites)}</dd>
          <dt>넘겨받은 결과 ID</dt><dd class="mono-id">${arr(p.evidence_result_ids).map(e).join(', ') || '없음'}</dd>
        </dl>
        <p class="hint">확률은 동결된 계획기가 보고한 값이며 정답이 아닙니다. 선택된 사이트의 실제 결함 여부는 이 단계에서 알 수 없습니다.</p>`;
    } else if (s.kind === 'review') {
      const a = obj(s.admission?.payload) || {}, o = obj(s.observation?.payload) || {};
      body = `<dl class="detail-list">
          <dt>검토 사이트</dt><dd class="mono-id">${e(siteShort(a.site_id ?? o.site_id))}</dd>
          <dt>결과 ID</dt><dd class="mono-id">${e(o.result_id ?? a.result_id ?? '—')}</dd>
          <dt>예약 (승인 시)</dt><dd>${cu(a.reserved_cost)}</dd>
          <dt>실제 청구</dt><dd>${cu(o.charged)}</dd>
          <dt>승인 전 사용</dt><dd>${cu(a.spent_before)}</dd>
          <dt>누적 사용</dt><dd>${cu(o.cumulative_spend)}</dd>
          <dt>센서 보고</dt><dd>${o.reported_doi === true ? `DOI · ${e(o.reported_kind ?? '—')}` : o.reported_doi === false ? 'DOI 아님' : '—'}</dd>
          <dt>보고 품질</dt><dd>${fmt(o.quality, 3)}</dd>
          <dt>시도 횟수</dt><dd>${count(arr(o.attempts).length)}</dd>
        </dl>
        <p class="hint">센서 보고는 합성 응답이며 실제 결함의 정답이 아닙니다. 예약과 청구의 차이는 쓰이지 않은 재시도 예비분 등입니다.</p>`;
    } else {
      const up = obj(s.update?.payload) || {}, cl = obj(s.closed?.payload);
      body = `<dl class="detail-list">
          <dt>마지막으로 반영한 결과</dt><dd class="mono-id">${e(up.observed_result_id ?? '—')}</dd>
          <dt>넘겨받은 결과 ID</dt><dd class="mono-id">${arr(up.evidence_result_ids).map(e).join(', ') || '—'}</dd>
          <dt>다음 선택 (참고)</dt><dd class="mono-id">${e(siteShort(up.next_selected_site_id))}</dd>
          <dt>다음 결정</dt><dd>${up.next_decision === 'not_recorded_finalized' ? '기록 안 함 · 마감' : e(up.next_decision ?? '—')}</dd>
          <dt>이전 계획의 다음 후보를 따름</dt><dd>${up.lookahead_followed === true ? '예' : up.lookahead_followed === false ? '아니요' : '—'}</dd>
          <dt>마감 상태</dt><dd>${e(cl?.status ?? '—')}</dd>
        </dl>
        <p class="hint">마감 단계에서 계산된 다음 후보는 실행되지 않았고 비용도 청구되지 않았습니다.</p>`;
    }
    $('stageDetail').innerHTML = `<h3 id="stageDetailTitle">${state.stage + 1}. ${e(s.title)}</h3><p class="stage-time">기록 시각 ${e(time(s.at))}</p>${body}<h3 style="margin-top:14px">조정 (Omnigent SDK)</h3>${delegationText(s)}`;
  }

  function renderStageTables(s) {
    const out = [];
    if (s.kind === 'plan') {
      const p = obj(s.decision.payload) || {}, cands = arr(p.candidates).filter(obj);
      const roleName = r => r === 'selected_route_first' ? ['선택', 'sel'] : r === 'route_lookahead_next' ? ['경로의 다음 후보', 'look'] : r === 'single_step_alternative' ? ['1단계 대안', 'alt1'] : [r ?? '—', 'alt1'];
      out.push(cands.length ? `<div><h3>선택 vs 대안 (decision 이벤트 payload)</h3><div class="table-scroll" tabindex="0" role="region" aria-label="후보 비교 표"><table class="data-table cand-table"><caption>후보와 비용 구성은 core 요약이 아니라 이벤트 payload에서 읽습니다. 확률은 계획기 보고값이며 정답이 아닙니다.</caption>
        <thead><tr><th scope="col">역할</th><th scope="col">사이트</th><th scope="col" class="num">보고된 잠재 DOI 확률 (보상)</th><th scope="col" class="num">1단계 가치</th><th scope="col" class="num">수율 점수 (진단용·보상 아님)</th><th scope="col" class="num">이동</th><th scope="col" class="num">로드</th><th scope="col" class="num">체류</th><th scope="col" class="num">재시도 예비</th><th scope="col" class="num">외부 재스캔</th><th scope="col" class="num">예약 합계</th></tr></thead>
        <tbody>${cands.map(c => { const [rn, rc] = roleName(c.role), cp = obj(c.cost_parts) || {}; return `<tr class="${c.site_id === p.selected_site_id ? 'is-selected' : ''}"><td><span class="pill ${rc}">${e(rn)}</span></td><td class="site">${e(siteShort(c.site_id))}</td><td class="num">${fmt(c.selection_reward, 4)}</td><td class="num">${fmt(c.single_step_value, 4)}</td><td class="num">${fmt(c.reported_yield_score, 4)}</td><td class="num">${fmt(cp.stage, 3)}</td><td class="num">${fmt(cp.load, 0)}</td><td class="num">${fmt(cp.dwell, 0)}</td><td class="num">${fmt(cp.retry_reserve, 0)}</td><td class="num">${fmt(cp.outside_rescan, 0)}</td><td class="num">${fmt(c.reserved_cost, 4)}</td></tr>`; }).join('')}</tbody></table></div>
        <ul class="note-list"><li>계획기는 <code>(r_i + γ·r_j) / (c_i + γ·c_j)</code>가 가장 큰 경로의 첫 사이트를 고릅니다 (γ = ${fmt(p.score_parts?.gamma, 0)}, 평가한 쌍 ${count(p.score_parts?.pairs_evaluated)}개, 후보 목록 ${count(p.score_parts?.shortlist_size)}개). 그래서 1단계 가치나 확률이 가장 높은 사이트가 선택되지 않을 수 있습니다.</li><li>경로 쌍 예약 합계 ${cu(p.score_parts?.pair_reserved_cost)}, 웨이퍼 전환 지금 ${p.score_parts?.wafer_switch_now === true ? '예' : p.score_parts?.wafer_switch_now === false ? '아니요' : '—'}.</li></ul></div>`
        : '<div class="empty"><strong>후보 목록이 없습니다</strong>이 decision 이벤트의 <code>payload.candidates</code>가 비어 있습니다.</div>');
    } else if (s.kind === 'review') {
      const a = obj(s.admission?.payload) || {}, o = obj(s.observation?.payload) || {}, cp = obj(o.cost) || {};
      const plan = state.stages.slice(0, state.stage).reverse().find(x => x.kind === 'plan');
      const pc = obj(arr(plan?.decision?.payload?.candidates).find(c => c?.site_id === (a.site_id ?? o.site_id))?.cost_parts) || {};
      const row = (name, res, ch) => `<tr><th scope="row">${e(name)}</th><td class="num">${fmt(res, 4)}</td><td class="num">${fmt(ch, 4)}</td></tr>`;
      out.push(`<div><h3>예약 vs 청구</h3><div class="table-scroll" tabindex="0" role="region" aria-label="예약과 청구 비교 표"><table class="data-table cost-table"><caption>예약 구성은 직전 decision 이벤트, 청구 구성은 observation 이벤트에서 읽습니다.</caption>
        <thead><tr><th scope="col">항목</th><th scope="col" class="num">예약 CU</th><th scope="col" class="num">청구 CU</th></tr></thead>
        <tbody>${row('이동', pc.stage, cp.stage)}${row('로드', pc.load, cp.load)}${row('체류', pc.dwell, cp.dwell)}${row('재시도 (예비 / 사용)', pc.retry_reserve, cp.retry)}${row('외부 재스캔', pc.outside_rescan, cp.outside_rescan)}${row('합계', a.reserved_cost, o.charged)}</tbody></table></div></div>`);
    } else {
      const up = obj(s.update?.payload) || {};
      out.push(`<div><h3>마감 시점의 다음 후보 (실행 안 됨)</h3><p class="footnote">analysis_update 이벤트에는 후보 ID만 있고 확률·비용 구성은 기록되지 않았습니다. 다음 결정이 기록되지 않은 채 마감되었습니다.</p><ul class="note-list">${arr(up.next_candidate_site_ids).map(id => `<li><code>${e(siteShort(id))}</code>${id === up.next_selected_site_id ? ' — 계획기의 다음 선택' : ''}</li>`).join('') || '<li>후보 ID가 없습니다.</li>'}</ul></div>`);
    }
    out.push(renderDelegationTable());
    $('stageTables').className = 'stage-tables';
    $('stageTables').innerHTML = out.join('');
  }

  function renderDelegationTable() {
    const dls = arr(state.data.orchestration?.delegations);
    if (!dls.length) return '<div class="empty"><strong>위임 기록이 없습니다</strong><code>orchestration.delegations</code>가 비어 있습니다.</div>';
    const calls = arr(state.data.orchestration?.specialist_tool_calls);
    return `<div><h3>SDK 위임 ${count(dls.length)}회 (현재 단계 강조)</h3><div class="table-scroll" tabindex="0" role="region" aria-label="위임 표"><table class="data-table deleg-table"><caption>위임 순번과 재생 단계는 기록 순서로 짝지었습니다. 미래 단계의 위임도 목록에 보이지만 그 결과는 해당 단계에서만 지도에 나타납니다.</caption>
      <thead><tr><th scope="col">#</th><th scope="col">에이전트</th><th scope="col">도구</th><th scope="col">출력 상태</th><th scope="col">수락</th><th scope="col">SDK call id</th><th scope="col">세션</th></tr></thead>
      <tbody>${dls.map((dl, i) => { const tc = calls.find(c => c?.delegation === i + 1); return `<tr class="${i === state.stage ? 'is-current-del' : i > state.stage ? 'is-future' : ''}"${i === state.stage ? ' aria-current="step"' : ''}><td class="num">${i + 1}</td><td>${e(dl?.agent ?? '—')}</td><td>${e(tc?.tool ?? '—')}</td><td>${i > state.stage ? '—' : e(tc?.output_status ?? '—')}</td><td>${dl?.accepted === true ? '예' : dl?.accepted === false ? '아니요' : '—'}</td><td class="site">${e(dl?.sdk_call_id ?? '—')}</td><td class="site">${e(shortHash(dl?.child_session_id))}</td></tr>`; }).join('')}</tbody></table></div></div>`;
  }

  // ---------- chain ----------
  function renderChain(d) {
    const events = arr(d.events).filter(obj);
    let prev = null;
    const rows = events.map(ev => {
      const linked = prev ? ev.prev_sha256 === prev.sha256 : null;
      const gap = prev && finite(prev.index) && finite(ev.index) ? ev.index - prev.index - 1 : null;
      const r = `<tr><td class="num">${e(ev.index ?? '—')}</td><td>${e(ev.type ?? '—')}</td><td>${e(time(ev.at))}</td><td class="site">${e(shortHash(ev.sha256))}</td><td class="site">${e(shortHash(ev.prev_sha256))}</td><td>${prev == null ? '<span class="pill">처음 (앞 이벤트 없음)</span>' : linked ? '<span class="pill link">바로 앞과 연결</span>' : `<span class="pill gap">생략된 이벤트 ${gap != null && gap > 0 ? count(gap) + '개' : ''} 뒤</span>`}</td></tr>`;
      prev = ev; return r;
    });
    const gaps = events.filter((ev, i) => i > 0 && ev.prev_sha256 !== events[i - 1].sha256).length;
    $('chainBody').innerHTML = events.length ? `<div class="table-scroll" tabindex="0" role="region" aria-label="이벤트 표"><table class="data-table chain-table"><caption>파일에 들어 있는 이벤트 ${count(events.length)}개. 사슬이 끊긴 곳 ${count(gaps)}곳은 원본에서 걸러진 이벤트가 있던 자리입니다.</caption>
      <thead><tr><th scope="col" class="num">index</th><th scope="col">유형</th><th scope="col">시각</th><th scope="col">sha256</th><th scope="col">prev_sha256</th><th scope="col">파일 안 연결</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>
      <ul class="note-list"><li>각 이벤트의 해시와 이전 해시는 원본 기록에서 그대로 옮긴 값입니다. 이 화면은 이벤트 내용을 다시 해시해 검증하지 않습니다.</li><li>걸러진 부분 집합이므로 중간 이벤트 없이 이 해시들만으로는 원본 전체 사슬이 온전한지 증명할 수 없습니다. 전체 증명은 원본 세션 증명(<code>${e(shortHash(d.verification?.session_proof_sha256))}</code>)과 원본 기록을 함께 봐야 합니다.</li><li>기록된 <code>events_sha256</code>: <code>${e(d.events_sha256 ?? '—')}</code></li></ul>`
      : '<div class="empty"><strong>이벤트가 없습니다</strong><code>events</code> 배열이 비어 있습니다.</div>';
  }

  // ---------- receipt ----------
  function renderReceipt(d) {
    const v = obj(d.verification) || {};
    const size = state.bytes ? state.bytes.byteLength : null;
    const items = [['파일', DATA_PATH], ['크기', size != null ? `${count(size)} bytes` : '—'], ['내려받은 바이트 SHA-256 (브라우저 계산)', '<span id="byteHash">계산 중…</span>'], ['검증 상태', e(v.status ?? '—')], ['검증 범위', e(v.scope ?? '—')], ['주 벤치마크 여부', v.primary_benchmark === false ? '아님' : e(v.primary_benchmark ?? '—')], ['세션 증명 SHA-256', `<code>${e(v.session_proof_sha256 ?? '—')}</code>`], ['실행기 소스 SHA-256', `<code>${e(v.runner_source_sha256 ?? '—')}</code>`], ['계획기 모델 해시', `<code>${e(d.core?.model_hash ?? '—')}</code>`], ['SDK 정규화', e(v.sdk_normalization ?? '—')], ['복구 중 생성·전송', v.recovery_creates_or_sends === false ? '없음' : e(v.recovery_creates_or_sends ?? '—')]];
    $('receiptBody').innerHTML = `<dl class="receipt">${items.map(([k, val]) => `<div><dt>${e(k)}</dt><dd>${k === '파일' ? `<code>${e(val)}</code>` : val}</dd></div>`).join('')}</dl>
      <div class="receipt-actions"><button type="button" class="control primary-action" id="download2">원본 JSON 내려받기 (${size != null ? count(size) + ' bytes' : '—'})</button></div>`;
    $('download2').addEventListener('click', download);
    if (state.bytes && window.crypto?.subtle) crypto.subtle.digest('SHA-256', state.bytes).then(h => { const el = $('byteHash'); if (el) el.innerHTML = `<code>${[...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('')}</code>`; }).catch(() => { const el = $('byteHash'); if (el) el.textContent = '계산할 수 없습니다'; });
    else { const el = $('byteHash'); if (el) el.textContent = '이 환경에서는 계산할 수 없습니다 (보안 컨텍스트 필요)'; }
  }

  function renderLimits(d) {
    const own = ['이 화면은 기록된 실행을 다시 읽을 뿐 브라우저에서 실험을 실행하지 않습니다.', '두 번의 유료 검토는 조정이 실제로 일어났다는 기록일 뿐, 선택 정확도나 팹 처리량을 입증하지 않습니다.', '센서가 보고한 결함 종류와 확률은 정답이 아닙니다. 미측정 사이트는 끝까지 알 수 없음으로 남습니다.'];
    const list = [...arr(d.limitations).filter(x => typeof x === 'string'), ...own];
    $('limitationsList').innerHTML = list.map(x => `<li>${e(x)}</li>`).join('');
  }

  function download() {
    if (!state.bytes) return;
    const url = URL.createObjectURL(new Blob([state.bytes], {type:'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = 'inspection-live.json'; document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  $('download').addEventListener('click', download);
  window.__inspectionLive = {state, go};
  load();
})();

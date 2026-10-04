(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const DATA_URL = './data/inspection-quality.json';
  const DATA_FILE = 'web/data/inspection-quality.json';
  const KIND = 'inspection_v3_quality_diagnostics';
  const SCOPE = 'posthoc_diagnostic_authored_synthetic';
  const SCEN_NAME = {stationary:'정상 상태', novel_cluster:'새 군집', nuisance_heavy:'잡음 결함 다수', process_shift:'공정 이동', low_contrast:'저대비'};
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
  const fmt = (n, d = 2) => finite(n) ? n.toLocaleString('ko-KR', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
  const fmtT = (n, d = 2) => finite(n) ? n.toLocaleString('ko-KR', {maximumFractionDigits:d}) : '—';
  const count = n => finite(n) ? n.toLocaleString('ko-KR') : '—';
  const pct = (n, d = 1) => finite(n) ? fmt(n * 100, d) + '%' : '—';
  const p3 = n => finite(n) ? n.toFixed(3).replace(/^0\./, '.').replace(/^-0\./, '−.') : '—';
  const scenName = k => SCEN_NAME[k] || k;
  const sum = (list, f) => { let t = 0; for (const x of list) { const v = nn(f(x)); if (v == null) return null; t += v; } return t; };
  const empty = (title, paths) => `<div class="empty"><strong>${e(title)}</strong>필요한 필드 ${paths.map(p => `<code>${e(p)}</code>`).join(', ')}가 없거나 유효한 범위가 아닙니다 · 파일 <code>${DATA_FILE}</code></div>`;
  const check = ok => ok == null ? '<span class="chk">확인 불가</span>' : ok ? '<span class="chk ok">일치</span>' : '<span class="chk bad">불일치</span>';
  const near = (a, b, tol = 1e-6) => finite(a) && finite(b) ? Math.abs(a - b) <= tol : null;

  // ---------- loading ----------
  function showState(html) { const box = $('loadState'); box.hidden = false; box.innerHTML = html; }
  function retryButton() { const b = document.createElement('button'); b.type = 'button'; b.className = 'control'; b.textContent = '다시 불러오기'; b.addEventListener('click', load); $('loadState').append(b); }
  async function load() {
    $('app').hidden = true; $('download').disabled = true;
    showState('<div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div><p>저장된 사후 진단을 불러오는 중…</p>');
    let response;
    try { response = await fetch(DATA_URL, {cache:'no-store'}); }
    catch (error) {
      console.error('Inspection quality fetch:', error);
      showState(`<h2>진단 파일에 접근할 수 없습니다</h2><p>네트워크 또는 로컬 파일 열기 제한으로 <code>${DATA_FILE}</code> 파일을 읽지 못했습니다. 정적 서버로 <code>web/</code>을 연 뒤 다시 시도하세요.</p>`);
      return retryButton();
    }
    if (response.status === 404) {
      showState(`<h2>아직 검증된 진단이 없습니다</h2><p>사후 품질 진단이 아직 <code>${DATA_FILE}</code> 파일로 내보내지지 않았습니다. 감사를 통과한 진단이 내보내지면 이 화면이 채워집니다. 예시 수치는 표시하지 않습니다.</p>`);
      return retryButton();
    }
    try {
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const bytes = await response.arrayBuffer();
      const data = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes));
      if (!obj(data)) throw Error('최상위 값이 객체가 아닙니다');
      if (data.schema_version !== 1) throw Error(`지원하지 않는 schema_version ${text(data.schema_version)}`);
      if (data.kind !== KIND) throw Error(`kind가 ${KIND}가 아닙니다 (${text(data.kind)})`);
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
      showState(`<h2>진단 파일을 해석할 수 없습니다</h2><p>${e(error.message)}. <code>${DATA_FILE}</code>의 내보내기 형식을 확인한 뒤 다시 시도하세요.</p>`);
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
    $('scenSelect').innerHTML = '<option value="all">전체 비교</option>' + opts;
    $('errSelect').innerHTML = '<option value="all">5개 시나리오 합산</option>' + opts;
    if (state.scenarios.length !== 5) $('errSelect').options[0].textContent = `${state.scenarios.length}개 시나리오 합산`;
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
    if (typeof d.generated_at === 'string' && !Number.isNaN(Date.parse(d.generated_at))) generated = new Date(d.generated_at).toLocaleString('ko-KR', {dateStyle:'medium', timeStyle:'short'});
    const items = [['정책', d.candidate, true], ['모델', d.model, true], ['모드', d.mode, true], ['lot당 예산', pos(d.budget_cu) != null ? `${count(d.budget_cu)} CU` : null],
      ['lot', pos(d.lots) != null ? `${count(d.lots)}개` : null], ['감사', d.audit_status], ['생성', generated]];
    $('studyMeta').innerHTML = items.map(([k, v, mono]) => `<div><dt>${e(k)}</dt><dd${mono ? ' class="mono"' : ''}>${e(v)}</dd></div>`).join('');
    const warn = [];
    if (d.scope !== SCOPE) warn.push(`scope가 <code>${SCOPE}</code>가 아닙니다 (<code>${e(d.scope)}</code>).`);
    if (d.replaces_primary !== false) warn.push('<code>replaces_primary</code>가 false로 기록되지 않았습니다. 주 결과와의 관계를 확인하세요.');
    if (d.audit_status !== 'passed') warn.push(`감사 상태가 passed가 아닙니다 (<code>${e(d.audit_status)}</code>). 아래 수치는 검증되지 않은 것으로 읽으세요.`);
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
    const title = dies != null && visits != null ? `왜 ${count(dies)} 다이 중 평균 ${fmtT(visits)}곳만 검토받나` : '왜 일부 다이만 검토받나';
    $('budgetTitle').textContent = title;
    if (!allOk(B, f, l, r) || B < f + r) { $('budgetBody').innerHTML = empty('방문 상한을 계산할 수 없습니다', ['budget_cu', 'visit_bound.first_review_min_cu', 'visit_bound.later_review_min_cu', 'visit_bound.retry_reserve_cu']); return; }
    const n = 1 + Math.floor((B - f - r) / l + 1e-9);
    const charged = f + l * (n - 1), unspent = B - charged;
    const match = maxV == null ? null : n === maxV && near(charged, vb.charged_at_bound) !== false && near(unspent, vb.unspent_at_bound) !== false;
    const allDie = pos(o.min_cu_to_review_every_die_per_lot), allCand = pos(o.min_cu_to_review_every_candidate_per_lot), cands = nn(m.optical_candidates);
    const overBound = prob(o.visits_over_optimistic_bound), reviewed = prob(o.dies_reviewed_fraction);

    const statement = dies != null && visits != null
      ? `lot 하나에는 다이 <span class="num">${count(dies)}</span>개(웨이퍼 3장)가 있지만, 유료 SEM 검토를 실제로 받은 곳은 lot당 평균 <span class="num">${fmtT(visits)}</span>곳입니다. 최소 수수료만 내고 실패도 이동도 없다고 가정해도 상한은 <span class="num">${n}</span>회입니다.`
      : `최소 수수료만 내고 실패도 이동도 없다고 가정해도 lot당 유료 검토 상한은 <span class="num">${n}</span>회입니다.`;

    const formula = `<div class="formula" role="group" aria-label="방문 상한 유도">
      <p class="f-line"><span>첫 검토 최소</span> <b>${fmtT(f)}</b> + <span>이후 검토 최소</span> <b>${fmtT(l)}</b> × (n − 1) + <span>재시도 예비</span> <b>${fmtT(r)}</b> ≤ <span>예산</span> <b>${fmtT(B)}</b> CU</p>
      <p class="f-line">n ≤ 1 + ⌊(${fmtT(B)} − ${fmtT(f)} − ${fmtT(r)}) ÷ ${fmtT(l)}⌋ = 1 + ⌊${fmtT(B - f - r)} ÷ ${fmtT(l)}⌋ = <b>${n}</b>회</p>
      <p class="f-line f-sub">이때 청구 ${fmtT(f)} + ${fmtT(l)} × ${n - 1} = <b>${fmtT(charged)} CU</b>, 남는 ${fmtT(unspent)} CU는 다음 검토에 필요한 ${fmtT(l)} + ${fmtT(r)} = ${fmtT(l + r)} CU보다 적어 쓸 수 없습니다.</p>
      <p class="f-check">JSON의 <code>visit_bound.max_visits</code> ${maxV == null ? '—' : count(maxV)}, <code>charged_at_bound</code> ${fmtT(nn(vb.charged_at_bound))}, <code>unspent_at_bound</code> ${fmtT(nn(vb.unspent_at_bound))}와 이 화면의 계산: ${check(match)}</p>
    </div>`;

    const assumptions = arr(vb.assumptions).filter(x => typeof x === 'string');
    const bound = `<p class="prose">이 ${n}회는 <strong>느슨하고 낙관적인 상한</strong>입니다. 웨이퍼 한 번 적재, 첫 검토 뒤 스테이지 이동 0, 실패·누락 0을 가정합니다. 실제로 달성 가능한 최적(oracle) 계획도, 팹 처리량도 아닙니다. 매 검토 직전에 재시도 예비 ${fmtT(r)} CU를 남겨 두는 것은 하네스가 실제로 예약하는 방식 그대로입니다.</p>
      ${assumptions.length ? `<details class="raw"><summary>JSON에 기록된 가정 ${assumptions.length}개</summary><ul lang="en">${assumptions.map(x => `<li>${e(x)}</li>`).join('')}</ul></details>` : ''}`;

    const costRows = [];
    if (allDie != null) costRows.push({name:`모든 다이 ${dies != null ? count(dies) + '개' : ''} 검토`, value:allDie});
    if (allCand != null) costRows.push({name:`모든 광학 후보 ${cands != null ? fmtT(cands, 0) + '개' : ''} 검토`, value:allCand});
    costRows.push({name:'lot당 예산', value:B, cls:'is-budget'});
    const costMax = Math.max(...costRows.map(x => x.value));
    const cost = `<figure class="figure-block"><figcaption class="fig-title">전부 검토하려면 필요한 <strong>최소 청구 CU 하한</strong> (선형 축, 0부터)</figcaption>${hbar(costRows, costMax, 'CU')}
      <p class="footnote">위 두 값은 동결된 최소 수수료로 계산한 하한이며 예산 제안이 아닙니다. 실제 청구는 이동·재시도 때문에 이보다 큽니다.${allDie != null ? ` 모든 다이를 검토하려면 예산의 약 ${fmt(allDie / B, 1)}배가 필요합니다.` : ''}</p></figure>`;

    const rows = visitRows();
    const W = sum(rows, x => x.lots);
    const wmean = key => W && rows.every(x => x[key] != null && x.lots != null) ? rows.reduce((t, x) => t + x[key] * x.lots, 0) / W : null;
    const stopTotal = {}; rows.forEach(x => Object.entries(x.stops).forEach(([k, v]) => { if (nn(v) != null) stopTotal[k] = (stopTotal[k] || 0) + v; }));
    const stopText = Object.entries(stopTotal).map(([k, v]) => `<code>${e(k)}</code> ${count(v)}${W ? ` / ${count(W)}` : ''}`).join(', ');
    const gap = visits != null && maxV != null ? maxV - visits : null;
    const gStage = wmean('stage'), gLoad = wmean('load'), gRetry = wmean('retry'), gUnspent = wmean('unspent');
    const extra = allOk(gStage, gLoad, gRetry, gUnspent) ? gStage + gLoad + gRetry - (unspent - gUnspent) : null;

    const figures = [['관측 평균 검토', visits != null ? `${fmtT(visits)}<small>회/lot</small>` : '—'], ['낙관적 상한', `${n}<small>회/lot</small>`],
      ['상한 대비', pct(overBound)], ['검토된 다이 비율', pct(reviewed, 2)]];

    const gapProse = gap != null && extra != null
      ? `<p class="prose">관측 평균은 상한보다 lot당 <strong>${fmt(gap)}회</strong> 적습니다. 그 차이는 최소보다 많이 든 스테이지 이동 ${fmt(gStage, 1)} CU, 추가 웨이퍼 적재 ${fmt(gLoad, 1)} CU, 청구된 재시도 ${fmt(gRetry, 1)} CU에서 생깁니다. 끝에 남아 쓰지 못한 CU는 평균 ${fmt(gUnspent, 1)} CU로 상한 계산의 ${fmtT(unspent)} CU보다 ${fmt(unspent - gUnspent, 1)} CU 적습니다. 추가로 든 ${fmt(extra, 1)} CU는 이후 검토 ${fmt(extra / l, 2)}회분(÷ ${fmtT(l)} CU)으로, 관측된 차이 ${fmt(gap)}회와 ${Math.abs(extra / l - gap) < 0.05 ? '맞습니다' : `${fmt(Math.abs(extra / l - gap))}회 어긋납니다`}. 모든 lot의 정지 사유: ${stopText || '—'}.</p>`
      : '';

    const table = rows.length ? `<div class="table-scroll" role="region" tabindex="0" aria-label="시나리오별 검토 횟수와 추가 비용 표 · 스크롤 가능"><table class="data-table visit-table"><caption>lot당 평균 · 전체 행은 lot 수로 가중한 평균 · CU는 최소 수수료를 넘는 부분</caption>
      <thead><tr><th scope="col">시나리오</th><th scope="col" class="num">lot</th><th scope="col" class="num">평균 검토</th><th scope="col" class="num">상한 대비 부족</th><th scope="col" class="num">이동 초과 CU</th><th scope="col" class="num">추가 적재 CU</th><th scope="col" class="num">재시도 CU</th><th scope="col" class="num">남은 CU</th></tr></thead>
      <tbody>${rows.map(x => `<tr><th scope="row">${e(scenName(x.k))}</th><td class="num">${count(x.lots)}</td><td class="num">${fmt(x.visits)}</td><td class="num">${fmt(x.short)}</td><td class="num">${fmt(x.stage, 1)}</td><td class="num">${fmt(x.load, 1)}</td><td class="num">${fmt(x.retry, 1)}</td><td class="num">${fmt(x.unspent, 1)}</td></tr>`).join('')}
      <tr class="total"><th scope="row">전체</th><td class="num">${count(W)}</td><td class="num">${fmt(visits)}</td><td class="num">${fmt(gap)}</td><td class="num">${fmt(gStage, 1)}</td><td class="num">${fmt(gLoad, 1)}</td><td class="num">${fmt(gRetry, 1)}</td><td class="num">${fmt(gUnspent, 1)}</td></tr></tbody></table></div>` : '';

    $('budgetBody').innerHTML = `<p class="statement wide">${statement}</p>
      <dl class="figures">${figures.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>
      <div class="two-col"><div>${formula}${bound}</div>${cost}</div>
      <h3 class="sub-head">상한과 관측값의 차이</h3>${gapProse}${table}
      <p class="footnote">다이 수 ${dies != null ? count(dies) : '—'}는 웨이퍼 3장의 합입니다(검사 v3 프로토콜 <code>wafers_per_lot: 3</code>). 이 JSON에는 웨이퍼 수 필드가 없어 이 값만 프로토콜에서 옮겼습니다.</p>`;
  }

  // ---------- 2. balance ----------
  function renderBalance() {
    const o = obj(state.data.overall) || {}, m = obj(o.mean_per_lot) || {}, c = obj(o.counts) || {};
    const need = ['latent_doi', 'never_optically_admitted_doi', 'missed_candidate_doi', 'selected_latent_doi', 'tp_reported_positive', 'sensor_missed_doi', 'unresolved_doi', 'selected', 'selected_non_doi', 'reported_negative_non_doi', 'fp_reported_positive', 'unresolved_non_doi'];
    if (!need.every(k => nn(m[k]) != null && nn(c[k]) != null)) { $('balanceBody').innerHTML = empty('질량 보존표를 만들 수 없습니다', need.map(k => `overall.mean_per_lot.${k}`)); return; }
    const L = m.latent_doi;
    const lots = pos(state.data.lots);
    const segA = [
      {k:'never', name:'광학 후보로 제시되지 않음', note:'후보만 고르는 정책은 닿을 수 없음', f:'never_optically_admitted_doi'},
      {k:'unsel', name:'후보였지만 선택되지 않음', note:'예산 안에서 고르지 못함', f:'missed_candidate_doi'},
      {k:'sel', name:'선택되어 유료 검토', note:'아래에서 결과별로 나눔', f:'selected_latent_doi'}];
    const segB = [
      {k:'tp', name:'확인됨 (검토 양성)', note:'잠재 DOI이고 검토가 양성으로 보고', f:'tp_reported_positive'},
      {k:'sneg', name:'센서가 음성으로 보고', note:'잠재 DOI인데 검토 응답이 음성', f:'sensor_missed_doi'},
      {k:'unk', name:'미해결 · 결과 모름', note:'모든 시도가 실패·누락, 양호로 보지 않음', f:'unresolved_doi'},
      {k:'nondoi', name:'비DOI를 선택', note:'', f:'selected_non_doi'}];
    const S = m.selected;
    const stack = (segs, total, label) => !(total > 0) ? '<p class="footnote">분모가 0이어서 비율 그림을 표시하지 않습니다.</p>' : `<div class="stack" role="img" aria-label="${e(label)}">${segs.map(s => `<i class="seg ${s.k}" style="width:${m[s.f] / total * 100}%"></i>`).join('')}</div>`;
    const legend = (segs, total, denomName) => `<ul class="seg-legend">${segs.map(s => `<li><i class="sw ${s.k}" aria-hidden="true"></i><span class="sl-name">${s.name}${s.note ? `<small>${s.note}</small>` : ''}</span><span class="sl-val"><b>${fmt(m[s.f])}</b><small>${pct(prob(ratio(m[s.f], total)))} · ${denomName}</small></span></li>`).join('')}</ul>`;
    const nonNote = `비DOI ${fmt(m.selected_non_doi)} = 검토 음성 ${fmt(m.reported_negative_non_doi)} + 오탐 양성 ${fmt(m.fp_reported_positive)} + 미해결 ${fmt(m.unresolved_non_doi)}`;

    const chk1 = c.never_optically_admitted_doi + c.missed_candidate_doi + c.selected_latent_doi === c.latent_doi;
    const chk2 = c.tp_reported_positive + c.sensor_missed_doi + c.unresolved_doi === c.selected_latent_doi;
    const chk3 = c.selected_latent_doi + c.selected_non_doi === c.selected;
    const chk4 = c.reported_negative_non_doi + c.fp_reported_positive + c.unresolved_non_doi === c.selected_non_doi;

    const rows = [
      ['잠재 DOI (사후 진실)', 'latent_doi', 0],
      ['└ 광학 후보로 제시되지 않음', 'never_optically_admitted_doi', 1],
      ['└ 후보였지만 선택되지 않음', 'missed_candidate_doi', 1],
      ['└ 선택된 잠재 DOI', 'selected_latent_doi', 1],
      ['　└ 확인됨 (검토 양성)', 'tp_reported_positive', 2],
      ['　└ 센서가 음성으로 보고', 'sensor_missed_doi', 2],
      ['　└ 미해결 · 결과 모름', 'unresolved_doi', 2],
      ['선택된 비DOI', 'selected_non_doi', 0],
      ['└ 검토 음성', 'reported_negative_non_doi', 1],
      ['└ 오탐 양성', 'fp_reported_positive', 1],
      ['└ 미해결', 'unresolved_non_doi', 1],
      ['유료 검토 사이트 합계', 'selected', 0]];

    $('balanceBody').innerHTML = `<p class="statement wide">lot당 잠재 DOI <span class="num">${fmt(L)}</span>개 중 확인된 것은 <span class="num">${fmt(m.tp_reported_positive)}</span>개입니다. 가장 큰 몫(<span class="num">${fmt(m.missed_candidate_doi)}</span>)은 광학 후보였지만 예산 안에서 고르지 못한 DOI이고, <span class="num">${fmt(m.never_optically_admitted_doi)}</span>개는 애초에 후보로 제시되지 않았습니다.</p>
      <p class="prose truth-note"><strong>잠재 DOI는 오프라인 합성 평가에서만 존재하는 사후 진실입니다.</strong> 생성기가 알고 있는 값으로 결과를 분류할 때만 읽었고, 정책의 선택·점수·재생에는 쓰지 않았습니다. 실제 공정에서는 검토하지 않은 다이의 상태를 알 수 없으며, 이 화면은 검토하지 않은 다이를 양호로 표시하지 않습니다.</p>
      <figure class="figure-block balance-fig">
        <figcaption class="fig-title">잠재 DOI ${fmt(L)}개/lot의 행방 (0부터 ${fmt(L)}까지 선형)</figcaption>
        ${stack(segA, L, `잠재 DOI ${fmt(L)} 중 광학 미제시 ${fmt(m.never_optically_admitted_doi)}, 후보 미선택 ${fmt(m.missed_candidate_doi)}, 선택 ${fmt(m.selected_latent_doi)}`)}
        ${legend(segA, L, '잠재 DOI 대비')}
        <figcaption class="fig-title">유료 검토 ${fmt(S)}곳/lot의 결과 (0부터 ${fmt(S)}까지 선형)</figcaption>
        ${stack(segB, S, `검토 ${fmt(S)}곳 중 확인 ${fmt(m.tp_reported_positive)}, 센서 음성 ${fmt(m.sensor_missed_doi)}, 미해결 ${fmt(m.unresolved_doi)}, 비DOI ${fmt(m.selected_non_doi)}`)}
        ${legend(segB, S, '검토 사이트 대비')}
        <p class="footnote">${nonNote}.</p>
      </figure>
      <div class="table-scroll" role="region" tabindex="0" aria-label="잠재 DOI 질량 보존 표 · 스크롤 가능"><table class="data-table balance-table"><caption>lot당 평균은 ${lots ? count(lots) + '개 lot' : '전체 lot'} 합계를 lot 수로 나눈 값입니다. 표시는 소수 둘째 자리 반올림이라 하위 항목 합이 상위 값과 ±0.01 어긋날 수 있습니다. 보존 검사는 반올림 전 정수 합계로 합니다.</caption>
        <thead><tr><th scope="col">구분</th><th scope="col" class="num">lot당 평균</th><th scope="col" class="num">전체 합계</th></tr></thead>
        <tbody>${rows.map(([name, f, lv]) => `<tr class="lv${lv}"><th scope="row">${name}</th><td class="num">${fmt(m[f])}</td><td class="num">${count(c[f])}</td></tr>`).join('')}</tbody></table></div>
      <ul class="checks">
        <li>광학 미제시 ${count(c.never_optically_admitted_doi)} + 후보 미선택 ${count(c.missed_candidate_doi)} + 선택 ${count(c.selected_latent_doi)} = 잠재 DOI ${count(c.latent_doi)} ${check(chk1)}</li>
        <li>확인 ${count(c.tp_reported_positive)} + 센서 음성 ${count(c.sensor_missed_doi)} + 미해결 ${count(c.unresolved_doi)} = 선택된 잠재 DOI ${count(c.selected_latent_doi)} ${check(chk2)}</li>
        <li>선택된 잠재 DOI ${count(c.selected_latent_doi)} + 비DOI ${count(c.selected_non_doi)} = 유료 검토 ${count(c.selected)} ${check(chk3)}</li>
        <li>비DOI: 검토 음성 ${count(c.reported_negative_non_doi)} + 오탐 양성 ${count(c.fp_reported_positive)} + 미해결 ${count(c.unresolved_non_doi)} = ${count(c.selected_non_doi)} ${check(chk4)}</li>
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
    if (x.ceil == null) return {label:'판단 불가', cls:''};
    const main = x.ceil < CEILING_LOW ? {label:'광학 후보 천장', cls:'b-optic'} : {label:'검토 횟수(예산)', cls:'b-visit'};
    if (x.rank != null && x.rank >= RANK_HIGH) { main.label += ' + 순위 오류'; main.rankFlag = true; }
    return main;
  }

  function renderScenarios() {
    if (!state.scenarios.length) { $('scenBody').innerHTML = empty('시나리오별 진단이 없습니다', ['per_scenario']); return; }
    const list = state.scenarios.map(scenStats);
    const pick = state.scen;
    const ticks = [0, .25, .5, .75, 1];
    const chart = `<figure class="figure-block"><figcaption class="fig-title">광학 후보 천장과 실제로 선택된 잠재 DOI 비율 (둘 다 잠재 DOI 대비, 0–1 축)</figcaption>
      <ul class="rbars">${list.map(x => `<li class="${pick === x.k ? 'is-pick' : ''}${pick !== 'all' && pick !== x.k ? ' is-dim' : ''}"><span class="rb-name">${e(scenName(x.k))}</span><span class="rb-track">${x.ceil != null ? `<i class="ceil" style="width:${x.ceil * 100}%"></i>` : ''}${x.reached != null ? `<i class="reach" style="width:${x.reached * 100}%"></i>` : ''}</span><span class="rb-val">${p3(x.ceil)} <small>/ ${p3(x.reached)}</small></span></li>`).join('')}</ul>
      <div class="rb-axis" aria-hidden="true"><span></span><span>${ticks.map(t => `<b>${t === 0 ? '0' : t === 1 ? '1' : String(t).replace(/^0/, '')}</b>`).join('')}</span><span></span></div>
      <p class="seg-key"><span><i class="sw ceil" aria-hidden="true"></i>광학 후보 천장 = 후보 DOI ÷ 잠재 DOI</span><span><i class="sw reach" aria-hidden="true"></i>선택된 잠재 DOI ÷ 잠재 DOI</span></p></figure>`;

    let detail;
    if (pick === 'all') {
      const optic = list.filter(x => x.ceil != null && x.ceil < CEILING_LOW), visit = list.filter(x => x.ceil != null && x.ceil >= CEILING_LOW);
      const names = xs => xs.map(x => `${e(scenName(x.k))} ${p3(x.ceil)}`).join(', ');
      detail = `<p class="prose">병목은 둘로 갈립니다. <strong>광학 후보 천장</strong>이 낮은 시나리오(${names(optic) || '없음'})에서는 대부분의 잠재 DOI가 처음부터 후보로 제시되지 않아, 후보 안에서 아무리 잘 골라도 놓칩니다. 천장이 높은 시나리오(${names(visit) || '없음'})에서는 후보 안에 DOI가 충분하지만 lot당 검토 횟수가 모자라는 것이 병목입니다. 시나리오를 고르면 그 시나리오의 설명이 나옵니다.</p>`;
    } else {
      const x = list.find(y => y.k === pick), b = bottleneck(x);
      const parts = [];
      if (x.ceil != null) parts.push(`잠재 DOI ${fmt(x.latent)}개/lot 중 광학 후보로 제시된 것은 ${fmt(x.cand)}개(천장 ${p3(x.ceil)})입니다. ${fmt(x.never)}개는 후보로 제시되지 않아 후보만 고르는 어떤 정책도 닿을 수 없습니다.`);
      if (x.visits != null) parts.push(`평균 검토는 ${fmt(x.visits)}회이고, p ≥ 0.99인 후보만 ${fmt(x.p99)}개입니다. 그중 선택되지 않은 잠재 DOI가 ${fmt(x.unselP99)}개 남았습니다.`);
      if (x.rank != null) parts.push(`선택한 ${fmt(x.sel)}곳 중 비DOI는 ${fmt(x.nondoi)}곳(${pct(x.rank)})${b.rankFlag ? '으로, 순위 오류가 병목에 더해집니다' : '로 순위 오류는 작습니다'}.`);
      if (x.sneg != null) parts.push(`선택된 잠재 DOI 중 센서가 음성으로 보고한 것은 ${fmt(x.sneg)}개, 결과를 모르는 미해결은 ${fmt(x.unk)}개입니다.`);
      detail = `<p class="prose"><span class="bneck ${b.cls}">${e(b.label)}</span> ${parts.join(' ')}</p>`;
    }

    const table = `<div class="table-scroll" role="region" tabindex="0" aria-label="시나리오별 병목 표 · 스크롤 가능"><table class="data-table scen-table"><caption>lot당 평균 · 병목 분류는 이 화면의 읽기 규칙입니다: 천장 &lt; ${CEILING_LOW} 이면 광학 후보 천장, 아니면 검토 횟수. 비DOI 선택 비율 ≥ ${pct(RANK_HIGH, 0)}이면 순위 오류를 덧붙입니다.</caption>
      <thead><tr><th scope="col">시나리오</th><th scope="col" class="num">광학 천장</th><th scope="col" class="num">후보 DOI</th><th scope="col" class="num">잠재 DOI</th><th scope="col" class="num">평균 검토</th><th scope="col" class="num">확인</th><th scope="col" class="num">선택 비DOI</th><th scope="col" class="num">센서 음성</th><th scope="col" class="num">미해결</th><th scope="col">병목</th></tr></thead>
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
    if (!state.scenarios.length) { $('errBody').innerHTML = empty('오류 분리 진단이 없습니다', ['per_scenario.*.error_separation']); return; }
    const d = errData(state.err);
    if (!d) { $('errBody').innerHTML = empty('오류 분리 진단이 없습니다', [`per_scenario.${state.err === 'all' ? '*' : state.err}.error_separation`]); return; }
    const per = v => d.lots && v != null ? fmt(v / d.lots) : '—';
    const {t, r, s} = d;
    const precision = prob(ratio(t.tp, t.tp + t.fp)), recall = prob(ratio(t.tp, t.tp + t.fn));
    const scope = state.err === 'all' ? `${d.n}개 시나리오 합산, ${count(d.lots)} lot` : `${e(scenName(state.err))}, ${count(d.lots)} lot`;
    const thrText = d.thr != null ? fmtT(d.thr) : '—';

    const sel = allOk(r.selected_latent_doi, r.selected_non_doi) ? r.selected_latent_doi + r.selected_non_doi : null;
    const rankRate = prob(ratio(r.selected_non_doi, sel));

    $('errBody').innerHTML = `<p class="err-scope">범위: ${scope}. 합계는 정수 그대로, lot당 값은 합계 ÷ lot 수입니다.</p>
      <div class="err-block">
        <h3>① 임계값 분류 오류 <span class="tag">경로 순위에 쓰이지 않음</span></h3>
        <p class="prose">동결된 확률을 p ≥ ${thrText}에서 자른 분류 결과입니다. 모든 원래 광학 후보를 대상으로 하며, 검토 경로를 고르는 정책은 이 임계값을 쓰지 않습니다. 그래서 여기의 FP·FN은 실제로 검토를 받은 사이트의 오류가 아닙니다.</p>
        <div class="table-scroll" role="region" tabindex="0" aria-label="임계값 혼동 행렬 · 스크롤 가능"><table class="data-table conf-table"><caption>혼동 행렬 · 후보 수 (정답은 사후 잠재 DOI)</caption>
          <thead><tr><th scope="col">잠재 DOI</th><th scope="col" class="num">p ≥ ${thrText} (양성 판정)</th><th scope="col" class="num">p &lt; ${thrText} (음성 판정)</th></tr></thead>
          <tbody><tr><th scope="row">예</th><td class="num">TP ${count(t.tp)}</td><td class="num">FN ${count(t.fn)}</td></tr><tr><th scope="row">아니오</th><td class="num">FP ${count(t.fp)}</td><td class="num">TN ${count(t.tn)}</td></tr></tbody></table></div>
        <p class="kv">정밀도 <b>${p3(precision)}</b> · 후보 재현율 <b>${p3(recall)}</b> · FP ${per(t.fp)}/lot · FN ${per(t.fn)}/lot</p>
      </div>
      <div class="err-block">
        <h3>② 선택 순위 오류</h3>
        <p class="prose">실제로 유료 검토를 받은 사이트 가운데 잠재 DOI가 아니었던 곳입니다. 동결된 확률과 경로 비용 아래에서 잘못 고른 선택입니다.</p>
        <dl class="state-list"><dt>선택된 비DOI / 선택 사이트</dt><dd>${count(r.selected_non_doi)} / ${count(sel)} (${pct(rankRate)})</dd>
          <dt>lot당 선택된 비DOI</dt><dd>${per(r.selected_non_doi)}</dd>
          <dt>p ≥ 0.99 후보 (lot당)</dt><dd>${count(r.candidates_p_ge_099)} (${per(r.candidates_p_ge_099)})</dd>
          <dt>그중 선택됨 / 선택된 것 중 잠재 DOI</dt><dd>${count(r.selected_p_ge_099)} / ${count(r.selected_p_ge_099_doi)}</dd>
          <dt>선택되지 않은 p ≥ 0.99 잠재 DOI (lot당)</dt><dd>${count(r.unselected_candidate_doi_p_ge_099)} (${per(r.unselected_candidate_doi_p_ge_099)})</dd></dl>
        <p class="scope-note">후보 안의 실제 잠재 DOI 수가 검토 가능한 횟수보다 많으면, 완벽한 순위에서도 미검토 DOI가 남습니다. 높은 예측 확률 자체는 실제 DOI를 보장하지 않으며 순위 오류와 검토 예산을 함께 봐야 합니다.</p>
      </div>
      <div class="err-block">
        <h3>③ 센서 응답 오류</h3>
        <p class="prose">검토를 받았지만 응답이 틀렸거나 받지 못한 경우입니다. 실패·누락은 물리적 음성이 아니며 결과를 모르는 것으로 둡니다.</p>
        <dl class="state-list"><dt>잠재 DOI인데 음성으로 보고</dt><dd>${count(s.sensor_missed_doi)} (${per(s.sensor_missed_doi)}/lot)</dd>
          <dt>비DOI인데 양성으로 보고</dt><dd>${count(s.fp_reported_positive)} (${per(s.fp_reported_positive)}/lot)</dd>
          <dt>미해결 잠재 DOI / 비DOI</dt><dd>${count(s.unresolved_doi)} / ${count(s.unresolved_non_doi)}</dd>
          <dt>재시도가 필요했던 검토</dt><dd>${count(s.retried_reviews)} (${per(s.retried_reviews)}/lot)</dd>
          <dt>시도: 정상 / 실패 / 누락 / 기타</dt><dd>${count(s.att_ok)} / ${count(s.att_failure)} / ${count(s.att_missing)} / ${count(s.att_other)}</dd></dl>
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
    if (!d) { $('calBody').innerHTML = empty('보정 표를 만들 수 없습니다', [`per_scenario.${k === 'all' ? '*' : k}.${field}.bins`]); return; }
    const scope = `${k === 'all' ? `${state.scenarios.length}개 시나리오 합산 (n 가중 평균 p)` : e(scenName(k))} · ${pop === 'selected' ? '선택된 검토 사이트' : '광학 후보 전체'} n = ${count(d.n)}`;
    const row = (b, cls = '') => {
      const nOk = nn(b.n);
      const small = nOk != null && nOk > 0 && nOk < SMALL_N;
      const mp = nOk ? prob(b.mean_p) : null, rt = nOk ? prob(b.rate) : null, gap = mp != null && rt != null ? b.gap : null;
      return `<tr class="${cls}"><th scope="row" class="mono">${e(b.bin)}</th><td class="num">${count(nOk)}${small ? ' <span class="small-n">표본 적음</span>' : ''}</td><td class="num">${nOk ? p3(mp) : '<span class="null">— 표본 없음</span>'}</td><td class="num">${nOk ? p3(rt) : '—'}</td><td class="num">${gap == null ? '—' : (gap > 0 ? '+' : gap < 0 ? '−' : '') + p3(Math.abs(gap))}</td></tr>`;
    };
    const pts = d.bins.filter(b => nn(b.n) && prob(b.mean_p) != null && prob(b.rate) != null);
    const W = 280, P = 36, S = W - P - 10;
    const X = v => P + v * S, Y = v => 10 + (1 - v) * S;
    const grid = [0, .25, .5, .75, 1].map(t => `<line x1="${X(t)}" y1="${Y(0)}" x2="${X(t)}" y2="${Y(1)}" class="grid"/><line x1="${X(0)}" y1="${Y(t)}" x2="${X(1)}" y2="${Y(t)}" class="grid"/><text x="${X(t)}" y="${Y(0) + 16}" text-anchor="middle">${t}</text><text x="${X(0) - 6}" y="${Y(t) + 4}" text-anchor="end">${t}</text>`).join('');
    const svg = `<svg viewBox="0 0 ${W} ${W + 8}" role="img" aria-labelledby="calFigCap"><g>${grid}<line x1="${X(0)}" y1="${Y(0)}" x2="${X(1)}" y2="${Y(1)}" class="diag"/>${pts.map(b => `<circle cx="${X(b.mean_p)}" cy="${Y(b.rate)}" r="5" class="pt${nn(b.n) < SMALL_N ? ' small' : ''}"/>`).join('')}${d.hi && nn(d.hi.n) && prob(d.hi.mean_p) != null && prob(d.hi.rate) != null ? `<rect x="${X(d.hi.mean_p) - 4}" y="${Y(d.hi.rate) - 4}" width="8" height="8" class="pt hi"/>` : ''}</g></svg>`;
    const over = pts.filter(b => b.gap > 0.05).map(b => e(b.bin));
    const sentence = pts.length ? (over.length ? `평균 p가 실제 비율보다 0.05 넘게 높은 구간(과신): ${over.join(', ')}.` : '평균 p가 실제 비율을 0.05 넘게 웃도는 표본 구간은 없습니다. 과소 예측 여부는 표의 음수 차이도 확인하세요.') : '표본이 있는 구간이 없습니다.';
    $('calBody').innerHTML = `<p class="err-scope">범위: ${scope}. ${sentence}</p>
      <div class="cal-grid"><figure class="cal-fig">${svg}<figcaption id="calFigCap">보정 그림: 가로축 평균 예측 p, 세로축 실제 잠재 DOI 비율, 둘 다 0–1. 대각선은 완전 보정이며 대각선 아래 점은 과신입니다. 사각형은 p ≥ 0.99 구간, 속이 빈 점은 n &lt; ${SMALL_N}입니다. 같은 값이 보정 표에 있습니다.</figcaption></figure>
      <div class="table-scroll" role="region" tabindex="0" aria-label="보정 표 · 스크롤 가능"><table class="data-table cal-table"><caption>고정 구간 · 실제 비율 = 잠재 DOI ÷ n · 차이 = 평균 p − 실제 비율</caption>
        <thead><tr><th scope="col">p 구간</th><th scope="col" class="num">n</th><th scope="col" class="num">평균 p</th><th scope="col" class="num">실제 비율</th><th scope="col" class="num">차이</th></tr></thead>
        <tbody>${d.bins.map(b => row(b)).join('')}${d.hi ? row(d.hi, 'hi-row') : ''}</tbody></table></div></div>`;
  }

  // ---------- 5. receipt ----------
  function renderReceipt() {
    const d = state.data; if (!d) return;
    const size = state.bytes ? state.bytes.byteLength : null;
    const sha = state.sha === null ? '계산 중…' : state.sha === false ? '이 브라우저에서 계산할 수 없음 (crypto.subtle 없음)' : state.sha;
    const okAudit = d.audit_status === 'passed';
    const rows = [
      ['파일', `<code>${DATA_FILE}</code>`],
      ['바이트', size != null ? `${count(size)} bytes` : '—'],
      ['이 파일의 SHA-256 (브라우저 계산)', `<code class="hash">${e(sha)}</code>`],
      ['감사 상태', `<span class="${okAudit ? 'ok' : 'bad'}">${e(d.audit_status)}</span>`],
      ['감사 기록 SHA-256', `<code class="hash">${e(d.audit_sha256)}</code>`],
      ['동결 영수증 SHA-256', `<code class="hash">${e(d.freeze_sha256)}</code>`],
      ['진단 스크립트 SHA-256', `<code class="hash">${e(d.diagnostic_script_sha256)}</code>`],
      ['kind · schema', `<code>${e(d.kind)}</code> · ${e(d.schema_version)}`],
      ['scope', `<code>${e(d.scope)}</code>`],
      ['주 결과 대체', d.replaces_primary === false ? '아니오 (사전 등록 주 결과 유지)' : `<span class="bad">${e(d.replaces_primary)}</span>`],
      ['생성 시각', `<code>${e(d.generated_at)}</code>`]];
    const defs = obj(d.definitions) || {};
    const defList = Object.entries(defs).filter(([, v]) => typeof v === 'string');
    $('receiptBody').innerHTML = `<dl class="receipt">${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl>
      <div class="receipt-actions"><button type="button" class="control primary-action" id="download2">원본 JSON 내려받기 (${size != null ? count(size) + ' bytes' : '—'})</button><a class="control" href="./inspection-evidence.html">검사 v3 근거 화면</a></div>
      <p class="scope-note">이 파일의 SHA-256은 내려받은 바이트를 확인하기 위한 값입니다. 감사·동결 해시는 각각 <code>audit.json</code>과 동결 영수증을 가리키며, 이 화면은 그 원본을 다시 검증하지 않습니다. 검증은 진단 스크립트가 내보내기 전에 했습니다.</p>
      ${defList.length ? `<details class="raw"><summary>JSON 용어 정의 ${defList.length}개 (원문)</summary><dl class="defs" lang="en">${defList.map(([k, v]) => `<div><dt><code>${e(k)}</code></dt><dd>${e(v)}</dd></div>`).join('')}</dl></details>` : ''}`;
    $('download2').addEventListener('click', download);
  }

  function renderLimitations() {
    const list = arr(state.data.limitations).filter(x => typeof x === 'string');
    const fixed = ['이 화면은 개선 효과를 주장하지 않습니다. 실제 SEM 이미지 판정, 높은 신뢰도 보장, Jev 사용을 의미하지 않습니다.',
      '잠재 DOI와 그로부터 나온 모든 비율은 합성 생성기의 사후 진실로만 계산됩니다. 실제 공정에서 검토하지 않은 다이는 결과를 알 수 없습니다.'];
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

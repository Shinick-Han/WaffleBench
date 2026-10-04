(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const DATA_URL = './data/inspection-images-v2.json';
  const DATA_FILE = 'web/data/inspection-images-v2.json';
  const ROLE_ORDER = ['baseline', 'primary', 'secondary_descriptive'];
  const ROLE = {baseline:'기준선', primary:'주 방법 · 사전 지정', secondary_descriptive:'부차 · 기술용'};
  // Labels only; pass/fail always comes from evaluation.primary_endpoint.decision.
  const CONDITIONS = [
    ['recall_gain_at_least_10pp', '결함 재현율 증가 점추정 ≥ +10 %p'],
    ['bootstrap_ci_lower_above_zero', '재현율 증가 95% CI 하한 > 0'],
    ['primary_far_at_most_10pct', '주 방법 정상 오경보율 ≤ 10%'],
  ];
  const KIND = {fn:'놓친 결함 · FN', fp:'오경보 · FP'};
  const state = {data:null, bytes:null, sha:null, methods:[], method:null, highlight:null,
    errKind:'all', errType:'all', errSort:'near'};

  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const arr = v => Array.isArray(v) ? v : [];
  const text = v => v == null || v === '' ? '—' : String(v);
  const e = v => text(v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = (n, d=2) => finite(n) ? n.toLocaleString('ko-KR', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
  const signed = (n, d=2) => finite(n) ? (n > 0 ? '+' : n < 0 ? '−' : '') + fmt(Math.abs(n), d) : '—';
  const count = n => finite(n) ? n.toLocaleString('ko-KR') : '—';
  // Rates are counts over 100 images per class: whole percentage points only.
  const pp = n => finite(n) ? signed(Math.round(n * 100), 0) : '—';
  const rate = n => finite(n) && n >= 0 && n <= 1 ? `${Math.round(n * 100)}%` : '—';
  const ofN = (k, n) => finite(k) && finite(n) && n > 0 ? `${count(k)} / ${count(n)}` : '—';
  const ci = c => Array.isArray(c) && c.length === 2 && finite(c[0]) && finite(c[1]) ? `${pp(c[0])} ~ ${pp(c[1])} %p` : '—';
  const shortId = p => typeof p === 'string' ? p.split('/').slice(-2).join('/') : '—';
  const safeUrl = u => typeof u === 'string' && /^https:\/\/[^\s"'<>]+$/.test(u) ? u : null;
  const empty = (title, detail='') => `<div class="empty"><strong>${e(title)}</strong>${detail ? e(detail) + ' · ' : ''}파일 <code>${DATA_FILE}</code>의 해당 항목이 비어 있거나 검증되지 않았습니다.</div>`;
  const ICON = {
    met:'<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M2.5 7.4 5.6 10.4 11.5 3.8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    missed:'<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3.5 3.5l7 7M10.5 3.5l-7 7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
    none:'<svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true"><path d="M3 7h8" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
  };
  const tri = v => v === true ? 'met' : v === false ? 'missed' : 'none';

  // ---------- loading ----------
  function showState(html) { const box = $('loadState'); box.hidden = false; box.innerHTML = html; }
  function retryButton() { const b = document.createElement('button'); b.type = 'button'; b.className = 'control'; b.textContent = '다시 불러오기'; b.addEventListener('click', load); $('loadState').append(b); }
  async function load() {
    $('app').hidden = true; $('download').disabled = true;
    showState('<div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div><p>저장된 PCB 사진 검사 근거를 불러오는 중…</p>');
    let response;
    try { response = await fetch(DATA_URL, {cache:'no-store'}); }
    catch (error) {
      console.error('Inspection images fetch:', error);
      showState(`<h2>근거 파일에 접근할 수 없습니다</h2><p>네트워크 또는 로컬 파일 열기 제한으로 <code>${DATA_FILE}</code> 파일을 읽지 못했습니다. 정적 서버로 <code>web/</code>을 연 뒤 다시 시도하세요.</p>`);
      return retryButton();
    }
    if (response.status === 404) {
      showState(`<h2>아직 검증된 결과가 없습니다</h2><p>PCB 사진 검사 근거가 아직 내보내지지 않았습니다. 코디네이터가 감사를 마친 결과를 <code>${DATA_FILE}</code> 파일로 내보내면 이 화면이 채워집니다. 예시 수치는 표시하지 않습니다.</p>`);
      return retryButton();
    }
    try {
      if (!response.ok) throw Error(`HTTP ${response.status}`);
      const bytes = new Uint8Array(await response.arrayBuffer());
      const data = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(bytes));
      if (!obj(data)) throw Error('최상위 값이 객체가 아닙니다');
      if (data.schema_version !== 1) throw Error(`지원하지 않는 schema_version ${data.schema_version}`);
      if (data.kind !== 'inspection_images_v2_evidence' || data.scope !== 'real_pcb_photography_not_wafer_sem') throw Error('PCB 사진 근거 형식과 범위가 일치하지 않습니다');
      state.data = data; state.bytes = bytes; state.sha = null;
      prepare(data);
      $('loadState').hidden = true; $('app').hidden = false; $('download').disabled = false;
      $('download').textContent = `원본 JSON 내려받기 · ${count(bytes.byteLength)} B`;
      renderAll();
      digest(bytes);
    } catch (error) {
      console.error('Inspection images parse:', error);
      showState(`<h2>근거 파일을 해석할 수 없습니다</h2><p>${e(error.message)}. <code>${DATA_FILE}</code>의 내보내기 형식을 확인한 뒤 다시 시도하세요.</p>`);
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
    if (out) out.textContent = state.sha || '이 브라우저 환경에서 계산할 수 없음 (보안 컨텍스트 필요)';
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
    const when = ev.evaluated_at && !Number.isNaN(Date.parse(ev.evaluated_at)) ? new Date(ev.evaluated_at).toLocaleString('ko-KR') : null;
    const items = [
      ['연구', ev.study], ['범위', state.data.scope === 'real_pcb_photography_not_wafer_sem' ? '실제 PCB 사진 · 웨이퍼 SEM 아님' : state.data.scope],
      ['공식 테스트', finite(c.test_images) ? `${count(c.test_images)}장 (정상 ${count(c.normal)} · 결함 ${count(c.anomaly)})` : null],
      ['자료', ev.data_mode === 'real' ? '실측 내보내기' : ev.data_mode], ['평가 시각', when],
      ['감사', state.data.audit?.status === 'pass' ? '통과' : state.data.audit?.status],
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
    v.innerHTML = `${ICON[tri(s)]}${s === true ? '사전 기준 세 가지 모두 충족' : s === false ? '사전 기준 미충족' : '판정 기록 없음'}`;
    if (!pe || !P || !B) { $('primaryBody').innerHTML = empty('주 결과가 없습니다', 'primary_endpoint 또는 primary/baseline 방법 누락'); return; }
    const gain = obj(pe.primary_vs_baseline_bootstrap?.defect_recall_gain) || {};
    const far = obj(pe.primary_vs_baseline_bootstrap?.normal_far_difference) || {};
    const pt = ft(P), bt = ft(B);
    const nd = defects(P), nn = normals(P);
    const conds = CONDITIONS.map(([key, label]) => {
      const st = tri(dec?.[key]);
      return `<li class="cond ${st}">${ICON[st]}<span>${e(label)}</span><b>${st === 'met' ? '충족' : st === 'missed' ? '미충족' : '기록 없음'}</b></li>`;
    }).join('');
    const reqs = arr(def?.success_requires_all).filter(x => typeof x === 'string');
    const bs = obj(def?.bootstrap) || {};
    $('primaryBody').innerHTML = `
      <div class="primary-grid">
        <div>
          <p class="statement">결함 사진 <span class="num">${count(nd)}</span>장 가운데 주 방법은 <span class="num hit">${count(pt.true_positive)}장</span>, 기준선은 <span class="num">${count(bt.true_positive)}장</span>을 잡았습니다.</p>
          <p class="statement second">정상 사진 <span class="num">${count(nn)}</span>장 가운데 결함으로 잘못 거른 사진은 <span class="num">${count(bt.false_positive)}장</span>에서 <span class="num hit">${count(pt.false_positive)}장</span>으로 줄었습니다.</p>
          <dl class="figures">
            <div><dt>결함 재현율</dt><dd>${rate(bt.defect_recall)} → ${rate(pt.defect_recall)}</dd></div>
            <div><dt>재현율 증가 · 주 지표</dt><dd>${pp(gain.estimate)} %p <small>95% CI ${ci(gain.ci95)}</small></dd></div>
            <div><dt>정상 오경보율</dt><dd>${rate(bt.normal_false_alarm_rate)} → ${rate(pt.normal_false_alarm_rate)}</dd></div>
            <div><dt>오경보율 차이 · 부차</dt><dd>${pp(far.estimate)} %p <small>95% CI ${ci(far.ci95)}</small></dd></div>
          </dl>
        </div>
        <figure class="ci-figure">${ciChart(gain)}<figcaption>결함 재현율 증가(주 방법 − 기준선)의 점추정과 95% 백분위 구간 · 리샘플 ${count(gain.resamples ?? bs.resamples)}회 · 점선은 사전 기준 +10 %p</figcaption></figure>
      </div>
      <div class="conditions">
        <h3>사전 등록 성공 조건 <span class="tag">내보낸 판정 그대로</span></h3>
        <ul class="cond-list">${conds}</ul>
        ${reqs.length ? `<p class="footnote">등록 원문: ${reqs.map(r => `<span class="quote">${e(r)}</span>`).join(' · ')}</p>` : ''}
      </div>
      <p class="caveat"><strong>해상도만의 효과가 아닙니다.</strong> 주 방법은 기준선과 비교해 입력 해상도, 패치 격자, coreset 크기, 점수 집계를 한꺼번에 바꿨습니다(각 구성은 아래 비교 표). 이 차이는 파이프라인 전체의 복합 효과이며 어느 한 요인의 몫으로 나눌 수 없습니다.${def?.attribution ? ` <span class="quote">${e(def.attribution)}</span>` : ''}</p>`;
  }

  function ciChart(g) {
    const c = Array.isArray(g.ci95) ? g.ci95 : [];
    if (!finite(g.estimate) || !finite(c[0]) || !finite(c[1])) return `<div class="empty"><strong>신뢰구간 기록 없음</strong>defect_recall_gain.ci95가 비어 있습니다.</div>`;
    const lo = Math.min(-0.05, c[0] - 0.05), hi = Math.max(0.35, c[1] + 0.05);
    const W = 560, L = 18, R = 18, x = v => L + (v - lo) / (hi - lo) * (W - L - R);
    const ticks = []; for (let t = Math.ceil(lo * 20) / 20; t <= hi + 1e-9; t += 0.05) ticks.push(Math.round(t * 100) / 100);
    const axis = ticks.map(t => `<line x1="${x(t)}" x2="${x(t)}" y1="34" y2="96" class="grid"/><text x="${x(t)}" y="116" text-anchor="middle">${pp(t)}</text>`).join('');
    return `<svg viewBox="0 0 ${W} 124" role="img" aria-label="재현율 증가 ${pp(g.estimate)} %p, 95% CI ${ci(c)}, 기준 +10 %p, 0 기준선">
      ${axis}
      <line x1="${x(0)}" x2="${x(0)}" y1="22" y2="100" class="zero"/><text x="${x(0)}" y="16" text-anchor="middle">0</text>
      <line x1="${x(0.10)}" x2="${x(0.10)}" y1="22" y2="100" class="bar10"/><text x="${x(0.10)}" y="16" text-anchor="middle">기준 +10</text>
      <line x1="${x(c[0])}" x2="${x(c[1])}" y1="65" y2="65" class="ci"/>
      <line x1="${x(c[0])}" x2="${x(c[0])}" y1="55" y2="75" class="ci"/><line x1="${x(c[1])}" x2="${x(c[1])}" y1="55" y2="75" class="ci"/>
      <circle cx="${x(g.estimate)}" cy="65" r="7" class="est"/>
      <text x="${x(g.estimate)}" y="46" text-anchor="middle" class="ink">${pp(g.estimate)} %p</text>
      <text x="${x(c[0])}" y="92" text-anchor="middle">${pp(c[0])}</text><text x="${x(c[1])}" y="92" text-anchor="middle">${pp(c[1])}</text>
    </svg>`;
  }

  // ---------- comparison table ----------
  function renderCompare() {
    const ms = state.methods;
    if (!ms.length) { $('compareBody').innerHTML = empty('방법 기록이 없습니다', 'evaluation.methods 누락'); return; }
    const col = (m, html) => `<td class="${m.role === 'primary' ? 'is-primary ' : ''}num">${html}</td>`;
    const row = (label, f, cls='') => `<tr class="${cls}"><th scope="row">${label}</th>${ms.map(m => col(m, f(m))).join('')}</tr>`;
    const sec = obj(state.data.evaluation?.secondary_descriptive?.highres448max_vs_baseline224_bootstrap);
    $('compareBody').innerHTML = `
      <div class="table-scroll" role="region" tabindex="0" aria-label="세 방법 비교 표 · 가로 스크롤 가능">
        <table class="compare-table">
          <caption>각 방법은 학습용 정상 사진에서 정한 자기 임계값(정상 보정 점수의 95번째 백분위)으로 판정합니다. 점수 &gt; 임계값이면 결함.</caption>
          <thead><tr><th scope="col">항목</th>${ms.map(m => `<th scope="col" class="num${m.role === 'primary' ? ' is-primary' : ''}"><span class="mname">${e(m.id)}</span><span class="role ${e(m.role)}">${e(ROLE[m.role] || m.role)}</span></th>`).join('')}</tr></thead>
          <tbody>
            ${row('구성', m => `<span class="cfg">${e(m.label)}</span>`, 'cfg-row')}
            ${row('결함 재현율 <small>주 지표</small>', m => `<b>${ofN(ft(m).true_positive, defects(m))}</b> <small>${rate(ft(m).defect_recall)}</small>`, 'key')}
            ${row('잡은 결함 · TP', m => count(ft(m).true_positive))}
            ${row('놓친 결함 · FN', m => count(ft(m).false_negative))}
            ${row('오경보 · FP', m => count(ft(m).false_positive))}
            ${row('정상 통과 · TN', m => count(ft(m).true_negative))}
            ${row('정상 오경보율', m => `<b>${ofN(ft(m).false_positive, normals(m))}</b> <small>${rate(ft(m).normal_false_alarm_rate)}</small>`, 'key')}
            ${row('고정 임계값 <small>방법 고유 단위</small>', m => fmt(ft(m).threshold, 2))}
            ${row('이미지 AUROC <small>순위 지표 · 부차</small>', m => fmt(m.image_auroc_secondary, 3), 'rank-row')}
            ${row('평균 정밀도 AP <small>순위 지표 · 부차</small>', m => fmt(m.image_average_precision_secondary, 3), 'rank-row')}
          </tbody>
        </table>
      </div>
      <p class="footnote">AUROC·AP는 점수의 순서만 평가하며, 고정 임계값에서 실제로 몇 장을 맞혔는지를 말하지 않습니다. 정확도 주장은 위 판정 수로만 합니다.${sec ? ` 448max(부차·기술용)와 기준선의 재현율 차이는 ${pp(sec.defect_recall_gain?.estimate)} %p (95% CI ${ci(sec.defect_recall_gain?.ci95)})로 기록되어 있으나 성공 판정에는 쓰지 않습니다.` : ''}</p>`;
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
      s.textContent = `${m.id} · ${ROLE[m.role] || m.role || '역할 없음'}`;
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
    if (!id) { $('scoresBody').innerHTML = empty('점수 기록이 없습니다', 'evaluation.methods 누락'); return; }
    const {m, thr, rows, skipped} = scoreRows(id);
    if (!rows.length) { $('scoresBody').innerHTML = empty('이 방법의 이미지별 점수가 없습니다', `evaluation.images[].${id}_score 누락`); return; }
    const tally = {TP:0, FN:0, FP:0, TN:0}; rows.forEach(r => tally[r.cls]++);
    const t = ft(m);
    const agrees = tally.TP === t.true_positive && tally.FN === t.false_negative && tally.FP === t.false_positive && tally.TN === t.true_negative;
    const sorted = [...rows].sort((a, b) => b.margin - a.margin);
    $('scoresBody').innerHTML = `
      <figure class="strip-figure">
        <div class="strip" id="strip"></div>
        <figcaption>
          <span class="key"><i class="dot defect" aria-hidden="true"></i>결함 사진 (채움)</span>
          <span class="key"><i class="dot normal" aria-hidden="true"></i>정상 사진 (테두리)</span>
          <span class="key"><i class="dot err" aria-hidden="true"></i>잘못 판정 (진한 테두리)</span>
          <span class="tally">이 그림에서 다시 센 판정 · 결함 ${count(tally.TP)} 잡음 / ${count(tally.FN)} 놓침 · 정상 ${count(tally.FP)} 오경보 / ${count(tally.TN)} 통과
          ${agrees ? '<b class="ok">내보낸 집계와 일치</b>' : '<b class="bad">내보낸 집계와 다름 — 원본을 확인하세요</b>'}${skipped ? ` · 점수 없음 ${count(skipped)}행 제외` : ''}</span>
        </figcaption>
      </figure>
      <p class="footnote">가로축은 <code>${e(id)}</code> 점수에서 그 방법의 고정 임계값 ${fmt(thr, 2)}을 뺀 값(방법 고유 단위)입니다. 0보다 오른쪽이면 결함으로 판정합니다.</p>
      <details class="fallback"><summary>점수 표로 보기 · ${count(rows.length)}행</summary>
        <div class="table-scroll record-scroll" role="region" tabindex="0" aria-label="${e(id)} 이미지별 점수 표 · 스크롤 가능">
          <table class="score-table"><caption>임계값 대비 여유가 큰 순 · ${e(id)}</caption>
            <thead><tr><th scope="col">이미지</th><th scope="col">실제</th><th scope="col" class="num">점수</th><th scope="col" class="num">임계값 대비</th><th scope="col">판정</th></tr></thead>
            <tbody>${sorted.map(r => `<tr class="${r.cls === 'FN' || r.cls === 'FP' ? 'is-error' : ''}"><th scope="row" class="img">${e(shortId(r.image))}</th><td>${r.label === 1 ? '결함' : '정상'}</td><td class="num">${fmt(r.score, 2)}</td><td class="num">${signed(r.margin, 2)}</td><td>${verdictText(r.cls)}</td></tr>`).join('')}</tbody>
          </table>
        </div>
      </details>`;
    drawStrip(id, rows);
  }
  const verdictText = c => ({TP:'결함 · 잡음 TP', FN:'결함 · 놓침 FN', FP:'정상 · 오경보 FP', TN:'정상 · 통과 TN'})[c];

  function drawStrip(id, rows) {
    const host = $('strip');
    if (!host) return;
    const W = Math.max(300, Math.round(host.clientWidth || 900));
    const small = W < 560, r = small ? 3.4 : 4.6, gap = 0.6;
    const L = small ? 46 : 64, R = 12, top = 26, laneH = small ? 104 : 128, H = top + laneH * 2 + 34;
    let lo = Math.min(0, ...rows.map(d => d.margin)), hi = Math.max(0, ...rows.map(d => d.margin));
    const pad = (hi - lo) * 0.04 || 1; lo -= pad; hi += pad;
    const x = v => L + (v - lo) / (hi - lo) * (W - L - R);
    const lanes = [{label:'결함', key:1, cy: top + laneH / 2}, {label:'정상', key:0, cy: top + laneH * 1.5}];
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
    const svg = `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" aria-label="${e(id)} 점수 분포: 결함 ${rows.filter(d => d.label === 1).length}장, 정상 ${rows.filter(d => d.label === 0).length}장. 자세한 값은 아래 점수 표에 있습니다.">
      <rect x="${x(0)}" y="${top - 4}" width="${W - R - x(0)}" height="${laneH * 2 + 8}" class="flag-zone"/>
      <text x="${x(0) + 6}" y="${top - 10}" class="zone-label">결함 판정 →</text>
      <text x="${x(0) - 6}" y="${top - 10}" text-anchor="end" class="zone-label">← 정상 판정</text>
      ${ticks.map(t => `<line x1="${x(t)}" x2="${x(t)}" y1="${top}" y2="${top + laneH * 2}" class="grid"/><text x="${x(t)}" y="${H - 12}" text-anchor="middle">${signed(t, step < 1 ? 1 : 0)}</text>`).join('')}
      <line x1="${L}" x2="${W - R}" y1="${top + laneH}" y2="${top + laneH}" class="lane-rule"/>
      ${lanes.map(l => `<text x="${L - 10}" y="${l.cy + 4}" text-anchor="end" class="ink">${l.label}</text>`).join('')}
      <line x1="${x(0)}" x2="${x(0)}" y1="${top - 4}" y2="${top + laneH * 2 + 4}" class="thr"/>
      ${marks.map(k => `<circle cx="${k.cx.toFixed(1)}" cy="${k.cy.toFixed(1)}" r="${r}" class="pt ${k.d.label === 1 ? 'defect' : 'normal'}${err(k.d.cls) ? ' err' : ''}"><title>${e(shortId(k.d.image))} · ${verdictText(k.d.cls)} · 임계값 대비 ${signed(k.d.margin, 2)}</title></circle>`).join('')}
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
    if (!obj(state.data.posthoc_errors)) { $('errorsBody').innerHTML = empty('사후 오류 검토가 없습니다', 'posthoc_errors 누락'); return; }
    const types = [...new Set(all.map(r => r.defect_type).filter(t => typeof t === 'string'))].sort();
    const P = byRole('primary');
    const mismatch = P && (fn !== ft(P).false_negative || fp !== ft(P).false_positive);
    const byType = Object.entries(obj(pe.anomaly_by_defect_type) || {}).filter(([, v]) => obj(v));
    const flips = obj(pe.paired_flips_vs_baseline224);
    $('errorsBody').innerHTML = `
      <p class="err-lead">놓친 결함 <b class="num">${count(fn)}장</b>, 오경보 <b class="num">${count(fp)}장</b>${mismatch ? ' <b class="bad">· 판정 집계와 행 수가 다릅니다</b>' : ''}</p>
      <div class="table-tools">
        <label class="field">종류<select id="errKind"><option value="all">전체</option><option value="fn">놓친 결함 · FN</option><option value="fp">오경보 · FP</option></select></label>
        <label class="field">결함 주석<select id="errType"><option value="all">전체 주석</option>${types.map(t => `<option value="${e(t)}">${e(t)}</option>`).join('')}</select></label>
        <label class="field">정렬<select id="errSort"><option value="near">임계값에 가까운 순</option><option value="far">임계값에서 먼 순</option><option value="id">이미지 ID 순</option></select></label>
        <span id="errCount" class="count-line" role="status" aria-live="polite"></span>
      </div>
      <div class="table-scroll record-scroll" role="region" tabindex="0" aria-label="주 방법 오류 사진 표 · 스크롤 가능">
        <table class="err-table"><caption>여유 = 주 방법 점수 − 주 방법 임계값. 음수인 결함은 놓쳤고, 양수인 정상은 오경보입니다.</caption>
          <thead><tr><th scope="col">이미지</th><th scope="col">종류</th><th scope="col">결함 주석</th><th scope="col" class="num">주 방법 여유</th><th scope="col">기준선 판정</th><th scope="col">448max 판정</th><th scope="col"><span class="sr">분포에서 보기</span></th></tr></thead>
          <tbody id="errRows"></tbody>
        </table>
      </div>
      <div class="err-aside">
        <div>
          <h3>결함 주석별 놓침 <span class="tag">기술용 · 표본 작음</span></h3>
          ${byType.length ? `<div class="table-scroll" role="region" tabindex="0" aria-label="결함 주석별 놓침 표"><table class="type-table"><thead><tr><th scope="col">주석</th><th scope="col" class="num">테스트 장수</th><th scope="col" class="num">주 방법 놓침</th></tr></thead><tbody>${byType.map(([t, v]) => `<tr><th scope="row">${e(t)}</th><td class="num">${count(v.test)}</td><td class="num">${count(v.primary448_missed)} / ${count(v.test)}</td></tr>`).join('')}</tbody></table></div>
          <p class="footnote">예: missing 주석은 ${typeOf(byType, 'missing')}로 장수가 적어 비율로 일반화하지 않습니다. 사후 검토이며 사전 지표가 아닙니다.</p>` : empty('주석별 집계가 없습니다')}
        </div>
        <div>
          <h3>기준선 대비 판정이 바뀐 사진</h3>
          ${flips ? `<dl class="state-list">
            <dt>주 방법만 잡은 결함</dt><dd>${count(flips.defect_caught_only_by_primary)}장</dd>
            <dt>기준선만 잡은 결함</dt><dd>${count(flips.defect_caught_only_by_baseline)}장</dd>
            <dt>기준선만 오경보한 정상</dt><dd>${count(flips.normal_flagged_only_by_baseline)}장</dd>
            <dt>주 방법만 오경보한 정상</dt><dd>${count(flips.normal_flagged_only_by_primary)}장</dd></dl>` : empty('짝 비교 기록이 없습니다')}
        </div>
      </div>`;
    const kind = $('errKind'), type = $('errType'), sort = $('errSort');
    kind.value = state.errKind; type.value = types.includes(state.errType) ? state.errType : 'all'; sort.value = state.errSort;
    kind.addEventListener('change', () => { state.errKind = kind.value; fillErrors(); });
    type.addEventListener('change', () => { state.errType = type.value; fillErrors(); });
    sort.addEventListener('change', () => { state.errSort = sort.value; fillErrors(); });
    fillErrors();
  }
  function typeOf(byType, t) { const v = byType.find(([k]) => k === t)?.[1]; return v ? `${count(v.primary448_missed)} / ${count(v.test)}장` : '기록 없음'; }
  const flag = (v, kind) => v === true ? `<span class="flag yes">${kind === 'fn' ? '잡음' : '오경보'}</span>` : v === false ? `<span class="flag no">${kind === 'fn' ? '놓침' : '통과'}</span>` : '—';
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
    $('errCount').textContent = `${count(rows.length)} / ${count(all.length)}장 표시`;
    const P = byRole('primary');
    $('errRows').innerHTML = rows.length ? rows.map(r => `<tr class="${r.image === state.highlight ? 'is-current' : ''}">
      <th scope="row" class="img">${e(shortId(r.image))}</th>
      <td><span class="kind ${r.kind}"><i aria-hidden="true"></i>${KIND[r.kind]}</span></td>
      <td>${r.kind === 'fp' ? '<span class="muted">정상</span>' : e(r.defect_type)}</td>
      <td class="num">${signed(mg(r), 2)}</td>
      <td>${flag(r.baseline224_flagged, r.kind)}</td>
      <td>${flag(r.highres448max_flagged, r.kind)}</td>
      <td>${P ? `<button type="button" class="step-link" data-image="${e(r.image)}" aria-label="${e(shortId(r.image))} 점수 분포에서 보기">분포에서 보기</button>` : ''}</td></tr>`).join('')
      : `<tr><td colspan="7"><div class="empty"><strong>조건에 맞는 사진이 없습니다</strong><button type="button" class="control" id="errReset">필터 초기화</button></div></td></tr>`;
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
    const live = $('scoreLive'); if (live) live.textContent = `${P.id} 점수 분포에 ${shortId(image)} 위치를 표시했습니다.`;
  }

  // ---------- predecessor ----------
  function renderPredecessor() {
    const p = obj(state.data.predecessor);
    if (!p) { $('predBody').innerHTML = empty('선행 PCB1 기록이 없습니다', 'predecessor 누락'); return; }
    const ms = Object.entries(obj(p.methods) || {}).filter(([, m]) => obj(m));
    const g = obj(p.methods?.global), pa = obj(p.methods?.patch);
    const gt = ft(g), pt = ft(pa);
    $('predBody').innerHTML = `
      ${g && pa ? `<p class="statement small">PCB1에서 patch 방법은 결함 <span class="num">${count(gt.true_positive)}장 → ${count(pt.true_positive)}장</span>으로 덜 잡고, 정상 오경보는 <span class="num">${count(gt.false_positive)}장 → ${count(pt.false_positive)}장</span>으로 늘었습니다. 이 부정적 결과는 PCB2의 성공으로 뒤집히지 않습니다.</p>` : ''}
      ${ms.length ? `<div class="table-scroll" role="region" tabindex="0" aria-label="선행 PCB1 방법 표 · 가로 스크롤 가능"><table class="pred-table">
        <caption>${e(p.study)} · 테스트 ${count(p.counts?.test_images)}장 (정상 ${count(p.counts?.normal)} · 결함 ${count(p.counts?.anomaly)}) · 신뢰구간 없음</caption>
        <thead><tr><th scope="col">방법</th><th scope="col" class="num">결함 재현율</th><th scope="col" class="num">TP</th><th scope="col" class="num">FN</th><th scope="col" class="num">FP</th><th scope="col" class="num">TN</th><th scope="col" class="num">정상 오경보율</th><th scope="col" class="num">AUROC <small>순위</small></th></tr></thead>
        <tbody>${ms.map(([id, m]) => { const t = ft(m); return `<tr><th scope="row"><span class="mname">${e(id)}</span><span class="cfg">${e(m.label)}</span></th><td class="num"><b>${ofN(t.true_positive, defects(m))}</b> <small>${rate(t.defect_recall)}</small></td><td class="num">${count(t.true_positive)}</td><td class="num">${count(t.false_negative)}</td><td class="num">${count(t.false_positive)}</td><td class="num">${count(t.true_negative)}</td><td class="num"><b>${ofN(t.false_positive, normals(m))}</b> <small>${rate(t.normal_false_alarm_rate)}</small></td><td class="num">${fmt(m.image_auroc, 3)}</td></tr>`; }).join('')}</tbody>
      </table></div>` : empty('PCB1 방법 기록이 없습니다')}
      ${arr(p.limitations).length ? `<ul class="plain">${arr(p.limitations).map(l => `<li>${e(l)}</li>`).join('')}</ul>` : ''}`;
  }

  function renderReplication() {
    const r = obj(state.data.replication), ev = obj(r?.evaluation), au = obj(r?.audit);
    if (!r || r.scope !== 'separate_real_pcb3_replication' || ev?.study !== 'inspection-image-replication-v3-pcb3' || au?.status !== 'pass' || au.freeze_digest !== ev.freeze_digest) {
      $('replicationBody').innerHTML = empty('아직 검증된 PCB3 기록이 없습니다', '독립 감사와 동결 기록이 일치하는 별도 결과가 필요합니다.'); return;
    }
    const g = obj(ev.primary_endpoint?.primary_vs_baseline_bootstrap?.defect_recall_gain) || {};
    const ms = Object.entries(obj(ev.methods) || {}).filter(([,m]) => obj(m));
    const primary = ft(ev.methods?.primary448), passed = ev.primary_endpoint?.decision?.success === true;
    $('replicationBody').innerHTML = `<p class="statement small">PCB3에서 개선 폭 <span class="num">${signed(finite(g.estimate) ? g.estimate*100 : null,0)}%p</span> · 95% CI [${arr(g.ci95).map(v=>signed(finite(v)?v*100:null,0)).join(', ')}]%p. 사전 지정한 세 조건 ${passed ? '통과' : '미충족'} · 주 방법은 결함 ${count(primary.false_negative)} / ${count(ev.counts?.anomaly)}개를 여전히 놓쳤습니다.</p>
      <p class="caveat">${e(r.selection_disclosure)}. 통과 기준은 개선 ≥10%p, CI 하한 >0, 오경보 ≤10%입니다. +11%p는 개선 기준을 한 장 차이로 넘었고 CI 하한도 +1%p에 그쳤습니다. 효과 크기가 PCB2와 같다고 입증하지 않습니다.</p>
      <div class="table-scroll" role="region" tabindex="0" aria-label="별도 PCB3 검증 표 · 가로 스크롤 가능"><table class="pred-table"><caption>${e(ev.study)} · 결함 ${count(ev.counts?.anomaly)}장 · 정상 ${count(ev.counts?.normal)}장</caption><thead><tr><th scope="col">방법</th><th scope="col">역할</th><th scope="col" class="num">결함 검출</th><th scope="col" class="num">미탐 FN</th><th scope="col" class="num">정상 오경보</th><th scope="col" class="num">오경보율</th></tr></thead><tbody>${ms.map(([id,m])=>{const t=ft(m);return `<tr><th scope="row">${e(id)}</th><td>${e(m.role)}</td><td class="num">${count(t.true_positive)} / ${count(ev.counts?.anomaly)} · ${rate(t.defect_recall)}</td><td class="num">${count(t.false_negative)}</td><td class="num">${count(t.false_positive)} / ${count(ev.counts?.normal)}</td><td class="num">${rate(t.normal_false_alarm_rate)}</td></tr>`}).join('')}</tbody></table></div>
      <p class="caveat">secondary는 설명용으로 유지하며, 57%의 더 높은 검출률을 보고 주 방법을 바꾸지 않습니다. 복합 변경·단일 seed·촬영 조건의 한계는 그대로입니다. 실제 웨이퍼/SEM/팹 성능은 측정하지 않았습니다.</p>
      <dl class="hash-list"><dt>별도 freeze digest · 감사 일치</dt><dd><code>${e(ev.freeze_digest)}</code></dd><dt>별도 test-scores SHA-256</dt><dd><code>${e(ev.test_scores_sha256)}</code></dd></dl>`;
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
          <h3>고정과 감사</h3>
          <dl class="hash-list">
            <dt>감사 상태</dt><dd>${au.status === 'pass' ? '<b class="ok">통과</b>' : e(au.status)}</dd>
            <dt>freeze digest</dt><dd><code>${e(ev.freeze_digest)}</code>${freezeMatch ? '<span class="ok"> · 감사 기록과 일치</span>' : au.freeze_digest ? '<span class="bad"> · 감사 기록과 다름</span>' : ''}</dd>
            <dt>test-scores SHA-256</dt><dd><code>${e(ev.test_scores_sha256)}</code>${scoresMatch ? '<span class="ok"> · 감사 입력과 일치</span>' : ''}</dd>
            ${inputs.map(([k, v]) => `<dt>감사 입력 · ${e(k)}</dt><dd><code>${e(v)}</code></dd>`).join('')}
          </dl>
        </div>
        <div>
          <h3>부트스트랩과 내려받은 파일</h3>
          <dl class="hash-list">
            <dt>리샘플</dt><dd>${count(bs.resamples)}회 · ${e(bs.interval)}</dd>
            <dt>리샘플 단위</dt><dd>${e(bs.unit)}</dd>
            <dt>seed stream</dt><dd><code>${e(seeds || null)}</code></dd>
            <dt>파일</dt><dd><code>${DATA_FILE}</code></dd>
            <dt>정확한 크기</dt><dd>${count(state.bytes?.byteLength)} bytes</dd>
            <dt>파일 SHA-256 <small>브라우저 계산</small></dt><dd><code id="fileSha">${state.sha ? e(state.sha) : state.sha === false ? '이 브라우저 환경에서 계산할 수 없음 (보안 컨텍스트 필요)' : '계산 중…'}</code></dd>
          </dl>
        </div>
      </div>
      <p class="caveat"><strong>신뢰구간이 다루지 않는 것.</strong> 구간은 테스트 이미지 단위로 리샘플한 값입니다. 위 주 시험의 구간은 PCB2 한 범주, 한 번의 seed를 다룹니다. 별도 PCB3 검증은 다른 범주에서의 한 번의 결과이며, 범주 전반·카메라·조명·라인 변화의 변동을 측정한 구간이 아닙니다.</p>`;
  }

  // ---------- sources ----------
  function renderSources() {
    const a = obj(state.data.attribution);
    if (!a) { $('sourcesBody').innerHTML = empty('출처 기록이 없습니다', 'attribution 누락'); return; }
    const link = (u, label) => { const s = safeUrl(u); return s ? `<a href="${e(s)}" rel="noopener noreferrer" target="_blank">${e(label)}<span class="sr"> (새 창)</span></a>` : e(label); };
    $('sourcesBody').innerHTML = `<ul class="refs">
      <li><span class="ref-k">데이터</span><span>${link(a.url, a.dataset)} · 라이선스 <b>${e(a.license)}</b>. 사진은 이 화면에 포함하거나 다시 배포하지 않으며, 이미지 ID만 표시합니다.</span></li>
      <li><span class="ref-k">방법</span><span>${e(a.method)} · 참고 구현 ${link(a.method_url, 'PatchCore (amazon-science/patchcore-inspection)')}. 공식 재현이 아니며 논문 수치와 비교하지 않습니다.</span></li>
    </ul>`;
  }

  function renderLimitations() {
    const ev = obj(state.data.evaluation) || {};
    const items = [...new Set([...arr(state.data.limitations), ...arr(ev.limitations), ...arr(ev.metric_notes)].filter(x => typeof x === 'string'))];
    $('limitationsList').innerHTML = items.length ? items.map(l => `<li>${e(l)}</li>`).join('') : `<li>내보낸 한계 기록이 없습니다 · <code>${DATA_FILE}</code></li>`;
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

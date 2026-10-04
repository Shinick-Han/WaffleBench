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
  const fmt = (v, d = 2) => finite(v) ? v.toLocaleString('en-US', {minimumFractionDigits:d, maximumFractionDigits:d}) : '—';
  const count = v => finite(v) ? Math.round(v).toLocaleString('en-US') : '—';
  const cu = (v, d = 4) => finite(v) ? `${fmt(v, d)} CU` : '—';
  const shortHash = h => typeof h === 'string' && h.length > 16 ? `${h.slice(0, 10)}…${h.slice(-6)}` : (h ?? '—');
  const siteShort = id => typeof id === 'string' ? id.replace(/^lot-[0-9a-f]+:/, '') : '—';
  const near = (a, b) => finite(a) && finite(b) && Math.abs(a - b) < 1e-6;
  const time = iso => { const t = Date.parse(iso); return Number.isFinite(t) ? new Date(t).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC') : '—'; };
  const okMark = ok => ok == null ? '<span>—</span>' : ok ? '<span class="ok">match</span>' : '<span class="bad">mismatch</span>';
  const yesNo = v => v === true ? 'yes' : v === false ? 'no' : '—';

  // ---------- load ----------
  function setLoad(html, retry) {
    const ls = $('loadState'); ls.hidden = false; ls.innerHTML = html;
    if (retry) { const b = document.createElement('button'); b.type = 'button'; b.className = 'control'; b.textContent = 'Retry'; b.addEventListener('click', load); ls.append(b); }
  }
  async function load() {
    $('app').hidden = true; $('download').disabled = true; state.bytes = null;
    setLoad('<div class="skeleton" aria-hidden="true"><span></span><span></span><span></span></div><p>Loading stored run record…</p>');
    let response;
    try { response = await fetch(DATA_URL, {cache:'no-store'}); }
    catch (error) {
      console.error('Inspection live fetch:', error);
      return setLoad(`<h2>Could not load the record</h2><p>A network error prevented reading <code>${e(DATA_PATH)}</code>. Check your connection and try again.</p>`, true);
    }
    if (response.status === 404) {
      return setLoad(`<h2>No verified record yet</h2><p><code>${e(DATA_PATH)}</code> does not exist (HTTP 404). The run record has not been exported or has not been verified yet. This page never fabricates numbers.</p>`, true);
    }
    if (!response.ok) return setLoad(`<h2>Could not load the record</h2><p>The request for <code>${e(DATA_PATH)}</code> failed with HTTP ${e(response.status)}.</p>`, true);
    let data;
    try {
      state.bytes = await response.arrayBuffer();
      data = JSON.parse(new TextDecoder('utf-8', {fatal:true}).decode(state.bytes));
    } catch (error) {
      console.error('Inspection live parse:', error);
      state.bytes = null;
      return setLoad(`<h2>Could not parse the record</h2><p><code>${e(DATA_PATH)}</code> is not valid UTF-8 JSON. The file may be mid-update; try again.</p>`, true);
    }
    const d = obj(data);
    if (!d || d.schema_version !== 1 || d.kind !== KIND || d.scope !== SCOPE) {
      state.bytes = null;
      return setLoad(`<h2>Unsupported record format</h2><p><code>${e(DATA_PATH)}</code> requires <code>schema_version: 1</code> and <code>kind: ${e(KIND)}</code>. Received: schema_version ${e(d?.schema_version ?? 'missing')}, kind ${e(d?.kind ?? 'missing')}.</p>`, true);
    }
    state.data = d;
    try { render(); }
    catch (error) {
      console.error('Inspection live render:', error);
      return setLoad(`<h2>Could not display the record</h2><p><code>${e(DATA_PATH)}</code> does not have the expected structure.</p>`, true);
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
        stages.push({kind:'plan', title: stages.length === 0 ? 'Initial plan' : 'Next plan', update:pendingUpdate, decision:ev, events: pendingUpdate ? [pendingUpdate, ev] : [ev]});
        pendingUpdate = null;
      } else if (ev.type === 'admission') {
        reviewN += 1;
        stages.push({kind:'review', title: reviewN === 1 ? 'First review' : reviewN === 2 ? 'Second review' : `Review ${reviewN}`, admission:ev, observation:null, seq:p.decision_sequence, events:[ev]});
      } else if (ev.type === 'observation') {
        const st = [...stages].reverse().find(s => s.kind === 'review' && s.seq === p.decision_sequence && !s.observation);
        if (st) { st.observation = ev; st.events.push(ev); }
        else stages.push({kind:'review', title:'Review', admission:null, observation:ev, seq:p.decision_sequence, events:[ev]});
      } else if (ev.type === 'analysis_update') {
        pendingUpdate = ev;
      } else if (ev.type === 'closed') {
        stages.push({kind:'final', title:'Final update', update:pendingUpdate, closed:ev, events: pendingUpdate ? [pendingUpdate, ev] : [ev]});
        pendingUpdate = null;
      }
    }
    if (pendingUpdate) stages.push({kind:'final', title:'Analysis update (no close record)', update:pendingUpdate, closed:null, events:[pendingUpdate]});
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
    const items = [['Lot', c.lot_id], ['Sites', count(c.n_sites ?? state.sites.size)], ['Policy', c.policy], ['Variant', c.variant], ['Status', d.status], ['Scope', d.scope]];
    $('studyMeta').innerHTML = items.map(([k, v]) => `<div><dt>${e(k)}</dt><dd>${e(v ?? '—')}</dd></div>`).join('');
    const warn = [];
    if (d.scope !== SCOPE) warn.push(`scope is not ${SCOPE} (received: ${d.scope ?? 'missing'}).`);
    if (d.verification?.primary_benchmark === true) warn.push('verification.primary_benchmark is recorded as true. This page interprets the record only as a separate demonstration.');
    if (d.verification && d.verification.status !== 'passed') warn.push(`The recorded verification status is not passed (${d.verification.status ?? 'missing'}).`);
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
        <div><dt>SDK delegations</dt><dd class="num">${count(delegations)}</dd></div>
        <div><dt>Paid sensor reviews</dt><dd class="num">${count(reviews)}</dd></div>
        <div><dt>Analysis updates</dt><dd class="num">${count(updates)}</dd></div>
        <div><dt>Total charged / budget</dt><dd class="num">${fmt(charged, 4)}<small> / ${fmt(limit, 0)} CU</small></dd></div>
      </dl>
      <p class="check-line">Against the recorded verification summary — delegations ${okMark(chk(delegations, v.delegations))} · reviews ${okMark(chk(reviews, v.paid_reviews))} · updates ${okMark(chk(updates, v.analysis_updates))} · charged ${okMark(chk(charged, v.charged_cu, true))} · budget ${okMark(chk(limit, v.budget_cu, true))}. ${count(c.reviews_executed)} of at most ${count(c.max_reviews)} reviews executed.</p>
      ${closed ? `<p class="check-line">Closing summary<span class="final-tag">Final</span> status <strong>${e(closed.status ?? '—')}</strong> · reviews ${count(closed.reviews_executed)} · updates ${count(closed.updates)} · spent ${cu(closed.spent)} · remaining budget ${cu(c.budget?.remaining)}. These are values after the run ended; the per-stage budget in the replay below is read separately from each stage's own events.</p>` : '<p class="check-line">No core.closed closing summary.</p>'}`;
  }

  function renderRoles(d) {
    const p0 = obj(arr(d.events).find(x => x?.type === 'decision')?.payload) || {};
    const sp = obj(p0.score_parts) || {};
    const sem = arr(d.events).find(x => x?.type === 'observation')?.payload?.selection_reward_semantics;
    $('rolesBody').innerHTML = `
      <dl class="roles">
        <div><dt>LLM agents (coordination)</dt><dd>The Omnigent coordinator delegates work through the SDK to <code>inspection_analyst</code> and <code>inspection_experimenter</code>. The analyst calls the planning tool and the experimenter calls the paid review tool. Neither decides which site to inspect.</dd></div>
        <div><dt>Frozen planner (selection)</dt><dd>Policy <code>${e(d.core?.policy ?? '—')}</code> picks the next site from fixed probabilities (<code>reward_source: ${e(sp.reward_source ?? '—')}</code>) and reserved costs. The probabilities are not learned or updated online during the run.</dd></div>
        <div><dt>Synthetic sensor (response)</dt><dd>Review results (reported defect kind, quality, charged CU) are authored numeric synthetic responses. A reported result is not ground truth about whether a defect exists.</dd></div>
      </dl>
      <div class="rule"><p>The planner reward is the <strong>latent DOI probability</strong>${sem ? ` (<code>selection_reward_semantics: ${e(sem)}</code>)` : ''}. The <code>reported_yield_score</code> shown alongside in the tables is diagnostic only, not the actual reward.</p>
      ${sp.rule ? `<p style="margin-top:6px">Recorded rule: <code>${e(sp.rule)}</code></p>` : ''}</div>`;
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
    $('wafers').innerHTML = wafers.map(w => `<figure class="wafer"><figcaption><strong>Wafer ${e(w)}</strong><span id="wcap${e(w)}">—</span></figcaption><canvas data-wafer="${e(w)}" role="img" aria-label="Wafer ${e(w)} map"></canvas></figure>`).join('') || '<p class="empty"><strong>No site coordinates</strong>The sites array is empty, so no map can be drawn.</p>';
    state.canvases = [...$('wafers').querySelectorAll('canvas')];
    const g = state.geometry;
    $('geometryNote').textContent = g ? `Public geometry only: ⌀${fmt(g.wafer_diameter_mm, 0)} mm · die ${fmt(g.die_width_mm, 0)} × ${fmt(g.die_height_mm, 0)} mm · edge exclusion ${fmt(g.edge_exclusion_mm, 0)} mm. At each stage every site not yet reviewed is drawn as 'unknown', and later observations are never shown early. States are distinguished by fill, outline and dashes as well as color.` : 'No geometry, so no map can be drawn.';
    if (n === 0) {
      $('stageDetail').innerHTML = '<div class="empty"><strong>No events to replay</strong><code>events</code> contains no decision, admission, observation, analysis_update or closed events.</div>';
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
    if (!silent) $('stageAnnounce').textContent = `Stage ${state.stage + 1}: ${s.title}. Spent ${fmt(s.spent, 4)} CU, remaining budget ${fmt(s.remaining, 4)} CU.`;
    renderBudget(s); renderDetail(s); renderStageTables(s); renderSiteTable(s); drawAll();
  }

  function renderBudget(s) {
    const limit = budgetLimit(state.data);
    const pct = finite(s.spent) && finite(limit) && limit > 0 ? Math.min(100, Math.max(0, s.spent / limit * 100)) : 0;
    $('stageBudget').innerHTML = `Per this stage's events: spent <strong class="num">${fmt(s.spent, 4)}</strong> / ${fmt(limit, 0)} CU · remaining <strong class="num">${fmt(s.remaining, 4)}</strong> CU · spent + remaining = budget ${okMark(s.budgetAgrees)}${s.monotone === false ? ' · <span class="bad">Spend decreased from the previous stage</span>' : ''}
      <div class="bar" role="img" aria-label="${fmt(pct, 1)}% of budget spent"><i style="width:${pct}%"></i></div>`;
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
      const cap = $(`wcap${w}`); if (cap) cap.textContent = `reviewed ${count(measured)} · unmeasured ${count(total - measured)}`;
      cv.setAttribute('aria-label', `Wafer ${w} map: of ${total} sites, ${measured} reviewed by this stage; the rest are unknown. The same information is in the table below.`);
    }
  }

  function renderSiteTable(s) {
    const {marks, hl} = stageMarks(s);
    const ids = [...new Set([...hl.keys(), ...marks.keys()])];
    const label = id => {
      const parts = [];
      const m = marks.get(id);
      if (m) parts.push(m.kind === 'positive' ? `Reviewed: sensor reported DOI (${m.obs.reported_kind ?? 'no kind'})` : m.kind === 'negative' ? 'Reviewed: sensor reported not DOI' : `Review failed (${m.obs.status ?? '—'})`);
      const h = hl.get(id);
      if (h === 'selected') parts.push(s.kind === 'review' ? 'Reviewed at this stage' : s.kind === 'final' ? 'Next selection (not recorded · closed)' : 'Selected at this stage');
      if (h === 'alt') parts.push(s.kind === 'final' ? 'Next candidate (not executed)' : 'Alternative at this stage');
      if (!m) parts.push('Still unmeasured');
      return parts.join(' · ');
    };
    $('siteTable').querySelector('tbody').innerHTML = ids.length ? ids.map(id => {
      const site = state.sites.get(id);
      return `<tr class="${hl.get(id) === 'selected' ? 'is-selected' : ''}"><td class="site">${e(siteShort(id))}</td><td>${e(site?.wafer ?? '—')}</td><td class="num">${fmt(site?.x_mm, 2)}</td><td class="num">${fmt(site?.y_mm, 2)}</td><td>${e(label(id))}</td></tr>`;
    }).join('') : '<tr><td colspan="5">No sites to show.</td></tr>';
  }

  function delegationText(s) {
    const dl = s.delegation, tc = s.toolCall;
    if (!dl && !tc) return '<p class="hint">No SDK delegation record is paired with this stage.</p>';
    return `<dl class="detail-list">
      <dt>SDK delegate</dt><dd>${e(dl?.agent ?? '—')}</dd>
      <dt>Tool called</dt><dd>${e(tc?.tool ?? '—')}${tc?.arguments && 'finalize' in tc.arguments ? ` (finalize ${e(tc.arguments.finalize)})` : ''}</dd>
      <dt>Tool output status</dt><dd>${e(tc?.output_status ?? '—')}</dd>
      <dt>Session reuse</dt><dd>${dl?.reuses_child_of_delegation != null ? `Session of delegation ${e(dl.reuses_child_of_delegation)}` : 'New session'}</dd>
    </dl>`;
  }

  function renderDetail(s) {
    const p = s.kind === 'plan' ? obj(s.decision.payload) || {} : null;
    let body = '';
    if (s.kind === 'plan') {
      const sel = arr(p.candidates).find(c => c?.site_id === p.selected_site_id);
      const up = obj(s.update?.payload);
      body = `${up ? `<p class="hint" style="margin:0 0 8px">Preceding analysis update: result <code>${e(up.observed_result_id ?? '—')}</code> (site <code>${e(siteShort(up.observed_site_id))}</code>) was handed over as evidence. Followed the previous plan's next candidate: ${yesNo(up.lookahead_followed)}.</p>` : ''}
        <dl class="detail-list">
          <dt>Decision sequence</dt><dd>${e(p.decision_sequence ?? '—')}</dd>
          <dt>Selected site</dt><dd class="mono-id">${e(siteShort(p.selected_site_id))}</dd>
          <dt>Reported latent DOI probability</dt><dd>${fmt(sel?.selection_reward, 4)}</dd>
          <dt>Reserved cost</dt><dd>${cu(sel?.reserved_cost)}</dd>
          <dt>Two-step route value</dt><dd>${fmt(p.score, 4)}</dd>
          <dt>Affordable candidates</dt><dd>${count(p.affordable_sites)}</dd>
          <dt>Evidence result IDs</dt><dd class="mono-id">${arr(p.evidence_result_ids).map(e).join(', ') || 'none'}</dd>
        </dl>
        <p class="hint">Probabilities are values reported by the frozen planner, not ground truth. Whether the selected site actually has a defect is unknown at this stage.</p>`;
    } else if (s.kind === 'review') {
      const a = obj(s.admission?.payload) || {}, o = obj(s.observation?.payload) || {};
      body = `<dl class="detail-list">
          <dt>Reviewed site</dt><dd class="mono-id">${e(siteShort(a.site_id ?? o.site_id))}</dd>
          <dt>Result ID</dt><dd class="mono-id">${e(o.result_id ?? a.result_id ?? '—')}</dd>
          <dt>Reserved (at admission)</dt><dd>${cu(a.reserved_cost)}</dd>
          <dt>Actually charged</dt><dd>${cu(o.charged)}</dd>
          <dt>Spent before admission</dt><dd>${cu(a.spent_before)}</dd>
          <dt>Cumulative spend</dt><dd>${cu(o.cumulative_spend)}</dd>
          <dt>Sensor report</dt><dd>${o.reported_doi === true ? `DOI · ${e(o.reported_kind ?? '—')}` : o.reported_doi === false ? 'Not DOI' : '—'}</dd>
          <dt>Reported quality</dt><dd>${fmt(o.quality, 3)}</dd>
          <dt>Attempts</dt><dd>${count(arr(o.attempts).length)}</dd>
        </dl>
        <p class="hint">The sensor report is a synthetic response, not ground truth about a real defect. The gap between reserved and charged is mostly unused retry reserve.</p>`;
    } else {
      const up = obj(s.update?.payload) || {}, cl = obj(s.closed?.payload);
      body = `<dl class="detail-list">
          <dt>Last result applied</dt><dd class="mono-id">${e(up.observed_result_id ?? '—')}</dd>
          <dt>Evidence result IDs</dt><dd class="mono-id">${arr(up.evidence_result_ids).map(e).join(', ') || '—'}</dd>
          <dt>Next selection (reference)</dt><dd class="mono-id">${e(siteShort(up.next_selected_site_id))}</dd>
          <dt>Next decision</dt><dd>${up.next_decision === 'not_recorded_finalized' ? 'Not recorded · closed' : e(up.next_decision ?? '—')}</dd>
          <dt>Followed the previous plan's next candidate</dt><dd>${yesNo(up.lookahead_followed)}</dd>
          <dt>Close status</dt><dd>${e(cl?.status ?? '—')}</dd>
        </dl>
        <p class="hint">The next candidates computed at closing were not executed and nothing was charged for them.</p>`;
    }
    $('stageDetail').innerHTML = `<h3 id="stageDetailTitle">${state.stage + 1}. ${e(s.title)}</h3><p class="stage-time">Recorded at ${e(time(s.at))}</p>${body}<h3 style="margin-top:14px">Coordination (Omnigent SDK)</h3>${delegationText(s)}`;
  }

  function renderStageTables(s) {
    const out = [];
    if (s.kind === 'plan') {
      const p = obj(s.decision.payload) || {}, cands = arr(p.candidates).filter(obj);
      const roleName = r => r === 'selected_route_first' ? ['Selected', 'sel'] : r === 'route_lookahead_next' ? ['Route next', 'look'] : r === 'single_step_alternative' ? ['One-step alternative', 'alt1'] : [r ?? '—', 'alt1'];
      out.push(cands.length ? `<div><h3>Selected vs alternatives (decision event payload)</h3><div class="table-scroll" tabindex="0" role="region" aria-label="Candidate comparison table"><table class="data-table cand-table"><caption>Candidates and cost components are read from the event payload, not the core summary. Probabilities are planner-reported values, not ground truth.</caption>
        <thead><tr><th scope="col">Role</th><th scope="col">Site</th><th scope="col" class="num">Reported latent DOI probability (reward)</th><th scope="col" class="num">One-step value</th><th scope="col" class="num">Yield score (diagnostic · not reward)</th><th scope="col" class="num">Stage</th><th scope="col" class="num">Load</th><th scope="col" class="num">Dwell</th><th scope="col" class="num">Retry reserve</th><th scope="col" class="num">Outside rescan</th><th scope="col" class="num">Reserved total</th></tr></thead>
        <tbody>${cands.map(c => { const [rn, rc] = roleName(c.role), cp = obj(c.cost_parts) || {}; return `<tr class="${c.site_id === p.selected_site_id ? 'is-selected' : ''}"><td><span class="pill ${rc}">${e(rn)}</span></td><td class="site">${e(siteShort(c.site_id))}</td><td class="num">${fmt(c.selection_reward, 4)}</td><td class="num">${fmt(c.single_step_value, 4)}</td><td class="num">${fmt(c.reported_yield_score, 4)}</td><td class="num">${fmt(cp.stage, 3)}</td><td class="num">${fmt(cp.load, 0)}</td><td class="num">${fmt(cp.dwell, 0)}</td><td class="num">${fmt(cp.retry_reserve, 0)}</td><td class="num">${fmt(cp.outside_rescan, 0)}</td><td class="num">${fmt(c.reserved_cost, 4)}</td></tr>`; }).join('')}</tbody></table></div>
        <ul class="note-list"><li>The planner picks the first site of the route that maximizes <code>(r_i + γ·r_j) / (c_i + γ·c_j)</code> (γ = ${fmt(p.score_parts?.gamma, 0)}, ${count(p.score_parts?.pairs_evaluated)} pairs evaluated, shortlist of ${count(p.score_parts?.shortlist_size)}). So the site with the highest one-step value or probability may not be selected.</li><li>Reserved total for the route pair ${cu(p.score_parts?.pair_reserved_cost)}, wafer switch now: ${yesNo(p.score_parts?.wafer_switch_now)}.</li></ul></div>`
        : '<div class="empty"><strong>No candidate list</strong><code>payload.candidates</code> of this decision event is empty.</div>');
    } else if (s.kind === 'review') {
      const a = obj(s.admission?.payload) || {}, o = obj(s.observation?.payload) || {}, cp = obj(o.cost) || {};
      const plan = state.stages.slice(0, state.stage).reverse().find(x => x.kind === 'plan');
      const pc = obj(arr(plan?.decision?.payload?.candidates).find(c => c?.site_id === (a.site_id ?? o.site_id))?.cost_parts) || {};
      const row = (name, res, ch) => `<tr><th scope="row">${e(name)}</th><td class="num">${fmt(res, 4)}</td><td class="num">${fmt(ch, 4)}</td></tr>`;
      out.push(`<div><h3>Reserved vs charged</h3><div class="table-scroll" tabindex="0" role="region" aria-label="Reserved vs charged table"><table class="data-table cost-table"><caption>Reserved components are read from the preceding decision event; charged components from the observation event.</caption>
        <thead><tr><th scope="col">Item</th><th scope="col" class="num">Reserved CU</th><th scope="col" class="num">Charged CU</th></tr></thead>
        <tbody>${row('Stage', pc.stage, cp.stage)}${row('Load', pc.load, cp.load)}${row('Dwell', pc.dwell, cp.dwell)}${row('Retry (reserve / used)', pc.retry_reserve, cp.retry)}${row('Outside rescan', pc.outside_rescan, cp.outside_rescan)}${row('Total', a.reserved_cost, o.charged)}</tbody></table></div></div>`);
    } else {
      const up = obj(s.update?.payload) || {};
      out.push(`<div><h3>Next candidates at closing (not executed)</h3><p class="footnote">The analysis_update event holds only candidate IDs; probabilities and cost components were not recorded. The run closed without recording a next decision.</p><ul class="note-list">${arr(up.next_candidate_site_ids).map(id => `<li><code>${e(siteShort(id))}</code>${id === up.next_selected_site_id ? ' — planner\'s next selection' : ''}</li>`).join('') || '<li>No candidate IDs.</li>'}</ul></div>`);
    }
    out.push(renderDelegationTable());
    $('stageTables').className = 'stage-tables';
    $('stageTables').innerHTML = out.join('');
  }

  function renderDelegationTable() {
    const dls = arr(state.data.orchestration?.delegations);
    if (!dls.length) return '<div class="empty"><strong>No delegation records</strong><code>orchestration.delegations</code> is empty.</div>';
    const calls = arr(state.data.orchestration?.specialist_tool_calls);
    return `<div><h3>${count(dls.length)} SDK delegations (current stage highlighted)</h3><div class="table-scroll" tabindex="0" role="region" aria-label="Delegation table"><table class="data-table deleg-table"><caption>Delegations are paired with replay stages in recorded order. Delegations for future stages are listed, but their results appear on the map only at their own stage.</caption>
      <thead><tr><th scope="col">#</th><th scope="col">Agent</th><th scope="col">Tool</th><th scope="col">Output status</th><th scope="col">Accepted</th><th scope="col">SDK call id</th><th scope="col">Session</th></tr></thead>
      <tbody>${dls.map((dl, i) => { const tc = calls.find(c => c?.delegation === i + 1); return `<tr class="${i === state.stage ? 'is-current-del' : i > state.stage ? 'is-future' : ''}"${i === state.stage ? ' aria-current="step"' : ''}><td class="num">${i + 1}</td><td>${e(dl?.agent ?? '—')}</td><td>${e(tc?.tool ?? '—')}</td><td>${i > state.stage ? '—' : e(tc?.output_status ?? '—')}</td><td>${yesNo(dl?.accepted)}</td><td class="site">${e(dl?.sdk_call_id ?? '—')}</td><td class="site">${e(shortHash(dl?.child_session_id))}</td></tr>`; }).join('')}</tbody></table></div></div>`;
  }

  // ---------- chain ----------
  function renderChain(d) {
    const events = arr(d.events).filter(obj);
    let prev = null;
    const rows = events.map(ev => {
      const linked = prev ? ev.prev_sha256 === prev.sha256 : null;
      const gap = prev && finite(prev.index) && finite(ev.index) ? ev.index - prev.index - 1 : null;
      const r = `<tr><td class="num">${e(ev.index ?? '—')}</td><td>${e(ev.type ?? '—')}</td><td>${e(time(ev.at))}</td><td class="site">${e(shortHash(ev.sha256))}</td><td class="site">${e(shortHash(ev.prev_sha256))}</td><td>${prev == null ? '<span class="pill">First (no preceding event)</span>' : linked ? '<span class="pill link">Linked to previous</span>' : `<span class="pill gap">After ${gap != null && gap > 0 ? count(gap) + ' ' : ''}omitted event${gap === 1 ? '' : 's'}</span>`}</td></tr>`;
      prev = ev; return r;
    });
    const gaps = events.filter((ev, i) => i > 0 && ev.prev_sha256 !== events[i - 1].sha256).length;
    $('chainBody').innerHTML = events.length ? `<div class="table-scroll" tabindex="0" role="region" aria-label="Event table"><table class="data-table chain-table"><caption>${count(events.length)} events in this file. The ${count(gaps)} chain break(s) mark where events were filtered out of the original.</caption>
      <thead><tr><th scope="col" class="num">index</th><th scope="col">Type</th><th scope="col">Time</th><th scope="col">sha256</th><th scope="col">prev_sha256</th><th scope="col">Link within file</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>
      <ul class="note-list"><li>Each event's hash and previous hash are copied verbatim from the original record. This page does not re-hash event contents to verify them.</li><li>Because this is a filtered subset, these hashes alone, without the intermediate events, cannot prove that the full original chain is intact. Full proof requires the original session proof (<code>${e(shortHash(d.verification?.session_proof_sha256))}</code>) together with the original record.</li><li>Recorded <code>events_sha256</code>: <code>${e(d.events_sha256 ?? '—')}</code></li></ul>`
      : '<div class="empty"><strong>No events</strong>The <code>events</code> array is empty.</div>';
  }

  // ---------- receipt ----------
  function renderReceipt(d) {
    const v = obj(d.verification) || {};
    const size = state.bytes ? state.bytes.byteLength : null;
    const items = [['File', DATA_PATH], ['Size', size != null ? `${count(size)} bytes` : '—'], ['SHA-256 of downloaded bytes (computed in browser)', '<span id="byteHash">Computing…</span>'], ['Verification status', e(v.status ?? '—')], ['Verification scope', e(v.scope ?? '—')], ['Primary benchmark', v.primary_benchmark === false ? 'No' : e(v.primary_benchmark ?? '—')], ['Session proof SHA-256', `<code>${e(v.session_proof_sha256 ?? '—')}</code>`], ['Runner source SHA-256', `<code>${e(v.runner_source_sha256 ?? '—')}</code>`], ['Planner model hash', `<code>${e(d.core?.model_hash ?? '—')}</code>`], ['SDK normalization', e(v.sdk_normalization ?? '—')], ['Creates or sends during recovery', v.recovery_creates_or_sends === false ? 'None' : e(v.recovery_creates_or_sends ?? '—')]];
    $('receiptBody').innerHTML = `<dl class="receipt">${items.map(([k, val]) => `<div><dt>${e(k)}</dt><dd>${k === 'File' ? `<code>${e(val)}</code>` : val}</dd></div>`).join('')}</dl>
      <div class="receipt-actions"><button type="button" class="control primary-action" id="download2">Download raw JSON (${size != null ? count(size) + ' bytes' : '—'})</button></div>`;
    $('download2').addEventListener('click', download);
    if (state.bytes && window.crypto?.subtle) crypto.subtle.digest('SHA-256', state.bytes).then(h => { const el = $('byteHash'); if (el) el.innerHTML = `<code>${[...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('')}</code>`; }).catch(() => { const el = $('byteHash'); if (el) el.textContent = 'Could not compute'; });
    else { const el = $('byteHash'); if (el) el.textContent = 'Cannot be computed in this environment (requires a secure context)'; }
  }

  function renderLimits(d) {
    const own = ['This page only re-reads a recorded run; it does not run experiments in the browser.', 'The two paid reviews only record that coordination actually happened; they do not demonstrate selection accuracy or fab throughput.', 'Defect kinds and probabilities reported by the sensor are not ground truth. Unmeasured sites remain unknown to the end.'];
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

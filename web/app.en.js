'use strict';
/* WaffleBench app. Every scientific value shown here comes from the loaded
   snapshot (DATA_CONTRACT.md, Snapshot JSON v1). Missing values render as
   explicit nulls; nothing is estimated or filled in by the browser. */
const $=id=>document.getElementById(id);
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=(name,cls='')=>`<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const CORNERS=['SS','TT','FF','FS','SF'],VDDS=[1.2,1.5,1.8,2.2,2.5,2.9,3.3],TEMPS=[125,85,27,0,-40];
const POLICY_COLORS=['#276449','#a18b5f','#6a8dba','#8a77a5','#9a5a4a','#4f7f86'];
const POLICY_PURPOSE={adaptive_idw_plus_distance:'Exploit error + probe unexplored regions',random:'Primary comparison control',space_filling:'Outcome-independent fixed plan',idw_without_distance:'Added value of the exploration term'};
const KIND_INFO={'demo-prepare':['Demo preparation','Runs the numerical preflight, 9 calibration observations and the 3 shared initial observations for seed 1001, then saves the prepared run and snapshot. No adaptive updates are made yet.'],reproduce:['Short reproduction check','An explicitly labeled short reproduction check, isolated from the primary research campaign. Usage is recorded.']};
const JOB_STATE={queued:['Queued','gray'],running:['Running','green'],completed:['Completed','green'],incomplete:['Incomplete','amber'],failed:['Failed','red'],cancelled:['Cancelled','gray']};

const num=v=>typeof v==='number'&&Number.isFinite(v)?v:null;
const NULL='<span class="null">null</span>';
const fmtPs=v=>num(v)==null?'—':(v*1e12).toFixed(1);
const fmtPct=v=>num(v)==null?'—':(v*100).toFixed(1)+'%';
const fmtN=(v,d=0)=>num(v)==null?'—':v.toFixed(d);
const show=v=>v==null?NULL:typeof v==='object'?`<span class="mono">${esc(JSON.stringify(v))}</span>`:esc(v);
const pointId=(c,v,t)=>`${c}|${v.toFixed(3)}|${t.toFixed(1)}`;
function keyOf(r){if(!r||typeof r!=='object')return null;const p=r.pvt;if(p&&typeof p.corner==='string'&&num(p.vdd)!=null&&num(p.temp_c)!=null)return pointId(p.corner,p.vdd,p.temp_c);if(typeof r.point_id==='string'){const [c,v,t]=r.point_id.split('|');if(c&&Number.isFinite(+v)&&Number.isFinite(+t))return pointId(c,+v,+t);}return null;}
function fmtKey(k){if(!k)return 'No condition recorded';const [c,v,t]=k.split('|');return `${c} · ${(+v).toFixed(1)} V · ${+t} °C`;}
const fmtTime=ts=>{const d=new Date(ts);return ts&&!isNaN(d)?d.toLocaleString('en-US',{hour12:false}):'—';};
const isHeld=r=>r&&(r.phase==='held_out'||r.phase==='heldout');
const PHASE_LABEL={calibration:'Calibration',initial:'Shared initial',search:'Search',held_out:'Held-out · post hoc',heldout:'Held-out · post hoc',evaluation:'Evaluation only'};
const phaseLabel=p=>p==null?'phase null':PHASE_LABEL[p]||String(p);

const state={view:'lab',mode:null,controller:null,snap:null,raw:null,sha:null,loadError:null,m:null,stage:0,corner:'SS',point:null,playing:false,visiblePolicies:null,job:null,pollError:null,serverSha:null};

/* ---------- snapshot adapter ---------- */
function normalize(s){
  const warn=[];
  const arr=k=>{const v=s[k];if(v==null){warn.push(`Field ${k} is missing`);return [];}if(!Array.isArray(v)){warn.push(`Field ${k} is not an array`);return [];}return v.filter(x=>x&&typeof x==='object');};
  const obj=k=>{const v=s[k];if(v==null)return null;if(typeof v!=='object'||Array.isArray(v)){warn.push(`Field ${k} is not an object`);return null;}return v;};
  const bySeq=(a,b)=>(num(a.sequence)??Infinity)-(num(b.sequence)??Infinity);
  const m={run:obj('run'),model:obj('model'),benchmark:obj('benchmark'),cost:obj('cost'),observations:arr('observations'),evaluations:arr('evaluations'),decisions:arr('decisions').slice().sort(bySeq),trace:arr('trace').slice().sort(bySeq),coverage:arr('coverage'),limitations:Array.isArray(s.limitations)?s.limitations.map(String):[],warn};
  m.obsById=new Map();m.obsByKey=new Map();m.evalById=new Map();m.covByKey=new Map();m.revealIdx=new Map();
  m.observations.forEach(o=>{if(o.result_id!=null)m.obsById.set(String(o.result_id),o);const k=keyOf(o);if(k){if(!m.obsByKey.has(k))m.obsByKey.set(k,[]);m.obsByKey.get(k).push(o);}});
  m.evaluations.forEach(e=>{const id=String(e.result_id);if(m.evalById.has(id))warn.push(`Result ${id} has multiple evaluations; showing the first record`);else m.evalById.set(id,e);});
  m.coverage.forEach(c=>{const k=keyOf(c);if(k)m.covByKey.set(k,c);});
  m.decisions.forEach((d,i)=>{if(d.observed_result_id!=null&&!m.revealIdx.has(String(d.observed_result_id)))m.revealIdx.set(String(d.observed_result_id),i);});
  m.traceStage=traceStages(m);
  m.empty=!m.run&&!m.observations.length&&!m.evaluations.length&&!m.decisions.length&&!m.trace.length&&!m.benchmark;
  return m;
}
const isOmnigent=a=>/^omnigent\b/i.test(String(a||''));
/* Replay stage at which each trace row may be shown. Trace sequences are global
   ledger counters and decision sequences are run-local, so the two are never
   compared. A row is linked to the replay by an explicit detail.decision_sequence /
   detail.query_index, by the ledger order of the runner's own decision and search
   query_result rows (each decision is executed exactly once, in order), or by the
   result ids it carries. Stage i shows decision i's choice; its result appears at
   stage i+1. A link that cannot be resolved waits for the final stage. Unlinked rows
   (preparation, run open/close) follow the latest linked row before them. */
function traceStages(m){
  const n=m.decisions.length,bySeq=new Map(),byQi=new Map();
  m.decisions.forEach((d,i)=>{if(num(d.sequence)!=null&&!bySeq.has(d.sequence))bySeq.set(d.sequence,i);if(num(d.query_index)!=null&&!byQi.has(d.query_index))byQi.set(d.query_index,i);});
  const idStage=id=>{const k=String(id),o=m.obsById.get(k);if(o&&isHeld(o))return n;const i=m.revealIdx.get(k);if(i!==undefined)return i+1;return o&&o.phase==='search'?n:0;};
  let decK=0,resK=0,carry=0;
  return m.trace.map(t=>{
    const det=t.detail&&typeof t.detail==='object'&&!Array.isArray(t.detail)?t.detail:{};
    const ids=(Array.isArray(t.result_ids)?t.result_ids:[]).concat(det.observed_result_id!=null?[det.observed_result_id]:[]);
    const isResult=t.action==='query_result'||ids.length>0;
    let st=null;
    const explicit=num(det.decision_sequence)!=null||num(det.query_index)!=null;
    if(explicit){const i=num(det.decision_sequence)!=null?bySeq.get(det.decision_sequence):byQi.get(det.query_index);st=i===undefined?n:isResult?i+1:i;}
    else if(!isOmnigent(t.actor)&&t.action==='decision'){const d=m.decisions[decK];st=d&&(det.selected_point_id==null||det.selected_point_id===d.selected_point_id)?decK:n;decK++;}
    else if(!isOmnigent(t.actor)&&t.action==='query_result'&&det.phase==='search'){const d=m.decisions[resK];st=d&&(det.point_id==null||det.point_id===d.selected_point_id)?resK+1:n;resK++;}
    if(ids.length){const s=Math.max(...ids.map(idStage));st=st==null?s:Math.max(st,s);}
    if(st==null)st=carry;else carry=Math.max(carry,st);
    return Math.min(st,n);
  });
}
const D=()=>state.m?state.m.decisions.length:0;
const finalStage=()=>state.stage>=D();
/* A result is visible in replay once the decision that observed it has passed.
   Results not tied to a decision (calibration, shared initial) are visible from
   the start; held-out (post-hoc) results only at the end of the replay. */
function revealed(id,phase){if(isHeld({phase}))return finalStage();const i=state.m.revealIdx.get(String(id));return i===undefined||i<state.stage;}
function cellAt(k){
  const m=state.m;const out={key:k,kind:'unobserved',err:null,obs:null,ev:null,cov:m?m.covByKey.get(k)||null:null,candidate:null};
  if(!m)return out;
  const vis=(m.obsByKey.get(k)||[]).filter(o=>revealed(o.result_id,o.phase));
  const o=vis[vis.length-1];
  if(o){out.obs=o;out.ev=m.evalById.get(String(o.result_id))||null;out.err=out.ev?num(out.ev.abs_relative_error):null;out.kind=isHeld(o)?'held_out':o.phase==='calibration'?'calibration':'observed';}
  else if(out.cov){const c=out.cov;const ok=c.state==='held_out'?finalStage():c.result_id!=null?revealed(c.result_id,null):finalStage();
    if(ok&&c.state&&c.state!=='unobserved'){out.kind=c.state;out.err=num(c.abs_relative_error);if(c.result_id!=null)out.ev=m.evalById.get(String(c.result_id))||null;}}
  const d=m.decisions[state.stage];
  if(d&&Array.isArray(d.candidates))out.candidate=d.candidates.find(c=>keyOf(c)===k)||null;
  return out;
}

/* ---------- loading ---------- */
async function sha256(text){try{if(!crypto.subtle)return null;const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text));return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');}catch{return null;}}
async function detectMode(){if(document.querySelector('meta[name="falsify-mode"]')?.content==='static')return null;try{const r=await fetch('./api/job',{cache:'no-store'});if(r.ok&&(r.headers.get('content-type')||'').includes('application/json')){const j=await r.json();if(j&&j.controller==='falsify-lab-local')return j;}}catch{}return null;}
/* Manual retry after a failed load. One request at a time; the alert notice is
   re-rendered with the outcome and focus returns to its retry button if it remains. */
async function retryLoad(){if(state.loading)return;state.loading=true;renderChrome();try{await loadSnapshot();}finally{state.loading=false;}renderChrome();const b=document.querySelector('[data-retry-load]');if(b)b.focus();else{$('mainContent').focus({preventScroll:true});showToast('Snapshot reloaded.');}}
async function loadSnapshot(){
  const url=state.mode==='local'?'./api/snapshot':'./data/snapshot.json';
  let raw=null,err=null;
  try{const r=await fetch(url,{cache:'no-store'});raw=await r.text();
    if(!r.ok){let code='';try{code=JSON.parse(raw).error;}catch{}err=code==='snapshot_unavailable'?{kind:'missing',message:'The configured snapshot file does not exist yet.'}:{kind:'http',message:`Snapshot request failed (HTTP ${r.status})`};raw=null;}
  }catch(e){err={kind:'network',message:'Could not load the snapshot: '+e.message};}
  let snap=null;
  if(raw!=null){try{snap=JSON.parse(raw);}catch{err={kind:'parse',message:'Could not parse the snapshot JSON.'};}
    if(snap&&(typeof snap!=='object'||Array.isArray(snap))){snap=null;err={kind:'parse',message:'The snapshot is not a JSON object.'};}
    else if(snap&&snap.schema_version!==1){err={kind:'schema',message:`Unsupported schema_version: ${String(snap.schema_version)}`};snap=null;}}
  const prevStage=state.stage;stopPlaying();
  state.raw=snap?raw:null;state.snap=snap;state.loadError=err;state.m=snap?normalize(snap):null;state.sha=snap?await sha256(raw):null;
  state.stage=Math.min(prevStage,D());if(!state.point)initPoint();
  const pol=snap&&state.m.benchmark&&Array.isArray(state.m.benchmark.policies)?state.m.benchmark.policies:[];
  if(!state.visiblePolicies)state.visiblePolicies=new Set(pol.map(p=>String(p.id)));
  renderAll();
}
function initPoint(){const m=state.m;if(!m)return;const d=m.decisions[0];const c=d&&Array.isArray(d.candidates)&&(d.candidates.find(x=>x.point_id===d.selected_point_id)||d.candidates[0]);const k=keyOf(c)||keyOf(m.observations[m.observations.length-1]);if(k){state.point=k;state.corner=k.split('|')[0];}}

/* ---------- shared chrome ---------- */
function showToast(s){$('toast').textContent=s;$('toast').hidden=false;clearTimeout(showToast.timer);showToast.timer=setTimeout(()=>$('toast').hidden=true,4000);}
function renderChrome(){
  const s=state.snap,m=state.m,local=state.mode==='local';
  const notes=[];
  notes.push(local?`<div class="preview-notice"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>Local mode</strong> · Showing experiment records stored on this computer. Only the two allowed jobs (demo-prepare, reproduce) can be started.</span></div>`:`<div class="preview-notice"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>Recorded-run replay</strong> · This public view explores and replays stored real records. It performs no remote computation and runs no new experiments.</span></div>`);
  if(s&&s.release)notes.push(`<div class="preview-notice"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>Records from two real runs</strong> · The workbench shows the seed 1001 agent demo; Policy comparison shows the separate 40-run primary study. Demo and primary-study costs are shown separately.</span></div>`);
  else if(s&&s.campaign&&['development','reproduce'].includes(s.campaign.kind))notes.push(`<div class="preview-notice"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>${s.campaign.kind==='development'?'Development validation':'Short reproduction check'}</strong> · Real computation records, kept separate from primary-study results.</span></div>`);
  if(state.loadError)notes.push(`<div class="preview-notice warn" role="alert"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>No snapshot to display</strong> · ${esc(state.loadError.message)} All scientific values on screen are empty.</span><button class="btn compact notice-action" data-retry-load ${state.loading?'disabled':''}>${state.loading?'Reloading…':'Retry'}</button></div>`);
  if(s&&s.data_mode!=='real')notes.push(`<div class="preview-notice fixture" role="alert"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>Not real data · data_mode=${esc(s.data_mode)}</strong> · This is a test fixture. Do not interpret or cite it as research results.</span></div>`);
  else if(m&&m.empty)notes.push(`<div class="preview-notice warn"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>No results yet</strong> · The snapshot has no observation, decision or comparison records. Empty views are never filled with example numbers.</span></div>`);
  if(m&&m.warn.length)notes.push(`<div class="preview-notice warn"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>Snapshot format warnings</strong><ul>${m.warn.slice(0,6).map(w=>`<li>${esc(w)}</li>`).join('')}</ul></span></div>`);
  if(local&&state.pollError)notes.push(`<div class="preview-notice warn" role="alert"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>Controller connection lost</strong> · ${esc(state.pollError)} Keeping the last received records and retrying.</span></div>`);
  const j=state.job;
  if(local&&j){const [lab,col]=JOB_STATE[j.state]||[j.state,'gray'];notes.push(`<div class="job-strip"><span class="tag ${col}"><span class="dot"></span>${esc(lab)}</span><span>Job <strong>${esc(j.kind)}</strong> · ${esc(j.job_id)}</span>${j.error?`<span class="muted">${esc(j.error)}</span>`:''}<button class="btn compact" data-open-jobs>Details</button></div>`);}
  $('noticeStack').innerHTML=notes.join('');
  const run=m&&m.run;
  $('snapshotMeta').innerHTML=s?`<span>Run <strong>${run?esc(run.run_id??'run_id null'):'No run record'}</strong></span><span>${run?`${esc(run.policy??'policy null')} · seed ${esc(run.seed??'null')} · ${esc(run.status??'status null')}`:'run: null'}</span><span>Generated ${s.generated_at?esc(fmtTime(s.generated_at)):NULL}</span>`:`<span>Snapshot <strong>none</strong></span>`;
  const jobActive=j&&(j.state==='queued'||j.state==='running');
  $('modeTag').className='tag '+(local?(jobActive?'amber':'green'):'gray');
  $('modeTag').innerHTML=`<span class="dot"></span>${local?(jobActive?'Local · job running':'Local · real records'):'Replay · public'}`;
  $('sideStatus').textContent=local?(jobActive?`Job running · ${j.kind}`:'Local experiment records'):'Recorded-run replay';
  $('jobButton').hidden=!local;
  $('exportButton').disabled=!state.raw;
  $('footerMeta').textContent=`WaffleBench · Research design v1.0${s&&s.protocol_hash?' · protocol '+String(s.protocol_hash).slice(0,12):''}${state.sha?' · snapshot sha256 '+state.sha.slice(0,12):''}`;
}
function setView(v){stopPlaying();state.view=v;document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===v);if(b.dataset.view===v)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});['lab','benchmark','records'].forEach(x=>$('view-'+x).hidden=x!==v);const titles={lab:['Research workbench','Experiment workbench','Observations and search history across PVT conditions for a circuit delay model'],benchmark:['Policy comparison','Policy comparison','Results that favor the hypothesis and null results are read by the same criteria.'],records:['Experiment records','Experiment records','Follow conditions and result IDs from prediction to observation to the next decision.']};$('crumbTitle').textContent=titles[v][0];$('pageTitle').textContent=titles[v][1];$('pageSubtitle').textContent=titles[v][2];if(dirty[v])renderView(v);}
/* Lazy view rendering. Only the shown view is rebuilt. A new snapshot or replay
   step marks hidden views dirty, and a dirty view is rebuilt from the current
   state before it is shown, so no view ever displays an older snapshot. */
const dirty={lab:true,benchmark:true,records:true};
function renderView(v){dirty[v]=false;if(v==='lab')renderLab();else if(v==='benchmark')renderBenchmark();else renderRecords();}

/* ---------- workbench ---------- */
function mapStatus(c){
  if(c.kind==='failed')return ['failed','Failure record'];
  if(c.kind==='unobserved')return ['unknown','Not evaluated'];
  const e=c.ev;
  if(e&&e.clear_counterexample===true)return ['bad','Bad · clear counterexample'];
  if(e&&e.clear_counterexample===false&&e.secondary_counterexample===true)return ['boundary','Boundary · secondary counterexample'];
  if(e&&e.clear_counterexample===false&&e.secondary_counterexample===false)return ['good','Good · within model tolerance'];
  return ['unknown','Not evaluated'];
}
/* Illustrative physical dimensions only; no scientific value is inferred from placement. */
const WAFER_GEOMETRY=Object.freeze({diameter:300,dieWidth:8,dieHeight:6,scribe:0.08,edgeExclusion:3});
function waferDisplayGeometry(){
  const g=WAFER_GEOMETRY,pitchX=g.dieWidth+g.scribe,pitchY=g.dieHeight+g.scribe;
  const radius=g.diameter/2-g.edgeExclusion,rects=[];
  const siteFits=(x,y)=>Math.hypot(Math.abs(x)+g.dieWidth/2,Math.abs(y)+g.dieHeight/2)<=radius;
  for(let row=-24;row<=24;row++)for(let col=-18;col<=18;col++){
    const x=col*pitchX,y=row*pitchY;
    if(siteFits(x,y))rects.push(`M${(150+x-g.dieWidth/2).toFixed(2)} ${(150+y-g.dieHeight/2).toFixed(2)}h${g.dieWidth}v${g.dieHeight}h-${g.dieWidth}Z`);
  }
  const sites=TEMPS.map((_,r)=>VDDS.map((_,c)=>({x:150+(c-3)*4*pitchX,y:150+(r-2)*6*pitchY})));
  return {path:rects.join(''),sites};
}
const WAFER_DISPLAY=waferDisplayGeometry();
function renderMap(){
  let html=`<svg class="wafer-geometry" viewBox="0 0 300 300" aria-hidden="true" focusable="false"><circle class="wafer-edge-ring" cx="150" cy="150" r="147"/><path class="wafer-dies" d="${WAFER_DISPLAY.path}"/></svg>`;
  TEMPS.forEach((t,row)=>{VDDS.forEach((v,col)=>{
    const k=pointId(state.corner,v,t),c=cellAt(k);const selected=state.point===k;
    const site=WAFER_DISPLAY.sites[row][col];
    const [status,statusLabel]=mapStatus(c);
    const estimate=c.kind==='unobserved'&&!!c.candidate;
    const held=c.kind==='held_out';
    const cls=['heat-cell','die-'+status];
    if(held)cls.push('die-heldout');
    if(estimate)cls.push('die-estimate');
    if(selected)cls.push('selected');
    const marks={good:'<path d="m3 8 3 3 6-7"/>',bad:'<path d="M4 4 12 12M12 4 4 12"/>',boundary:'<path d="m8 2 6 6-6 6-6-6Z"/>',unknown:'<circle cx="8" cy="8" r="1.6"/>',failed:'<path d="M8 2v8m0 3v1"/>'};
    const mark=estimate?'<span class="die-mark" aria-hidden="true">~</span>':`<svg class="die-mark" viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${marks[status]}</svg>`;
    const value=estimate?num(c.candidate.idw_predicted_abs_error):c.err;
    const provenance=held?'Held-out · post hoc':c.kind==='calibration'?'Calibration observation':c.kind==='observed'?'Observed':c.kind==='failed'?'Failure record':estimate?'Candidate · IDW estimate':'Unobserved';
    html+=`<button type="button" class="${cls.join(' ')}" style="--site-x:${(site.x/3).toFixed(4)}%;--site-y:${(site.y/3).toFixed(4)}%" data-key="${esc(k)}" aria-pressed="${selected}" aria-label="${esc(fmtKey(k))}, ${esc(provenance)}, ${esc(statusLabel)}${c.err!=null?', error '+fmtPct(c.err):estimate&&value!=null?', IDW estimated error '+fmtPct(value):''}"><span class="site-die">${mark}</span></button>`;});});
  const fk=$('heatmap').contains(document.activeElement)?document.activeElement.dataset.key:null;
  $('heatmap').innerHTML=html;
  if(fk){const b=$('heatmap').querySelector(`[data-key="${CSS.escape(fk)}"]`);if(b)b.focus();}
  document.querySelectorAll('[data-corner]').forEach(b=>{b.classList.toggle('active',b.dataset.corner===state.corner);b.setAttribute('aria-pressed',b.dataset.corner===state.corner);});
  const k=state.point&&state.point.startsWith(state.corner+'|')?state.point:null;
  if(!k){$('pointDetail').innerHTML=`<div><strong>${esc(state.corner)} corner</strong><small>Select a condition to show stored observations, predictions and held-out evaluations.</small></div>`;return;}
  const c=cellAt(k),ev=c.ev,o=c.obs,cov=c.cov;
  const kindText={observed:'Observed',calibration:'Calibration observation',held_out:'Held-out · post hoc',failed:'Failure record',unobserved:'Unobserved'}[c.kind]||c.kind;
  const rid=o?o.result_id:cov&&c.kind!=='unobserved'?cov.result_id:null;
  const pred=ev?ev.predicted_tpd_s:cov&&c.kind!=='unobserved'?cov.predicted_tpd_s:null;
  const obsv=o?o.tpd_s:ev?ev.simulated_tpd_s:cov&&c.kind!=='unobserved'?cov.observed_tpd_s:null;
  const sub=c.kind==='unobserved'?(c.candidate?`Candidate in the current decision · IDW estimated error ${fmtPct(c.candidate.idw_predicted_abs_error)} (not observed)`:'No observation recorded up to this replay step'):`${kindText}${rid!=null?' · ':''}${rid!=null?`<button class="text-action" data-evidence="${esc(rid)}">${esc(rid)}</button>`:''} · predicted ${fmtPs(pred)} ps · observed ${fmtPs(obsv)} ps`;
  $('pointDetail').innerHTML=`<div><strong>${esc(fmtKey(k))} · ${esc(mapStatus(c)[1])}</strong><small>${sub}</small></div><div class="value data">${c.kind==='unobserved'?'—':fmtPct(c.err)}<small>${c.kind==='unobserved'?'No observed error':c.err==null?'No evaluation record':'|predicted − observed| / observed'}</small></div>`;
}
function renderSteps(){
  const m=state.m,n=D();
  if(!m||(!n&&!m.observations.length)){$('steps').innerHTML='<span class="steps-empty">No recorded loop steps.</span>';$('replayProgress').textContent='';return;}
  const labels=stageLabels();
  $('replayProgress').textContent=`Step ${state.stage+1} / ${n+1} · ${labels[state.stage]} · ${state.stage>=n?'All recorded results revealed':'Observations from later decisions still hidden'}`;
  const fs=$('steps').contains(document.activeElement)?document.activeElement.dataset.step:null;
  $('steps').innerHTML=labels.map((label,i)=>`${i?'<span class="step-connector" aria-hidden="true"></span>':''}<button class="step ${state.stage===i?'active':state.stage>i?'done':''}" data-step="${i}" ${state.stage===i?'aria-current="step"':''} aria-label="View step ${i+1}: ${esc(label)}"><i>${i+1}</i>${esc(label)}</button>`).join('');
  if(fs!=null){const b=$('steps').querySelector(`[data-step="${fs}"]`);if(b)b.focus();}
}
const stageLabels=()=>['Initial evidence',...state.m.decisions.map((d,i)=>`Decision ${d.sequence??i+1} result`)];
function renderBudget(){
  const m=state.m,run=m&&m.run,b=run&&run.budget&&typeof run.budget==='object'?run.budget:null;
  const limit=b?num(b.limit):null;let used=b?num(b.used):null,remaining=b?num(b.remaining):null;
  const d=m&&m.decisions[state.stage];
  if(d&&limit!=null&&num(d.remaining_budget)!=null){remaining=d.remaining_budget;used=limit-remaining;}
  if(!run||limit==null){$('budgetHead').innerHTML=`<small>${run?'No budget record (null)':'No run record'}</small>`;$('budgetBar').innerHTML='';$('budgetLegend').innerHTML=`<span>${run?'budget.limit = null':'run = null'}</span>`;$('budgetBar').setAttribute('aria-label','No budget record');return;}
  const cal=m.observations.filter(o=>o.phase==='calibration'&&revealed(o.result_id,o.phase)).length;
  $('budgetHead').innerHTML=`${used==null?'—':used} <small>/ ${limit} runs</small>`;
  $('budgetBar').innerHTML=limit<=200?Array.from({length:limit},(_,i)=>`<span class="${i<Math.min(cal,used??0)?'cal':i<(used??0)?'searched':''}"></span>`).join(''):'';
  $('budgetBar').setAttribute('aria-label',`${used==null?'Unknown number':used} of ${limit} runs used`);
  $('budgetLegend').innerHTML=`<span>Calibration ${cal}</span><span>Search/other ${used==null?'—':Math.max(used-cal,0)}</span><span>Remaining ${remaining==null?'—':remaining}</span>`;
}
function candidateRole(r){return r==='exploitation'?'Exploit error':r==='exploration'?'Explore unvisited':r==null?'role null':String(r);}
function renderCandidates(){
  const m=state.m,d=m&&m.decisions[state.stage];
  if(!d){$('candidateCost').textContent='';$('candidates').innerHTML=`<div class="panel-empty" style="grid-column:1/-1"><strong>${D()?'All recorded decisions have been replayed.':'No recorded decisions.'}</strong>${D()?' Showing results through the last decision’s observation.':' Candidate comparisons and selections appear here once a run is recorded.'}</div>`;return;}
  const cs=Array.isArray(d.candidates)?d.candidates:[];
  const costs=[...new Set(cs.map(c=>num(c.cost_queries)).filter(x=>x!=null))];
  $('candidateCost').textContent=costs.length===1?`Cost: ${costs[0]} simulation(s) each`:costs.length?'Cost: varies by candidate':'No cost record';
  const note=cs.length<2?`<div class="panel-empty" style="grid-column:1/-1">Only ${cs.length} candidate(s) recorded for this decision.</div>`:'';
  $('candidates').innerHTML=cs.map((c,i)=>{const sel=c.point_id!=null&&c.point_id===d.selected_point_id;return `<button class="candidate ${sel?'picked':''}" data-candidate="${i}" aria-label="View evidence for ${sel?'selected':'comparison'} candidate ${esc(fmtKey(keyOf(c)))}"><div class="candidate-top"><span class="tag ${sel?'green':'gray'}">${sel?'Selected':'Comparison candidate'}</span><small>${esc(candidateRole(c.role))}</small></div><h3>${esc(fmtKey(keyOf(c)))}</h3><p>The actual outcome is revealed only after selection and observation. The values below are the score components at decision time.</p><div class="candidate-meta"><span>IDW est. error <strong>${fmtPct(c.idw_predicted_abs_error)}</strong></span><span>Score <strong class="data">${fmtN(c.score,3)}</strong></span><span>Distance <strong class="data">${fmtN(c.min_normalized_distance,3)}</strong></span></div><div class="candidate-evidence"><span>View evidence and score breakdown</span>${icon('arrow')}</div></button>`;}).join('')+note;
}
function actorStyle(a){const s=String(a||'').toLowerCase();if(s.includes('experiment'))return ['blue','flask'];if(s.includes('analyst'))return ['','chart'];if(s.includes('supervisor'))return ['gold','network'];return ['','file'];}
function visibleTrace(){const m=state.m;if(!m)return [];if(finalStage())return m.trace;return m.trace.filter((t,i)=>m.traceStage[i]<=state.stage);}
const traceDetail=d=>d==null?'':typeof d==='string'?esc(d):`<span class="mono">${esc(JSON.stringify(d))}</span>`;
const actorKind=a=>isOmnigent(a)?'Omnigent':String(a||'')==='deterministic runner'?'Deterministic runner · not Omnigent':'';
function traceItem(t){const [col,sym]=actorStyle(t.actor);const ids=Array.isArray(t.result_ids)?t.result_ids:[];const kind=actorKind(t.actor);return `<div class="trace-item"><span class="role-icon ${col}">${icon(sym)}</span><div class="trace-head"><strong>${esc(t.actor??'actor null')}</strong><small title="${esc(t.timestamp??'')}">${kind?esc(kind)+' · ':''}Ledger #${esc(t.sequence??'?')} · ${esc(fmtTime(t.timestamp))}</small></div><p><strong>${esc(t.action??'')}</strong> ${traceDetail(t.detail)}</p>${ids.map(id=>`<button class="result-link" data-evidence="${esc(id)}">${icon('file')} ${esc(id)}</button>`).join(' ')}</div>`;}
function renderTrace(){
  const m=state.m,all=m?m.trace:[],vis=visibleTrace();
  const omni=all.filter(t=>isOmnigent(t.actor)).length;
  $('traceSubtitle').textContent=!all.length?'No agent or runner records':omni?`Omnigent ${omni} · deterministic runner and others ${all.length-omni} · showing ${vis.length}/${all.length}`:`Deterministic runner records · no Omnigent records · ${vis.length}/${all.length}`;
  $('traceBody').innerHTML=!vis.length?`<div class="panel-empty" style="padding:8px 0">${all.length?'No records revealed up to this replay step.':'The snapshot has no trace records. Agent collaboration is not assumed or shown.'}</div>`:vis.slice(-5).map(traceItem).join('')+(vis.length>5?`<button class="btn compact trace-more" id="traceAll">View all records (${vis.length})</button>`:'');
  $('decisionSummary').innerHTML=decisionSummary();
}
const changedText=(v,yes,no)=>v===true?yes:v===false?no:'Selection change: null';
const candList=cs=>Array.isArray(cs)&&cs.length?cs.map(c=>`${esc(fmtKey(keyOf(c)))}${c&&c.role!=null?' ('+esc(candidateRole(c.role))+')':''}`).join(', '):NULL;
/* The current stage shows decision i at its choice time (its rank fields compare the
   ranking without/with the outcome preceding that choice). The previous decision's
   observed result, and its optional post_result_update, only appear once revealed. */
function decisionSummary(){
  const m=state.m;if(!m||!D())return '<strong>No decision records yet.</strong><p>Candidate ranks and selection changes appear after real decisions are recorded.</p>';
  const cur=m.decisions[state.stage],prev=state.stage>0?m.decisions[state.stage-1]:null,out=[];
  if(prev){const u=prev.post_result_update&&typeof prev.post_result_update==='object'&&!Array.isArray(prev.post_result_update)?prev.post_result_update:null;
    const rid=prev.observed_result_id;
    out.push(`<strong>Observed result of decision #${esc(prev.sequence??'?')} → next test</strong><p>Selected ${esc(prev.selected_point_id??'null')} · observed ${rid!=null?`<button class="text-action" data-evidence="${esc(rid)}">${esc(rid)}</button>`:'no result ID (null)'}<br>${u?`Rank update from result ${show(u.rank_before)} → ${show(u.rank_after)} · ${esc(changedText(u.selection_changed,'This result changed the next selection','The next selection is unchanged after this result'))}${u.observed_result_id!=null&&u.observed_result_id!==rid?` · update based on result ${esc(u.observed_result_id)}`:''}<br>Updated next candidates ${candList(u.next_candidates)}`:'No post_result_update record · no update was recorded immediately after the result.'}</p>`);}
  if(cur){out.push(`<strong>Decision #${esc(cur.sequence??'?')} · at selection</strong><p>Selected ${esc(cur.selected_point_id??'null')} · evidence ${Array.isArray(cur.evidence_result_ids)?cur.evidence_result_ids.length:0} · remaining budget ${esc(cur.remaining_budget??'null')}${cur.actor!=null?' · '+esc(cur.actor):''}<br>Rank before the latest evidence ${show(cur.rank_before)} → after ${show(cur.rank_after)} · ${esc(changedText(cur.selection_changed,'The previous result changed this selection','The selection is unchanged after the previous result'))}<br>This decision’s observed result is revealed at the next replay step.</p>`);}
  return out.join('');
}
function renderLab(){
  renderSteps();renderBudget();renderMap();renderCandidates();renderTrace();
  const n=D(),m=state.m,local=state.mode==='local';
  const atEnd=state.stage>=n,label=!n?(local?'Open run control':'View policy comparison'):atEnd?'View policy comparison':'Next recorded step';
  $('nextTitle').textContent=!m?'No snapshot to display.':!n?'No recorded discovery loop yet.':atEnd?'The recorded loop has been replayed to the end.':state.stage===0?'Next: the observed result of the first decision.':'Next: the observed result of the next decision.';
  $('nextDescription').textContent=!n?(local?'Running the demo preparation job records calibration and initial observations. Adaptive decisions are recorded later by the agent loop.':'The public view only replays stored records.'):atEnd?'Next: in Policy comparison, review results by selection method under the same budget, the primary verdict and limitations.':'Replays one step at a time in recorded order. This is not live computation.';
  $('advanceButton').innerHTML=`${label}${icon('arrow')}`;$('topAdvanceButton').textContent=label;
  $('topAdvanceButton').disabled=$('advanceButton').disabled=false;
  $('playButton').disabled=$('resetButton').disabled=n===0;$('finalButton').disabled=n===0||atEnd;
}
function advance(){stopPlaying();const n=D();if(!n){state.mode==='local'?openJobs():setView('benchmark');return;}if(state.stage>=n)setView('benchmark');else setStage(state.stage+1);}
function setStage(i){state.stage=Math.max(0,Math.min(i,D()));const m=state.m,d=m&&m.decisions[state.stage],prev=m&&m.decisions[state.stage-1];
  let k=null;if(d&&Array.isArray(d.candidates)){const c=d.candidates.find(x=>x.point_id===d.selected_point_id)||d.candidates[0];k=keyOf(c);}else if(prev){const o=m.obsById.get(String(prev.observed_result_id));k=keyOf(o);}
  if(k){state.point=k;state.corner=k.split('|')[0];}refreshLab();}
function refreshLab(){if(state.view==='lab')renderView('lab');else dirty.lab=true;}
let playTimer;function stopPlaying(){clearInterval(playTimer);state.playing=false;$('playButton').innerHTML=`${icon('play')}<span>Replay records</span>`;}
function play(){if(state.playing){stopPlaying();return;}if(!D())return;setStage(0);state.playing=true;$('playButton').innerHTML=`${icon('close')}<span>Pause replay</span>`;playTimer=setInterval(()=>{if(state.stage>=D()){stopPlaying();return;}setStage(state.stage+1);if(state.stage>=D())stopPlaying();},2100);}

/* ---------- benchmark ---------- */
function niceMax(v){if(v<=3)return 3;const step=Math.ceil(v/5);return step*5;}
/* The mean curve is the core's exported mean_cumulative_clear (complete runs only),
   never recomputed here. Individual runs are drawn as recorded; incomplete runs are
   dashed and are not part of the mean. */
const runComplete=r=>r&&(r.status==='complete'||r.status==='completed');
function policyCurve(p){const runs=Array.isArray(p.runs)?p.runs.filter(r=>r&&typeof r==='object'):[];
  const lines=runs.filter(r=>Array.isArray(r.cumulative_clear)).map(r=>({arr:r.cumulative_clear,complete:runComplete(r)}));
  const mean=Array.isArray(p.mean_cumulative_clear)?p.mean_cumulative_clear.map(num):null;
  const meanFinal=mean?(mean.length?mean[mean.length-1]:null):num(p.mean_cumulative_clear);
  const completeRuns=num(p.complete_runs)!=null?p.complete_runs:runs.filter(runComplete).length;
  const len=Math.max(0,...lines.map(l=>l.arr.length),mean?mean.length:0);
  return {runs,lines,mean,meanFinal,completeRuns,incomplete:runs.filter(r=>!runComplete(r)),len};}
/* Chart geometry and per-policy SVG depend only on the snapshot, so they are built
   once per normalized snapshot (a new snapshot gets a new state.m). A legend toggle
   only re-joins the fragments of the visible policies. */
function benchModel(){const m=state.m;if(m.benchView)return m.benchView;
  const b=m.benchmark;const pol=Array.isArray(b.policies)?b.policies.filter(p=>p&&typeof p==='object'):[];
  const curves=pol.map(p=>policyCurve(p));const len=Math.max(1,...curves.map(c=>c.len));
  const ymaxData=Math.max(0,...curves.flatMap(c=>[...c.lines.flatMap(l=>l.arr),...(c.mean||[])].map(num).filter(x=>x!=null)));const ymax=niceMax(ymaxData);
  const x=n=>48+(n/len)*560,y=v=>275-v/ymax*235;const ystep=ymax/ (ymax%3===0?3:5);const xstep=Math.max(1,Math.ceil(len/5));
  let svg='';for(let v=0;v<=ymax+1e-9;v+=ystep)svg+=`<line class="gridline" x1="48" x2="608" y1="${y(v)}" y2="${y(v)}"/><text x="40" y="${y(v)+4}" text-anchor="end">${+v.toFixed(1)}</text>`;
  svg+=`<line class="axis-line" x1="48" x2="608" y1="275" y2="275"/>`;
  for(let n=0;n<=len;n+=xstep)svg+=`<text x="${x(n)}" y="296" text-anchor="middle">${n}</text>`;
  svg+='<text x="48" y="18">Mean cumulative clear counterexamples</text><text x="608" y="338" text-anchor="end">Search simulations</text>';
  const frags=pol.map((p,i)=>{let f='';const col=POLICY_COLORS[i%POLICY_COLORS.length],c=curves[i];
    c.lines.forEach(l=>{const pts=l.arr.map((v,j)=>num(v)==null?null:[x(j+1),y(v)]).filter(Boolean);if(pts.length>1)f+=`<path class="run-line${l.complete?'':' incomplete'}" stroke="${col}" ${l.complete?'':'stroke-dasharray="2 3"'} d="${pts.map(([px,py],j)=>`${j?'L':'M'}${px.toFixed(1)},${py.toFixed(1)}`).join(' ')}"/>`;});
    const pts=(c.mean||[]).map((v,j)=>v==null?null:[x(j+1),y(v)]).filter(Boolean);
    if(pts.length){f+=`<path class="curve" stroke="${col}" ${i===3?'stroke-dasharray="5 4"':''} d="${pts.map(([px,py],j)=>`${j?'L':'M'}${px.toFixed(1)},${py.toFixed(1)}`).join(' ')}"/>`;const [lx,ly]=pts[pts.length-1];f+=`<circle cx="${lx}" cy="${ly}" r="3.5" fill="${col}"/>`;}
    return f;});
  return m.benchView={pol,curves,grid:svg,frags};}
function renderBenchChart(){const {pol,grid,frags}=benchModel();$('benchmarkChart').innerHTML=grid+pol.map((p,i)=>state.visiblePolicies.has(String(p.id))?frags[i]:'').join('');}
function renderBenchmark(){
  const b=state.m&&state.m.benchmark;const pol=b&&Array.isArray(b.policies)?b.policies.filter(p=>p&&typeof p==='object'):[];
  if(!b){$('chartSubtitle').textContent='No policy comparison results';$('benchmarkChart').innerHTML='';$('benchmarkChart').setAttribute('aria-label','No policy comparison data');$('chartLegend').innerHTML='';$('chartDescription').textContent='benchmark = null · The snapshot contains no primary campaign results (4 methods × 10 seeds).';
    $('benchmarkConclusion').innerHTML='<span class="tag gray">No results</span><h2>No policy comparison<br>results yet.</h2><p>Comparison results are not shown before the primary study runs. No example curves or intervals are drawn in their place.</p><div class="conclusion-note">Primary success criterion: mean ≥ +2 over random, paired bootstrap 95% interval lower bound &gt; 0, 10 complete seed pairs.</div>';
    $('benchmarkTable').innerHTML='<tr><td colspan="5" class="muted">No recorded policy results.</td></tr>';$('benchmarkTableNote').textContent='The quantitative comparison shows the effect of the selection policy; the live demo shows Omnigent collaboration. They are reported separately.';$('benchmarkStatusTag').textContent='No results';
    renderBenchDetails(null);return;}
  $('benchmarkStatusTag').textContent='Status '+String(b.status??'null');
  const {curves}=benchModel();
  renderBenchChart();$('benchmarkChart').setAttribute('aria-label','Mean over completed runs exported by the core (mean_cumulative_clear) and per-run cumulative_clear curves');
  $('chartSubtitle').textContent=`Bold lines: mean over completed runs only (mean_cumulative_clear) · completed runs ${curves.map(c=>c.completeRuns).join(' / ')} · clear counterexample = error above 11%`;
  const noMean=pol.filter((p,i)=>!curves[i].mean).map(p=>p.label??p.id),nInc=curves.reduce((s,c)=>s+c.incomplete.length,0);
  $('chartDescription').textContent=`Thin solid lines are completed runs; dotted lines are incomplete runs (${nInc}, excluded from the mean).${noMean.length?` No mean (null): ${noMean.join(', ')}.`:''} Click the legend to compare curves.`;
  $('chartLegend').innerHTML=pol.map((p,i)=>`<button class="legend-btn" data-policy="${esc(p.id)}" aria-pressed="${state.visiblePolicies.has(String(p.id))}"><span class="line-swatch" style="--swatch:${POLICY_COLORS[i%POLICY_COLORS.length]}"></span>${esc(p.label??p.id)}${curves[i].mean?'':' · no mean'}</button>`).join('');
  const pr=b.primary&&typeof b.primary==='object'?b.primary:{};const ci=Array.isArray(pr.ci95)&&pr.ci95.length===2&&pr.ci95.every(v=>num(v)!=null)?pr.ci95:null;
  const tag=pr.success===true?['green','Primary success criterion met']:pr.success===false?['amber','Primary success criterion not met']:['gray','Primary verdict unavailable (null)'];
  const head=pr.success===true?'More counterexamples found<br>with the same budget.':pr.success===false?'Superiority was<br>not confirmed.':'The primary success verdict<br>cannot be made.';
  const md=num(pr.mean_difference);
  $('benchmarkConclusion').innerHTML=`<span class="tag ${tag[0]}">${tag[1]}</span><h2>${head}</h2><p>${pr.reason!=null?esc(pr.reason):'No verdict reason recorded (reason = null).'}</p><div class="conclusion-metric"><span>Complete seed pairs</span><strong>${pr.complete_pairs??'—'}</strong></div><div class="conclusion-metric"><span>Adaptive − random mean difference</span><strong>${md==null?'—':(md>0?'+':'')+md.toFixed(2)}</strong></div><div class="conclusion-metric"><span>Paired bootstrap 95% interval</span><strong style="font-size:14px">${ci?`${ci[0].toFixed(2)} ~ ${ci[1].toFixed(2)}`:'null · not computed'}</strong></div><div class="conclusion-note">Values are the primary results recorded by the core. The UI does not recompute intervals or means.${pr.comparison!=null?`<br>Comparison: ${compLabel(pr.comparison,policyLabel(pol))}`:''}</div>${Array.isArray(pr.paired_differences)&&pr.paired_differences.length?disclosure(`Per-seed differences · ${pr.paired_differences.length} pairs`,pairChips(pr.paired_differences)):''}${b.primary!=null?rawJson('primary raw fields',b.primary):''}`;
  const ri=pol.findIndex(p=>p.id==='random');const rm=ri<0?null:curves[ri].meanFinal;
  $('benchmarkTable').innerHTML=pol.length?pol.map((p,i)=>{const c=curves[i],mean=c.meanFinal;const diff=p.id==='random'?'Baseline':mean!=null&&rm!=null?((mean-rm>0?'+':'')+(mean-rm).toFixed(2)):'—';const inc=c.incomplete.map(r=>`seed ${r.seed??'null'}: ${r.status??'null'}`);return `<tr><td><span class="policy-name"><span class="dot" style="color:${POLICY_COLORS[i%POLICY_COLORS.length]}"></span>${esc(p.label??p.id)}</span></td><td>${mean==null?'— <span class="muted">no mean (null)</span>':mean.toFixed(2)}</td><td>${diff}</td><td>${esc(c.completeRuns)}/${c.runs.length} complete${inc.length?`<br><span class="muted">incomplete · ${esc(inc.join(', '))}</span>`:''}</td><td class="muted">${esc(POLICY_PURPOSE[p.id]||'')}</td></tr>`;}).join(''):'<tr><td colspan="5" class="muted">The policies array is empty.</td></tr>';
  $('benchmarkTableNote').textContent='The mean is the last value of mean_cumulative_clear, exported by the core from completed runs only. Incomplete runs are excluded from the mean and the completed count and are never replaced with success values. A null mean is shown as —. The difference vs. random is a simple difference of the two exported means; the primary verdict uses only the seed-paired primary result.';
  renderBenchDetails(b);
}
function kvList(o){if(o==null)return `<p>${NULL}</p>`;if(typeof o!=='object')return `<p>${esc(o)}</p>`;if(Array.isArray(o))return o.length?`<ul>${o.map(x=>`<li>${show(x)}</li>`).join('')}</ul>`:'<p class="muted">Empty array</p>';const ks=Object.keys(o);return ks.length?`<dl class="kv">${ks.map(k=>`<dt>${esc(k)}</dt><dd>${show(o[k])}</dd>`).join('')}</dl>`:'<p class="muted">Empty object</p>';}
/* Supporting evidence. Summaries read exact exported fields (formatted values keep
   the raw number in a title); bulky per-run/per-seed arrays and each full original
   object stay in closed disclosures with their original precision. Absent keys are
   not shown; present nulls render as null. */
const disclosure=(sum,body)=>`<details class="disclosure"><summary>${sum}</summary><div class="disclosure-body">${body}</div></details>`;
const rawJson=(label,v)=>disclosure(esc(label),`<pre class="json-block">${esc(JSON.stringify(v,null,2))}</pre>`);
const isObj=v=>v!=null&&typeof v==='object'&&!Array.isArray(v);
const has=(o,k)=>isObj(o)&&Object.prototype.hasOwnProperty.call(o,k);
const keyTitle=k=>` title="${esc(k)}"`;
const raw=(v,text)=>`<span class="data" title="${esc(v)}">${text}</span>`;
const vVal=v=>v==null?NULL:typeof v==='object'?'<span class="muted">see raw fields</span>':typeof v==='number'?`<span class="data">${esc(v)}</span>`:esc(v);
const vPct=v=>num(v)==null?vVal(v):raw(v,fmtPct(v));
const vNum=v=>num(v)==null?vVal(v):raw(v,v.toFixed(4));
const vSec=v=>num(v)==null?vVal(v):raw(v,v.toFixed(1)+' s');
const vDiff=v=>num(v)==null?vVal(v):raw(v,(v>0?'+':'')+v.toFixed(2));
const vCi=v=>v==null?'null · not computed':Array.isArray(v)&&v.length===2&&v.every(x=>num(x)!=null)?raw(JSON.stringify(v),`${v[0].toFixed(2)} ~ ${v[1].toFixed(2)}`):`<span class="mono">${esc(JSON.stringify(v))}</span>`;
const vCount=v=>Array.isArray(v)?`<span class="data">${v.length}</span>`:vVal(v);
function fields(o,spec){const rows=spec.filter(([k])=>has(o,k)).map(([k,l,f])=>`<dt${keyTitle(k)}>${esc(l)}</dt><dd>${(f||vVal)(o[k])}</dd>`);return rows.length?`<dl class="kv">${rows.join('')}</dl>`:'';}
const policyLabel=pol=>id=>{const p=pol.find(x=>String(x.id)===String(id));return p&&p.label!=null?String(p.label):String(id);};
function compLabel(c,pl){const mm=typeof c==='string'&&c.match(/^(.+) minus (.+)$/);return mm?`<span${keyTitle(c)}>${esc(pl(mm[1]))} − ${esc(pl(mm[2]))}</span>`:esc(c);}
function pairChips(a){return `<div class="paired">${a.map(x=>{const v=x&&typeof x==='object'?num(x.difference):num(x);const sd=x&&typeof x==='object'&&x.seed!=null?`seed ${esc(x.seed)}: `:'';return `<span>${sd}${v==null?'null':(v>0?'+':'')+v}</span>`;}).join('')}</div>`;}
const ERR_SPEC=[['points','Evaluated points'],['clear_counterexamples','Clear counterexamples (>11%)'],['secondary_counterexamples','Secondary counterexamples (>10%)'],['mean_abs_relative_error','Mean absolute relative error',vPct],['max_abs_relative_error','Max absolute relative error',vPct]];
const COST_SPEC=[['campaign_kind','Campaign kind'],['logical_queries','Logical queries'],['attempts','Simulation attempts'],['successes','Successes'],['failures','Failures'],['unfinished_attempts','Unfinished attempts'],['cache_hits','Cache hits'],['restricted_cache_hits','Restricted cache hits'],['simulation_seconds','Simulation time',vSec],['policy_seconds','Policy compute time',vSec],['wall_seconds','Total wall time',vSec],['posthoc_attempts_separate_from_research_budget','Post hoc attempts outside the research budget']];
const PURPOSE_LABEL={preflight:'Numerical preflight',calibration:'Calibration',benchmark:'Primary search',posthoc:'Post hoc evaluation',postflight:'Postflight check',live:'Workbench demo',attempts_max:'Attempt cap',wall_seconds_max:'Wall time cap (s)'};
const PART_LABEL={training:'Calibration points',search_pool:'Search candidates',held_out:'Held-out points',prior_excluded:'Excluded prior check',low_vdd:'Low-VDD range',high_vdd:'High-VDD range'};
const mapList=(o,labels)=>`<dl class="kv">${Object.keys(o).map(k=>`<dt${keyTitle(k)}>${esc(labels[k]||k)}</dt><dd>${vVal(o[k])}</dd>`).join('')}</dl>`;
function statTable(rows){return `<div class="table-wrap" tabindex="0" role="region" aria-label="Error by range table"><table class="results-table mini-table"><thead><tr><th scope="col">Range</th><th scope="col">Points</th><th scope="col">Clear</th><th scope="col">Secondary</th><th scope="col">Mean error</th><th scope="col">Max error</th></tr></thead><tbody>${rows.map(([k,o])=>`<tr><th scope="row"${keyTitle(k)}>${esc(PART_LABEL[k]||k)}</th><td>${vVal(o.points)}</td><td>${vVal(o.clear_counterexamples)}</td><td>${vVal(o.secondary_counterexamples)}</td><td>${vPct(o.mean_abs_relative_error)}</td><td>${vPct(o.max_abs_relative_error)}</td></tr>`).join('')}</tbody></table></div>`;}
const card=(title,body,cls='')=>`<section class="detail-card${cls}"><h3>${title}</h3>${body}</section>`;
function heldOutCard(h,pl){if(!isObj(h))return card('Held-out evaluation',h==null?`<p>${NULL}</p>`:rawJson('held_out raw fields',h));
  let body='';
  if(isObj(h.model_errors))body+=`<p class="card-lead">Frozen-model error at held-out points</p>${fields(h.model_errors,ERR_SPEC)}`;else body+=fields(h,[['model_errors','Frozen-model errors']]);
  body+=fields(h,[['missing_points','Missing points',vCount]]);
  const bp=h.idw_error_map_mae_by_policy;
  if(isObj(bp))body+=`<p class="card-lead">IDW error-map MAE by policy</p><dl class="kv">${Object.keys(bp).map(k=>`<dt${keyTitle(k)}>${esc(pl(k))}</dt><dd>${vNum(bp[k])}</dd>`).join('')}</dl>`;else body+=fields(h,[['idw_error_map_mae_by_policy','IDW error-map MAE by policy']]);
  if(h.note!=null)body+=`<p class="card-note">${esc(h.note)}</p>`;
  const runs=h.idw_error_map_mae;
  if(Array.isArray(runs)&&runs.length)body+=disclosure(`Per-run IDW error-map MAE · ${runs.length}`,`<div class="table-wrap" tabindex="0" role="region" aria-label="Per-run IDW error-map MAE table"><table class="results-table mini-table"><thead><tr><th scope="col">Policy</th><th scope="col">seed</th><th scope="col">Status</th><th scope="col">MAE</th></tr></thead><tbody>${runs.map(r=>isObj(r)?`<tr><td>${esc(pl(r.policy))}</td><td>${vVal(r.seed)}</td><td>${vVal(r.status)}</td><td>${vNum(r.idw_map_mae)}</td></tr>`:`<tr><td colspan="4">${vVal(r)}</td></tr>`).join('')}</tbody></table></div>`);
  return card('Held-out evaluation',body+rawJson('held_out raw fields',h));}
function referenceCard(r){if(!isObj(r))return card('Full-grid reference',r==null?`<p>${NULL}</p>`:rawJson('reference raw fields',r));
  let body=fields(r,[['points_evaluated','Full-grid points evaluated'],['missing_points','Missing points',vCount]]);
  if(r.h2_note!=null)body+=`<p class="card-note">${esc(r.h2_note)}</p>`;
  const rows=[...(isObj(r.by_partition)?Object.entries(r.by_partition):[]),...['low_vdd','high_vdd'].filter(k=>has(r,k)).map(k=>[k,r[k]])].filter(([,o])=>isObj(o));
  if(rows.length)body+=disclosure(`Error breakdown by partition and voltage range · ${rows.length} ranges`,statTable(rows));
  return card('Full-grid reference',body+rawJson('reference raw fields',r));}
function costCard(title,c,key){if(!isObj(c))return card(title,c==null?`<p>${NULL}</p><p class="card-note">No cost record (null)</p>`:rawJson(key+' raw fields',c));
  let body=fields(c,COST_SPEC);const sub=['attempts_by_purpose','caps'].filter(k=>isObj(c[k]));
  if(sub.length)body+=disclosure('Attempts by purpose · caps',sub.map(k=>`<p class="card-lead"${keyTitle(k)}>${k==='caps'?'Caps':'Attempts by purpose'}</p>${mapList(c[k],PURPOSE_LABEL)}`).join(''));
  return card(title,body+rawJson(key+' raw fields',c));}
function secondaryCard(sc,pl){if(!Array.isArray(sc))return card('Secondary comparisons',sc==null?`<p>${NULL}</p>`:rawJson('secondary_comparisons raw fields',sc));
  const body=sc.length?sc.map(c=>{if(!isObj(c))return `<div class="comparison">${vVal(c)}</div>`;
    const pairs=Array.isArray(c.paired_differences)?c.paired_differences:null;
    const extra=(pairs&&pairs.length?pairChips(pairs):'')+fields(c,[['excluded_pairs','Excluded pairs',vCount]])+(isObj(c.bootstrap)?`<p class="card-lead">bootstrap</p>${mapList(c.bootstrap,{})}`:'');
    return `<div class="comparison"><strong>${has(c,'comparison')?compLabel(c.comparison,pl):'no comparison'}</strong>${fields(c,[['complete_pairs','Complete seed pairs'],['mean_difference','Mean difference',vDiff],['ci95','Paired bootstrap 95% interval',vCi],['reason','Verdict reason']])}${disclosure(`Per-seed differences${pairs?' · '+pairs.length+' pairs':''}`,extra||'<p class="muted">No record</p>')}</div>`;}).join(''):'<p class="muted">Empty array</p>';
  return card('Secondary comparisons',body+rawJson('secondary_comparisons raw fields',sc));}
function limitsCard(snapL,benchL){const list=a=>a.length?`<ul class="limit-list">${a.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<p class="muted">No record (empty array)</p>';
  return card('Limitations',`<p class="card-lead" title="limitations">Snapshot limitations · ${snapL.length}</p>${list(snapL)}${benchL?`<p class="card-lead" title="benchmark.limitations">Primary-study limitations · ${benchL.length}</p>${list(benchL)}`:''}`,' wide');}
function renderBenchDetails(b){const m=state.m,pol=b&&Array.isArray(b.policies)?b.policies.filter(p=>p&&typeof p==='object'):[],pl=policyLabel(pol);
  $('benchmarkDetails').innerHTML=[heldOutCard(b?b.held_out:null,pl),referenceCard(b?b.reference:null),secondaryCard(b?b.secondary_comparisons:null,pl),costCard('Primary-study cost',b?b.cost:null,'benchmark.cost'),costCard(state.snap&&state.snap.release?'Workbench demo cost':'Snapshot run cost',m?m.cost:null,'cost'),limitsCard(m?m.limitations:[],b&&Array.isArray(b.limitations)?b.limitations.map(String):null)].join('');}

/* ---------- records ---------- */
function recordRows(){const m=state.m;if(!m)return [];return m.recordRows||(m.recordRows=buildRecordRows(m));}
function buildRecordRows(m){const rows=m.observations.map(o=>({id:o.result_id,key:keyOf(o),phase:o.phase,obs:o,ev:m.evalById.get(String(o.result_id))||null}));
  m.evaluations.forEach(e=>{if(!m.obsById.has(String(e.result_id)))rows.push({id:e.result_id,key:keyOf(e),phase:isHeld(m.covByKey.get(keyOf(e))&&{phase:m.covByKey.get(keyOf(e)).state==='held_out'?'held_out':''})?'held_out':'evaluation',obs:null,ev:e});});return rows;}
function verdict(ev){if(!ev)return ['gray','Not evaluated'];if(ev.clear_counterexample===true)return ['amber','Clear counterexample'];if(ev.secondary_counterexample===true)return ['gray','Secondary counterexample (>10%)'];if(ev.clear_counterexample===false&&ev.secondary_counterexample===false)return ['green','Within tolerance'];return ['gray','verdict null'];}
function renderRecords(){
  const q=$('recordSearch').value.trim().toLowerCase(),f=$('recordFilter').value,corner=$('recordCorner').value,sort=$('recordSort').value;const all=recordRows();
  let list=all.filter(r=>{const ev=r.ev;if(!(String(r.id)+' '+(r.key||'')+' '+fmtKey(r.key)+' '+(r.phase||'')+' '+phaseLabel(r.phase)).toLowerCase().includes(q))return false;if(corner!=='all'&&!(r.key||'').startsWith(corner+'|'))return false;
    if(f==='clear')return ev&&ev.clear_counterexample===true;if(f==='secondary')return ev&&ev.secondary_counterexample===true&&ev.clear_counterexample!==true;if(f==='pass')return ev&&ev.clear_counterexample===false&&ev.secondary_counterexample===false;if(f==='unevaluated')return !ev;if(f==='heldout')return r.phase==='held_out'||r.phase==='heldout';return true;});
  /* Error sorts read the exported abs_relative_error only. Rows without a numeric
     error stay last in recorded order; ties keep recorded order. */
  if(sort==='error-desc'||sort==='error-asc'){const dir=sort==='error-desc'?-1:1,err=r=>num(r.ev?r.ev.abs_relative_error:null);
    list=list.map((r,i)=>[r,i,err(r)]).sort((a,b)=>a[2]==null||b[2]==null?(a[2]==null)-(b[2]==null)||a[1]-b[1]:(a[2]-b[2])*dir||a[1]-b[1]).map(x=>x[0]);}
  const filtered=q!==''||f!=='all'||corner!=='all';
  $('recordReset').disabled=!filtered;
  $('recordCount').textContent=`${list.length} / ${all.length} records`;
  $('recordsTable').innerHTML=list.map(r=>{const [c,l]=verdict(r.ev);const obs=r.obs?r.obs.tpd_s:r.ev?r.ev.simulated_tpd_s:null;return `<tr><td class="mono">${esc(r.id)}</td><td>${esc(fmtKey(r.key))}</td><td><span class="phase" title="phase = ${esc(r.phase??'null')}">${esc(phaseLabel(r.phase))}</span></td><td>${fmtPs(r.ev?r.ev.predicted_tpd_s:null)}</td><td>${fmtPs(obs)}</td><td>${fmtPct(r.ev?r.ev.abs_relative_error:null)}</td><td><span class="tag ${c}">${esc(l)}</span></td><td><button class="text-action" data-evidence="${esc(r.id)}">View details</button></td></tr>`;}).join('');
  const active=[q!==''?`search “${esc($('recordSearch').value.trim())}”`:'',corner!=='all'?`corner ${esc(corner)}`:'',f!=='all'?`verdict “${esc($('recordFilter').selectedOptions[0].textContent)}”`:''].filter(Boolean);
  $('emptyRecords').hidden=list.length!==0;$('emptyRecords').innerHTML=all.length?`<p>No records match ${active.join(', ')}.</p><button class="btn compact" type="button" data-reset-records>Reset filters and show all ${all.length} records</button>`:'<p>The snapshot has no observation or evaluation records yet.</p>';
}

/* ---------- dialogs ---------- */
/* Focus return. The trigger is remembered when the dialog first opens (links followed
   inside an open dialog keep the original). On close (button, Escape or backdrop) focus
   goes back to it, or, if a re-render replaced it, to the equivalent control in the
   same view, else to the main region. Closing restores synchronously: the 'close'
   event is queued and may arrive after a later reopen, so it is only a fallback. */
let dialogReturn=null;
function rememberTrigger(){const el=document.activeElement;if(!el||el===document.body||$('detailDialog').contains(el))return null;
  const d=el.dataset||{},sel=el.id?'#'+CSS.escape(el.id):d.evidence!=null?`[data-evidence="${CSS.escape(d.evidence)}"]`:d.candidate!=null?`[data-candidate="${CSS.escape(d.candidate)}"]`:d.openJobs!=null?'[data-open-jobs]':null;
  const v=el.closest('[id^="view-"]');return {el,sel,view:v?v.id:null};}
function restoreFocus(){if($('detailDialog').open)return;const r=dialogReturn;dialogReturn=null;if(!r){if(document.activeElement===document.body)$('mainContent').focus({preventScroll:true});return;}const ok=el=>el&&el.isConnected&&!el.disabled&&el.getClientRects().length>0;
  if(ok(r.el)){r.el.focus();return;}
  if(r.sel){const scope=r.view?$(r.view):document;const alt=scope&&[...scope.querySelectorAll(r.sel)].find(ok);if(alt){alt.focus();return;}}
  $('mainContent').focus({preventScroll:true});}
/* Content replaced inside an open dialog must not drop focus to the page. */
function closeDialog(){$('detailDialog').close();restoreFocus();}
function keepDialogFocus(){if($('detailDialog').open&&!$('detailDialog').contains(document.activeElement))$('closeDialog').focus();}
function openDialog(title,html){const dlg=$('detailDialog');if(!dlg.open)dialogReturn=rememberTrigger();$('dialogTitle').textContent=title;$('dialogBody').innerHTML=html;$('dialogBody').scrollTop=0;if(!dlg.open)dlg.showModal();keepDialogFocus();}
function evidence(id){const m=state.m;if(!m)return;const o=m.obsById.get(String(id))||null,e=m.evalById.get(String(id))||null;
  if(!o&&!e){openDialog('Evidence detail · '+id,`<span class="tag amber">No record</span><p style="margin-top:13px">The snapshot has no observation or evaluation for result ID ${esc(id)}.</p>`);return;}
  const k=keyOf(o||e),pv=o&&o.provenance&&typeof o.provenance==='object'?o.provenance:null;
  const decs=m.decisions.filter(d=>d.observed_result_id===id||(Array.isArray(d.evidence_result_ids)&&d.evidence_result_ids.includes(id)));
  const trs=m.trace.filter(t=>Array.isArray(t.result_ids)&&t.result_ids.includes(id));
  const [vc,vl]=verdict(e);const isMode=state.snap.data_mode==='real';
  openDialog('Evidence detail · '+id,`<span class="tag ${isMode?vc:'red'}">${isMode?esc(vl):'Fixture · not a real measurement'}</span> <span class="tag gray">${esc(o?o.phase??'phase null':'Evaluation-only record')}</span><p style="margin-top:13px">${esc(fmtKey(k))} · Frozen-model predictions and simulated observations are shown separately.</p>
  <div class="evidence-grid"><div class="evidence-field"><small>Frozen-model prediction (predicted_tpd_s)</small><strong>${fmtPs(e?e.predicted_tpd_s:null)} ps</strong></div><div class="evidence-field"><small>ngspice observation (${o?'tpd_s':'simulated_tpd_s'})</small><strong>${fmtPs(o?o.tpd_s:e?e.simulated_tpd_s:null)} ps</strong></div></div>
  <h3>Evaluation ${e?fmtPct(e.abs_relative_error):'none'}</h3><dl class="kv"><dt>relative_error</dt><dd>${show(e?e.relative_error:null)}</dd><dt>clear (&gt;0.11)</dt><dd>${show(e?e.clear_counterexample:null)}</dd><dt>secondary (&gt;0.10)</dt><dd>${show(e?e.secondary_counterexample:null)}</dd><dt>model_hash</dt><dd class="mono">${show(e?e.model_hash:null)}</dd></dl>
  <h3>Observation and provenance</h3><dl class="kv"><dt>tphl / tplh</dt><dd>${fmtPs(o?o.tphl_s:null)} / ${fmtPs(o?o.tplh_s:null)} ps</dd><dt>cache_hit</dt><dd>${show(o?o.cache_hit:null)}</dd><dt>wall_time_s</dt><dd>${show(o?o.wall_time_s:null)}</dd><dt>netlist_sha256</dt><dd class="mono">${show(pv?pv.netlist_sha256:null)}</dd><dt>simulator</dt><dd>${show(pv?pv.simulator:null)}</dd></dl>
  <h3>Raw measurement lines</h3><pre class="json-block" id="rawMeas"></pre>
  <h3>Linked decisions ${decs.length} · trace ${trs.length}</h3>${decs.length?`<ul>${decs.map(d=>`<li>Decision #${esc(d.sequence??'?')} · ${d.observed_result_id===id?'Observed result of this decision':'Used as evidence'} · selected ${esc(d.selected_point_id??'null')}</li>`).join('')}</ul>`:'<p>No linked decisions.</p>'}
  <h3>Raw record</h3><pre class="json-block" id="rawRecord"></pre>`);
  const rm=pv&&pv.raw_meas_lines;$('rawMeas').textContent=rm==null?'null':Array.isArray(rm)?rm.join('\n'):String(rm);
  $('rawRecord').textContent=JSON.stringify({observation:o,evaluation:e},null,2);}
function showCandidate(i){const m=state.m,d=m&&m.decisions[state.stage];const c=d&&Array.isArray(d.candidates)?d.candidates[i]:null;if(!c)return;const sel=c.point_id===d.selected_point_id;const di=m.decisions.indexOf(d);
  openDialog('Candidate review · '+fmtKey(keyOf(c)),`<span class="tag ${sel?'green':'gray'}">${sel?'Selected candidate':'Comparison candidate'} · ${esc(candidateRole(c.role))}</span><p style="margin-top:13px">Candidate recorded in decision #${esc(d.sequence??'?')}. The score is a heuristic proxy for expected learning, not a probability or calibrated uncertainty.</p><div class="evidence-grid"><div class="evidence-field"><small>IDW estimated error (not observed)</small><strong>${fmtPct(c.idw_predicted_abs_error)}</strong></div><div class="evidence-field"><small>Selection score</small><strong>${fmtN(c.score,3)}</strong></div></div><dl class="kv"><dt>point_id</dt><dd class="mono">${show(c.point_id)}</dd><dt>min_normalized_distance</dt><dd>${show(c.min_normalized_distance)}</dd><dt>cost_queries</dt><dd>${show(c.cost_queries)}</dd><dt>Evidence result IDs</dt><dd class="mono">${show(d.evidence_result_ids)}</dd><dt>Remaining budget</dt><dd>${show(d.remaining_budget)}</dd></dl><h3>After the result</h3><p>${sel?(d.observed_result_id!=null?(state.stage>di?`Observed result <button class="text-action" data-evidence="${esc(d.observed_result_id)}">${esc(d.observed_result_id)}</button> was recorded.`:'The observed result is revealed at the next replay step.'):'No observed result ID was recorded (null).'):'The actual outcome of an unselected candidate is unknown and not shown.'}</p><pre class="json-block" id="rawCand"></pre>`);$('rawCand').textContent=JSON.stringify(c,null,2);}
function showTraceAll(){openDialog('Full evidence trail',visibleTrace().map(traceItem).join('')||'<p>No records</p>');}
function showProtocol(){const s=state.snap,m=state.m,model=m&&m.model;openDialog('Frozen research design · v1.0',`<span class="tag green">Design frozen · records read from the snapshot</span><h3>Research question</h3><p>Can adaptive selection find the failure conditions of a circuit delay model calibrated on little data more efficiently?</p><h3>Identical comparison conditions</h3><ul><li>5 process corners × 7 voltages × 5 temperatures = 175 conditions</li><li>9 calibration points, 1 prior connectivity-check point excluded, 40 held-out points, 125 search candidates</li><li>4 selection methods × 10 seeds, each with 9 calibration + 15 search simulations</li><li>10% error tolerance; the primary metric counts clear counterexamples above 11%</li><li>Primary success criterion: mean ≥ +2 over random, resampled interval lower bound > 0</li></ul><h3>Current snapshot</h3><dl class="kv"><dt>data_mode</dt><dd>${show(s?s.data_mode:null)}</dd><dt>generated_at</dt><dd>${show(s?s.generated_at:null)}</dd><dt>protocol_hash</dt><dd class="mono">${show(s?s.protocol_hash:null)}</dd><dt>snapshot sha256</dt><dd class="mono">${show(state.sha)}</dd><dt>model_hash</dt><dd class="mono">${show(model?model.model_hash:null)}</dd><dt>model form</dt><dd>${show(model?model.form:null)}</dd><dt>coefficients</dt><dd>${show(model?model.coefficients:null)}</dd><dt>calibration ids</dt><dd>${show(model?model.calibration_result_ids:null)}</dd></dl><h3>Interpretation and limitations</h3><p>This is a computational experiment on a generic Level 1 device model. Replicates measure seed-to-seed variation, not independent physical device experiments. The quantitative comparison shows the effect of the selection policy; the live demo shows Omnigent collaboration.</p>${kvList(m?m.limitations:[])}<h3>What you can do here</h3><p>${state.mode==='local'?'Explore, replay and export stored records, and start or cancel the two fixed jobs (demo-prepare, reproduce). The primary research campaign cannot be started from this view.':'Explore, replay and export (JSON) stored real records. The public view never runs simulations or changes research state.'}</p>`);}
function exportSnapshot(){if(!state.raw){showToast('No snapshot to export.');return;}const blob=new Blob([state.raw],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='wafflebench-snapshot-'+String(state.snap.generated_at||'no-timestamp').replace(/[^0-9A-Za-z]/g,'')+'.json';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);showToast(`Exported the exact snapshot shown on screen${state.sha?' · sha256 '+state.sha.slice(0,12):''}.`);}

/* ---------- local job control ---------- */
function jobPanel(){const c=state.controller||{},j=state.job,active=j&&(j.state==='queued'||j.state==='running');
  const why=!c.jobs_enabled?'This server was started with --read-only and cannot start jobs.':!c.core_available?'The core command falsify_lab.cli is not in this checkout. Jobs are disabled because they would produce no results.':active?'Another job is running. Only one job runs at a time.':'';
  const kinds=(c.kinds||[]).map(k=>`<div class="job-kind"><h3>${esc((KIND_INFO[k]||[k])[0])} <span class="mono muted">${esc(k)}</span></h3><p class="muted">${esc((KIND_INFO[k]||['',''])[1])}</p><button class="btn primary compact" data-start-job="${esc(k)}" ${why?'disabled':''}>Start</button></div>`).join('');
  let cur='<p>No jobs have been run yet.</p>';
  if(j){const [lab,col]=JOB_STATE[j.state]||[j.state,'gray'];cur=`<p><span class="tag ${col}"><span class="dot"></span>${esc(lab)}</span> <span class="mono">${esc(j.kind)} · ${esc(j.job_id)}</span></p><dl class="kv"><dt>Created</dt><dd>${esc(fmtTime(j.created_at))}</dd><dt>Started</dt><dd>${esc(fmtTime(j.started_at))}</dd><dt>Finished</dt><dd>${esc(fmtTime(j.finished_at))}</dd><dt>returncode</dt><dd>${show(j.returncode)}</dd><dt>Termination reason</dt><dd>${show(j.termination_reason)}</dd><dt>Error</dt><dd>${show(j.error)}</dd><dt>Command</dt><dd class="mono">${show((j.argv||[]).join(' '))}</dd><dt>Log location</dt><dd class="mono">${show(j.log_dir)}</dd></dl>${active?`<button class="btn compact" id="cancelJob">${icon('close')}Cancel job</button>`:''}<h3>CLI summary (stdout JSON)</h3><pre class="json-block" id="jobSummary"></pre><h3>stderr tail</h3><pre class="json-block" id="jobStderr"></pre>`;}
  return `<p>Only the two allowed jobs can be started. Commands and paths are fixed on the server, and the primary research campaign cannot be started here.</p>${why?`<p class="muted"><strong>${esc(why)}</strong></p>`:''}<div class="job-kinds">${kinds}</div><h3>Current · last job</h3>${cur}`;}
function openJobs(){openDialog('Local run control',jobPanel());fillJobPre();state.jobsOpen=true;}
function fillJobPre(){const j=state.job;if(!j||!$('jobSummary'))return;$('jobSummary').textContent=j.summary==null?'null':JSON.stringify(j.summary,null,2);$('jobStderr').textContent=j.stderr_tail||'(empty)';}
async function postJSON(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),cache:'no-store'});let j={};try{j=await r.json();}catch{}return {ok:r.ok,status:r.status,body:j};}
const ERR_TEXT={job_active:'A job is already running.',core_unavailable:'The core command is unavailable, so the job was not started.',jobs_disabled:'This server is read-only.',invalid_kind:'This job kind is not allowed.',origin_rejected:'The request origin was rejected.',no_active_job:'There is no job to cancel.'};
async function startJob(kind){try{const r=await postJSON('./api/jobs',{kind});showToast(r.ok?`Started job ${kind}.`:(ERR_TEXT[r.body.error]||`Job start failed (HTTP ${r.status})`)+(r.body.message?' '+r.body.message:''));}catch(e){showToast('Job request failed: '+e.message);}pollNow();}
async function cancelJob(){try{const r=await postJSON('./api/jobs/cancel',state.job?{job_id:state.job.job_id}:{});showToast(r.ok?'Cancellation requested.':(ERR_TEXT[r.body.error]||`Cancel failed (HTTP ${r.status})`));}catch(e){showToast('Cancel request failed: '+e.message);}pollNow();}
let pollTimer=null,pollDelay=2000,polling=false;
function applyController(j){const prev=state.job;state.controller=j;state.job=j.job||null;state.pollError=null;
  const changed=JSON.stringify(prev)!==JSON.stringify(state.job);
  if(changed&&$('detailDialog').open&&$('dialogTitle').textContent==='Local run control'){$('dialogBody').innerHTML=jobPanel();fillJobPre();keepDialogFocus();}
  if(prev&&state.job&&prev.job_id===state.job.job_id&&prev.state!==state.job.state&&!['queued','running'].includes(state.job.state))showToast(`Job ${state.job.kind}: ${(JOB_STATE[state.job.state]||[state.job.state])[0]}`);
  const sha=j.snapshot&&j.snapshot.sha256||null;return sha!==state.serverSha?(state.serverSha=sha,true):false;}
async function pollNow(){if(polling)return;polling=true;clearTimeout(pollTimer);
  try{const r=await fetch('./api/job',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);const j=await r.json();const snapChanged=applyController(j);if(snapChanged)await loadSnapshot();else renderChrome();const active=state.job&&['queued','running'].includes(state.job.state);pollDelay=active?1500:8000;}
  catch(e){state.pollError=String(e.message||e);pollDelay=Math.min(Math.max(pollDelay*2,3000),15000);renderChrome();}
  finally{polling=false;pollTimer=setTimeout(pollNow,pollDelay);}}

/* ---------- events ---------- */
/* New snapshot: every view is invalidated; only the shown one is rebuilt now. */
function renderAll(){renderChrome();dirty.lab=dirty.benchmark=dirty.records=true;renderView(state.view);}
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>setView(b.dataset.view)));
document.querySelectorAll('[data-corner]').forEach(b=>b.addEventListener('click',()=>{state.corner=b.dataset.corner;renderMap();}));
$('heatmap').addEventListener('click',e=>{const b=e.target.closest('[data-key]');if(b){state.point=b.dataset.key;renderMap();}});
$('steps').addEventListener('click',e=>{const b=e.target.closest('[data-step]');if(b){stopPlaying();setStage(Number(b.dataset.step));}});
$('candidates').addEventListener('click',e=>{const b=e.target.closest('[data-candidate]');if(b)showCandidate(Number(b.dataset.candidate));});
document.addEventListener('click',e=>{const ev=e.target.closest('[data-evidence]');if(ev){evidence(ev.dataset.evidence);return;}if(e.target.closest('#traceAll')){showTraceAll();return;}if(e.target.closest('[data-open-jobs]')){openJobs();return;}const sj=e.target.closest('[data-start-job]');if(sj){sj.disabled=true;startJob(sj.dataset.startJob);return;}if(e.target.closest('#cancelJob')){cancelJob();return;}if(e.target.closest('[data-reset-records]')){resetRecords();return;}if(e.target.closest('[data-retry-load]')){retryLoad();}});
$('chartLegend').addEventListener('click',e=>{const b=e.target.closest('[data-policy]');if(b){const p=b.dataset.policy;state.visiblePolicies.has(p)?state.visiblePolicies.delete(p):state.visiblePolicies.add(p);renderBenchChart();$('chartLegend').querySelectorAll('[data-policy]').forEach(x=>x.setAttribute('aria-pressed',state.visiblePolicies.has(x.dataset.policy)));}});
$('advanceButton').addEventListener('click',advance);$('topAdvanceButton').addEventListener('click',advance);
$('resetButton').addEventListener('click',()=>{stopPlaying();setStage(0);});$('finalButton').addEventListener('click',()=>{stopPlaying();setStage(D());if(state.view==='lab')$('advanceButton').focus();});$('playButton').addEventListener('click',play);
$('recordSearch').addEventListener('input',renderRecords);$('recordFilter').addEventListener('change',renderRecords);$('recordCorner').addEventListener('change',renderRecords);$('recordSort').addEventListener('change',renderRecords);
/* Reset clears search and filters; the chosen sort order is kept. */
function resetRecords(){$('recordSearch').value='';$('recordCorner').value='all';$('recordFilter').value='all';renderRecords();$('recordSearch').focus();}
$('recordReset').addEventListener('click',resetRecords);
$('protocolButton').addEventListener('click',showProtocol);$('exportButton').addEventListener('click',exportSnapshot);$('jobButton').addEventListener('click',openJobs);
$('closeDialog').addEventListener('click',closeDialog);$('detailDialog').addEventListener('cancel',e=>{e.preventDefault();closeDialog();});$('detailDialog').addEventListener('close',restoreFocus);$('detailDialog').addEventListener('click',e=>{if(e.target===$('detailDialog')){const r=$('detailDialog').getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)closeDialog();}});

(async function init(){state.visiblePolicies=null;renderAll();const ctl=await detectMode();state.mode=ctl?'local':'static';if(ctl){applyController(ctl);}await loadSnapshot();if(ctl)pollTimer=setTimeout(pollNow,pollDelay);})();

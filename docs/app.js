'use strict';
/* Falsify Lab app. Every scientific value shown here comes from the loaded
   snapshot (DATA_CONTRACT.md, Snapshot JSON v1). Missing values render as
   explicit nulls; nothing is estimated or filled in by the browser. */
const $=id=>document.getElementById(id);
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=(name,cls='')=>`<svg class="icon ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const CORNERS=['SS','TT','FF','FS','SF'],VDDS=[1.2,1.5,1.8,2.2,2.5,2.9,3.3],TEMPS=[125,85,27,0,-40];
const POLICY_COLORS=['#276449','#a18b5f','#6a8dba','#8a77a5','#9a5a4a','#4f7f86'];
const POLICY_PURPOSE={adaptive_idw_plus_distance:'오차 활용 + 미탐색 영역 조사',random:'주 비교 대조군',space_filling:'결과와 무관한 사전 계획',idw_without_distance:'탐색 요소의 추가 가치'};
const KIND_INFO={'demo-prepare':['데모 준비','수치 사전 검증, 보정 9회, seed 1001의 공통 초기 3회를 실행하고 준비된 실행과 스냅샷을 저장합니다. 적응형 갱신은 아직 하지 않습니다.'],reproduce:['짧은 재현 검사','본 연구 캠페인과 분리된, 명시적으로 표시되는 짧은 재현 검사입니다. 사용량이 기록됩니다.']};
const JOB_STATE={queued:['대기','gray'],running:['실행 중','green'],completed:['완료','green'],incomplete:['불완전','amber'],failed:['실패','red'],cancelled:['취소','gray']};

const num=v=>typeof v==='number'&&Number.isFinite(v)?v:null;
const NULL='<span class="null">null</span>';
const fmtPs=v=>num(v)==null?'—':(v*1e12).toFixed(1);
const fmtPct=v=>num(v)==null?'—':(v*100).toFixed(1)+'%';
const fmtN=(v,d=0)=>num(v)==null?'—':v.toFixed(d);
const show=v=>v==null?NULL:typeof v==='object'?`<span class="mono">${esc(JSON.stringify(v))}</span>`:esc(v);
const pointId=(c,v,t)=>`${c}|${v.toFixed(3)}|${t.toFixed(1)}`;
function keyOf(r){if(!r||typeof r!=='object')return null;const p=r.pvt;if(p&&typeof p.corner==='string'&&num(p.vdd)!=null&&num(p.temp_c)!=null)return pointId(p.corner,p.vdd,p.temp_c);if(typeof r.point_id==='string'){const [c,v,t]=r.point_id.split('|');if(c&&Number.isFinite(+v)&&Number.isFinite(+t))return pointId(c,+v,+t);}return null;}
function fmtKey(k){if(!k)return '조건 기록 없음';const [c,v,t]=k.split('|');return `${c} · ${(+v).toFixed(1)} V · ${+t} °C`;}
const fmtTime=ts=>{const d=new Date(ts);return ts&&!isNaN(d)?d.toLocaleString('ko-KR',{hour12:false}):'—';};
const isHeld=r=>r&&(r.phase==='held_out'||r.phase==='heldout');
const PHASE_LABEL={calibration:'보정',initial:'공통 초기',search:'탐색',held_out:'보류 평가 · 사후',heldout:'보류 평가 · 사후',evaluation:'평가 전용'};
const phaseLabel=p=>p==null?'단계 null':PHASE_LABEL[p]||String(p);

const state={view:'lab',mode:null,controller:null,snap:null,raw:null,sha:null,loadError:null,m:null,stage:0,corner:'SS',point:null,playing:false,visiblePolicies:null,job:null,pollError:null,serverSha:null};

/* ---------- snapshot adapter ---------- */
function normalize(s){
  const warn=[];
  const arr=k=>{const v=s[k];if(v==null){warn.push(`${k} 필드가 없습니다`);return [];}if(!Array.isArray(v)){warn.push(`${k} 필드가 배열이 아닙니다`);return [];}return v.filter(x=>x&&typeof x==='object');};
  const obj=k=>{const v=s[k];if(v==null)return null;if(typeof v!=='object'||Array.isArray(v)){warn.push(`${k} 필드가 객체가 아닙니다`);return null;}return v;};
  const bySeq=(a,b)=>(num(a.sequence)??Infinity)-(num(b.sequence)??Infinity);
  const m={run:obj('run'),model:obj('model'),benchmark:obj('benchmark'),cost:obj('cost'),observations:arr('observations'),evaluations:arr('evaluations'),decisions:arr('decisions').slice().sort(bySeq),trace:arr('trace').slice().sort(bySeq),coverage:arr('coverage'),limitations:Array.isArray(s.limitations)?s.limitations.map(String):[],warn};
  m.obsById=new Map();m.obsByKey=new Map();m.evalById=new Map();m.covByKey=new Map();m.revealIdx=new Map();
  m.observations.forEach(o=>{if(o.result_id!=null)m.obsById.set(String(o.result_id),o);const k=keyOf(o);if(k){if(!m.obsByKey.has(k))m.obsByKey.set(k,[]);m.obsByKey.get(k).push(o);}});
  m.evaluations.forEach(e=>{const id=String(e.result_id);if(m.evalById.has(id))warn.push(`결과 ${id}의 평가가 여러 개입니다. 첫 기록을 표시합니다`);else m.evalById.set(id,e);});
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
async function retryLoad(){if(state.loading)return;state.loading=true;renderChrome();try{await loadSnapshot();}finally{state.loading=false;}renderChrome();const b=document.querySelector('[data-retry-load]');if(b)b.focus();else{$('mainContent').focus({preventScroll:true});showToast('스냅샷을 다시 불러왔습니다.');}}
async function loadSnapshot(){
  const url=state.mode==='local'?'./api/snapshot':'./data/snapshot.json';
  let raw=null,err=null;
  try{const r=await fetch(url,{cache:'no-store'});raw=await r.text();
    if(!r.ok){let code='';try{code=JSON.parse(raw).error;}catch{}err=code==='snapshot_unavailable'?{kind:'missing',message:'설정된 스냅샷 파일이 아직 없습니다.'}:{kind:'http',message:`스냅샷 요청 실패 (HTTP ${r.status})`};raw=null;}
  }catch(e){err={kind:'network',message:'스냅샷을 불러오지 못했습니다: '+e.message};}
  let snap=null;
  if(raw!=null){try{snap=JSON.parse(raw);}catch{err={kind:'parse',message:'스냅샷 JSON을 해석할 수 없습니다.'};}
    if(snap&&(typeof snap!=='object'||Array.isArray(snap))){snap=null;err={kind:'parse',message:'스냅샷이 JSON 객체가 아닙니다.'};}
    else if(snap&&snap.schema_version!==1){err={kind:'schema',message:`지원하지 않는 schema_version: ${String(snap.schema_version)}`};snap=null;}}
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
  notes.push(local?`<div class="preview-notice"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>로컬 모드</strong> · 이 컴퓨터의 저장된 실험 기록을 표시합니다. 허용된 두 작업(demo-prepare, reproduce)만 시작할 수 있습니다.</span></div>`:`<div class="preview-notice"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>기록된 실행 재생</strong> · 공개 화면은 저장된 실제 기록을 탐색·재생합니다. 원격 계산이나 새 실험 실행은 하지 않습니다.</span></div>`);
  if(s&&s.release)notes.push(`<div class="preview-notice"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>두 실제 실행의 기록</strong> · 작업대는 seed 1001의 에이전트 시연, 정책 비교는 별도 본 연구 40회입니다. 시연 비용과 본 연구 비용을 각각 표시합니다.</span></div>`);
  else if(s&&s.campaign&&['development','reproduce'].includes(s.campaign.kind))notes.push(`<div class="preview-notice"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>${s.campaign.kind==='development'?'개발 검증':'짧은 재현 검사'}</strong> · 본 연구 결과와 분리된 실제 계산 기록입니다.</span></div>`);
  if(state.loadError)notes.push(`<div class="preview-notice warn" role="alert"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>표시할 스냅샷 없음</strong> · ${esc(state.loadError.message)} 화면의 모든 과학적 값은 비어 있습니다.</span><button class="btn compact notice-action" data-retry-load ${state.loading?'disabled':''}>${state.loading?'다시 불러오는 중…':'다시 시도'}</button></div>`);
  if(s&&s.data_mode!=='real')notes.push(`<div class="preview-notice fixture" role="alert"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>실제 데이터 아님 · data_mode=${esc(s.data_mode)}</strong> · 테스트용 픽스처입니다. 연구 결과로 해석하거나 인용하지 마세요.</span></div>`);
  else if(m&&m.empty)notes.push(`<div class="preview-notice warn"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>결과가 아직 없습니다</strong> · 스냅샷에 관측·결정·비교 기록이 없습니다. 빈 화면을 예시 수치로 채우지 않습니다.</span></div>`);
  if(m&&m.warn.length)notes.push(`<div class="preview-notice warn"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>스냅샷 형식 경고</strong><ul>${m.warn.slice(0,6).map(w=>`<li>${esc(w)}</li>`).join('')}</ul></span></div>`);
  if(local&&state.pollError)notes.push(`<div class="preview-notice warn" role="alert"><svg class="icon notice-icon"><use href="#i-info"/></svg><span><strong>컨트롤러 연결 끊김</strong> · ${esc(state.pollError)} 마지막으로 받은 기록을 유지하고 다시 시도합니다.</span></div>`);
  const j=state.job;
  if(local&&j){const [lab,col]=JOB_STATE[j.state]||[j.state,'gray'];notes.push(`<div class="job-strip"><span class="tag ${col}"><span class="dot"></span>${esc(lab)}</span><span>작업 <strong>${esc(j.kind)}</strong> · ${esc(j.job_id)}</span>${j.error?`<span class="muted">${esc(j.error)}</span>`:''}<button class="btn compact" data-open-jobs>상세</button></div>`);}
  $('noticeStack').innerHTML=notes.join('');
  const run=m&&m.run;
  $('snapshotMeta').innerHTML=s?`<span>실행 <strong>${run?esc(run.run_id??'run_id null'):'실행 기록 없음'}</strong></span><span>${run?`${esc(run.policy??'policy null')} · seed ${esc(run.seed??'null')} · ${esc(run.status??'status null')}`:'run: null'}</span><span>생성 ${s.generated_at?esc(fmtTime(s.generated_at)):NULL}</span>`:`<span>스냅샷 <strong>없음</strong></span>`;
  const jobActive=j&&(j.state==='queued'||j.state==='running');
  $('modeTag').className='tag '+(local?(jobActive?'amber':'green'):'gray');
  $('modeTag').innerHTML=`<span class="dot"></span>${local?(jobActive?'로컬 · 작업 실행 중':'로컬 · 실제 기록'):'기록 재생 · 공개'}`;
  $('sideStatus').textContent=local?(jobActive?`작업 실행 중 · ${j.kind}`:'로컬 실험 기록'):'기록된 실행 재생';
  $('jobButton').hidden=!local;
  $('exportButton').disabled=!state.raw;
  $('footerMeta').textContent=`Falsify Lab · 연구 설계 v1.0${s&&s.protocol_hash?' · protocol '+String(s.protocol_hash).slice(0,12):''}${state.sha?' · snapshot sha256 '+state.sha.slice(0,12):''}`;
}
function setView(v){stopPlaying();state.view=v;document.querySelectorAll('[data-view]').forEach(b=>{b.classList.toggle('active',b.dataset.view===v);if(b.dataset.view===v)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});['lab','benchmark','records'].forEach(x=>$('view-'+x).hidden=x!==v);const titles={lab:['연구 작업대','실험 결과가 다음 질문을 바꿉니다.','회로 지연 모델의 실패 조건을 찾는, 근거로 연결된 AI 연구실.'],benchmark:['정책 비교','좋은 선택이었는지, 비교해서 확인합니다.','가설에 유리한 결과와 차이가 없는 결과를 같은 기준으로 읽습니다.'],records:['실험 기록','모든 판단에는 돌아갈 수 있는 근거가 있습니다.','조건과 결과 ID를 따라, 예측에서 관측과 다음 결정까지 확인합니다.']};$('crumbTitle').textContent=titles[v][0];$('pageTitle').textContent=titles[v][1];$('pageSubtitle').textContent=titles[v][2];if(dirty[v])renderView(v);}
/* Lazy view rendering. Only the shown view is rebuilt. A new snapshot or replay
   step marks hidden views dirty, and a dirty view is rebuilt from the current
   state before it is shown, so no view ever displays an older snapshot. */
const dirty={lab:true,benchmark:true,records:true};
function renderView(v){dirty[v]=false;if(v==='lab')renderLab();else if(v==='benchmark')renderBenchmark();else renderRecords();}

/* ---------- workbench ---------- */
function renderMap(){
  let html='<span></span>'+VDDS.map(v=>`<span class="axis">${v.toFixed(1)}</span>`).join('');
  TEMPS.forEach(t=>{html+=`<span class="axis y">${t}°</span>`;VDDS.forEach(v=>{
    const k=pointId(state.corner,v,t),c=cellAt(k);const selected=state.point===k;
    let cls=['heat-cell'],text='·',label='미관측',s=null;
    if(c.kind==='observed'||c.kind==='calibration'){cls.push('observed');label=c.kind==='calibration'?'보정 관측':'관측';text=c.err!=null?fmtPct(c.err):'평가 없음';s=c.err;}
    else if(c.kind==='held_out'){cls.push('heldout');label='보류 평가(사후)';text=c.err!=null?fmtPct(c.err):'보류';s=c.err;}
    else if(c.kind==='failed'){cls.push('failed');label='실패';text='실패';}
    if(c.candidate){cls.push('upcoming');if(s==null&&c.kind==='unobserved'){const e=num(c.candidate.idw_predicted_abs_error);cls.push('estimate');label='후보 · IDW 추정';text=e!=null?'~'+fmtPct(e):'후보';if(e!=null)s=e*.5;}}
    if(s==null&&!cls.includes('failed'))cls.push('unobserved');
    if(selected)cls.push('selected');
    const sc=s==null?0:Math.min(s*100/38,1);const style=s==null?'':`style="--cell:rgb(${Math.round(237-21*sc)},${Math.round(241-88*sc)},${Math.round(212-124*sc)})"`;
    html+=`<button class="${cls.join(' ')}" ${style} data-key="${esc(k)}" aria-pressed="${selected}" aria-label="${esc(fmtKey(k))}, ${esc(label)}${c.err!=null?', 오차 '+fmtPct(c.err):''}">${esc(text)}</button>`;});});
  const fk=$('heatmap').contains(document.activeElement)?document.activeElement.dataset.key:null;
  $('heatmap').innerHTML=html;
  if(fk){const b=$('heatmap').querySelector(`[data-key="${CSS.escape(fk)}"]`);if(b)b.focus();}
  document.querySelectorAll('[data-corner]').forEach(b=>{b.classList.toggle('active',b.dataset.corner===state.corner);b.setAttribute('aria-pressed',b.dataset.corner===state.corner);});
  const k=state.point&&state.point.startsWith(state.corner+'|')?state.point:null;
  if(!k){$('pointDetail').innerHTML=`<div><strong>${esc(state.corner)} 코너</strong><small>조건을 선택하면 저장된 관측·예측·보류 평가를 표시합니다.</small></div>`;return;}
  const c=cellAt(k),ev=c.ev,o=c.obs,cov=c.cov;
  const kindText={observed:'관측',calibration:'보정 관측',held_out:'보류 평가 · 사후',failed:'실패 기록',unobserved:'미관측'}[c.kind]||c.kind;
  const rid=o?o.result_id:cov&&c.kind!=='unobserved'?cov.result_id:null;
  const pred=ev?ev.predicted_tpd_s:cov&&c.kind!=='unobserved'?cov.predicted_tpd_s:null;
  const obsv=o?o.tpd_s:ev?ev.simulated_tpd_s:cov&&c.kind!=='unobserved'?cov.observed_tpd_s:null;
  const sub=c.kind==='unobserved'?(c.candidate?`현재 결정의 후보 · IDW 추정 오차 ${fmtPct(c.candidate.idw_predicted_abs_error)} (관측 아님)`:'이 재생 단계까지 관측 기록 없음'):`${kindText}${rid!=null?' · ':''}${rid!=null?`<button class="text-action" data-evidence="${esc(rid)}">${esc(rid)}</button>`:''} · 예측 ${fmtPs(pred)} ps · 관측 ${fmtPs(obsv)} ps`;
  $('pointDetail').innerHTML=`<div><strong>${esc(fmtKey(k))}</strong><small>${sub}</small></div><div class="value data">${c.kind==='unobserved'?'—':fmtPct(c.err)}<small>${c.kind==='unobserved'?'관측 오차 없음':c.err==null?'평가 기록 없음':'|예측−관측| / 관측'}</small></div>`;
}
function renderSteps(){
  const m=state.m,n=D();
  if(!m||(!n&&!m.observations.length)){$('steps').innerHTML='<span class="steps-empty">기록된 루프 단계가 없습니다.</span>';$('replayProgress').textContent='';return;}
  const labels=stageLabels();
  $('replayProgress').textContent=`${state.stage+1} / ${n+1}단계 · ${labels[state.stage]} · ${state.stage>=n?'기록된 모든 결과 공개':'이후 결정의 관측 결과는 아직 숨김'}`;
  const fs=$('steps').contains(document.activeElement)?document.activeElement.dataset.step:null;
  $('steps').innerHTML=labels.map((label,i)=>`${i?'<span class="step-connector" aria-hidden="true"></span>':''}<button class="step ${state.stage===i?'active':state.stage>i?'done':''}" data-step="${i}" ${state.stage===i?'aria-current="step"':''} aria-label="${i+1}단계 ${esc(label)} 보기"><i>${i+1}</i>${esc(label)}</button>`).join('');
  if(fs!=null){const b=$('steps').querySelector(`[data-step="${fs}"]`);if(b)b.focus();}
}
const stageLabels=()=>['초기 근거',...state.m.decisions.map((d,i)=>`결정 ${d.sequence??i+1} 결과`)];
function renderBudget(){
  const m=state.m,run=m&&m.run,b=run&&run.budget&&typeof run.budget==='object'?run.budget:null;
  const limit=b?num(b.limit):null;let used=b?num(b.used):null,remaining=b?num(b.remaining):null;
  const d=m&&m.decisions[state.stage];
  if(d&&limit!=null&&num(d.remaining_budget)!=null){remaining=d.remaining_budget;used=limit-remaining;}
  if(!run||limit==null){$('budgetHead').innerHTML=`<small>${run?'예산 기록 없음 (null)':'실행 기록 없음'}</small>`;$('budgetBar').innerHTML='';$('budgetLegend').innerHTML=`<span>${run?'budget.limit = null':'run = null'}</span>`;$('budgetBar').setAttribute('aria-label','예산 기록 없음');return;}
  const cal=m.observations.filter(o=>o.phase==='calibration'&&revealed(o.result_id,o.phase)).length;
  $('budgetHead').innerHTML=`${used==null?'—':used} <small>/ ${limit}회</small>`;
  $('budgetBar').innerHTML=limit<=200?Array.from({length:limit},(_,i)=>`<span class="${i<Math.min(cal,used??0)?'cal':i<(used??0)?'searched':''}"></span>`).join(''):'';
  $('budgetBar').setAttribute('aria-label',`${limit}회 중 ${used==null?'알 수 없음':used+'회'} 사용`);
  $('budgetLegend').innerHTML=`<span>보정 ${cal}회</span><span>탐색·기타 ${used==null?'—':Math.max(used-cal,0)}회</span><span>남은 ${remaining==null?'—':remaining}회</span>`;
}
function candidateRole(r){return r==='exploitation'?'오차 활용':r==='exploration'?'미탐색 조사':r==null?'역할 null':String(r);}
function renderCandidates(){
  const m=state.m,d=m&&m.decisions[state.stage];
  if(!d){$('candidateCost').textContent='';$('candidates').innerHTML=`<div class="panel-empty" style="grid-column:1/-1"><strong>${D()?'기록된 모든 결정을 재생했습니다.':'기록된 결정이 없습니다.'}</strong>${D()?'마지막 결정의 관측 결과까지 표시하고 있습니다.':'두 후보 비교와 선택은 실행 기록이 생기면 여기에 표시됩니다.'}</div>`;return;}
  const cs=Array.isArray(d.candidates)?d.candidates:[];
  const costs=[...new Set(cs.map(c=>num(c.cost_queries)).filter(x=>x!=null))];
  $('candidateCost').textContent=costs.length===1?`비용: 각각 시뮬레이션 ${costs[0]}회`:costs.length?'비용: 후보별 상이':'비용 기록 없음';
  const note=cs.length<2?`<div class="panel-empty" style="grid-column:1/-1">이 결정에는 후보가 ${cs.length}개만 기록되어 있습니다.</div>`:'';
  $('candidates').innerHTML=cs.map((c,i)=>{const sel=c.point_id!=null&&c.point_id===d.selected_point_id;return `<button class="candidate ${sel?'picked':''}" data-candidate="${i}" aria-label="${sel?'선택된':'비교'} 후보 ${esc(fmtKey(keyOf(c)))} 근거 보기"><div class="candidate-top"><span class="tag ${sel?'green':'gray'}">${sel?'선택됨':'비교 후보'}</span><small>${esc(candidateRole(c.role))}</small></div><h3>${esc(fmtKey(keyOf(c)))}</h3><p>실제 결과는 선택·관측 이후에만 공개됩니다. 아래 값은 결정 시점의 점수 구성입니다.</p><div class="candidate-meta"><span>IDW 추정 오차 <strong>${fmtPct(c.idw_predicted_abs_error)}</strong></span><span>점수 <strong class="data">${fmtN(c.score,3)}</strong></span><span>거리 <strong class="data">${fmtN(c.min_normalized_distance,3)}</strong></span></div><div class="candidate-evidence"><span>근거와 점수 구성 보기</span>${icon('arrow')}</div></button>`;}).join('')+note;
}
function actorStyle(a){const s=String(a||'').toLowerCase();if(s.includes('experiment'))return ['blue','flask'];if(s.includes('analyst'))return ['','chart'];if(s.includes('supervisor'))return ['gold','network'];return ['','file'];}
function visibleTrace(){const m=state.m;if(!m)return [];if(finalStage())return m.trace;return m.trace.filter((t,i)=>m.traceStage[i]<=state.stage);}
const traceDetail=d=>d==null?'':typeof d==='string'?esc(d):`<span class="mono">${esc(JSON.stringify(d))}</span>`;
const actorKind=a=>isOmnigent(a)?'Omnigent':String(a||'')==='deterministic runner'?'결정론적 실행기 · Omnigent 아님':'';
function traceItem(t){const [col,sym]=actorStyle(t.actor);const ids=Array.isArray(t.result_ids)?t.result_ids:[];const kind=actorKind(t.actor);return `<div class="trace-item"><span class="role-icon ${col}">${icon(sym)}</span><div class="trace-head"><strong>${esc(t.actor??'actor null')}</strong><small title="${esc(t.timestamp??'')}">${kind?esc(kind)+' · ':''}원장 #${esc(t.sequence??'?')} · ${esc(fmtTime(t.timestamp))}</small></div><p><strong>${esc(t.action??'')}</strong> ${traceDetail(t.detail)}</p>${ids.map(id=>`<button class="result-link" data-evidence="${esc(id)}">${icon('file')} ${esc(id)}</button>`).join(' ')}</div>`;}
function renderTrace(){
  const m=state.m,all=m?m.trace:[],vis=visibleTrace();
  const omni=all.filter(t=>isOmnigent(t.actor)).length;
  $('traceSubtitle').textContent=!all.length?'에이전트·실행기 기록 없음':omni?`Omnigent ${omni}개 · 결정론적 실행기 등 ${all.length-omni}개 · ${vis.length}/${all.length}개 표시`:`결정론적 실행기 기록 · Omnigent 기록 없음 · ${vis.length}/${all.length}개`;
  $('traceBody').innerHTML=!vis.length?`<div class="panel-empty" style="padding:8px 0">${all.length?'이 재생 단계까지 공개된 기록이 없습니다.':'스냅샷에 trace 기록이 없습니다. 에이전트 협업을 가정해 표시하지 않습니다.'}</div>`:vis.slice(-5).map(traceItem).join('')+(vis.length>5?`<button class="btn compact trace-more" id="traceAll">전체 기록 보기 (${vis.length})</button>`:'');
  $('decisionSummary').innerHTML=decisionSummary();
}
const changedText=(v,yes,no)=>v===true?yes:v===false?no:'선택 변경 여부 null';
const candList=cs=>Array.isArray(cs)&&cs.length?cs.map(c=>`${esc(fmtKey(keyOf(c)))}${c&&c.role!=null?' ('+esc(candidateRole(c.role))+')':''}`).join(', '):NULL;
/* The current stage shows decision i at its choice time (its rank fields compare the
   ranking without/with the outcome preceding that choice). The previous decision's
   observed result, and its optional post_result_update, only appear once revealed. */
function decisionSummary(){
  const m=state.m;if(!m||!D())return '<strong>결정 기록이 아직 없습니다.</strong><p>후보 순위와 선택 변경 여부는 실제 결정이 기록된 뒤 표시됩니다.</p>';
  const cur=m.decisions[state.stage],prev=state.stage>0?m.decisions[state.stage-1]:null,out=[];
  if(prev){const u=prev.post_result_update&&typeof prev.post_result_update==='object'&&!Array.isArray(prev.post_result_update)?prev.post_result_update:null;
    const rid=prev.observed_result_id;
    out.push(`<strong>결정 #${esc(prev.sequence??'?')}의 관측 결과 → 다음 시험</strong><p>선택 ${esc(prev.selected_point_id??'null')} · 관측 ${rid!=null?`<button class="text-action" data-evidence="${esc(rid)}">${esc(rid)}</button>`:'결과 ID 없음 (null)'}<br>${u?`결과 반영 순위 ${show(u.rank_before)} → ${show(u.rank_after)} · ${esc(changedText(u.selection_changed,'이 결과로 다음 선택이 바뀌었습니다','이 결과 이후에도 다음 선택이 같습니다'))}${u.observed_result_id!=null&&u.observed_result_id!==rid?` · 갱신 기준 결과 ${esc(u.observed_result_id)}`:''}<br>갱신된 다음 후보 ${candList(u.next_candidates)}`:'post_result_update 기록 없음 · 결과 직후 갱신은 기록되지 않았습니다.'}</p>`);}
  if(cur){out.push(`<strong>결정 #${esc(cur.sequence??'?')} · 선택 시점</strong><p>선택 ${esc(cur.selected_point_id??'null')} · 근거 ${Array.isArray(cur.evidence_result_ids)?cur.evidence_result_ids.length:0}건 · 남은 예산 ${esc(cur.remaining_budget??'null')}${cur.actor!=null?' · '+esc(cur.actor):''}<br>직전 근거 반영 전 순위 ${show(cur.rank_before)} → 반영 후 ${show(cur.rank_after)} · ${esc(changedText(cur.selection_changed,'직전 결과가 이 선택을 바꿨습니다','직전 결과 이후에도 선택이 같습니다'))}<br>이 결정의 관측 결과는 다음 재생 단계에서 공개됩니다.</p>`);}
  return out.join('');
}
function renderLab(){
  renderSteps();renderBudget();renderMap();renderCandidates();renderTrace();
  const n=D(),m=state.m,local=state.mode==='local';
  const atEnd=state.stage>=n,label=!n?(local?'실행 제어 열기':'정책 비교 보기'):atEnd?'정책 비교 보기':'다음 기록 단계';
  $('nextTitle').textContent=!m?'표시할 스냅샷이 없습니다.':!n?'기록된 발견 루프가 아직 없습니다.':atEnd?'기록된 루프를 끝까지 재생했습니다.':state.stage===0?'첫 번째 결정의 관측 결과를 볼 차례입니다.':'다음 결정의 관측 결과를 볼 차례입니다.';
  $('nextDescription').textContent=!n?(local?'데모 준비 작업을 실행하면 보정·초기 관측이 기록됩니다. 적응형 결정은 이후 에이전트 루프에서 기록됩니다.':'공개 화면은 저장된 기록만 재생합니다.'):atEnd?'다음 단계: 정책 비교에서 같은 예산의 선택 방식별 결과, 주 판정과 한계를 확인하세요.':'기록된 순서대로 한 단계씩 재생합니다. 실시간 계산이 아닙니다.';
  $('advanceButton').innerHTML=`${label}${icon('arrow')}`;$('topAdvanceButton').textContent=label;
  $('topAdvanceButton').disabled=$('advanceButton').disabled=false;
  $('playButton').disabled=$('resetButton').disabled=n===0;$('finalButton').disabled=n===0||atEnd;
}
function advance(){stopPlaying();const n=D();if(!n){state.mode==='local'?openJobs():setView('benchmark');return;}if(state.stage>=n)setView('benchmark');else setStage(state.stage+1);}
function setStage(i){state.stage=Math.max(0,Math.min(i,D()));const m=state.m,d=m&&m.decisions[state.stage],prev=m&&m.decisions[state.stage-1];
  let k=null;if(d&&Array.isArray(d.candidates)){const c=d.candidates.find(x=>x.point_id===d.selected_point_id)||d.candidates[0];k=keyOf(c);}else if(prev){const o=m.obsById.get(String(prev.observed_result_id));k=keyOf(o);}
  if(k){state.point=k;state.corner=k.split('|')[0];}refreshLab();}
function refreshLab(){if(state.view==='lab')renderView('lab');else dirty.lab=true;}
let playTimer;function stopPlaying(){clearInterval(playTimer);state.playing=false;$('playButton').innerHTML=`${icon('play')}<span>기록 재생</span>`;}
function play(){if(state.playing){stopPlaying();return;}if(!D())return;setStage(0);state.playing=true;$('playButton').innerHTML=`${icon('close')}<span>재생 멈춤</span>`;playTimer=setInterval(()=>{if(state.stage>=D()){stopPlaying();return;}setStage(state.stage+1);if(state.stage>=D())stopPlaying();},2100);}

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
  svg+='<text x="48" y="18">평균 누적 명확 반례</text><text x="608" y="338" text-anchor="end">탐색 시뮬레이션 수</text>';
  const frags=pol.map((p,i)=>{let f='';const col=POLICY_COLORS[i%POLICY_COLORS.length],c=curves[i];
    c.lines.forEach(l=>{const pts=l.arr.map((v,j)=>num(v)==null?null:[x(j+1),y(v)]).filter(Boolean);if(pts.length>1)f+=`<path class="run-line${l.complete?'':' incomplete'}" stroke="${col}" ${l.complete?'':'stroke-dasharray="2 3"'} d="${pts.map(([px,py],j)=>`${j?'L':'M'}${px.toFixed(1)},${py.toFixed(1)}`).join(' ')}"/>`;});
    const pts=(c.mean||[]).map((v,j)=>v==null?null:[x(j+1),y(v)]).filter(Boolean);
    if(pts.length){f+=`<path class="curve" stroke="${col}" ${i===3?'stroke-dasharray="5 4"':''} d="${pts.map(([px,py],j)=>`${j?'L':'M'}${px.toFixed(1)},${py.toFixed(1)}`).join(' ')}"/>`;const [lx,ly]=pts[pts.length-1];f+=`<circle cx="${lx}" cy="${ly}" r="3.5" fill="${col}"/>`;}
    return f;});
  return m.benchView={pol,curves,grid:svg,frags};}
function renderBenchChart(){const {pol,grid,frags}=benchModel();$('benchmarkChart').innerHTML=grid+pol.map((p,i)=>state.visiblePolicies.has(String(p.id))?frags[i]:'').join('');}
function renderBenchmark(){
  const b=state.m&&state.m.benchmark;const pol=b&&Array.isArray(b.policies)?b.policies.filter(p=>p&&typeof p==='object'):[];
  if(!b){$('chartSubtitle').textContent='정책 비교 결과 없음';$('benchmarkChart').innerHTML='';$('benchmarkChart').setAttribute('aria-label','정책 비교 데이터 없음');$('chartLegend').innerHTML='';$('chartDescription').textContent='benchmark = null · 본 연구 캠페인(4개 방식 × 10개 시작점)의 결과가 스냅샷에 없습니다.';
    $('benchmarkConclusion').innerHTML='<span class="tag gray">결과 없음</span><h2>정책 비교 결과가<br>아직 없습니다.</h2><p>본 연구 실행 전에는 비교 결과를 표시하지 않습니다. 예시 곡선이나 구간을 대신 그리지 않습니다.</p><div class="conclusion-note">주 성공 기준: 무작위 대비 평균 +2개 이상, paired bootstrap 95% 구간 하한 &gt; 0, 완전한 seed 쌍 10개.</div>';
    $('benchmarkTable').innerHTML='<tr><td colspan="5" class="muted">기록된 정책 결과가 없습니다.</td></tr>';$('benchmarkTableNote').textContent='정량 비교는 선택 정책의 효과를, 실시간 시연은 Omnigent 협업을 보여줍니다. 둘을 구분해 보고합니다.';$('benchmarkStatusTag').textContent='결과 없음';
    renderBenchDetails(null);return;}
  $('benchmarkStatusTag').textContent='상태 '+String(b.status??'null');
  const {curves}=benchModel();
  renderBenchChart();$('benchmarkChart').setAttribute('aria-label','코어가 내보낸 완료 실행 평균(mean_cumulative_clear)과 개별 실행의 cumulative_clear 곡선');
  $('chartSubtitle').textContent=`굵은 선은 완료 실행만의 평균(mean_cumulative_clear) · 완료 실행 수 ${curves.map(c=>c.completeRuns).join(' / ')} · 명확한 반례는 오차 11% 초과`;
  const noMean=pol.filter((p,i)=>!curves[i].mean).map(p=>p.label??p.id),nInc=curves.reduce((s,c)=>s+c.incomplete.length,0);
  $('chartDescription').textContent=`얇은 실선은 완료 실행, 점선은 미완료 실행(${nInc}개, 평균에서 제외)입니다.${noMean.length?` 평균 없음(null): ${noMean.join(', ')}.`:''} 범례를 눌러 곡선을 비교하세요.`;
  $('chartLegend').innerHTML=pol.map((p,i)=>`<button class="legend-btn" data-policy="${esc(p.id)}" aria-pressed="${state.visiblePolicies.has(String(p.id))}"><span class="line-swatch" style="--swatch:${POLICY_COLORS[i%POLICY_COLORS.length]}"></span>${esc(p.label??p.id)}${curves[i].mean?'':' · 평균 없음'}</button>`).join('');
  const pr=b.primary&&typeof b.primary==='object'?b.primary:{};const ci=Array.isArray(pr.ci95)&&pr.ci95.length===2&&pr.ci95.every(v=>num(v)!=null)?pr.ci95:null;
  const tag=pr.success===true?['green','주 성공 기준 충족']:pr.success===false?['amber','주 성공 기준 미충족']:['gray','주 판정 불가 (null)'];
  const head=pr.success===true?'같은 예산으로<br>반례를 더 발견했습니다.':pr.success===false?'우월성이 확인되지<br>않았습니다.':'주 성공 판정을<br>수행할 수 없습니다.';
  const md=num(pr.mean_difference);
  $('benchmarkConclusion').innerHTML=`<span class="tag ${tag[0]}">${tag[1]}</span><h2>${head}</h2><p>${pr.reason!=null?esc(pr.reason):'판정 이유가 기록되지 않았습니다 (reason = null).'}</p><div class="conclusion-metric"><span>완전한 seed 쌍</span><strong>${pr.complete_pairs??'—'}</strong></div><div class="conclusion-metric"><span>적응형 − 무작위 평균 차이</span><strong>${md==null?'—':(md>0?'+':'')+md.toFixed(2)+'개'}</strong></div><div class="conclusion-metric"><span>paired bootstrap 95% 구간</span><strong style="font-size:14px">${ci?`${ci[0].toFixed(2)} ~ ${ci[1].toFixed(2)}`:'null · 계산되지 않음'}</strong></div><div class="conclusion-note">값은 코어가 기록한 primary 결과입니다. 화면은 구간이나 평균을 다시 계산하지 않습니다.${pr.comparison!=null?`<br>비교: ${compLabel(pr.comparison,policyLabel(pol))}`:''}</div>${Array.isArray(pr.paired_differences)&&pr.paired_differences.length?disclosure(`seed별 차이 · ${pr.paired_differences.length}쌍`,pairChips(pr.paired_differences)):''}${b.primary!=null?rawJson('primary 원본 필드',b.primary):''}`;
  const ri=pol.findIndex(p=>p.id==='random');const rm=ri<0?null:curves[ri].meanFinal;
  $('benchmarkTable').innerHTML=pol.length?pol.map((p,i)=>{const c=curves[i],mean=c.meanFinal;const diff=p.id==='random'?'기준':mean!=null&&rm!=null?((mean-rm>0?'+':'')+(mean-rm).toFixed(2)+'개'):'—';const inc=c.incomplete.map(r=>`seed ${r.seed??'null'}: ${r.status??'null'}`);return `<tr><td><span class="policy-name"><span class="dot" style="color:${POLICY_COLORS[i%POLICY_COLORS.length]}"></span>${esc(p.label??p.id)}</span></td><td>${mean==null?'— <span class="muted">평균 없음 (null)</span>':mean.toFixed(2)}</td><td>${diff}</td><td>${esc(c.completeRuns)}/${c.runs.length} 완료${inc.length?`<br><span class="muted">미완료 · ${esc(inc.join(', '))}</span>`:''}</td><td class="muted">${esc(POLICY_PURPOSE[p.id]||'')}</td></tr>`;}).join(''):'<tr><td colspan="5" class="muted">policies 배열이 비어 있습니다.</td></tr>';
  $('benchmarkTableNote').textContent='평균은 코어가 완료 실행만으로 내보낸 mean_cumulative_clear의 마지막 값입니다. 미완료 실행은 평균과 완료 수에서 제외되며 성공값으로 대체하지 않습니다. 평균이 null이면 —로 표시합니다. 무작위 대비 차이는 두 내보낸 평균의 단순 차이이며, 주 판정은 seed 쌍 기반 primary 결과만 사용합니다.';
  renderBenchDetails(b);
}
function kvList(o){if(o==null)return `<p>${NULL}</p>`;if(typeof o!=='object')return `<p>${esc(o)}</p>`;if(Array.isArray(o))return o.length?`<ul>${o.map(x=>`<li>${show(x)}</li>`).join('')}</ul>`:'<p class="muted">빈 배열</p>';const ks=Object.keys(o);return ks.length?`<dl class="kv">${ks.map(k=>`<dt>${esc(k)}</dt><dd>${show(o[k])}</dd>`).join('')}</dl>`:'<p class="muted">빈 객체</p>';}
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
const vVal=v=>v==null?NULL:typeof v==='object'?'<span class="muted">원본 필드 참고</span>':typeof v==='number'?`<span class="data">${esc(v)}</span>`:esc(v);
const vPct=v=>num(v)==null?vVal(v):raw(v,fmtPct(v));
const vNum=v=>num(v)==null?vVal(v):raw(v,v.toFixed(4));
const vSec=v=>num(v)==null?vVal(v):raw(v,v.toFixed(1)+'초');
const vDiff=v=>num(v)==null?vVal(v):raw(v,(v>0?'+':'')+v.toFixed(2)+'개');
const vCi=v=>v==null?'null · 계산되지 않음':Array.isArray(v)&&v.length===2&&v.every(x=>num(x)!=null)?raw(JSON.stringify(v),`${v[0].toFixed(2)} ~ ${v[1].toFixed(2)}`):`<span class="mono">${esc(JSON.stringify(v))}</span>`;
const vCount=v=>Array.isArray(v)?`<span class="data">${v.length}개</span>`:vVal(v);
function fields(o,spec){const rows=spec.filter(([k])=>has(o,k)).map(([k,l,f])=>`<dt${keyTitle(k)}>${esc(l)}</dt><dd>${(f||vVal)(o[k])}</dd>`);return rows.length?`<dl class="kv">${rows.join('')}</dl>`:'';}
const policyLabel=pol=>id=>{const p=pol.find(x=>String(x.id)===String(id));return p&&p.label!=null?String(p.label):String(id);};
function compLabel(c,pl){const mm=typeof c==='string'&&c.match(/^(.+) minus (.+)$/);return mm?`<span${keyTitle(c)}>${esc(pl(mm[1]))} − ${esc(pl(mm[2]))}</span>`:esc(c);}
function pairChips(a){return `<div class="paired">${a.map(x=>{const v=x&&typeof x==='object'?num(x.difference):num(x);const sd=x&&typeof x==='object'&&x.seed!=null?`seed ${esc(x.seed)}: `:'';return `<span>${sd}${v==null?'null':(v>0?'+':'')+v}</span>`;}).join('')}</div>`;}
const ERR_SPEC=[['points','평가 지점'],['clear_counterexamples','명확한 반례 (>11%)'],['secondary_counterexamples','보조 반례 (>10%)'],['mean_abs_relative_error','평균 절대 상대오차',vPct],['max_abs_relative_error','최대 절대 상대오차',vPct]];
const COST_SPEC=[['campaign_kind','캠페인 종류'],['logical_queries','논리 질의'],['attempts','시뮬레이션 시도'],['successes','성공'],['failures','실패'],['unfinished_attempts','미완료 시도'],['cache_hits','캐시 적중'],['restricted_cache_hits','제한 캐시 적중'],['simulation_seconds','시뮬레이션 시간',vSec],['policy_seconds','정책 계산 시간',vSec],['wall_seconds','전체 경과 시간',vSec],['posthoc_attempts_separate_from_research_budget','연구 예산과 별도인 사후 평가 시도']];
const PURPOSE_LABEL={preflight:'수치 사전 검증',calibration:'보정',benchmark:'본 연구 탐색',posthoc:'사후 평가',postflight:'사후 검증',live:'작업대 시연',attempts_max:'시도 상한',wall_seconds_max:'경과 시간 상한 (초)'};
const PART_LABEL={training:'보정 지점',search_pool:'탐색 후보',held_out:'보류 지점',prior_excluded:'이전 검증 제외',low_vdd:'저전압 구간',high_vdd:'고전압 구간'};
const mapList=(o,labels)=>`<dl class="kv">${Object.keys(o).map(k=>`<dt${keyTitle(k)}>${esc(labels[k]||k)}</dt><dd>${vVal(o[k])}</dd>`).join('')}</dl>`;
function statTable(rows){return `<div class="table-wrap" tabindex="0" role="region" aria-label="구간별 오차 표"><table class="results-table mini-table"><thead><tr><th scope="col">구간</th><th scope="col">지점</th><th scope="col">명확 반례</th><th scope="col">보조 반례</th><th scope="col">평균 오차</th><th scope="col">최대 오차</th></tr></thead><tbody>${rows.map(([k,o])=>`<tr><th scope="row"${keyTitle(k)}>${esc(PART_LABEL[k]||k)}</th><td>${vVal(o.points)}</td><td>${vVal(o.clear_counterexamples)}</td><td>${vVal(o.secondary_counterexamples)}</td><td>${vPct(o.mean_abs_relative_error)}</td><td>${vPct(o.max_abs_relative_error)}</td></tr>`).join('')}</tbody></table></div>`;}
const card=(title,body,cls='')=>`<section class="detail-card${cls}"><h3>${title}</h3>${body}</section>`;
function heldOutCard(h,pl){if(!isObj(h))return card('보류 평가',h==null?`<p>${NULL}</p>`:rawJson('held_out 원본 필드',h));
  let body='';
  if(isObj(h.model_errors))body+=`<p class="card-lead">고정 모델의 보류 지점 오차</p>${fields(h.model_errors,ERR_SPEC)}`;else body+=fields(h,[['model_errors','고정 모델 오차']]);
  body+=fields(h,[['missing_points','누락 지점',vCount]]);
  const bp=h.idw_error_map_mae_by_policy;
  if(isObj(bp))body+=`<p class="card-lead">정책별 IDW 오차 지도 MAE</p><dl class="kv">${Object.keys(bp).map(k=>`<dt${keyTitle(k)}>${esc(pl(k))}</dt><dd>${vNum(bp[k])}</dd>`).join('')}</dl>`;else body+=fields(h,[['idw_error_map_mae_by_policy','정책별 IDW 오차 지도 MAE']]);
  if(h.note!=null)body+=`<p class="card-note">${esc(h.note)}</p>`;
  const runs=h.idw_error_map_mae;
  if(Array.isArray(runs)&&runs.length)body+=disclosure(`실행별 IDW 오차 지도 MAE · ${runs.length}개`,`<div class="table-wrap" tabindex="0" role="region" aria-label="실행별 IDW 오차 지도 MAE 표"><table class="results-table mini-table"><thead><tr><th scope="col">정책</th><th scope="col">seed</th><th scope="col">상태</th><th scope="col">MAE</th></tr></thead><tbody>${runs.map(r=>isObj(r)?`<tr><td>${esc(pl(r.policy))}</td><td>${vVal(r.seed)}</td><td>${vVal(r.status)}</td><td>${vNum(r.idw_map_mae)}</td></tr>`:`<tr><td colspan="4">${vVal(r)}</td></tr>`).join('')}</tbody></table></div>`);
  return card('보류 평가',body+rawJson('held_out 원본 필드',h));}
function referenceCard(r){if(!isObj(r))return card('전수 참조',r==null?`<p>${NULL}</p>`:rawJson('reference 원본 필드',r));
  let body=fields(r,[['points_evaluated','전수 평가 지점'],['missing_points','누락 지점',vCount]]);
  if(r.h2_note!=null)body+=`<p class="card-note">${esc(r.h2_note)}</p>`;
  const rows=[...(isObj(r.by_partition)?Object.entries(r.by_partition):[]),...['low_vdd','high_vdd'].filter(k=>has(r,k)).map(k=>[k,r[k]])].filter(([,o])=>isObj(o));
  if(rows.length)body+=disclosure(`분할·전압 구간별 오차 분해 · ${rows.length}개 구간`,statTable(rows));
  return card('전수 참조',body+rawJson('reference 원본 필드',r));}
function costCard(title,c,key){if(!isObj(c))return card(title,c==null?`<p>${NULL}</p><p class="card-note">비용 기록 없음 (null)</p>`:rawJson(key+' 원본 필드',c));
  let body=fields(c,COST_SPEC);const sub=['attempts_by_purpose','caps'].filter(k=>isObj(c[k]));
  if(sub.length)body+=disclosure('목적별 시도 · 상한',sub.map(k=>`<p class="card-lead"${keyTitle(k)}>${k==='caps'?'상한':'목적별 시도'}</p>${mapList(c[k],PURPOSE_LABEL)}`).join(''));
  return card(title,body+rawJson(key+' 원본 필드',c));}
function secondaryCard(sc,pl){if(!Array.isArray(sc))return card('보조 비교',sc==null?`<p>${NULL}</p>`:rawJson('secondary_comparisons 원본 필드',sc));
  const body=sc.length?sc.map(c=>{if(!isObj(c))return `<div class="comparison">${vVal(c)}</div>`;
    const pairs=Array.isArray(c.paired_differences)?c.paired_differences:null;
    const extra=(pairs&&pairs.length?pairChips(pairs):'')+fields(c,[['excluded_pairs','제외된 쌍',vCount]])+(isObj(c.bootstrap)?`<p class="card-lead">bootstrap</p>${mapList(c.bootstrap,{})}`:'');
    return `<div class="comparison"><strong>${has(c,'comparison')?compLabel(c.comparison,pl):'comparison 없음'}</strong>${fields(c,[['complete_pairs','완전한 seed 쌍'],['mean_difference','평균 차이',vDiff],['ci95','paired bootstrap 95% 구간',vCi],['reason','판정 이유']])}${disclosure(`seed별 차이${pairs?' · '+pairs.length+'쌍':''}`,extra||'<p class="muted">기록 없음</p>')}</div>`;}).join(''):'<p class="muted">빈 배열</p>';
  return card('보조 비교',body+rawJson('secondary_comparisons 원본 필드',sc));}
function limitsCard(snapL,benchL){const list=a=>a.length?`<ul class="limit-list">${a.map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<p class="muted">기록 없음 (빈 배열)</p>';
  return card('한계',`<p class="card-lead" title="limitations">스냅샷 한계 · ${snapL.length}개</p>${list(snapL)}${benchL?`<p class="card-lead" title="benchmark.limitations">본 연구 한계 · ${benchL.length}개</p>${list(benchL)}`:''}`,' wide');}
function renderBenchDetails(b){const m=state.m,pol=b&&Array.isArray(b.policies)?b.policies.filter(p=>p&&typeof p==='object'):[],pl=policyLabel(pol);
  $('benchmarkDetails').innerHTML=[heldOutCard(b?b.held_out:null,pl),referenceCard(b?b.reference:null),secondaryCard(b?b.secondary_comparisons:null,pl),costCard('본 연구 비용',b?b.cost:null,'benchmark.cost'),costCard(state.snap&&state.snap.release?'작업대 시연 비용':'스냅샷 실행 비용',m?m.cost:null,'cost'),limitsCard(m?m.limitations:[],b&&Array.isArray(b.limitations)?b.limitations.map(String):null)].join('');}

/* ---------- records ---------- */
function recordRows(){const m=state.m;if(!m)return [];return m.recordRows||(m.recordRows=buildRecordRows(m));}
function buildRecordRows(m){const rows=m.observations.map(o=>({id:o.result_id,key:keyOf(o),phase:o.phase,obs:o,ev:m.evalById.get(String(o.result_id))||null}));
  m.evaluations.forEach(e=>{if(!m.obsById.has(String(e.result_id)))rows.push({id:e.result_id,key:keyOf(e),phase:isHeld(m.covByKey.get(keyOf(e))&&{phase:m.covByKey.get(keyOf(e)).state==='held_out'?'held_out':''})?'held_out':'evaluation',obs:null,ev:e});});return rows;}
function verdict(ev){if(!ev)return ['gray','평가 없음'];if(ev.clear_counterexample===true)return ['amber','명확한 반례'];if(ev.secondary_counterexample===true)return ['gray','보조 반례 (>10%)'];if(ev.clear_counterexample===false&&ev.secondary_counterexample===false)return ['green','허용 범위'];return ['gray','판정 null'];}
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
  $('recordCount').textContent=`${list.length} / ${all.length}개 기록`;
  $('recordsTable').innerHTML=list.map(r=>{const [c,l]=verdict(r.ev);const obs=r.obs?r.obs.tpd_s:r.ev?r.ev.simulated_tpd_s:null;return `<tr><td class="mono">${esc(r.id)}</td><td>${esc(fmtKey(r.key))}</td><td><span class="phase" title="phase = ${esc(r.phase??'null')}">${esc(phaseLabel(r.phase))}</span></td><td>${fmtPs(r.ev?r.ev.predicted_tpd_s:null)}</td><td>${fmtPs(obs)}</td><td>${fmtPct(r.ev?r.ev.abs_relative_error:null)}</td><td><span class="tag ${c}">${esc(l)}</span></td><td><button class="text-action" data-evidence="${esc(r.id)}">상세 보기</button></td></tr>`;}).join('');
  const active=[q!==''?`검색어 “${esc($('recordSearch').value.trim())}”`:'',corner!=='all'?`코너 ${esc(corner)}`:'',f!=='all'?`판정 “${esc($('recordFilter').selectedOptions[0].textContent)}”`:''].filter(Boolean);
  $('emptyRecords').hidden=list.length!==0;$('emptyRecords').innerHTML=all.length?`<p>${active.join(', ')} 조건에 맞는 기록이 없습니다.</p><button class="btn compact" type="button" data-reset-records>필터 초기화하고 ${all.length}개 기록 모두 보기</button>`:'<p>스냅샷에 관측·평가 기록이 아직 없습니다.</p>';
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
  if(!o&&!e){openDialog('근거 상세 · '+id,`<span class="tag amber">기록 없음</span><p style="margin-top:13px">결과 ID ${esc(id)}에 해당하는 관측·평가가 스냅샷에 없습니다.</p>`);return;}
  const k=keyOf(o||e),pv=o&&o.provenance&&typeof o.provenance==='object'?o.provenance:null;
  const decs=m.decisions.filter(d=>d.observed_result_id===id||(Array.isArray(d.evidence_result_ids)&&d.evidence_result_ids.includes(id)));
  const trs=m.trace.filter(t=>Array.isArray(t.result_ids)&&t.result_ids.includes(id));
  const [vc,vl]=verdict(e);const isMode=state.snap.data_mode==='real';
  openDialog('근거 상세 · '+id,`<span class="tag ${isMode?vc:'red'}">${isMode?esc(vl):'픽스처 · 실제 측정 아님'}</span> <span class="tag gray">${esc(o?o.phase??'phase null':'평가 전용 기록')}</span><p style="margin-top:13px">${esc(fmtKey(k))} · 고정 모델의 예측과 시뮬레이션 관측을 구분해 표시합니다.</p>
  <div class="evidence-grid"><div class="evidence-field"><small>고정 모델 예측 (predicted_tpd_s)</small><strong>${fmtPs(e?e.predicted_tpd_s:null)} ps</strong></div><div class="evidence-field"><small>ngspice 관측 (${o?'tpd_s':'simulated_tpd_s'})</small><strong>${fmtPs(o?o.tpd_s:e?e.simulated_tpd_s:null)} ps</strong></div></div>
  <h3>평가 ${e?fmtPct(e.abs_relative_error):'없음'}</h3><dl class="kv"><dt>relative_error</dt><dd>${show(e?e.relative_error:null)}</dd><dt>clear (&gt;0.11)</dt><dd>${show(e?e.clear_counterexample:null)}</dd><dt>secondary (&gt;0.10)</dt><dd>${show(e?e.secondary_counterexample:null)}</dd><dt>model_hash</dt><dd class="mono">${show(e?e.model_hash:null)}</dd></dl>
  <h3>관측과 출처</h3><dl class="kv"><dt>tphl / tplh</dt><dd>${fmtPs(o?o.tphl_s:null)} / ${fmtPs(o?o.tplh_s:null)} ps</dd><dt>cache_hit</dt><dd>${show(o?o.cache_hit:null)}</dd><dt>wall_time_s</dt><dd>${show(o?o.wall_time_s:null)}</dd><dt>netlist_sha256</dt><dd class="mono">${show(pv?pv.netlist_sha256:null)}</dd><dt>simulator</dt><dd>${show(pv?pv.simulator:null)}</dd></dl>
  <h3>원시 측정 줄</h3><pre class="json-block" id="rawMeas"></pre>
  <h3>연결된 결정 ${decs.length}건 · trace ${trs.length}건</h3>${decs.length?`<ul>${decs.map(d=>`<li>결정 #${esc(d.sequence??'?')} · ${d.observed_result_id===id?'이 결정의 관측 결과':'근거로 사용'} · 선택 ${esc(d.selected_point_id??'null')}</li>`).join('')}</ul>`:'<p>연결된 결정이 없습니다.</p>'}
  <h3>원본 기록</h3><pre class="json-block" id="rawRecord"></pre>`);
  const rm=pv&&pv.raw_meas_lines;$('rawMeas').textContent=rm==null?'null':Array.isArray(rm)?rm.join('\n'):String(rm);
  $('rawRecord').textContent=JSON.stringify({observation:o,evaluation:e},null,2);}
function showCandidate(i){const m=state.m,d=m&&m.decisions[state.stage];const c=d&&Array.isArray(d.candidates)?d.candidates[i]:null;if(!c)return;const sel=c.point_id===d.selected_point_id;const di=m.decisions.indexOf(d);
  openDialog('후보 검토 · '+fmtKey(keyOf(c)),`<span class="tag ${sel?'green':'gray'}">${sel?'선택된 후보':'비교 후보'} · ${esc(candidateRole(c.role))}</span><p style="margin-top:13px">결정 #${esc(d.sequence??'?')}에서 기록된 후보입니다. 점수는 예상 학습량을 대신하는 휴리스틱이며, 확률이나 교정된 불확실성을 뜻하지 않습니다.</p><div class="evidence-grid"><div class="evidence-field"><small>IDW 추정 오차 (관측 아님)</small><strong>${fmtPct(c.idw_predicted_abs_error)}</strong></div><div class="evidence-field"><small>선택 점수</small><strong>${fmtN(c.score,3)}</strong></div></div><dl class="kv"><dt>point_id</dt><dd class="mono">${show(c.point_id)}</dd><dt>min_normalized_distance</dt><dd>${show(c.min_normalized_distance)}</dd><dt>cost_queries</dt><dd>${show(c.cost_queries)}</dd><dt>근거 결과 ID</dt><dd class="mono">${show(d.evidence_result_ids)}</dd><dt>남은 예산</dt><dd>${show(d.remaining_budget)}</dd></dl><h3>결과 이후</h3><p>${sel?(d.observed_result_id!=null?(state.stage>di?`관측 결과 <button class="text-action" data-evidence="${esc(d.observed_result_id)}">${esc(d.observed_result_id)}</button>가 기록되었습니다.`:'관측 결과는 다음 재생 단계에서 공개됩니다.'):'관측 결과 ID가 기록되지 않았습니다 (null).'):'선택되지 않은 후보의 실제 결과는 알 수 없으며 표시하지 않습니다.'}</p><pre class="json-block" id="rawCand"></pre>`);$('rawCand').textContent=JSON.stringify(c,null,2);}
function showTraceAll(){openDialog('근거 이동 기록 전체',visibleTrace().map(traceItem).join('')||'<p>기록 없음</p>');}
function showProtocol(){const s=state.snap,m=state.m,model=m&&m.model;openDialog('고정한 연구 설계 · v1.0',`<span class="tag green">설계 확정 · 기록은 스냅샷에서 읽음</span><h3>연구 질문</h3><p>적은 데이터로 보정한 회로 지연 모델의 실패 조건을 적응형 선택으로 더 효율적으로 찾을 수 있는가?</p><h3>동일한 비교 조건</h3><ul><li>공정 5종 × 전압 7종 × 온도 5종 = 175개 조건</li><li>보정 9점, 이전 연결 검증 지점 1점 제외, 보류 40점, 탐색 후보 125점</li><li>4가지 선택 방식 × 10개 시작점, 각각 보정 9회 + 탐색 15회</li><li>허용 오차 10%, 주 지표의 명확한 반례는 11% 초과</li><li>주 성공 기준: 무작위 대비 평균 +2개 이상, 재표집 구간 하한 > 0</li></ul><h3>현재 스냅샷</h3><dl class="kv"><dt>data_mode</dt><dd>${show(s?s.data_mode:null)}</dd><dt>generated_at</dt><dd>${show(s?s.generated_at:null)}</dd><dt>protocol_hash</dt><dd class="mono">${show(s?s.protocol_hash:null)}</dd><dt>snapshot sha256</dt><dd class="mono">${show(state.sha)}</dd><dt>model_hash</dt><dd class="mono">${show(model?model.model_hash:null)}</dd><dt>model form</dt><dd>${show(model?model.form:null)}</dd><dt>coefficients</dt><dd>${show(model?model.coefficients:null)}</dd><dt>calibration ids</dt><dd>${show(model?model.calibration_result_ids:null)}</dd></dl><h3>해석과 한계</h3><p>현재 일반적인 Level 1 소자 모델에서의 계산 실험입니다. 반복은 시작점 변동을 측정하며, 독립된 실제 소자 실험을 뜻하지 않습니다. 정량 비교는 선택 정책의 효과를, 실시간 시연은 Omnigent 협업을 보여줍니다.</p>${kvList(m?m.limitations:[])}<h3>이 화면에서 할 수 있는 것</h3><p>${state.mode==='local'?'저장된 기록 탐색·재생·내보내기, 그리고 고정된 두 작업(demo-prepare, reproduce)의 시작과 취소. 본 연구 캠페인은 이 화면에서 시작할 수 없습니다.':'저장된 실제 기록의 탐색·재생·JSON 내보내기. 공개 화면은 시뮬레이션을 실행하거나 연구 상태를 바꾸지 않습니다.'}</p>`);}
function exportSnapshot(){if(!state.raw){showToast('내보낼 스냅샷이 없습니다.');return;}const blob=new Blob([state.raw],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='falsify-lab-snapshot-'+String(state.snap.generated_at||'no-timestamp').replace(/[^0-9A-Za-z]/g,'')+'.json';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);showToast(`화면과 같은 스냅샷을 그대로 내보냈습니다${state.sha?' · sha256 '+state.sha.slice(0,12):''}.`);}

/* ---------- local job control ---------- */
function jobPanel(){const c=state.controller||{},j=state.job,active=j&&(j.state==='queued'||j.state==='running');
  const why=!c.jobs_enabled?'이 서버는 --read-only로 시작되어 작업을 시작할 수 없습니다.':!c.core_available?'코어 명령 falsify_lab.cli가 이 체크아웃에 없습니다. 작업을 시작해도 결과가 생기지 않으므로 비활성화했습니다.':active?'다른 작업이 실행 중입니다. 한 번에 하나만 실행합니다.':'';
  const kinds=(c.kinds||[]).map(k=>`<div class="job-kind"><h3>${esc((KIND_INFO[k]||[k])[0])} <span class="mono muted">${esc(k)}</span></h3><p class="muted">${esc((KIND_INFO[k]||['',''])[1])}</p><button class="btn primary compact" data-start-job="${esc(k)}" ${why?'disabled':''}>시작</button></div>`).join('');
  let cur='<p>아직 실행한 작업이 없습니다.</p>';
  if(j){const [lab,col]=JOB_STATE[j.state]||[j.state,'gray'];cur=`<p><span class="tag ${col}"><span class="dot"></span>${esc(lab)}</span> <span class="mono">${esc(j.kind)} · ${esc(j.job_id)}</span></p><dl class="kv"><dt>생성</dt><dd>${esc(fmtTime(j.created_at))}</dd><dt>시작</dt><dd>${esc(fmtTime(j.started_at))}</dd><dt>종료</dt><dd>${esc(fmtTime(j.finished_at))}</dd><dt>returncode</dt><dd>${show(j.returncode)}</dd><dt>종료 이유</dt><dd>${show(j.termination_reason)}</dd><dt>오류</dt><dd>${show(j.error)}</dd><dt>명령</dt><dd class="mono">${show((j.argv||[]).join(' '))}</dd><dt>로그 위치</dt><dd class="mono">${show(j.log_dir)}</dd></dl>${active?`<button class="btn compact" id="cancelJob">${icon('close')}작업 취소</button>`:''}<h3>CLI 요약 (stdout JSON)</h3><pre class="json-block" id="jobSummary"></pre><h3>stderr 끝부분</h3><pre class="json-block" id="jobStderr"></pre>`;}
  return `<p>허용된 두 작업만 시작할 수 있습니다. 명령과 경로는 서버에 고정되어 있으며, 본 연구 캠페인은 여기서 시작할 수 없습니다.</p>${why?`<p class="muted"><strong>${esc(why)}</strong></p>`:''}<div class="job-kinds">${kinds}</div><h3>현재 · 마지막 작업</h3>${cur}`;}
function openJobs(){openDialog('로컬 실행 제어',jobPanel());fillJobPre();state.jobsOpen=true;}
function fillJobPre(){const j=state.job;if(!j||!$('jobSummary'))return;$('jobSummary').textContent=j.summary==null?'null':JSON.stringify(j.summary,null,2);$('jobStderr').textContent=j.stderr_tail||'(비어 있음)';}
async function postJSON(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),cache:'no-store'});let j={};try{j=await r.json();}catch{}return {ok:r.ok,status:r.status,body:j};}
const ERR_TEXT={job_active:'이미 실행 중인 작업이 있습니다.',core_unavailable:'코어 명령을 사용할 수 없어 작업을 시작하지 않았습니다.',jobs_disabled:'읽기 전용 서버입니다.',invalid_kind:'허용되지 않은 작업 종류입니다.',origin_rejected:'요청 출처가 거부되었습니다.',no_active_job:'취소할 작업이 없습니다.'};
async function startJob(kind){try{const r=await postJSON('./api/jobs',{kind});showToast(r.ok?`${kind} 작업을 시작했습니다.`:(ERR_TEXT[r.body.error]||`작업 시작 실패 (HTTP ${r.status})`)+(r.body.message?' '+r.body.message:''));}catch(e){showToast('작업 요청 실패: '+e.message);}pollNow();}
async function cancelJob(){try{const r=await postJSON('./api/jobs/cancel',state.job?{job_id:state.job.job_id}:{});showToast(r.ok?'취소를 요청했습니다.':(ERR_TEXT[r.body.error]||`취소 실패 (HTTP ${r.status})`));}catch(e){showToast('취소 요청 실패: '+e.message);}pollNow();}
let pollTimer=null,pollDelay=2000,polling=false;
function applyController(j){const prev=state.job;state.controller=j;state.job=j.job||null;state.pollError=null;
  const changed=JSON.stringify(prev)!==JSON.stringify(state.job);
  if(changed&&$('detailDialog').open&&$('dialogTitle').textContent==='로컬 실행 제어'){$('dialogBody').innerHTML=jobPanel();fillJobPre();keepDialogFocus();}
  if(prev&&state.job&&prev.job_id===state.job.job_id&&prev.state!==state.job.state&&!['queued','running'].includes(state.job.state))showToast(`작업 ${state.job.kind}: ${(JOB_STATE[state.job.state]||[state.job.state])[0]}`);
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

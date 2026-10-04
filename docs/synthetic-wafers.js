(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const GEOMETRY = Object.freeze({diameter_mm:300,die_width_mm:8,die_height_mm:6,scribe_mm:0.08,edge_exclusion_mm:3});
  const METRICS = Object.freeze([
    ['leakage_na','누설','nA',2],['delay_ps','지연','ps',1],['contact_ohm','접촉 저항','Ω',1],
    ['vth_v','문턱 전압','V',3],['cd_nm','CD','nm',1],['overlay_nm','오버레이','nm',1]
  ]);
  const BIN = {pass:'양품',fail:'불량',inconclusive:'판정 보류'};
  const PAGE_SIZE = 25;
  const state = {data:null,wafer:0,filter:'all',defect:'all',view:'bin',search:'',page:0,selected:null};
  let rawJSON = '';
  const canvas = $('waferMap');
  const ctx = canvas.getContext('2d');
  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const text = value => value == null ? '—' : String(value);
  const e = value => text(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const metric = (value, precision) => finite(value) ? value.toFixed(precision) : '—';
  const wafer = () => state.data.wafers[state.wafer];
  const matches = die => (state.filter === 'all' || die.bin === state.filter) && (state.defect === 'all' || (state.view === 'truth' ? die.defects : die.detections).includes(state.defect)) && die.die_id.toLowerCase().includes(state.search);
  const filtered = () => wafer().dies.filter(matches);
  const firstMatching = () => filtered().find(d => d.bin === 'fail') || filtered()[0];
  const reconcileSelection = preferFailure => {
    if (preferFailure || !wafer().dies.some(d => d.die_id === state.selected && matches(d))) state.selected = firstMatching()?.die_id || null;
  };
  const names = ids => ids.map(id => state.data.catalog.find(item => item.id === id)?.name || id).join(', ') || '없음';
  const testNames = ids => ids.map(id => state.data.tests.find(item => item.id === id)?.name || id).join(', ') || '없음';

  function validate(data) {
    if (data?.schema_version !== 1 || data.data_mode !== 'synthetic' || data.seed !== 7001305) throw Error('지원하지 않는 합성 데이터 형식입니다.');
    for (const [key,value] of Object.entries(GEOMETRY)) if (data.geometry?.[key] !== value) throw Error('웨이퍼 치수가 예상 형식과 다릅니다.');
    if (!Array.isArray(data.wafers) || data.wafers.length !== 3 || !Array.isArray(data.catalog) || !Array.isArray(data.tests) || !Array.isArray(data.sources) || !Array.isArray(data.limitations)) throw Error('필수 데이터가 없습니다.');
    const validPositions = new Set();
    for (let row=-24;row<=24;row++) for (let col=-18;col<=18;col++) {
      const x=col*8.08,y=row*6.08;
      if (Math.hypot(Math.abs(x)+4,Math.abs(y)+3)<=147) validPositions.add(`${row},${col}`);
    }
    if (validPositions.size !== 1305) throw Error('다이 격자를 확인할 수 없습니다.');
    for (const w of data.wafers) {
      if (!Array.isArray(w.dies) || w.dies.length !== 1305 || !w.summary || !Array.isArray(w.patterns)) throw Error('웨이퍼별 다이 기록이 불완전합니다.');
      const seen = new Set();
      for (const d of w.dies) {
        const key=`${d.row},${d.col}`;
        if (!validPositions.has(key) || seen.has(key) || !finite(d.x_mm) || !finite(d.y_mm) || Math.abs(d.x_mm-d.col*8.08)>0.001 || Math.abs(d.y_mm-d.row*6.08)>0.001 || !Object.hasOwn(BIN,d.bin) || !Array.isArray(d.defects) || !Array.isArray(d.detections) || !Array.isArray(d.fail_reasons) || !d.metrics) throw Error('다이 좌표 또는 검사 기록을 확인할 수 없습니다.');
        seen.add(key);
      }
    }
    if (data.counts?.wafers !== 3 || data.counts.dies !== 3915 || data.counts.measurements !== 156600) throw Error('합성 데이터의 전체 건수가 일치하지 않습니다.');
  }

  function setStateMessage(message, retry=false) {
    const box=$('loadState'); box.hidden=false; box.replaceChildren(document.createTextNode(message));
    if (retry) { const button=document.createElement('button'); button.type='button'; button.className='control retry'; button.textContent='다시 시도'; button.addEventListener('click',load); box.append(button); }
  }
  async function load() {
    $('app').hidden=true; $('download').disabled=true; setStateMessage('합성 데이터 불러오는 중…');
    try {
      const response=await fetch('./data/synthetic-wafers.json', {cache:'no-store'});
      if (!response.ok) throw Error(`데이터를 불러오지 못했습니다 (${response.status}).`);
      const raw=await response.text(); const data=JSON.parse(raw); validate(data);
      rawJSON=raw; state.data=data; state.wafer=0; state.filter='all'; state.defect='all'; state.view='bin'; state.search=''; state.page=0; state.selected=null;
      $('binFilter').value='all'; $('defectFilter').value='all'; $('dieSearch').value='';
      $('loadState').hidden=true; $('app').hidden=false; $('download').disabled=false;
      renderStatic(); reconcileSelection(true); render();
    } catch (error) { console.error('Synthetic wafer load:',error); setStateMessage(`${error.message || '데이터를 읽을 수 없습니다.'} 원본 파일을 확인한 뒤 다시 시도하세요.`,true); }
  }
  function renderStatic() {
    $('waferButtons').innerHTML=state.data.wafers.map((w,i)=>`<button type="button" data-wafer="${i}" aria-pressed="false"><strong>${e(w.wafer_id)}</strong><span class="wafer-caption">${e(w.scenario)}</span><small>불량 ${e(w.summary.fail)} · 보류 ${e(w.summary.inconclusive)}</small></button>`).join('');
    $('defectFilter').innerHTML='<option value="all">전체 유형</option>'+state.data.catalog.filter(item=>item.modeled).map(item=>`<option value="${e(item.id)}">${e(item.name)}</option>`).join('');
    const sources=new Map(state.data.sources.map(source=>[source.id,source]));
    $('catalogRows').innerHTML=state.data.catalog.map(item=>{
      const links=(item.source_ids || []).map(id=>sources.get(id)).filter(source=>source && /^https:\/\//.test(source.url)).map(source=>`<a href="${e(source.url)}" target="_blank" rel="noopener noreferrer">${e(source.title)}</a>`).join('') || '—';
      return `<tr><th scope="row">${e(item.name)}</th><td>${e(item.family)}</td><td>${e(item.stage)}</td><td>${e(item.observable)}</td><td>${item.modeled ? '모델링' : '미모델링'}</td><td>${links}</td></tr>`;
    }).join('') || '<tr><td colspan="6">목록이 없습니다.</td></tr>';
    $('limitationsList').innerHTML=state.data.limitations.map(line=>`<li>${e(line)}</li>`).join('');
  }
  function render() {
    const w=wafer(), rows=filtered();
    $('scenario').textContent=`${w.wafer_id} · ${w.scenario}`;
    $('datasetMeta').textContent=`시드 ${state.data.seed} · ${state.data.counts.wafers}장 · ${state.data.counts.dies.toLocaleString('ko-KR')}개 다이 · ${state.data.counts.measurements.toLocaleString('ko-KR')}회 합성 검사 시도`;
    $('patternSummary').textContent='작성한 공간 패턴: '+w.patterns.map(id=>state.data.patterns.find(p=>p.id===id)?.name || id).join(' · ');
    $('waferButtons').querySelectorAll('button').forEach((button,i)=>button.setAttribute('aria-pressed', String(i===state.wafer)));
    document.querySelectorAll('[data-view]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.view===state.view)));
    $('summaryMetrics').innerHTML=`<span>전체 <strong>${e(w.summary.total)}</strong></span><span class="summary-pass">양품 <strong>${e(w.summary.pass)}</strong></span><span class="summary-fail">불량 <strong>${e(w.summary.fail)}</strong></span><span class="summary-hold">보류 <strong>${e(w.summary.inconclusive)}</strong></span><span>생성 결함 <strong>${e(w.summary.defect_dies)}</strong></span><span>모의 탐지 <strong>${e(w.summary.detected_dies)}</strong></span>`;
    $('filterCount').textContent=`표시 ${rows.length.toLocaleString('ko-KR')} / ${w.dies.length.toLocaleString('ko-KR')}`;
    $('resetFilters').hidden=rows.length!==0;
    $('viewExplanation').textContent=state.view==='truth' ? '생성된 정답 레이블은 생성기가 부여한 값입니다. 센서로 입증한 원인이 아닙니다. 이 화면의 결함 필터는 생성 결함 기준입니다.' : '관측 판정은 합성 검사의 결과입니다. 이 화면의 결함 필터는 모의 탐지 기준입니다.';
    $('legend').innerHTML=state.view==='bin'
      ? '<span><i class="swatch pass"></i>양품</span><span><i class="swatch fail"></i>불량</span><span><i class="swatch inconclusive"></i>판정 보류</span><span><i class="swatch neutral"></i>필터 제외</span>'
      : '<span><i class="swatch defect"></i>생성된 결함 레이블</span><span><i class="swatch detected"></i>모의 탐지 레이블</span><span><i class="swatch neutral"></i>해당 레이블 없음 / 필터 제외</span>';
    draw(); renderDetail(); renderRows(rows);
  }
  function renderDetail() {
    const d=wafer().dies.find(item=>item.die_id===state.selected);
    $('detailRef').textContent=d?.die_id || '';
    if (!d) { $('dieDetail').innerHTML='<p class="detail-text">일치하는 다이가 없습니다. 필터를 초기화하거나 검색어를 바꿔 주세요.</p><button type="button" class="control" data-reset="true">필터 초기화</button>'; $('mapHelp').textContent='선택된 다이: 없음 · 화살표 키로 이동, Enter 키로 상세 기록 이동'; return; }
    $('mapHelp').textContent=`선택된 다이: ${d.die_id}, ${BIN[d.bin]} · 화살표 키로 이동, Enter 키로 상세 기록 이동`;
    $('dieDetail').innerHTML=`<p class="detail-lead">${e(d.die_id)} <span class="verdict ${d.bin}"><i aria-hidden="true"></i>${BIN[d.bin]}</span></p><p class="detail-text">행 ${d.row}, 열 ${d.col} · (${metric(d.x_mm,2)}, ${metric(d.y_mm,2)}) mm</p><p class="detail-context">전기 지표 1.0 V / 25 °C · 오버레이 X</p><dl class="detail-list">${METRICS.map(([id,name,unit,precision])=>`<dt>${name}</dt><dd>${metric(d.metrics[id],precision)} ${unit}</dd>`).join('')}<dt>실패 검사</dt><dd>${e(d.tests_failed)}</dd><dt>누락 검사</dt><dd>${e(d.tests_missing)}</dd></dl><div class="detail-block"><strong>실패 사유 · 검사 항목</strong><p>${e(testNames(d.fail_reasons))}</p></div><div class="detail-block"><strong>모의 탐지 · 불완전한 검사</strong><p>${e(names(d.detections))}</p></div><div class="detail-block"><strong>생성된 정답 레이블 · 센서로 검증되지 않음</strong><p>${e(names(d.defects))}</p></div>`;
  }
  function renderRows(rows) {
    const pageCount=Math.max(1,Math.ceil(rows.length/PAGE_SIZE)); state.page=Math.min(state.page,pageCount-1);
    const start=state.page*PAGE_SIZE, pageRows=rows.slice(start,start+PAGE_SIZE);
    $('dieRows').innerHTML=pageRows.map(d=>`<tr class="${d.die_id===state.selected?'is-selected':''}"><th scope="row" class="mono">${e(d.die_id)}</th><td><span class="verdict ${d.bin}"><i aria-hidden="true"></i>${BIN[d.bin]}</span></td>${METRICS.map(([id,, ,precision])=>`<td class="num">${metric(d.metrics[id],precision)}</td>`).join('')}<td class="reason">실패 ${e(d.tests_failed)} · 누락 ${e(d.tests_missing)}</td><td><button class="control" type="button" data-die="${e(d.die_id)}" aria-label="${e(d.die_id)} 상세 보기">선택</button></td></tr>`).join('') || '<tr><td colspan="10">일치하는 다이가 없습니다. 필터를 초기화하거나 검색어를 변경하세요.</td></tr>';
    $('pageInfo').textContent=rows.length ? `${start+1}–${Math.min(start+PAGE_SIZE,rows.length)} / ${rows.length} · ${state.page+1} / ${pageCount}쪽` : '0개 기록';
    $('prevPage').disabled=state.page===0; $('nextPage').disabled=state.page>=pageCount-1;
  }
  function select(d, focusDetail=false) {
    if (!d) return; state.selected=d.die_id; draw(); renderDetail(); renderRows(filtered());
    if (focusDetail) { $('detailTitle').setAttribute('tabindex','-1'); $('detailTitle').focus({preventScroll:true}); $('detailTitle').scrollIntoView({block:'nearest'}); }
  }
  function resetFilters() {
    state.filter='all'; state.defect='all'; state.search=''; state.page=0;
    $('binFilter').value='all'; $('defectFilter').value='all'; $('dieSearch').value='';
    reconcileSelection(true); render();
  }
  function draw() {
    if (!ctx || !state.data) return;
    const ratio=Math.min(window.devicePixelRatio || 1,2), size=600;
    if (canvas.width!==size*ratio) { canvas.width=size*ratio; canvas.height=size*ratio; }
    ctx.setTransform(ratio,0,0,ratio,0,0); ctx.clearRect(0,0,size,size);
    // Canvas is a visual projection only. Die positions, verdicts and labels all come from the JSON.
    ctx.save(); ctx.beginPath(); ctx.arc(300,300,300,0,Math.PI*2); ctx.clip();
    ctx.fillStyle='#d7ded7'; ctx.fillRect(0,0,600,600);
    const film=ctx.createLinearGradient(45,20,555,575);
    [[0,'#d9d1eb'],[.16,'#a9c5e2'],[.33,'#b7dce1'],[.5,'#c6ddbf'],[.69,'#e9d9ac'],[.85,'#e3c9cf'],[1,'#b9bed5']].forEach(([stop,color])=>film.addColorStop(stop,color));
    ctx.globalAlpha=.75; ctx.fillStyle=film; ctx.fillRect(0,0,600,600);
    const glint=ctx.createLinearGradient(100,30,410,400);glint.addColorStop(0,'#ffffff99');glint.addColorStop(.35,'#ffffff0a');glint.addColorStop(.62,'#ffffff55');glint.addColorStop(1,'#ffffff08');ctx.globalAlpha=1;ctx.fillStyle=glint;ctx.fillRect(0,0,600,600);
    const visible=new Set(filtered().map(d=>d.die_id));
    const dies=wafer().dies;
    for (const d of dies) {
      const x=300+d.x_mm*2-8,y=300+d.y_mm*2-6;
      if (!visible.has(d.die_id)) ctx.fillStyle='rgba(229,235,226,.55)';
      else if (state.view==='truth') ctx.fillStyle=d.defects.length ? '#963f34' : d.detections.length ? '#276449' : 'rgba(245,249,243,.70)';
      else ctx.fillStyle=d.bin==='pass'?'rgba(232,242,232,.70)':d.bin==='fail'?'#963f34':'#916414';
      ctx.fillRect(x,y,16,12);
      if (visible.has(d.die_id) && state.view==='truth' && d.defects.length && d.detections.length) {ctx.fillStyle='#276449';ctx.fillRect(x+11,y,5,12);}
    }
    ctx.restore();
    ctx.beginPath();ctx.arc(300,300,294,0,Math.PI*2);ctx.strokeStyle='#778b83';ctx.lineWidth=1.5;ctx.stroke();
    ctx.beginPath();ctx.arc(300,300,299,0,Math.PI*2);ctx.strokeStyle='#adbbb3';ctx.lineWidth=2;ctx.stroke();
    ctx.beginPath();ctx.arc(300,600,6,Math.PI,0);ctx.fillStyle='#f3f5f0';ctx.fill();ctx.strokeStyle='#778b83';ctx.lineWidth=1;ctx.stroke();
    const selected=dies.find(d=>d.die_id===state.selected);
    if (selected) {const x=300+selected.x_mm*2,y=300+selected.y_mm*2;ctx.strokeStyle='#202b28';ctx.lineWidth=2;ctx.strokeRect(x-10,y-8,20,16);ctx.strokeStyle='#fff';ctx.lineWidth=1;ctx.strokeRect(x-8,y-6,16,12);}
  }
  function canvasDie(event) {
    const r=canvas.getBoundingClientRect(); const x=(event.clientX-r.left)/r.width*600,y=(event.clientY-r.top)/r.height*600;
    return wafer().dies.find(d=>Math.abs(300+d.x_mm*2-x)<=8 && Math.abs(300+d.y_mm*2-y)<=6 && matches(d));
  }
  canvas.addEventListener('click',event=>select(canvasDie(event)));
  canvas.addEventListener('keydown',event=>{
    if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Enter'].includes(event.key)) return;
    event.preventDefault();const available=filtered(); if (!available.length) return;
    if (event.key==='Enter') {select(available.find(d=>d.die_id===state.selected)||available[0],true);return;}
    const current=available.find(d=>d.die_id===state.selected)||available.find(d=>d.row===0&&d.col===0)||available[0];
    const options=available.filter(d=>event.key==='ArrowLeft'?d.col<current.col:event.key==='ArrowRight'?d.col>current.col:event.key==='ArrowUp'?d.row<current.row:d.row>current.row);
    options.sort((a,b)=>{
      const score=d=>event.key==='ArrowLeft'||event.key==='ArrowRight' ? Math.abs(d.col-current.col)*3+Math.abs(d.row-current.row)*10 : Math.abs(d.row-current.row)*3+Math.abs(d.col-current.col)*10;
      return score(a)-score(b);
    });
    select(options[0]||current);
  });
  $('waferButtons').addEventListener('click',event=>{const button=event.target.closest('[data-wafer]');if (!button)return;state.wafer=Number(button.dataset.wafer);state.page=0;reconcileSelection(true);render();});
  document.querySelector('.view-switch').addEventListener('click',event=>{const button=event.target.closest('[data-view]');if(!button)return;state.view=button.dataset.view;state.page=0;reconcileSelection(false);render();});
  $('binFilter').addEventListener('change',event=>{state.filter=event.target.value;state.page=0;reconcileSelection(false);render();});
  $('defectFilter').addEventListener('change',event=>{state.defect=event.target.value;state.page=0;reconcileSelection(false);render();});
  $('dieSearch').addEventListener('input',event=>{state.search=event.target.value.trim().toLowerCase();state.page=0;reconcileSelection(false);render();});
  $('resetFilters').addEventListener('click',resetFilters);
  $('dieDetail').addEventListener('click',event=>{if(event.target.closest('[data-reset]'))resetFilters();});
  $('dieRows').addEventListener('click',event=>{const button=event.target.closest('[data-die]');if(button)select(wafer().dies.find(d=>d.die_id===button.dataset.die),true);});
  $('prevPage').addEventListener('click',()=>{state.page--;renderRows(filtered());});
  $('nextPage').addEventListener('click',()=>{state.page++;renderRows(filtered());});
  $('download').addEventListener('click',()=>{if(!rawJSON)return;const url=URL.createObjectURL(new Blob([rawJSON],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='synthetic-wafers.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
  window.addEventListener('resize',()=>{if(state.data)draw();});
  load();
})();

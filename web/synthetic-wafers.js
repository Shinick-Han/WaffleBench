(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const GEOMETRY = Object.freeze({diameter_mm:300,die_width_mm:8,die_height_mm:6,scribe_mm:0.08,edge_exclusion_mm:3});
  const METRICS = Object.freeze([
    ['leakage_na','Leakage','nA',2],['delay_ps','Delay','ps',1],['contact_ohm','Contact resistance','Ω',1],
    ['vth_v','Threshold voltage','V',3],['cd_nm','CD','nm',1],['overlay_nm','Overlay','nm',1]
  ]);
  const BIN = {pass:'Pass',fail:'Fail',inconclusive:'Inconclusive'};
  const PAGE_SIZE = 25;
  // Presentation-only English labels. The dataset JSON (and its exact-byte download)
  // keeps the original source text; unknown values fall back to the raw string.
  const CATALOG_EN = Object.freeze({
    particle:['Particles / foreign material','Surface/contamination','Throughout process','Optical scattering, SEM; electrical impact depends on location'],
    residue:['Cleaning/resist residue','Surface/contamination','Cleaning/patterning','Optical, SEM, surface analysis'],
    contamination:['Surface contamination','Surface/contamination','Cleaning/handling','Surface analysis, optical, electrical test'],
    scratch:['Scratch','Mechanical/substrate','Polishing/handling','Optical, surface inspection'],
    pit:['Surface pit','Mechanical/substrate','Substrate/thin film','Optical, surface metrology'],
    bump:['Surface bump','Mechanical/substrate','Substrate/thin film','Optical, surface metrology'],
    edge_chip:['Wafer edge chipping','Mechanical/substrate','Substrate/handling','Edge optical inspection'],
    crack:['Wafer crack','Mechanical/substrate','Substrate/handling','Edge, surface, IR inspection'],
    stress:['Film/bulk stress','Mechanical/substrate','Thin film/substrate','Stress and shape metrology'],
    inclusion:['Bulk voids/inclusions','Mechanical/substrate','Substrate','Bulk-sensitive inspection; surface inspection alone is insufficient'],
    bridge:['Micro-bridge','Patterning','Lithography','SEM, continuity test; possible short'],
    broken_line:['Broken line','Patterning','Lithography','SEM, continuity test; possible open'],
    missing_contact:['Missing contact','Patterning','Lithography','SEM, resistance/continuity test'],
    merged_contact:['Merged contact','Patterning','Lithography','SEM, leakage/continuity test'],
    cd_shift:['CD shift','Process variation','Lithography','CD-SEM, electrical characteristics'],
    line_roughness:['Line edge/width roughness','Process variation','Lithography','Local CD-SEM metrology'],
    etch_bias:['Asymmetric etch/deposition','Process variation','Etch/deposition','Structural metrology, wafer distribution'],
    gapfill_void:['Gap-fill void','Thin film/interconnect','Dielectric deposition','Cross-section/buried-structure inspection'],
    dishing:['CMP dishing','Thin film/interconnect','CMP','Height and thickness metrology'],
    erosion:['CMP erosion','Thin film/interconnect','CMP','Height and thickness metrology'],
    epi_nodule:['Epi nodule','Crystal/epitaxy','Epitaxy','Surface/structural inspection'],
    epi_merge:['Premature epi merge','Crystal/epitaxy','Epitaxy','Structural inspection'],
    stacking_fault:['Twinning/stacking fault','Crystal/epitaxy','Epitaxy','Crystal/structural analysis'],
    oxide_failure:['Gate oxide integrity failure','Electrical','Device/wafer test','Leakage, oxide integrity test'],
    junction_leakage:['Abnormal junction leakage','Electrical','Device/wafer test','I-V, leakage test'],
    contact_resistance:['Abnormal line/via resistance','Electrical','Interconnect/wafer test','Resistance/electrical test structures'],
    vth_shift:['Threshold voltage shift','Electrical','Device/wafer test','Transistor I-V test'],
    tddb:['Time-dependent dielectric breakdown (TDDB)','Reliability','Accelerated stress','Requires time/voltage stress; cannot be confirmed by standard sort'],
    bti:['Bias temperature instability (BTI)','Reliability','Accelerated stress','Requires bias, temperature and time stress'],
    hci:['Hot carrier injection degradation (HCI)','Reliability','Accelerated stress','Requires comparing device characteristics before and after stress'],
    electromigration:['Electromigration','Reliability','Accelerated stress','Requires current, temperature and time stress'],
    bond_void:['Bond void','Back end/bonding','Hybrid bonding','IR, bond-interface inspection; outside this unbonded-wafer model'],
    bond_particle:['Bond-interface particle','Back end/bonding','Hybrid bonding','Surface cleanliness, IR inspection; outside the model'],
    dicing_crack:['Dicing/pick-up crack','Back end/bonding','Dicing/pick-up','Die, IR inspection; outside this pre-dicing model']
  });
  const CATALOG_FIELDS = ['name','family','stage','observable'];
  const catalogText = (item, field) => item ? (CATALOG_EN[item.id]?.[CATALOG_FIELDS.indexOf(field)] || item[field]) : null;
  const TEST_PREFIX_EN = Object.freeze([['배선/콘택트 저항','Interconnect/contact resistance'],['문턱 전압','Threshold voltage'],['박막 두께','Film thickness'],['지연','Delay'],['누설','Leakage']]);
  const testLabel = name => { if (typeof name !== 'string') return name; for (const [ko,en] of TEST_PREFIX_EN) if (name === ko || name.startsWith(ko+' ')) return en+name.slice(ko.length); return name; };
  const PATTERN_EN = Object.freeze({none:'No distinct pattern',random:'Random',local:'Local cluster',center:'Center',donut:'Donut',edge_local:'Local edge',edge_ring:'Edge ring',scratch:'Linear scratch',near_full:'Near-full wafer',reticle_repeat:'Repeating field (illustrative)'});
  const SCENARIO_EN = Object.freeze({W01:'Scattered particles and a local cleaning cluster',W02:'Edge process variation and a central leakage cluster',W03:'Handling scratch and field-repeating defects'});
  const scenarioText = w => SCENARIO_EN[w.wafer_id] || w.scenario;
  const LIMITATION_EN = Object.freeze({
    '모든 수치·판정·위치는 seed로 생성한 합성 데이터이며 실제 팹 측정·기존 PVT 연구 결과가 아닙니다.':'All values, verdicts and positions are seeded synthetic data, not real fab measurements or results of the existing PVT study.',
    '결함 카탈로그는 실용적 조사 범위입니다. 모든 재료·소자·패키지의 모든 결함을 망라하지 않습니다.':'The defect catalog reflects a practical survey scope. It does not cover every defect of every material, device or package.',
    '출처는 분류·검사 개념의 근거입니다. 확률·공정 편차·판정 한계·결함 효과는 작성한 예시이며 실측으로 보정하지 않았습니다.':'Sources support the classification and inspection concepts. Probabilities, process variation, verdict limits and defect effects are authored examples, not calibrated against measurements.',
    '40회/다이 검사 시도; 누락 값은 null입니다. fail은 관측 한계 초과, inconclusive는 초과 없이 필수 검사 누락, pass는 전부 한계 내입니다.':'40 test attempts per die; missing values are null. fail means an observed limit was exceeded, inconclusive means a required test is missing with no exceedance, and pass means every value is within limits.',
    '검출 결과는 민감도·오탐을 넣은 가상 센서 출력입니다. 생성 결함은 알려진 ground truth이며 인과 추론의 증거가 아닙니다.':'Detections are virtual sensor outputs with built-in sensitivity and false positives. Generated defects are known ground truth, not evidence for causal inference.',
    '입자 등 검출 결함이 있어도 전기 판정이 pass일 수 있습니다. 공간 패턴은 고유한 원인과 일대일 대응하지 않습니다.':'A die can pass electrically even with a detected defect such as a particle. Spatial patterns do not map one-to-one to a unique cause.',
    '3장 모두 같은 예시 lot입니다. 독립 팹·lot 검증이나 모델 학습 일반화, 수율 개선 효과를 입증하지 않습니다.':'All three wafers come from the same illustrative lot. They do not demonstrate independent fab/lot validation, model-training generalization or yield improvement.',
    '신뢰성 스트레스·후공정 결함은 조사만 했으며 이번 웨이퍼 sort 생성에 포함하지 않습니다.':'Reliability-stress and back-end defects were surveyed only and are not included in this wafer-sort generation.',
    '8×6mm die/300mm wafer 형상은 작성한 배치입니다. 노치·에지 처리와 공정 구조를 제조용 설계로 사용할 수 없습니다.':'The 8×6 mm die / 300 mm wafer geometry is an authored layout. Its notch, edge handling and process structure must not be used as a manufacturing design.'
  });
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
  const names = ids => ids.map(id => catalogText(state.data.catalog.find(item => item.id === id), 'name') || id).join(', ') || 'None';
  const testNames = ids => ids.map(id => testLabel(state.data.tests.find(item => item.id === id)?.name) || id).join(', ') || 'None';

  function validate(data) {
    if (data?.schema_version !== 1 || data.data_mode !== 'synthetic' || data.seed !== 7001305) throw Error('Unsupported synthetic data format.');
    for (const [key,value] of Object.entries(GEOMETRY)) if (data.geometry?.[key] !== value) throw Error('Wafer dimensions differ from the expected format.');
    if (!Array.isArray(data.wafers) || data.wafers.length !== 3 || !Array.isArray(data.catalog) || !Array.isArray(data.tests) || !Array.isArray(data.sources) || !Array.isArray(data.limitations)) throw Error('Required data is missing.');
    const validPositions = new Set();
    for (let row=-24;row<=24;row++) for (let col=-18;col<=18;col++) {
      const x=col*8.08,y=row*6.08;
      if (Math.hypot(Math.abs(x)+4,Math.abs(y)+3)<=147) validPositions.add(`${row},${col}`);
    }
    if (validPositions.size !== 1305) throw Error('Could not verify the die grid.');
    for (const w of data.wafers) {
      if (!Array.isArray(w.dies) || w.dies.length !== 1305 || !w.summary || !Array.isArray(w.patterns)) throw Error('Per-wafer die records are incomplete.');
      const seen = new Set();
      for (const d of w.dies) {
        const key=`${d.row},${d.col}`;
        if (!validPositions.has(key) || seen.has(key) || !finite(d.x_mm) || !finite(d.y_mm) || Math.abs(d.x_mm-d.col*8.08)>0.001 || Math.abs(d.y_mm-d.row*6.08)>0.001 || !Object.hasOwn(BIN,d.bin) || !Array.isArray(d.defects) || !Array.isArray(d.detections) || !Array.isArray(d.fail_reasons) || !d.metrics) throw Error('Could not verify die coordinates or inspection records.');
        seen.add(key);
      }
    }
    if (data.counts?.wafers !== 3 || data.counts.dies !== 3915 || data.counts.measurements !== 156600) throw Error('Synthetic data totals do not match.');
  }

  function setStateMessage(message, retry=false) {
    const box=$('loadState'); box.hidden=false; box.replaceChildren(document.createTextNode(message));
    if (retry) { const button=document.createElement('button'); button.type='button'; button.className='control retry'; button.textContent='Retry'; button.addEventListener('click',load); box.append(button); }
  }
  async function load() {
    $('app').hidden=true; $('download').disabled=true; setStateMessage('Loading synthetic data…');
    try {
      const response=await fetch('./data/synthetic-wafers.json', {cache:'no-store'});
      if (!response.ok) throw Error(`Could not load the data (${response.status}).`);
      const raw=await response.text(); const data=JSON.parse(raw); validate(data);
      rawJSON=raw; state.data=data; state.wafer=0; state.filter='all'; state.defect='all'; state.view='bin'; state.search=''; state.page=0; state.selected=null;
      $('binFilter').value='all'; $('defectFilter').value='all'; $('dieSearch').value='';
      $('loadState').hidden=true; $('app').hidden=false; $('download').disabled=false;
      renderStatic(); reconcileSelection(true); render();
    } catch (error) { console.error('Synthetic wafer load:',error); setStateMessage(`${error.message || 'Could not read the data.'} Check the source file and try again.`,true); }
  }
  function renderStatic() {
    $('waferButtons').innerHTML=state.data.wafers.map((w,i)=>`<button type="button" data-wafer="${i}" aria-pressed="false"><strong>${e(w.wafer_id)}</strong><span class="wafer-caption">${e(scenarioText(w))}</span><small>Fail ${e(w.summary.fail)} · Inconclusive ${e(w.summary.inconclusive)}</small></button>`).join('');
    $('defectFilter').innerHTML='<option value="all">All types</option>'+state.data.catalog.filter(item=>item.modeled).map(item=>`<option value="${e(item.id)}">${e(catalogText(item,'name'))}</option>`).join('');
    const sources=new Map(state.data.sources.map(source=>[source.id,source]));
    $('catalogRows').innerHTML=state.data.catalog.map(item=>{
      const links=(item.source_ids || []).map(id=>sources.get(id)).filter(source=>source && /^https:\/\//.test(source.url)).map(source=>`<a href="${e(source.url)}" target="_blank" rel="noopener noreferrer">${e(source.title)}</a>`).join('') || '—';
      return `<tr><th scope="row">${e(catalogText(item,'name'))}</th><td>${e(catalogText(item,'family'))}</td><td>${e(catalogText(item,'stage'))}</td><td>${e(catalogText(item,'observable'))}</td><td>${item.modeled ? 'Modeled' : 'Not modeled'}</td><td>${links}</td></tr>`;
    }).join('') || '<tr><td colspan="6">No entries.</td></tr>';
    $('limitationsList').innerHTML=state.data.limitations.map(line=>`<li>${e(LIMITATION_EN[line] || line)}</li>`).join('');
  }
  function render() {
    const w=wafer(), rows=filtered();
    $('scenario').textContent=`${w.wafer_id} · ${scenarioText(w)}`;
    $('datasetMeta').textContent=`Seed ${state.data.seed} · ${state.data.counts.wafers} wafers · ${state.data.counts.dies.toLocaleString('en-US')} dies · ${state.data.counts.measurements.toLocaleString('en-US')} synthetic test attempts`;
    $('patternSummary').textContent='Authored spatial patterns: '+w.patterns.map(id=>PATTERN_EN[id] || state.data.patterns.find(p=>p.id===id)?.name || id).join(' · ');
    $('waferButtons').querySelectorAll('button').forEach((button,i)=>button.setAttribute('aria-pressed', String(i===state.wafer)));
    document.querySelectorAll('[data-view]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.view===state.view)));
    $('summaryMetrics').innerHTML=`<span>Total <strong>${e(w.summary.total)}</strong></span><span class="summary-pass">Pass <strong>${e(w.summary.pass)}</strong></span><span class="summary-fail">Fail <strong>${e(w.summary.fail)}</strong></span><span class="summary-hold">Inconclusive <strong>${e(w.summary.inconclusive)}</strong></span><span>Generated defects <strong>${e(w.summary.defect_dies)}</strong></span><span>Simulated detections <strong>${e(w.summary.detected_dies)}</strong></span>`;
    $('filterCount').textContent=`Showing ${rows.length.toLocaleString('en-US')} / ${w.dies.length.toLocaleString('en-US')}`;
    $('resetFilters').hidden=rows.length!==0;
    $('viewExplanation').textContent=state.view==='truth' ? 'Generated ground-truth labels are assigned by the generator, not causes proven by sensors. In this view the defect filter uses generated defects.' : 'Observed verdicts are the results of synthetic tests. In this view the defect filter uses simulated detections.';
    $('legend').innerHTML=state.view==='bin'
      ? '<span><i class="swatch pass"></i>Pass</span><span><i class="swatch fail"></i>Fail</span><span><i class="swatch inconclusive"></i>Inconclusive</span><span><i class="swatch neutral"></i>Filtered out</span>'
      : '<span><i class="swatch defect"></i>Generated defect label</span><span><i class="swatch detected"></i>Simulated detection label</span><span><i class="swatch neutral"></i>No such label / filtered out</span>';
    draw(); renderDetail(); renderRows(rows);
  }
  function renderDetail() {
    const d=wafer().dies.find(item=>item.die_id===state.selected);
    $('detailRef').textContent=d?.die_id || '';
    if (!d) { $('dieDetail').innerHTML='<p class="detail-text">No matching dies. Reset the filters or change the search.</p><button type="button" class="control" data-reset="true">Reset filters</button>'; $('mapHelp').textContent='Selected die: none · Arrow keys to move, Enter to open details'; return; }
    $('mapHelp').textContent=`Selected die: ${d.die_id}, ${BIN[d.bin]} · Arrow keys to move, Enter to open details`;
    $('dieDetail').innerHTML=`<p class="detail-lead">${e(d.die_id)} <span class="verdict ${d.bin}"><i aria-hidden="true"></i>${BIN[d.bin]}</span></p><p class="detail-text">Row ${d.row}, column ${d.col} · (${metric(d.x_mm,2)}, ${metric(d.y_mm,2)}) mm</p><p class="detail-context">Electrical metrics at 1.0 V / 25 °C · Overlay X</p><dl class="detail-list">${METRICS.map(([id,name,unit,precision])=>`<dt>${name}</dt><dd>${metric(d.metrics[id],precision)} ${unit}</dd>`).join('')}<dt>Failed tests</dt><dd>${e(d.tests_failed)}</dd><dt>Missing tests</dt><dd>${e(d.tests_missing)}</dd></dl><div class="detail-block"><strong>Failure reasons · tests</strong><p>${e(testNames(d.fail_reasons))}</p></div><div class="detail-block"><strong>Simulated detection · imperfect inspection</strong><p>${e(names(d.detections))}</p></div><div class="detail-block"><strong>Generated ground-truth label · not sensor-verified</strong><p>${e(names(d.defects))}</p></div>`;
  }
  function renderRows(rows) {
    const pageCount=Math.max(1,Math.ceil(rows.length/PAGE_SIZE)); state.page=Math.min(state.page,pageCount-1);
    const start=state.page*PAGE_SIZE, pageRows=rows.slice(start,start+PAGE_SIZE);
    $('dieRows').innerHTML=pageRows.map(d=>`<tr class="${d.die_id===state.selected?'is-selected':''}"><th scope="row" class="mono">${e(d.die_id)}</th><td><span class="verdict ${d.bin}"><i aria-hidden="true"></i>${BIN[d.bin]}</span></td>${METRICS.map(([id,, ,precision])=>`<td class="num">${metric(d.metrics[id],precision)}</td>`).join('')}<td class="reason">Failed ${e(d.tests_failed)} · Missing ${e(d.tests_missing)}</td><td><button class="control" type="button" data-die="${e(d.die_id)}" aria-label="View details for ${e(d.die_id)}">Select</button></td></tr>`).join('') || '<tr><td colspan="10">No matching dies. Reset the filters or change the search.</td></tr>';
    $('pageInfo').textContent=rows.length ? `${start+1}–${Math.min(start+PAGE_SIZE,rows.length)} / ${rows.length} · page ${state.page+1} / ${pageCount}` : '0 records';
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

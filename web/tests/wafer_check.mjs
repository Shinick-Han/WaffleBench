// Semantic and geometry checks for the wafer visualization. No research data is written.
// PLAYWRIGHT_MODULE=<installed playwright> node web/tests/wafer_check.mjs <url> <snapshot> <report>
import {createRequire} from 'node:module';
import {readFileSync,writeFileSync} from 'node:fs';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const [url,snapshotPath,reportPath]=process.argv.slice(2);
const real=JSON.parse(readFileSync(snapshotPath,'utf8'));
const browser=await chromium.launch(),failures=[],results={};
const check=(ok,msg)=>{if(!ok)failures.push(msg)};
async function open(snapshot,width){
 const page=await browser.newPage({viewport:{width,height:900}});
 await page.route('**/api/**',r=>r.fulfill({status:404,body:'not found'}));
 await page.route('**/data/snapshot.json',r=>r.fulfill({body:JSON.stringify(snapshot),contentType:'application/json'}));
 await page.goto(url,{waitUntil:'networkidle'});await page.waitForSelector('#heatmap [data-key]');return page;
}
async function audit(page,tag){
 const defects=await page.evaluate(()=>{
  const failures=[];
  for(const b of document.querySelectorAll('#heatmap [data-key]')){
   const c=cellAt(b.dataset.key),e=c.ev,label=b.getAttribute('aria-label')||'';
   const expected=c.kind==='failed'?'failed':c.kind==='unobserved'?'unknown':
    e?.clear_counterexample===true?'bad':e?.clear_counterexample===false&&e?.secondary_counterexample===true?'boundary':
    e?.clear_counterexample===false&&e?.secondary_counterexample===false?'good':'unknown';
   const found=/양품/.test(label)?'good':/불량/.test(label)?'bad':/경계/.test(label)?'boundary':'unknown';
   if(expected==='failed'){if(found!=='unknown'||!/실패/.test(label))failures.push(b.dataset.key+' failed mislabeled');}
   else if(expected!==found)failures.push(b.dataset.key+' expected '+expected+', got '+label);
   if(c.kind==='held_out'&&!/사후/.test(label))failures.push(b.dataset.key+' lost posthoc label');
   if(c.candidate&&c.kind==='unobserved'&&!/추정/.test(label))failures.push(b.dataset.key+' lost estimate label');
   const r=b.getBoundingClientRect();if(r.width<24||r.height<24)failures.push(b.dataset.key+' target below 24px');
   if(r.right>innerWidth+1||r.left<0)failures.push(b.dataset.key+' clipped target');
  }
  if(document.querySelectorAll('#heatmap [data-key]').length!==35)failures.push('not exactly35 PVT points');
  if(document.documentElement.scrollWidth>innerWidth)failures.push('document overflow');
  return failures;
 });for(const d of defects)failures.push(tag+': '+d);
}
for(const width of [360,390,768,1440]){
 const p=await open(real,width),errors=[];p.on('pageerror',e=>errors.push(String(e)));
 const counts=[];
 for(let stage=0;stage<=real.decisions.length;stage++){
  await p.click('[data-step="'+stage+'"]');
  for(const corner of ['SS','TT','FF','FS','SF']){
   await p.click('[data-corner="'+corner+'"]');await audit(p,width+' stage'+stage+' '+corner);
   counts.push({stage,corner,...await p.locator('#heatmap [data-key]').evaluateAll(bs=>({good:bs.filter(b=>/양품/.test(b.getAttribute('aria-label'))).length,bad:bs.filter(b=>/불량/.test(b.getAttribute('aria-label'))).length,boundary:bs.filter(b=>/경계/.test(b.getAttribute('aria-label'))).length}))});
  }
 }
 const focus=p.locator('#heatmap [data-key]').first(),key=await focus.getAttribute('data-key');await focus.focus();await p.keyboard.press('Enter');
 check(await p.evaluate(k=>document.activeElement?.dataset.key===k&&document.activeElement?.getAttribute('aria-pressed')==='true',key),width+' keyboard focus/selection');
 check(errors.length===0,width+' runtime errors');
 results[width]={counts,errors};await p.close();
}
// Deliberately conflicting numeric error and flags: classification must honor the
// stored booleans, and missing flags must never become a fabricated pass/fail.
const fixture=structuredClone(real);fixture.data_mode='fixture';fixture.coverage=[];
const variants=[{clear_counterexample:false,secondary_counterexample:false,abs_relative_error:.9},
 {clear_counterexample:true,secondary_counterexample:true,abs_relative_error:0},
 {clear_counterexample:false,secondary_counterexample:true,abs_relative_error:.01},
 {clear_counterexample:false,secondary_counterexample:null,abs_relative_error:0},
 {clear_counterexample:null,secondary_counterexample:true,abs_relative_error:.9},
 {clear_counterexample:true,secondary_counterexample:null,abs_relative_error:0},
 {clear_counterexample:'false',secondary_counterexample:'false',abs_relative_error:0}];
variants.forEach((v,i)=>Object.assign(fixture.evaluations[i],v));
fixture.decisions[0].candidates.forEach(c=>c.idw_predicted_abs_error=.99);
const p=await open(fixture,390);await p.click('#finalButton');
for(const corner of ['SS','TT','FF','FS','SF']){await p.click('[data-corner="'+corner+'"]');await audit(p,'flag fixture '+corner);}
await p.click('#resetButton');await p.click('[data-corner="FF"]');await audit(p,'hidden/candidate fixture');
await p.close();await browser.close();writeFileSync(reportPath,JSON.stringify({results,stored_flag_cases:variants.length,failures},null,2));console.log(JSON.stringify({failures,stored_flag_cases:variants.length}));process.exit(failures.length?1:0);

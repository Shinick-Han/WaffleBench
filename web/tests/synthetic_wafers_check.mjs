// Dataset-source/UI reconciliation and accessibility, with a bounded optional visual capture.
import {createRequire} from 'node:module';
import {readFileSync,writeFileSync,mkdirSync} from 'node:fs';
import {createHash} from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'C:/Users/user/data-repair-agent/node_modules/playwright/index.js');
const [base,dataPath,out]=process.argv.slice(2);
if(!base||!dataPath||!out)throw Error('usage: synthetic_wafers_check.mjs <url> <json> <outdir>');
mkdirSync(out,{recursive:true});
const raw=readFileSync(dataPath),data=JSON.parse(raw),failures=[],results=[];
const check=(ok,msg)=>{if(!ok)failures.push(msg)};
const browser=await chromium.launch();
for(const width of [360,1440]){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(String(e)));
 await page.goto(base,{waitUntil:'networkidle'});
 await page.locator('#app').waitFor({state:'visible'});
 const first=await page.locator('#filterCount').innerText();
 check(first.includes('1,305')&&first.includes('불량 14'),width+' first wafer counts');
 check(await page.locator('.notice').innerText()==='합성 웨이퍼 검사 데이터 · 실제 팹 측정·기존 연구 결과 아님',width+' provenance notice');
 for(let w=0;w<3;w++){
  await page.locator(`[data-wafer="${w}"]`).click();
  check((await page.locator('#filterCount').innerText()).includes('불량 '+data.wafers[w].summary.fail),width+' wafer summary '+w);
  await page.selectOption('#binFilter','fail');
  const rowIDs=await page.locator('#dieRows tr th').allTextContents();
  const expected=data.wafers[w].dies.filter(d=>d.bin==='fail').slice(0,25).map(d=>d.die_id);
  check(JSON.stringify(rowIDs)===JSON.stringify(expected),width+' failed row values '+w);
  await page.selectOption('#binFilter','all');
 }
 await page.locator('[data-wafer="2"]').click();
 await page.selectOption('#defectFilter','scratch');
 const detectionCount=data.wafers[2].dies.filter(d=>d.detections.includes('scratch')).length;
 check((await page.locator('#filterCount').innerText()).includes('표시 '+detectionCount+' /'),width+' observed mechanism filter');
 await page.locator('[data-view="truth"]').click();
 const truthCount=data.wafers[2].dies.filter(d=>d.defects.includes('scratch')).length;
 check((await page.locator('#filterCount').innerText()).includes('표시 '+truthCount+' /'),width+' truth mechanism filter');
 await page.selectOption('#defectFilter','all');
 await page.locator('[data-view="bin"]').click();
 await page.locator('#waferMap').focus();await page.keyboard.press('ArrowRight');
 check((await page.locator('#mapHelp').innerText()).includes('W03-'),width+' map keyboard selection');
 await page.keyboard.press('Enter');
 check(await page.evaluate(()=>document.activeElement.id==='detailTitle'),width+' map enter focus');
 const bad=data.wafers[2].dies.find(d=>d.bin==='fail');
 await page.fill('#dieSearch',bad.die_id);
 await page.locator('#dieRows [data-die]').click();
 const detail=await page.locator('#dieDetail').innerText();
 check(detail.includes(bad.die_id)&&detail.includes('불량')&&detail.includes('센서로 검증되지 않음'),width+' die detail provenance');
 check(await page.evaluate(()=>document.activeElement.id==='detailTitle'),width+' table button focus');
 await page.fill('#dieSearch','no-match');
 check((await page.locator('#dieRows').innerText()).includes('일치하는 다이가 없습니다'),width+' empty search');
 check(await page.locator('#nextPage').isDisabled()&&await page.locator('#prevPage').isDisabled(),width+' empty paging');
 await page.fill('#dieSearch','');
 await page.locator('#nextPage').click();
 check((await page.locator('#pageInfo').innerText()).startsWith('26–50'),width+' paging');
 await page.locator('#prevPage').click();
 const downloadEvent=page.waitForEvent('download');await page.locator('#download').click();
 const download=await downloadEvent;const downloaded=readFileSync(await download.path());
 check(downloaded.equals(raw),width+' full JSON export exact bytes');
 await page.locator('.catalog summary').click();
 check(await page.locator('#catalogRows tr').count()===34,width+' research catalog complete');
 const dims=await page.evaluate(()=>({overflow:document.documentElement.scrollWidth-innerWidth,canvas:document.querySelector('canvas').getBoundingClientRect().width}));
 check(dims.overflow<=0,width+' document overflow');
 const small=await page.locator('button:visible,select:visible,input:visible').evaluateAll(nodes=>nodes.filter(n=>n.getBoundingClientRect().height<44).map(n=>n.id||n.textContent));
 check(!small.length,width+' small controls '+small.join(','));
 await page.locator('.catalog summary').click();
 if(process.env.CAPTURE){
  await page.locator('[data-wafer="1"]').click();
  await page.screenshot({path:out+'/'+width+'-overview.png',fullPage:true});
  await page.locator('#waferMap').screenshot({path:out+'/'+width+'-wafer.png'});
 }
 check(!errors.length,width+' runtime errors '+errors.join(','));
 results.push({width,...dims,observed_scratch:detectionCount,ground_truth_scratch:truthCount,errors});
 await page.close();
}
// The error surface exposes a recovery action and restores a working full dataset.
const recovery=await browser.newPage();let attempts=0;
await recovery.route('**/data/synthetic-wafers.json',r=>++attempts===1?r.fulfill({status:503,body:'unavailable'}):r.fulfill({body:raw,contentType:'application/json'}));
await recovery.goto(base);await recovery.getByRole('button',{name:'다시 시도',exact:true}).click();
await recovery.locator('#app').waitFor({state:'visible'});check(attempts===2,'recovery retries once');
await browser.close();
const report={failures,results,json_sha256:createHash('sha256').update(raw).digest('hex'),recovery_attempts:attempts};
writeFileSync(out+'/validation.json',JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report));
if(failures.length)process.exit(1);

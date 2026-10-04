// Checks rendered physical scale without treating illustrative dies as scientific observations.
// PLAYWRIGHT_MODULE=<playwright> node web/tests/wafer_geometry_check.mjs <url> <report>
import {createRequire} from 'node:module';import {writeFileSync} from 'node:fs';
const require=createRequire(import.meta.url);const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const [url,out]=process.argv.slice(2);const browser=await chromium.launch(),results={},failures=[];
for(const width of [360,390,768,1440]){
 const p=await browser.newPage({viewport:{width,height:900}});await p.goto(url,{waitUntil:'networkidle'});await p.waitForSelector('#heatmap [data-key]');await p.click('#finalButton');
 const first=p.locator('#heatmap [data-key]').first();await first.focus();await p.keyboard.press('Tab');
 results[width]=await p.evaluate(()=>{
  const svg=document.querySelector('.wafer-geometry'),map=document.querySelector('#heatmap'),sr=svg.getBoundingClientRect(),mr=map.getBoundingClientRect();
  const path=document.querySelector('.wafer-dies').getAttribute('d');
  const rects=[...path.matchAll(/M([\d.-]+) ([\d.-]+)h([\d.]+)v([\d.]+)h-([\d.]+)Z/g)].map(m=>({x:+m[1],y:+m[2],w:+m[3],h:+m[4],back:+m[5]}));
  const geometricFailures=[];
  for(const d of rects){
   if(d.w!==8||d.h!==6||d.back!==8)geometricFailures.push('physical die dimensions');
   if([[d.x,d.y],[d.x+d.w,d.y],[d.x+d.w,d.y+d.h],[d.x,d.y+d.h]].some(([x,y])=>Math.hypot(x-150,y-150)>147.02))geometricFailures.push('edge exclusion');
  }
  const xs=[...new Set(rects.map(d=>d.x))].sort((a,b)=>a-b),ys=[...new Set(rects.map(d=>d.y))].sort((a,b)=>a-b);
  for(let i=1;i<xs.length;i++)if(Math.abs(xs[i]-xs[i-1]-8.08)>.011)geometricFailures.push('horizontal scribe pitch');
  for(let i=1;i<ys.length;i++)if(Math.abs(ys[i]-ys[i-1]-6.08)>.011)geometricFailures.push('vertical scribe pitch');
  const buttons=[...map.querySelectorAll('[data-key]')],targets=buttons.map(b=>{const r=b.getBoundingClientRect(),d=b.querySelector('.site-die').getBoundingClientRect();return {key:b.dataset.key,left:r.left,right:r.right,top:r.top,bottom:r.bottom,width:r.width,height:r.height,physicalWidth:d.width/sr.width*300,physicalHeight:d.height/sr.height*300,x:parseFloat(b.style.getPropertyValue('--site-x'))*3,y:parseFloat(b.style.getPropertyValue('--site-y'))*3}});
  for(const t of targets){
   if(t.width<24||t.height<24)geometricFailures.push('small hit target '+t.key);
   if(Math.abs(t.physicalWidth-8)>.1||Math.abs(t.physicalHeight-6)>.1)geometricFailures.push('rendered die scale '+t.key);
   if(!rects.some(d=>Math.abs(d.x+d.w/2-t.x)<.011&&Math.abs(d.y+d.h/2-t.y)<.011))geometricFailures.push('linked site off lattice '+t.key);
  }
  for(let i=0;i<targets.length;i++)for(let j=i+1;j<targets.length;j++){
   const a=targets[i],b=targets[j];if(a.left<b.right-.1&&a.right>b.left+.1&&a.top<b.bottom-.1&&a.bottom>b.top+.1)geometricFailures.push('overlapping hit targets');
  }
  const focus=getComputedStyle(document.activeElement),note=document.querySelector('.heatmap-content').innerText;
  return {geometricFailures:[...new Set(geometricFailures)],dieCount:rects.length,columns:xs.length,rows:ys.length,targets:targets.length,viewBox:svg.getAttribute('viewBox'),ariaHidden:svg.getAttribute('aria-hidden'),backgroundDataKeys:svg.querySelectorAll('[data-key]').length,svgNodes:svg.querySelectorAll('*').length,overflow:document.documentElement.scrollWidth-innerWidth,aspect:mr.width/mr.height,focus:{style:focus.outlineStyle,width:focus.outlineWidth},dimensionNote:note,renderedDie:targets[0]};
 });const r=results[width];for(const d of r.geometricFailures)failures.push(width+' '+d);
 if(r.dieCount<1000||r.columns<30||r.rows<40)failures.push(width+' sparse geometry');
 if(r.targets!==35||r.backgroundDataKeys!==0||r.ariaHidden!=='true'||r.svgNodes>20)failures.push(width+' geometry/data separation');
 if(r.overflow>0||Math.abs(r.aspect-1)>.01)failures.push(width+' layout');
 if(r.focus.style==='none'||parseFloat(r.focus.width)<2)failures.push(width+' focus');
 if(!/300\s*mm/.test(r.dimensionNote)||!/8\s*[×x]\s*6\s*mm/.test(r.dimensionNote)||!/형상 예시/.test(r.dimensionNote))failures.push(width+' missing dimensional disclosure');
 await p.close();
}await browser.close();writeFileSync(out,JSON.stringify({results,failures},null,2));console.log(JSON.stringify({failures,geometry:results[360]}));process.exit(failures.length?1:0);

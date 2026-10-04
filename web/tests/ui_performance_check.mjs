// Local rendering-work and freshness check for web/ (not shipped, not run by default).
//
//   PLAYWRIGHT_MODULE=<path to node_modules/playwright/index.js> \
//   node web/tests/ui_performance_check.mjs <base-url> <primary-snapshot.json> [out.json]
//
// <base-url> is scripts/serve_app.py --read-only --snapshot <primary-snapshot.json>
// (local mode). Optional environment:
//   ALT_SNAPSHOT  second snapshot used for data replacement
//                 (default web/tests/fixtures/snapshot.fixture.json)
//   APP_JS        serve this file as app.en.js (the page script) instead (e.g. a baseline build, to compare)
//   REPS          fresh browser contexts per timing sample (default 5)
//
// Rendering work is measured from the outside: a MutationObserver attributes DOM
// mutations to the view section they land in, and Chrome's Performance metrics give
// script/layout/style time. Assertions check that hidden views are not rebuilt and
// that a view opened after a data replacement or replay step equals a fresh render.
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const [base, primaryPath, outPath] = process.argv.slice(2);
if (!base || !primaryPath) { console.error('usage: ui_performance_check.mjs <base-url> <primary-snapshot.json> [out.json]'); process.exit(2); }
const altPath = process.env.ALT_SNAPSHOT || fileURLToPath(new URL('./fixtures/snapshot.fixture.json', import.meta.url));
const appJs = process.env.APP_JS ? readFileSync(process.env.APP_JS) : null;
const REPS = Number(process.env.REPS || 5);
const sha = b => createHash('sha256').update(b).digest('hex');
const SNAP = { primary: readFileSync(primaryPath), alt: readFileSync(altPath) };
const SHA = { primary: sha(SNAP.primary), alt: sha(SNAP.alt) };

const failures = [];
const check = (ok, msg) => { if (!ok) failures.push(msg); };
const median = a => { const s = [...a].sort((x, y) => x - y); return s.length ? +(s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2).toFixed(3) : null; };
const short = s => createHash('sha256').update(s).digest('hex').slice(0, 16);

// Counts mutation records per region. A region is a view section; inside the lab the
// replay/advance controls (linked to the top-bar button) are counted separately.
const OBSERVER = () => {
  const counts = {};
  const regionOf = n => {
    const el = n.nodeType === 1 ? n : n.parentElement;
    if (!el) return 'other';
    if (el.closest('.next-action, .loop-controls')) return 'lab-controls';
    const v = el.closest('[id^="view-"]');
    return v ? v.id.slice(5) : 'chrome';
  };
  const sub = { benchmarkDetails: '#benchmarkDetails', benchmarkTable: '#benchmarkTable', benchmarkConclusion: '#benchmarkConclusion', chartLegendChildren: '#chartLegend', chart: '#benchmarkChart' };
  const handle = list => {
    for (const r of list) {
      const k = regionOf(r.target);
      counts[k] = (counts[k] || 0) + 1;
      const el = r.target.nodeType === 1 ? r.target : r.target.parentElement;
      for (const [name, sel] of Object.entries(sub)) {
        if (name === 'chartLegendChildren' && r.type !== 'childList') continue;
        if (el && el.closest(sel)) counts[name] = (counts[name] || 0) + 1;
      }
    }
  };
  const obs = new MutationObserver(handle);
  obs.observe(document, { subtree: true, childList: true, characterData: true, attributes: true });
  window.__mut = {
    // Pending records are delivered synchronously before the counters are read.
    take: () => { handle(obs.takeRecords()); const c = { ...counts }; for (const k in counts) delete counts[k]; return c; },
    flush: () => new Promise(r => setTimeout(r, 0)),
  };
  // Parsing index.html is not rendering work; count from the first snapshot load on.
  document.addEventListener('DOMContentLoaded', () => window.__mut.take());
};

const browser = await chromium.launch();

// state.current selects which snapshot the routed local API serves.
async function openPage({ mode = 'local', snapshot = 'primary', viewport = [1440, 900] } = {}) {
  const context = await browser.newContext({ viewport: { width: viewport[0], height: viewport[1] }, acceptDownloads: true });
  const page = await context.newPage();
  const ctl = { current: snapshot };
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !(mode === 'static' && /status of 404/.test(m.text()))) errors.push(m.text()); });
  if (appJs) await page.route('**/app.en.js', r => r.fulfill({ body: appJs, contentType: 'text/javascript; charset=utf-8' }));
  if (mode === 'static') {
    await page.route('**/api/job', r => r.fulfill({ status: 404, body: 'not found' }));
    await page.route('**/data/snapshot.json', r => r.fulfill({ body: SNAP[ctl.current], contentType: 'application/json' }));
  } else {
    await page.route('**/api/job', async r => {
      const res = await r.fetch(); const j = await res.json();
      j.snapshot = { available: true, sha256: SHA[ctl.current], bytes: SNAP[ctl.current].length };
      await r.fulfill({ response: res, body: JSON.stringify(j), contentType: 'application/json' });
    });
    await page.route('**/api/snapshot', r => r.fulfill({ body: SNAP[ctl.current], contentType: 'application/json' }));
  }
  await page.addInitScript(`(${OBSERVER.toString()})()`);
  const cdp = await context.newCDPSession(page);
  await cdp.send('Performance.enable');
  const t0 = Date.now();
  await page.goto(base, { waitUntil: 'load' });
  await waitSnapshot(page, ctl.current);
  const readyMs = Date.now() - t0;
  return { context, page, ctl, errors, cdp, readyMs };
}
const waitSnapshot = (page, which) => page.waitForFunction(p => document.getElementById('footerMeta').textContent.includes('snapshot sha256 ' + p), SHA[which].slice(0, 12), { timeout: 25000 });
async function perf(cdp) { const { metrics } = await cdp.send('Performance.getMetrics'); const g = n => metrics.find(m => m.name === n)?.value ?? 0; return { script: g('ScriptDuration'), layout: g('LayoutDuration'), style: g('RecalcStyleDuration') }; }
const perfDiff = (a, b) => ({ scriptMs: +((b.script - a.script) * 1000).toFixed(2), layoutMs: +((b.layout - a.layout) * 1000).toFixed(2), styleMs: +((b.style - a.style) * 1000).toFixed(2) });
const take = page => page.evaluate(() => window.__mut.take());
// Synchronous click + forced layout, timed inside the page.
const timedClick = (page, selector) => page.evaluate(sel => { const el = document.querySelector(sel); const t = performance.now(); el.click(); document.body.getBoundingClientRect(); return performance.now() - t; }, selector);
// An emptied class list serialises as class="" after classList.toggle; it renders the
// same as no attribute, so it is not a content difference.
const html = (page, sel) => page.evaluate(s => document.querySelector(s).innerHTML.replaceAll(' class=""', ''), sel);
const viewHtml = async page => ({ lab: await html(page, '#view-lab'), benchmark: await html(page, '#view-benchmark'), records: await html(page, '#view-records') });

const result = { app: process.env.APP_JS || 'served app.en.js', primarySha: SHA.primary, altSha: SHA.alt };

// 1. Initial load (fresh context each): time to snapshot shown, DOM work per view, CPU.
{
  const ready = [], muts = [], cpu = [];
  for (let i = 0; i < REPS; i++) {
    const s = await openPage();
    ready.push(s.readyMs);
    await s.page.evaluate(() => window.__mut.flush());
    muts.push(await take(s.page));
    const p = await perf(s.cdp); cpu.push(p.script * 1000);
    check(!s.errors.length, `initial load errors: ${s.errors.join(' | ')}`);
    await s.context.close();
  }
  result.initial = { readyMsMedian: median(ready), scriptMsMedian: median(cpu), mutationsFirstRun: muts[0] };
  check(!muts[0].benchmark && !muts[0].records, `initial load rebuilt hidden views: ${JSON.stringify(muts[0])}`);
}

// Fresh reference renders of each view for primary and alt snapshots (stage 0, defaults).
async function reference(which) {
  const s = await openPage({ snapshot: which });
  if (await s.page.$('[data-step="0"]')) await s.page.click('[data-step="0"]');
  const ref = { lab: await html(s.page, '#view-lab') };
  await s.page.click('[data-view=benchmark]'); ref.benchmark = await html(s.page, '#view-benchmark');
  await s.page.click('[data-view=records]'); ref.records = await html(s.page, '#view-records');
  check(!s.errors.length, `reference ${which} errors: ${s.errors.join(' | ')}`);
  await s.context.close();
  return ref;
}
const REF = { primary: await reference('primary'), alt: await reference('alt') };
// Content hashes of the reference views, for comparing two app builds' output.
result.referenceHashes = Object.fromEntries(Object.entries(REF).map(([k, v]) => [k, Object.fromEntries(Object.entries(v).map(([n, h]) => [n, short(h)]))]));

// 2. Replay, view switching, legend and records in one fresh page.
{
  const s = await openPage();
  const { page, cdp } = s;
  const steps = await page.$$eval('[data-step]', b => b.length);
  // Replay in the lab: only the lab may change.
  await take(page);
  let p0 = await perf(cdp); const replayMs = [];
  for (let r = 0; r < 4; r++) for (let i = 0; i < steps; i++) replayMs.push(await timedClick(page, `[data-step="${i}"]`));
  const replayMut = await take(page); let p1 = await perf(cdp);
  result.replay = { steps, clicks: replayMs.length, clickMsMedian: median(replayMs), mutations: replayMut, cpu: perfDiff(p0, p1) };
  check(!replayMut.benchmark && !replayMut.records, `replay mutated hidden views: ${JSON.stringify(replayMut)}`);

  // Recorded replay playing, then the records view opened: the replay stops, hidden
  // views stay untouched, and the lab reopens at the stage it had reached.
  await page.click('[data-step="0"]');
  await page.click('#playButton');
  await page.waitForTimeout(2400);
  await page.click('[data-view=records]');
  await take(page);
  await page.waitForTimeout(2600);
  const hiddenReplay = await take(page);
  result.replayWhileRecordsOpen = { mutations: hiddenReplay };
  check(!hiddenReplay.lab && !hiddenReplay.benchmark && !hiddenReplay['lab-controls'], `replay continued in hidden views: ${JSON.stringify(hiddenReplay)}`);
  await page.click('[data-view=lab]');
  const labAfter = await html(page, '#view-lab');
  const stage = await page.$eval('#steps [aria-current=step]', b => b.dataset.step);
  await page.click(`[data-step="${stage}"]`);
  check(labAfter === await html(page, '#view-lab'), 'lab reopened after replay differs from a direct render of the same stage');
  check(steps < 2 || stage !== '0', 'replay did not advance before the view switch');
  await page.click('[data-step="0"]');

  // View switching.
  await take(page);
  p0 = await perf(cdp); const switchMs = { lab: [], benchmark: [], records: [] };
  for (let r = 0; r < 6; r++) for (const v of ['benchmark', 'records', 'lab']) switchMs[v].push(await timedClick(page, `[data-view=${v}]`));
  const switchMut = await take(page); p1 = await perf(cdp);
  result.viewSwitch = { cycles: 6, msMedian: Object.fromEntries(Object.entries(switchMs).map(([k, v]) => [k, median(v)])), mutations: switchMut, cpu: perfDiff(p0, p1) };
  const now = await viewHtml(page);
  for (const v of ['lab', 'benchmark', 'records']) check(now[v] === REF.primary[v], `${v} after switching differs from a fresh render`);

  // Legend toggles: chart only; report details, table and conclusion untouched.
  await page.click('[data-view=benchmark]');
  const svg0 = await html(page, '#benchmarkChart'), curves0 = await page.$$eval('#benchmarkChart path.curve', x => x.length);
  const policies = await page.$$eval('#chartLegend [data-policy]', b => b.map(x => x.dataset.policy));
  await take(page);
  p0 = await perf(cdp); const legendMs = []; const offHashes = [];
  for (let r = 0; r < 3; r++) for (const id of policies) {
    legendMs.push(await timedClick(page, `#chartLegend [data-policy="${id}"]`));
    if (r === 0) {
      offHashes.push(short(await html(page, '#benchmarkChart')));
      check(await page.getAttribute(`#chartLegend [data-policy="${id}"]`, 'aria-pressed') === 'false', `legend ${id} aria-pressed after hide`);
      check(await page.$$eval('#benchmarkChart path.curve', x => x.length) < curves0 || !curves0, `legend ${id} hide did not remove its mean curve`);
    }
    legendMs.push(await timedClick(page, `#chartLegend [data-policy="${id}"]`));
  }
  const legendMut = await take(page); p1 = await perf(cdp);
  result.legend = { toggles: legendMs.length, clickMsMedian: median(legendMs), mutations: legendMut, cpu: perfDiff(p0, p1), hiddenChartHashes: offHashes, chartHash: short(svg0) };
  check(await html(page, '#benchmarkChart') === svg0, 'chart differs after toggling every policy off and on');
  check(!legendMut.benchmarkDetails && !legendMut.benchmarkTable && !legendMut.benchmarkConclusion && !legendMut.chartLegendChildren, `legend toggle rebuilt invariant benchmark parts: ${JSON.stringify(legendMut)}`);
  check(await html(page, '#view-benchmark') === REF.primary.benchmark, 'benchmark view differs after legend round trip');

  // Records input and filters.
  await page.click('[data-view=records]');
  const recMs = [];
  for (const q of ['R', 'SS', 'x-none', '']) {
    recMs.push(await page.evaluate(q => { const el = document.getElementById('recordSearch'); el.value = q; const t = performance.now(); el.dispatchEvent(new Event('input')); document.body.getBoundingClientRect(); return performance.now() - t; }, q));
    const r = await page.evaluate(() => ({ rows: document.querySelectorAll('#recordsTable tr').length, count: document.getElementById('recordCount').innerText, empty: !document.getElementById('emptyRecords').hidden }));
    check(r.count.startsWith(r.rows + ' ') && r.empty === (r.rows === 0), `records "${q}": ${JSON.stringify(r)}`);
  }
  for (const f of await page.$$eval('#recordFilter option', o => o.map(x => x.value))) {
    await page.selectOption('#recordFilter', f);
    const r = await page.evaluate(() => ({ rows: document.querySelectorAll('#recordsTable tr').length, count: document.getElementById('recordCount').innerText }));
    check(r.count.startsWith(r.rows + ' '), `filter ${f}: ${JSON.stringify(r)}`);
  }
  await page.selectOption('#recordFilter', 'all');
  result.records = { inputMsMedian: median(recMs) };
  check(await html(page, '#view-records') === REF.primary.records, 'records view differs after clearing search and filter');

  // Dialog and keyboard.
  await page.click('[data-view=lab]');
  await page.focus('#heatmap .heat-cell');
  await page.keyboard.press('Enter');
  check(await page.evaluate(() => document.querySelector('#heatmap .heat-cell.selected') !== null), 'keyboard heat cell selection');
  await page.click('#protocolButton');
  const open = await page.evaluate(() => document.getElementById('detailDialog').open);
  await page.keyboard.press('Escape');
  check(open && await page.evaluate(() => !document.getElementById('detailDialog').open), 'dialog open/Escape');
  check(!s.errors.length, `interaction errors: ${s.errors.join(' | ')}`);
  await s.context.close();
}

// 3. Data replacement while another view is open, then each view opened afterwards.
{
  const s = await openPage();
  const { page } = s;
  await page.click('[data-view=records]');
  await take(page);
  s.ctl.current = 'alt';
  await waitSnapshot(page, 'alt');
  await page.evaluate(() => window.__mut.flush());
  const swap = await take(page);
  result.replaceWhileRecordsOpen = { mutations: swap, activeView: await page.$eval('[data-view][aria-current=page]', b => b.dataset.view) };
  check(result.replaceWhileRecordsOpen.activeView === 'records', 'active tab changed by data replacement');
  check(!swap.lab && !swap.benchmark, `data replacement rebuilt hidden views: ${JSON.stringify(swap)}`);
  check(await html(page, '#view-records') === REF.alt.records, 'records (shown) not refreshed to the new snapshot');
  await page.click('[data-view=benchmark]');
  check(await html(page, '#view-benchmark') === REF.alt.benchmark, 'benchmark opened after replacement shows stale or different content');
  await page.click('[data-view=lab]');
  if (await page.$('[data-step="0"]')) await page.click('[data-step="0"]');
  check(await html(page, '#view-lab') === REF.alt.lab, 'lab opened after replacement shows stale or different content');
  // Back to the primary snapshot while the benchmark is open.
  await page.click('[data-view=benchmark]');
  s.ctl.current = 'primary';
  await waitSnapshot(page, 'primary');
  check(await html(page, '#view-benchmark') === REF.primary.benchmark, 'shown benchmark not refreshed on replacement');
  await page.click('[data-view=records]');
  check(await html(page, '#view-records') === REF.primary.records, 'records opened after second replacement stale');
  // Export is the displayed snapshot, byte for byte.
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#exportButton')]);
  const bytes = readFileSync(await dl.path());
  result.exportLocal = { sha256: sha(bytes), equal: bytes.equals(SNAP.primary) };
  check(result.exportLocal.equal, 'local export differs from the displayed snapshot');
  check(!s.errors.length, `replacement errors: ${s.errors.join(' | ')}`);
  await s.context.close();
}

// 4. Static mode: shown views match local references and export is exact.
{
  const s = await openPage({ mode: 'static' });
  const { page } = s;
  const notice = await page.$eval('#noticeStack', n => n.innerText);
  check(/Recorded-run replay/.test(notice), 'static mode notice missing');
  await page.click('[data-view=benchmark]');
  const chart = await page.$$eval('#benchmarkChart path', x => x.length);
  check(chart > 0, 'static benchmark chart empty');
  const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#exportButton')]);
  const bytes = readFileSync(await dl.path());
  result.exportStatic = { sha256: sha(bytes), equal: bytes.equals(SNAP.primary) };
  check(result.exportStatic.equal, 'static export differs from the displayed snapshot');
  check(!s.errors.length, `static errors: ${s.errors.join(' | ')}`);
  await s.context.close();
}

await browser.close();
const out = { result, failures };
if (outPath) writeFileSync(outPath, JSON.stringify(out, null, 1));
console.log(JSON.stringify(out, null, 1));
process.exit(failures.length ? 1 : 0);

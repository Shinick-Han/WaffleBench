// Local browser check for web/ (documented fallback when Aside cannot set viewports).
// Not part of the shipped app and not run by default.
//
//   PLAYWRIGHT_MODULE=<path to node_modules/playwright/index.js> \
//   node web/tests/ui_check.mjs <base-url> <out-dir> [expect=fixture|empty|missing|real]
//
// Serve the fixture with:
//   python scripts/serve_app.py --port 8791 --read-only --snapshot web/tests/fixtures/snapshot.fixture.json
// Serve the static empty build with:
//   python -m http.server 8792 --bind 127.0.0.1 --directory web
// missing: serve_app.py --read-only --snapshot <nonexistent path> (local mode, no file)
// real:    serve_app.py --read-only --snapshot <cli root>/snapshot.json (CLI output)
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const [base, outDir = '.', expect = 'fixture'] = process.argv.slice(2);
const failures = [];
const check = (ok, msg) => { if (!ok) failures.push(msg); };

const browser = await chromium.launch();
const results = {};
for (const [w, h] of [[390, 844], [1440, 900]]) {
  const page = await browser.newPage({ viewport: { width: w, height: h } });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(base, { waitUntil: 'networkidle' });
  await page.waitForTimeout(600);
  const r = { errors };
  for (const view of ['lab', 'benchmark', 'records']) {
    await page.click(`[data-view=${view}]`);
    await page.waitForTimeout(200);
    r[view] = await page.evaluate(() => ({
      overflow: document.documentElement.scrollWidth - window.innerWidth,
      notices: document.getElementById('noticeStack').innerText,
    }));
    check(r[view].overflow <= 0, `${w}px ${view}: horizontal overflow ${r[view].overflow}px`);
    await page.screenshot({ path: `${outDir}/${expect}-${view}-${w}.png`, fullPage: true });
    if (view === 'benchmark') {
      r.axisPx = await page.evaluate(() => [...document.querySelectorAll('#benchmarkChart text')].map(t => t.getBoundingClientRect().height));
      if (r.axisPx.length) check(Math.min(...r.axisPx) >= 10, `${w}px axis text too small: ${Math.min(...r.axisPx)}`);
    }
  }
  await page.click('[data-view=lab]');
  // Keyboard: focus a heat cell, press Enter, it becomes selected.
  await page.focus('#heatmap .heat-cell');
  await page.keyboard.press('Enter');
  r.keyboardSelected = await page.evaluate(() => document.activeElement?.getAttribute('aria-pressed') ?? document.querySelector('#heatmap .heat-cell.selected') !== null);
  // Modal: open the protocol dialog, Escape closes it and focus returns.
  await page.click('#protocolButton');
  r.dialogOpen = await page.evaluate(() => document.getElementById('detailDialog').open);
  await page.keyboard.press('Escape');
  r.dialogClosed = await page.evaluate(() => !document.getElementById('detailDialog').open);
  check(r.dialogOpen && r.dialogClosed, `${w}px dialog open/Escape failed`);
  // Records: search and filter agree with the row count text.
  await page.click('[data-view=records]');
  await page.fill('#recordSearch', expect === 'fixture' ? 'R0' : 'x');
  await page.selectOption('#recordCorner', 'SS');
  r.records = await page.evaluate(() => ({ rows: document.querySelectorAll('#recordsTable tr').length, count: document.getElementById('recordCount').innerText, empty: !document.getElementById('emptyRecords').hidden }));
  check(r.records.count.startsWith(String(r.records.rows) + ' '), `${w}px record count mismatch ${JSON.stringify(r.records)}`);
  if (expect === 'empty') {
    check(r.records.rows === 0 && r.records.empty, 'empty snapshot should show no rows and the empty state');
    check(/No results yet/.test(r.lab.notices), 'empty snapshot notice missing');
    check(/Recorded-run replay|Local mode/.test(r.lab.notices), 'mode notice missing');
  } else if (expect === 'missing') {
    check(r.records.rows === 0 && r.records.empty, 'missing snapshot should show no rows');
    check(/snapshot file does not exist yet/.test(r.lab.notices) && /Local mode/.test(r.lab.notices), `missing-snapshot notice: ${r.lab.notices}`);
  } else if (expect === 'real') {
    check(!/data_mode=/.test(r.lab.notices), 'real snapshot must not show the fixture banner');
  } else {
    check(/data_mode=fixture/.test(r.lab.notices), 'fixture banner missing');
  }
  // Replay: at every stage no trace row or summary link may expose a result of the
  // current or a later decision (generic; holds for fixture and real snapshots).
  await page.click('[data-view=lab]');
  const D = await page.evaluate(() => state.m ? state.m.decisions.length : 0);
  r.stages = [];
  for (let i = 0; D > 0 && i <= D; i++) {
    await page.click(`[data-step="${i}"]`);
    await page.waitForTimeout(80);
    const s = await page.evaluate(i => {
      const m = state.m, future = new Set(m.decisions.slice(i).map(d => d.observed_result_id).filter(x => x != null).map(String));
      const shown = [...document.querySelectorAll('#traceBody [data-evidence], #decisionSummary [data-evidence]')].map(b => b.dataset.evidence);
      const vis = visibleTrace();
      return { seqs: vis.map(t => t.sequence), leaked: vis.flatMap(t => t.result_ids || []).concat(shown).filter(id => future.has(String(id))),
        summary: document.getElementById('decisionSummary').innerText, shown, subtitle: document.getElementById('traceSubtitle').innerText, total: m.trace.length };
    }, i);
    check(i === D || s.leaked.length === 0, `${w}px stage ${i}: future results visible ${s.leaked}`);
    check(i < D || s.seqs.length === s.total, `${w}px final stage must show the full trace`);
    r.stages.push(s);
  }
  if (expect === 'fixture') {
    const [s0, s1, s2] = r.stages;
    const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    check(eq(s0.seqs, [40, 45, 48, 50, 55]), `stage 0 trace ${s0.seqs}`);
    check(eq(s1.seqs, [40, 45, 48, 50, 55, 70, 75, 98, 100]), `stage 1 trace ${s1.seqs}`);
    check(s2.seqs.length === 12, `final trace ${s2.seqs}`);
    check(/Decision #1 · at selection/.test(s0.summary) && !/Updated next candidates/.test(s0.summary), `stage 0 summary ${s0.summary}`);
    check(/Observed result of decision #1/.test(s1.summary) && /Updated next candidates/.test(s1.summary) && s1.shown.includes('FIXTURE-R04'), `stage 1 summary ${s1.summary}`);
    check(/Observed result of decision #2/.test(s2.summary) && /No post_result_update record/.test(s2.summary), `final summary ${s2.summary}`);
    check(/Omnigent 3 ·/.test(s0.subtitle), `omnigent subtitle ${s0.subtitle}`);
    await page.click('[data-view=benchmark]');
    await page.waitForTimeout(150);
    // Paired seed differences sit in a closed disclosure; expand it so innerText includes them.
    await page.evaluate(() => document.querySelectorAll('#benchmarkConclusion details').forEach(d => { d.open = true; }));
    r.bench = await page.evaluate(() => ({
      rows: [...document.querySelectorAll('#benchmarkTable tr')].map(tr => [...tr.cells].map(c => c.innerText)),
      dashed: document.querySelectorAll('#benchmarkChart path.run-line.incomplete').length,
      curves: document.querySelectorAll('#benchmarkChart path.curve').length,
      conclusion: document.getElementById('benchmarkConclusion').innerText,
    }));
    const row = id => r.bench.rows.find(c => c[0].includes(id)) || [];
    check(row('adaptive')[1] === '6.00', `adaptive mean must be the exported complete-run mean 6.00, got ${row('adaptive')[1]}`);
    check(/no mean/.test(row('space filling')[1] || '') && /^0\/3 complete/.test(row('space filling')[3] || ''), `null mean row ${row('space filling')}`);
    check(/seed 1003: incomplete/.test(row('adaptive')[3] || ''), `incomplete status not shown ${row('adaptive')}`);
    check(r.bench.dashed === 6 && r.bench.curves === 3, `chart dashed ${r.bench.dashed} curves ${r.bench.curves}`);
    check(/seed 1001: \+2/.test(r.bench.conclusion) && /null · not computed/.test(r.bench.conclusion), 'primary pairs/CI rendering');
  }
  // On a static host the ./api/job mode probe 404s by design; a missing local snapshot
  // answers 404 as well. Those are the only errors tolerated.
  const unexpected = errors.filter(e => !((expect === 'empty' || expect === 'missing') && /status of 404/.test(e)));
  check(unexpected.length === 0, `${w}px console errors: ${unexpected.join(' | ')}`);
  results[w] = r;
  await page.close();
}
await browser.close();
console.log(JSON.stringify({ results, failures }, null, 1));
process.exit(failures.length ? 1 : 0);

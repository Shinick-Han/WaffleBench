// Focused usability check for web/ (not shipped, not run by default).
//
//   PLAYWRIGHT_MODULE=<path to node_modules/playwright/index.js> \
//   node web/tests/ui_polish_check.mjs <base-url> <real-snapshot.json>
//
// <base-url> serves web/ (any serve_app.py instance). The page is driven in static
// mode: ./api/job is answered 404 and ./data/snapshot.json is routed to the chosen
// snapshot (real, the fixture, or an empty one), so no server state is involved.
// Covers records sort/reset, dialog focus return, keyboard focus after map/step/legend
// clicks, nav names at tablet/mobile widths, evidence disclosures, null/partial
// benchmark rendering, the load-retry path, and document overflow at four widths.
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const [base, realPath] = process.argv.slice(2);
if (!base || !realPath) { console.error('usage: ui_polish_check.mjs <base-url> <real-snapshot.json>'); process.exit(2); }
const SNAP = {
  real: readFileSync(realPath),
  fixture: readFileSync(fileURLToPath(new URL('./fixtures/snapshot.fixture.json', import.meta.url))),
  empty: Buffer.from(JSON.stringify({ schema_version: 1, data_mode: 'real', generated_at: null, limitations: [], run: null, model: null, observations: [], evaluations: [], decisions: [], trace: [], coverage: [], benchmark: null, cost: null })),
};
const failures = [];
const check = (ok, msg) => { if (!ok) failures.push(msg); };
const browser = await chromium.launch();

async function open(which, [w, h] = [1440, 900], { failFirst = false } = {}) {
  const page = await browser.newPage({ viewport: { width: w, height: h } });
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('console', m => { if (m.type() === 'error' && !/status of 404|ERR_FAILED|Failed to fetch/.test(m.text())) errors.push(m.text()); });
  let n = 0;
  await page.route('**/api/**', r => r.fulfill({ status: 404, body: 'not found' }));
  await page.route('**/data/snapshot.json', r => (failFirst && n++ === 0) ? r.abort('failed') : r.fulfill({ body: SNAP[which], contentType: 'application/json' }));
  await page.goto(base, { waitUntil: 'networkidle' });
  await page.waitForTimeout(300);
  return { page, errors };
}
const active = page => page.evaluate(() => { const a = document.activeElement; return a ? (a.id || a.dataset.key || a.dataset.step || a.dataset.policy || a.dataset.evidence || a.dataset.candidate || a.tagName) : null; });
const rows = page => page.evaluate(() => [...document.querySelectorAll('#recordsTable tr')].map(tr => ({ id: tr.cells[0].innerText, err: tr.cells[5].innerText })));
const pctVal = s => s === '—' ? null : parseFloat(s);

// 1. Records: labels, sort with unknowns last and stable ties, filters with search,
// zero-match recovery, reset keeps sort, count is a polite live region.
{
  const { page, errors } = await open('real');
  await page.click('[data-view=records]');
  const labels = await page.evaluate(() => ['recordSearch', 'recordCorner', 'recordFilter', 'recordSort'].map(id => document.getElementById(id).labels[0]?.innerText.trim()));
  check(labels.every(Boolean), `records controls need visible labels: ${labels}`);
  check(await page.getAttribute('#recordCount', 'aria-live') === 'polite', 'record count not a polite live region');
  const orig = await rows(page);
  for (const [sort, dir] of [['error-desc', -1], ['error-asc', 1]]) {
    await page.selectOption('#recordSort', sort);
    const got = await rows(page);
    check(got.length === orig.length, `${sort} changed row count`);
    const known = got.filter(r => pctVal(r.err) != null), unknown = got.filter(r => pctVal(r.err) == null);
    check(got.slice(0, known.length).every(r => pctVal(r.err) != null), `${sort}: unknown errors not last`);
    check(known.every((r, i) => !i || (pctVal(r.err) - pctVal(known[i - 1].err)) * dir >= 0), `${sort}: not ordered ${known.map(r => r.err)}`);
    const origUnknown = orig.filter(r => pctVal(r.err) == null).map(r => r.id);
    check(JSON.stringify(unknown.map(r => r.id)) === JSON.stringify(origUnknown), `${sort}: unknown rows not in recorded order`);
  }
  await page.selectOption('#recordSort', 'original');
  check(JSON.stringify(await rows(page)) === JSON.stringify(orig), 'original sort does not restore recorded order');
  // Phases: Korean label with the raw value kept.
  const phases = await page.$$eval('#recordsTable tr td:nth-child(3) span', s => s.map(x => [x.innerText, x.title]));
  check(phases.length && phases.every(([t, raw]) => /보정|공통 초기|탐색|보류/.test(t) && /^phase = (calibration|initial|search|held_out|heldout|evaluation)$/.test(raw)), `phase cells ${JSON.stringify(phases.slice(0, 3))}`);
  check(phases.every(([t]) => !/[a-z_]{4,}/.test(t)), 'raw phase keys shown in visible phase labels');
  // Filters + search combine; zero match offers reset.
  check(await page.isDisabled('#recordReset'), 'reset enabled with no filter');
  await page.selectOption('#recordSort', 'error-desc');
  await page.fill('#recordSearch', '탐색');
  const searchRows = (await rows(page)).length;
  check(searchRows > 0 && searchRows < orig.length, `Korean phase search rows ${searchRows}`);
  await page.selectOption('#recordCorner', 'FF');
  await page.selectOption('#recordFilter', 'heldout');
  const zero = await page.evaluate(() => ({ rows: document.querySelectorAll('#recordsTable tr').length, empty: document.getElementById('emptyRecords').innerText, btn: !!document.querySelector('#emptyRecords [data-reset-records]') }));
  check(zero.rows === 0 && zero.btn && /조건에 맞는 기록이 없습니다/.test(zero.empty), `zero-match recovery ${JSON.stringify(zero)}`);
  await page.click('#emptyRecords [data-reset-records]');
  const after = await page.evaluate(() => ({ n: document.querySelectorAll('#recordsTable tr').length, sort: document.getElementById('recordSort').value, q: document.getElementById('recordSearch').value, f: document.getElementById('recordFilter').value, c: document.getElementById('recordCorner').value, focus: document.activeElement.id, dis: document.getElementById('recordReset').disabled }));
  check(after.n === orig.length && after.q === '' && after.f === 'all' && after.c === 'all' && after.dis, `reset did not restore all rows ${JSON.stringify(after)}`);
  check(after.sort === 'error-desc', 'reset should keep the chosen sort');
  check(after.focus === 'recordSearch', `focus after reset ${after.focus}`);
  check(!errors.length, `records errors ${errors}`);
  await page.close();
}

// 2. Dialog focus return (close button, Escape, backdrop) and keyboard focus after
// re-rendering clicks (heat map, replay steps, legend).
{
  const { page, errors } = await open('real');
  for (const how of ['button', 'escape', 'backdrop']) {
    await page.focus('#protocolButton');
    await page.keyboard.press('Enter');
    check(await page.evaluate(() => document.getElementById('detailDialog').open && document.getElementById('detailDialog').contains(document.activeElement)), `${how}: focus not inside dialog`);
    if (how === 'button') await page.click('#closeDialog');
    else if (how === 'escape') await page.keyboard.press('Escape');
    else await page.mouse.click(5, 5);
    check(await page.evaluate(() => !document.getElementById('detailDialog').open), `${how}: dialog still open`);
    check(await active(page) === 'protocolButton', `${how}: focus returned to ${await active(page)}`);
  }
  // Evidence followed inside an open dialog keeps the original trigger.
  await page.click('[data-step="1"]');
  const evBtn = await page.$('#decisionSummary [data-evidence]');
  if (evBtn) {
    const id = await evBtn.getAttribute('data-evidence');
    await evBtn.focus(); await page.keyboard.press('Enter');
    await page.keyboard.press('Escape');
    check(await active(page) === id, `evidence dialog focus return ${await active(page)}`);
  }
  // Trigger replaced by a re-render: falls back to the equivalent control.
  await page.focus('[data-candidate="0"]');
  await page.keyboard.press('Enter');
  await page.evaluate(() => renderCandidates());
  await page.keyboard.press('Escape');
  check(await active(page) === '0', `re-rendered trigger fallback ${await active(page)}`);
  // Heat map keyboard selection keeps focus on the selected cell.
  const cells = await page.$$eval('#heatmap .heat-cell', b => b.map(x => x.dataset.key));
  await page.focus(`#heatmap [data-key="${cells[3]}"]`);
  await page.keyboard.press('Enter');
  check(await active(page) === cells[3] && await page.getAttribute(`#heatmap [data-key="${cells[3]}"]`, 'aria-pressed') === 'true', 'heat map focus lost after selection');
  await page.keyboard.press('Tab');
  check(await active(page) === cells[4], `tab order after selection ${await active(page)}`);
  // Replay step keeps focus; progress text and final-step access.
  await page.focus('[data-step="0"]');
  await page.keyboard.press('Enter');
  check(await active(page) === '0', 'step focus lost');
  const prog0 = await page.textContent('#replayProgress');
  check(/^1 \/ \d+단계/.test(prog0) && /숨김/.test(prog0), `progress at stage 0 ${prog0}`);
  await page.click('#finalButton');
  const fin = await page.evaluate(() => ({ stage: state.stage, D: state.m.decisions.length, dis: document.getElementById('finalButton').disabled, focus: document.activeElement.id, prog: document.getElementById('replayProgress').textContent, adv: document.getElementById('advanceButton').innerText }));
  check(fin.stage === fin.D && fin.dis && fin.focus === 'advanceButton' && /정책 비교 보기/.test(fin.adv), `final step ${JSON.stringify(fin)}`);
  // Legend toggle keeps focus on the legend button.
  await page.click('[data-view=benchmark]');
  const pid = await page.$eval('#chartLegend [data-policy]', b => b.dataset.policy);
  await page.focus(`#chartLegend [data-policy="${pid}"]`);
  await page.keyboard.press('Enter');
  check(await active(page) === pid, 'legend focus lost');
  await page.keyboard.press('Enter');
  check(!errors.length, `focus errors ${errors}`);
  await page.close();
}

// 3. Navigation names and skip link at tablet and mobile widths; overflow everywhere.
for (const [w, h] of [[360, 780], [390, 844], [768, 1024], [1440, 900]]) {
  const { page, errors } = await open('real', [w, h]);
  const names = await page.evaluate(() => [...document.querySelectorAll('.nav [data-view]')].map(b => b.getAttribute('aria-label') || b.innerText.trim()));
  check(JSON.stringify(names) === JSON.stringify(['연구 작업대', '정책 비교', '실험 기록']), `${w}px nav names ${names}`);
  for (const name of ['연구 작업대', '정책 비교', '실험 기록']) check(await page.getByRole('button', { name, exact: true }).count() >= 1, `${w}px nav button "${name}" has no accessible name`);
  await page.keyboard.press('Tab');
  check(await page.evaluate(() => document.activeElement.classList.contains('skip-link')), `${w}px first tab is not the skip link`);
  await page.keyboard.press('Enter');
  check(await active(page) === 'mainContent', `${w}px skip link target ${await active(page)}`);
  for (const view of ['lab', 'benchmark', 'records']) {
    await page.click(`[data-view=${view}]`);
    if (view === 'benchmark') await page.evaluate(() => document.querySelectorAll('#view-benchmark details').forEach(d => { d.open = true; }));
    const of = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    check(of <= 0, `${w}px ${view} overflow ${of}px`);
  }
  if (w <= 390) {
    // Conclusion is first on narrow screens and the collapsed details stay short.
    await page.click('[data-view=benchmark]');
    await page.evaluate(() => document.querySelectorAll('#view-benchmark details').forEach(d => { d.open = false; }));
    const geo = await page.evaluate(() => ({ concl: document.getElementById('benchmarkConclusion').getBoundingClientRect().top, chart: document.getElementById('benchmarkChart').getBoundingClientRect().top, page: document.documentElement.scrollHeight }));
    check(geo.concl < geo.chart, `${w}px conclusion not above chart`);
    check(geo.page < 9000, `${w}px benchmark page too tall ${geo.page}px`);
    const small = await page.$$eval('#view-benchmark .btn, #view-records .btn, .nav-button, #recordSort, .legend-btn', b => b.filter(x => x.getClientRects().length && x.getBoundingClientRect().height < 43.5).map(x => x.id || x.className));
    check(!small.length, `${w}px controls under 44px: ${small}`);
  }
  check(!errors.length, `${w}px errors ${errors}`);
  await page.close();
}

// 4. Evidence disclosures (real), null/partial benchmark (fixture), empty snapshot.
{
  const { page, errors } = await open('real');
  await page.click('[data-view=benchmark]');
  const d = await page.evaluate(() => {
    const body = document.getElementById('benchmarkDetails');
    const all = [...document.querySelectorAll('#view-benchmark details')];
    return { closed: all.every(x => !x.open), count: all.length, visiblePre: [...document.querySelectorAll('#view-benchmark pre')].filter(p => p.checkVisibility()).length, text: body.innerText, height: body.getBoundingClientRect().height };
  });
  check(d.closed && d.count >= 8 && d.visiblePre === 0, `disclosures not collapsed ${JSON.stringify({ closed: d.closed, count: d.count, pre: d.visiblePre })}`);
  check(/한계/.test(d.text) && /Generic SPICE Level-1/.test(d.text), 'limitations not visible by default');
  check(/본 연구 비용/.test(d.text) && /작업대 시연 비용/.test(d.text), 'primary and live costs not separated');
  check(/969/.test(d.text) && /\b23\b/.test(d.text), 'exported logical query counts missing from cost summaries');
  check(/4\.9%/.test(d.text) && /28\.2%/.test(d.text), 'held-out error summary missing');
  check(!/clear_counterexamples|logical_queries|mean_abs_relative_error/.test(d.text), 'raw keys duplicated under summary labels');
  check(await page.$eval('#benchmarkDetails dt[title="logical_queries"]', x => x.innerText) === '논리 질의', 'raw key not kept as title');
  check(await page.evaluate(() => [...document.querySelectorAll('#benchmarkDetails .detail-card')].every(c => { const s = getComputedStyle(c); return s.borderLeftWidth === '0px' && s.borderRadius === '0px' && s.backgroundColor === 'rgba(0, 0, 0, 0)'; })), 'detail groups are boxed as nested cards');
  // Raw JSON keeps full precision and every field.
  const snap = JSON.parse(SNAP.real);
  await page.click('#benchmarkDetails .detail-card:first-child details:last-of-type > summary');
  const pre = await page.$eval('#benchmarkDetails .detail-card:first-child details[open] pre', p => p.textContent);
  check(JSON.stringify(JSON.parse(pre)) === JSON.stringify(snap.benchmark.held_out), 'held_out raw JSON is not the exported object');
  const opened = await page.$eval('#benchmarkDetails .detail-card:first-child details[open] pre', p => p.getBoundingClientRect().height);
  check(opened > 0, 'opened raw JSON not shown');
  // Primary paired seeds behind a disclosure; conclusion numbers unchanged.
  const concl = await page.$eval('#benchmarkConclusion', n => n.innerText);
  check(/주 성공 기준 충족/.test(concl) && /3\.20 ~ 4\.20|3\.2/.test(concl) && /\+3\.70개/.test(concl), `conclusion values ${concl}`);
  check(!errors.length, `disclosure errors ${errors}`);
  await page.close();
}
{
  const { page, errors } = await open('fixture');
  await page.click('[data-view=benchmark]');
  const t = await page.$eval('#benchmarkDetails', n => n.innerText);
  check((t.match(/\bnull\b/g) || []).length >= 4, `fixture null sections not shown as null: ${t.slice(0, 300)}`);
  check(/FIXTURE: incomplete benchmark runs/.test(t), 'fixture limitations missing');
  await page.evaluate(() => document.querySelectorAll('#benchmarkConclusion details').forEach(d => { d.open = true; }));
  const c = await page.$eval('#benchmarkConclusion', n => n.innerText);
  check(/주 판정 불가/.test(c) && /seed 1002: \+2/.test(c) && /null · 계산되지 않음/.test(c), `fixture conclusion ${c}`);
  check(!errors.length, `fixture errors ${errors}`);
  await page.close();
}
{
  const { page, errors } = await open('empty');
  await page.click('[data-view=benchmark]');
  const t = await page.$eval('#benchmarkDetails', n => n.innerText);
  check(/기록 없음 \(빈 배열\)/.test(t) && /비용 기록 없음/.test(t), `empty details ${t.slice(0, 200)}`);
  await page.click('[data-view=records]');
  check(await page.isHidden('#emptyRecords [data-reset-records]') && /아직 없습니다/.test(await page.textContent('#emptyRecords')), 'empty snapshot records state');
  await page.click('[data-view=lab]');
  check(await page.isDisabled('#finalButton') && (await page.textContent('#replayProgress')) === '', 'empty lab replay controls');
  check(!errors.length, `empty errors ${errors}`);
  await page.close();
}

// 5. Network failure, then a manual retry loads the snapshot.
{
  const { page, errors } = await open('real', [390, 844], { failFirst: true });
  check(await page.isVisible('[data-retry-load]'), 'retry button missing after network failure');
  await page.click('[data-retry-load]');
  await page.waitForFunction(() => /snapshot sha256/.test(document.getElementById('footerMeta').textContent), null, { timeout: 10000 });
  check(!(await page.$('[data-retry-load]')) && await active(page) === 'mainContent', 'retry did not recover');
  check(!errors.length, `retry errors ${errors}`);
  await page.close();
}

await browser.close();
console.log(JSON.stringify({ failures }, null, 1));
process.exit(failures.length ? 1 : 0);

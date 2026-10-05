'use strict';
// Run: node --test tests/test_post_hackathon_budget.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Core = require('../web/post-hackathon-budget-core.js');

const WEB = path.join(__dirname, '..', 'web');
const original = JSON.parse(fs.readFileSync(path.join(WEB, 'data', 'inspection-v3.json'), 'utf8'));
const ledger = Core.prepareLedger(original.replay);
const close = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) <= eps, `${a} != ${b}`);

// Synthetic fixture rows for edge cases only; never rendered or exported.
const row = (step, site, charged, cumulative, extra = {}) => ({step, site_id: site, wafer: 0, charged, cumulative_spend: cumulative, status: 'ok', label: true, reported_positive: true, ...extra});
const fixture = rows => ({budget: 100, spent: Math.max(0, ...rows.map(r => typeof r?.cumulative_spend === 'number' ? r.cumulative_spend : 0)), sites: [{id: 'a'}, {id: 'b'}, {id: 'c'}, {id: 'd'}], rows});

test('original export passes validation unchanged', () => {
  assert.equal(ledger.ok, true, ledger.issues.join('; '));
  assert.deepEqual(ledger.issues, []);
  assert.equal(ledger.rows.length, 36);
  assert.equal(ledger.validCount, 36);
  assert.equal(ledger.totalSites, 3915);
  assert.equal(ledger.budget, 360);
  assert.equal(ledger.variant, original.primary.candidate);
});

test('original: cap 0 retains nothing and names the first recorded review', () => {
  const r = Core.capPrefix(ledger, 0);
  assert.equal(r.paidReviews, 0);
  assert.equal(r.charged, 0);
  assert.equal(r.unusedCap, 0);
  assert.equal(r.fractionReviewed, 0);
  assert.equal(r.stop, 'cap');
  assert.equal(r.next.step, 1);
  assert.equal(r.next.charged, 17);
  assert.equal(r.next.cumulative_spend, 17);
  assert.equal(r.beyond.length, 36);
});

test('original: just below the first charge retains nothing', () => {
  const r = Core.capPrefix(ledger, 16.99);
  assert.equal(r.paidReviews, 0);
  close(r.unusedCap, 16.99);
  assert.equal(r.next.site_id, 'lot-c25528653c685e93:w2:r7:c-7');
});

test('original: exactly 17 CU retains the first review', () => {
  const r = Core.capPrefix(ledger, 17);
  assert.equal(r.paidReviews, 1);
  assert.equal(r.uniqueSites, 1);
  assert.deepEqual(r.counts, {positive: 1, negative: 0, failed: 0});
  assert.equal(r.charged, 17);
  assert.equal(r.unusedCap, 0);
  close(r.fractionReviewed, 1 / 3915);
  assert.equal(r.next.step, 2);
  close(r.next.charged, 9.061191664253366);
  close(r.next.cumulative_spend, 26.061191664253364);
});

test('original: full exported budget matches the recorded run', () => {
  const r = Core.capPrefix(ledger, 360);
  assert.equal(r.paidReviews, 36);
  assert.equal(r.uniqueSites, 36);
  assert.equal(r.repeatedRecords, 0);
  assert.deepEqual(r.counts, {positive: 31, negative: 4, failed: 1});
  close(r.charged, original.replay.spent);
  close(r.charged, 352.2080928428702);
  close(r.unusedCap, 360 - 352.2080928428702);
  close(r.fractionReviewed, 36 / 3915);
  assert.equal(r.next, null);
  assert.equal(r.stop, 'end');
  // The final failed review is unknown, never a negative.
  assert.equal(r.retained[35].outcome, 'failed');
});

test('original: below the final threshold stops before the failed review', () => {
  const r = Core.capPrefix(ledger, 352.2);
  assert.equal(r.paidReviews, 35);
  assert.deepEqual(r.counts, {positive: 31, negative: 4, failed: 0});
  close(r.next.charged, 13.286012880577292);
});

test('beyond-cap rows never carry an outcome', () => {
  const r = Core.capPrefix(ledger, 100);
  assert.ok(r.beyond.length > 0);
  for (const b of r.beyond) for (const k of ['outcome', 'label', 'reported_positive', 'status']) assert.equal(k in b, false, k);
  for (const k of ['outcome', 'label', 'reported_positive', 'status']) assert.equal(k in r.next, false, k);
});

test('cap validation rejects non-finite, negative and over-budget values', () => {
  for (const bad of [NaN, Infinity, -Infinity, -0.01, 360.01, '17', null, undefined]) {
    const r = Core.capPrefix(ledger, bad);
    assert.ok(r.error, String(bad));
    assert.equal('paidReviews' in r, false);
  }
  assert.equal(Core.capPrefix(Core.prepareLedger({rows: []}), 0).error.includes('budget'), true);
});

test('equal thresholds are admitted or excluded together, never skipped', () => {
  const L = Core.prepareLedger(fixture([row(1, 'a', 10, 10), row(2, 'b', 0, 10), row(3, 'c', 30, 40), row(4, 'd', 1, 41)]));
  assert.equal(L.ok, true, L.issues.join('; '));
  assert.equal(Core.capPrefix(L, 9.99).paidReviews, 0);
  assert.equal(Core.capPrefix(L, 10).paidReviews, 2);
  // Row 4 is cheap but must not be admitted while the expensive row 3 is over the cap.
  const r = Core.capPrefix(L, 39);
  assert.equal(r.paidReviews, 2);
  assert.equal(r.next.step, 3);
  assert.equal(r.stop, 'cap');
});

test('repeated sites count once as reviewed sites but every paid record counts', () => {
  const L = Core.prepareLedger(fixture([
    row(1, 'a', 5, 5, {status: 'failure', label: null, reported_positive: false}),
    row(2, 'a', 5, 10, {label: false, reported_positive: false}),
    row(3, 'b', 5, 15, {status: 'missing', label: null, reported_positive: null}),
  ]));
  const r = Core.capPrefix(L, 100);
  assert.equal(r.paidReviews, 3);
  assert.equal(r.uniqueSites, 2);
  assert.equal(r.repeatedRecords, 1);
  assert.deepEqual(r.counts, {positive: 0, negative: 1, failed: 2});
  close(r.fractionReviewed, 2 / 4);
});

test('ok status without a usable label is failed, not negative', () => {
  assert.equal(Core.outcome({status: 'ok', label: null, reported_positive: null}), 'failed');
  assert.equal(Core.outcome({status: 'ok', label: null, reported_positive: false}), 'negative');
  assert.equal(Core.outcome({status: 'failure', label: true}), 'failed');
  assert.equal(Core.outcome({label: true}), 'positive');
  assert.equal(Core.outcome(null), 'failed');
});

test('malformed spend makes the rest of the ledger unavailable instead of zero-cost', () => {
  const cases = {
    missing: {charged: null},
    string: {charged: '5'},
    negative: {charged: -1, cumulative_spend: 4},
    inconsistent: {cumulative_spend: 99},
    decreasing: {charged: 5, cumulative_spend: 1},
    nosite: {site_id: null},
  };
  for (const [name, patch] of Object.entries(cases)) {
    const L = Core.prepareLedger(fixture([row(1, 'a', 5, 5), {...row(2, 'b', 5, 10), ...patch}, row(3, 'c', 5, 15)]));
    assert.equal(L.ok, false, name);
    assert.equal(L.invalidAt, 1, name);
    assert.equal(L.validCount, 1, name);
    assert.ok(L.issues.length > 0, name);
    const r = Core.capPrefix(L, 100);
    assert.equal(r.paidReviews, 1, name);
    assert.equal(r.charged, 5, name);
    assert.equal(r.stop, 'unavailable', name);
    assert.equal(r.next.charged, null, name);
    assert.equal(r.next.cumulative_spend, null, name);
    assert.ok(r.beyond.every(b => b.charged === null && b.valid === false), name);
  }
});

test('first row must start from zero and non-object rows break the ledger', () => {
  assert.equal(Core.prepareLedger(fixture([row(1, 'a', 5, 7)])).invalidAt, 0);
  const L = Core.prepareLedger(fixture([row(1, 'a', 5, 5), 'junk', row(3, 'c', 5, 15)]));
  assert.equal(L.invalidAt, 1);
  assert.equal(Core.capPrefix(L, 100).paidReviews, 1);
});

test('rows are sorted by step only when every step is numeric', () => {
  const sorted = Core.prepareLedger(fixture([row(2, 'b', 5, 10), row(1, 'a', 5, 5)]));
  assert.equal(sorted.ok, true);
  assert.deepEqual(sorted.rows.map(r => r.site_id), ['a', 'b']);
  // A non-numeric step keeps array order, which here is temporally inconsistent and therefore flagged.
  const kept = Core.prepareLedger(fixture([row('x', 'b', 5, 10), row(1, 'a', 5, 5)]));
  assert.deepEqual(kept.rows.map(r => r.site_id), ['b', 'a']);
  assert.equal(kept.invalidAt, 0);
});

test('missing replay, budget, and strange values are handled without throwing', () => {
  for (const input of [null, undefined, 42, 'x', [], {}, {rows: 'x'}, {rows: [null], budget: 'big'}]) {
    const L = Core.prepareLedger(input);
    assert.equal(L.ok, false);
    assert.ok(L.issues.length > 0);
  }
  const noSites = Core.prepareLedger({budget: 10, rows: []});
  assert.equal(noSites.totalSites, null);
  assert.equal(Core.capPrefix(noSites, 5).fractionReviewed, null);
});

test('page is notice-labeled, English, and links only to existing post-hackathon pages', () => {
  const html = fs.readFileSync(path.join(WEB, 'post-hackathon-budget.html'), 'utf8');
  assert.ok(html.includes('Post-hackathon development — not part of the submitted version.'));
  assert.ok(html.includes('<html lang="en">'));
  const hrefs = [...html.matchAll(/href="([^"]+)"/g)].map(m => m[1]).filter(h => !h.endsWith('.css') && h !== '#main');
  assert.deepEqual([...new Set(hrefs)].sort(), ['./inspection-evidence.html', './post-hackathon.html']);
  for (const h of hrefs) assert.ok(fs.existsSync(path.join(WEB, h)), h);
  const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map(m => m[1]);
  assert.deepEqual(scripts, ['./post-hackathon-budget-core.js', './post-hackathon-budget.js']);
  const js = fs.readFileSync(path.join(WEB, 'post-hackathon-budget.js'), 'utf8');
  assert.deepEqual([...js.matchAll(/fetch\(/g)].length, 1);
  assert.ok(js.includes("'./data/inspection-v3.json'"));
  assert.ok(!/localStorage|XMLHttpRequest|WebSocket|<canvas/.test(js));
});

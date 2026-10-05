// Post-hackathon recorded-spend explainer: pure logic shared by the browser page and Node tests.
// It cuts the stored paid-review sequence of ONE original 360-CU run at a spend cap. It never
// re-runs, replans or re-scores anything, and never invents a cost or an outcome.
(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.BudgetCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const finite = n => typeof n === 'number' && Number.isFinite(n);
  const obj = v => v && typeof v === 'object' && !Array.isArray(v) ? v : null;
  const arr = v => Array.isArray(v) ? v : [];
  // Float tolerance for comparing a recorded cumulative total with prior total + charge.
  const tolerance = v => 1e-6 * Math.max(1, Math.abs(v));
  const CAP_EPS = 1e-9;

  // Same semantics as post-hackathon.js / inspection-evidence.js. These are recorded review
  // observations, not truth-confirmed DOI; failed or missing is never a negative or a good die.
  function outcome(row) {
    if (!obj(row)) return 'failed';
    if (row.status != null && row.status !== 'ok') return 'failed';
    if (row.label === true) return 'positive';
    if (row.label === false) return 'negative';
    if (row.status === 'ok' && typeof row.reported_positive === 'boolean') return row.reported_positive ? 'positive' : 'negative';
    return 'failed';
  }

  // Validates the recorded ledger in temporal order. Rows after the first malformed row are kept
  // only as "unavailable": their spend cannot be trusted, so no prefix may extend past it.
  function prepareLedger(replay) {
    const r = obj(replay);
    const issues = [];
    if (!r) return {ok:false, rows:[], validCount:0, invalidAt:null, budget:null, exportedSpent:null, totalSites:null, issues:['The export has no replay object.']};
    const raw = arr(r.rows);
    if (!Array.isArray(r.rows)) issues.push('replay.rows is missing or not an array.');
    // Follow the original renderer's ordering only when every step is numeric; otherwise keep array order.
    const ordered = raw.map((row, index) => ({row, index}));
    const allNumeric = ordered.length > 0 && ordered.every(o => obj(o.row) && finite(o.row.step));
    if (allNumeric) ordered.sort((a, b) => a.row.step - b.row.step || a.index - b.index);

    const rows = [];
    let prevCum = 0, invalidAt = null;
    ordered.forEach((o, i) => {
      const row = o.row, problem = invalidAt != null ? 'after an earlier malformed row' : rowProblem(row, prevCum, i);
      if (problem && invalidAt == null) { invalidAt = i; issues.push(`Recorded row ${i + 1}${obj(row) && row.step != null ? ` (step ${String(row.step)})` : ''}: ${problem}. Spend from this row on is unavailable.`); }
      const valid = invalidAt == null;
      if (valid) prevCum = row.cumulative_spend;
      rows.push({
        position: i + 1,
        step: obj(row) && (finite(row.step) || typeof row.step === 'string') ? row.step : null,
        site_id: obj(row) && row.site_id != null ? String(row.site_id) : null,
        wafer: obj(row) && row.wafer != null && (finite(row.wafer) || typeof row.wafer === 'string') ? row.wafer : null,
        charged: valid ? row.charged : null,
        cumulative_spend: valid ? row.cumulative_spend : null,
        valid,
        outcome: valid ? outcome(row) : null,
        status: obj(row) && typeof row.status === 'string' ? row.status : null,
      });
    });

    const budget = finite(r.budget) && r.budget > 0 ? r.budget : null;
    if (budget == null) issues.push('replay.budget is missing or not a positive number, so no cap range can be offered.');
    const validCount = invalidAt == null ? rows.length : invalidAt;
    const lastCum = validCount ? rows[validCount - 1].cumulative_spend : 0;
    if (budget != null && lastCum > budget + tolerance(budget)) issues.push(`Recorded cumulative spend ${lastCum} exceeds the exported budget ${budget}.`);
    const exportedSpent = finite(r.spent) ? r.spent : null;
    if (invalidAt == null && exportedSpent != null && rows.length && Math.abs(exportedSpent - lastCum) > tolerance(exportedSpent)) issues.push(`replay.spent ${exportedSpent} differs from the last recorded cumulative spend ${lastCum}.`);
    const siteIds = new Set(arr(r.sites).filter(s => obj(s) && s.id != null).map(s => String(s.id)));
    return {ok: budget != null && invalidAt == null && issues.length === 0, rows, validCount, invalidAt, budget, exportedSpent,
      totalSites: Array.isArray(r.sites) ? siteIds.size : null, siteIds, issues,
      lot_id: r.lot_id == null ? null : String(r.lot_id), variant: r.variant == null ? null : String(r.variant)};
  }

  function rowProblem(row, prevCum, i) {
    if (!obj(row)) return 'not an object';
    if (row.site_id == null || row.site_id === '') return 'no site_id';
    if (!finite(row.charged)) return 'charged is missing or not a finite number';
    if (row.charged < 0) return 'charged is negative';
    if (!finite(row.cumulative_spend)) return 'cumulative_spend is missing or not a finite number';
    if (row.cumulative_spend < 0) return 'cumulative_spend is negative';
    if (row.cumulative_spend < prevCum - tolerance(prevCum)) return 'cumulative_spend decreases';
    const expected = prevCum + row.charged;
    if (Math.abs(row.cumulative_spend - expected) > tolerance(expected)) return `cumulative_spend ${row.cumulative_spend} is not the previous total ${prevCum} plus charged ${row.charged}${i === 0 ? ' (the first row must start from 0)' : ''}`;
    return null;
  }

  function validateCap(cap, budget) {
    if (typeof cap !== 'number' || !Number.isFinite(cap)) return 'The spend cap must be a finite number.';
    if (cap < 0) return 'The spend cap cannot be negative.';
    if (!finite(budget)) return 'No exported budget is available, so no cap can be applied.';
    if (cap > budget) return `The spend cap cannot exceed the exported original budget of ${budget} CU.`;
    return null;
  }

  // Contiguous recorded prefix with cumulative_spend <= cap. Stops at the first row over the cap
  // (never skips it to admit a cheaper later row) and at the first malformed row.
  function capPrefix(ledger, cap) {
    const L = obj(ledger);
    if (!L) return {error:'No ledger.'};
    const error = validateCap(cap, L.budget);
    if (error) return {error};
    let n = 0;
    while (n < L.validCount && L.rows[n].cumulative_spend <= cap + CAP_EPS) n++;
    const retained = L.rows.slice(0, n);
    const counts = {positive:0, negative:0, failed:0};
    for (const row of retained) counts[row.outcome]++;
    const sites = new Set(retained.map(row => row.site_id));
    const charged = n ? retained[n - 1].cumulative_spend : 0;
    const nextRow = L.rows[n] || null;
    const stop = n < L.validCount ? 'cap' : L.invalidAt != null && n === L.invalidAt ? 'unavailable' : 'end';
    // Beyond-cap rows expose only identity and charge; their outcome is deliberately omitted.
    const strip = row => ({position:row.position, step:row.step, site_id:row.site_id, wafer:row.wafer, charged:row.charged, cumulative_spend:row.cumulative_spend, valid:row.valid});
    return {
      cap, budget:L.budget,
      paidReviews:n,
      uniqueSites:sites.size,
      repeatedRecords:n - sites.size,
      counts,
      charged,
      unusedCap:cap - charged,
      fractionReviewed:finite(L.totalSites) && L.totalSites > 0 ? sites.size / L.totalSites : null,
      totalSites:L.totalSites,
      retained,
      next: nextRow ? strip(nextRow) : null,
      beyond: L.rows.slice(n).map(strip),
      stop,
      recordedTotal:L.rows.length,
    };
  }

  return {outcome, prepareLedger, validateCap, capPrefix};
});

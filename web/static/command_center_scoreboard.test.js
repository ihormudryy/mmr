/* Node tests for the Scoreboard tab renderer (no browser, no dependencies). */
'use strict';

const assert = require('node:assert/strict');
const {formatMoney, formatPct, formatNumber, escapeHtml, renderScoreboard} = require('./command_center_scoreboard.js');

let passed = 0;
function test(name, callback) {
  try {
    callback();
    passed += 1;
  } catch (error) {
    error.message = `${name}: ${error.message}`;
    throw error;
  }
}

const DISCLAIMER = 'Paper trading. Nothing here is proof of live edge.';
const DRAWDOWN = 'drawdown from end-of-day equity; intraday lows may be missed';
const SPY = 'SPY price only; dividends excluded';

function trips(extra = {}) {
  return {closed: 0, open: 0, unresolved_fee_trips: 0, net_pnl_usd: 0, net_pnl_complete: true, fees_usd: 0,
          fees_complete: true, win_rate: null, profit_factor: null, turnover: 0, ...extra};
}

const EMPTY_REPORT = {
  label: 'PAPER', disclaimer: DISCLAIMER,
  experiment: {id: 'exp-0123456789abcdef0123', state: 'ARMED', started_at: '2026-10-06T13:35:00+00:00',
               base_currency: 'USD'},
  account: {sessions: 0, start_nlv_usd: 100000, end_nlv_usd: null, end_date: null, pnl_usd: null,
            return_pct: null, eod_drawdown_pct: null, eod_drawdown_label: DRAWDOWN, sharpe_daily: null,
            sharpe_annualised: null, sharpe_warning: 'SMALL_SAMPLE', unknown_nlv_sessions: 0},
  benchmarks: {spy: {return_pct: null, base_date: '2026-10-05', last_date: null, version: null,
                     provider: 'history_duckdb', label: SPY},
               vs_spy_pp: null, simulated: {label: 'simulated', status: 'UNAVAILABLE', rows: null, pnl_usd: null},
               ai_cost_usd: null, ai_calls: null, ai_costs_status: 'UNAVAILABLE', pnl_minus_ai_cost_usd: null},
  trips: trips(), splits: {strategy_version: {}, decider: {}, style: {}},
  sessions: [], warnings: [], incidents: [], outbox: {enabled: false, pending: null, last_sent_at: null},
};

function session(date, endState, extra = {}) {
  return {date, end_state: endState, start_nlv_usd: 100000, end_nlv_usd: 100250, return_pct: 0.25,
          realized_pnl_usd: 10, commissions_usd: 2, peak_gross_exposure_usd: 1000, trade_count: 1,
          open_positions: 0, start_source: 'prev_end', missing_sessions_before: 0, ...extra};
}

const FULL_REPORT = {
  ...EMPTY_REPORT,
  account: {...EMPTY_REPORT.account, sessions: 3, end_nlv_usd: 100500, return_pct: 0.5, eod_drawdown_pct: 1.2,
            sharpe_daily: 0.31, sharpe_annualised: 4.9},
  benchmarks: {...EMPTY_REPORT.benchmarks, spy: {...EMPTY_REPORT.benchmarks.spy, return_pct: 2.0, version: 1},
               vs_spy_pp: -1.5},
  trips: trips({closed: 2, win_rate: 0.5, profit_factor: 2.0, net_pnl_usd: 5}),
  splits: {strategy_version: {'sv-1': trips({closed: 2})}, decider: {jev: trips({closed: 1}),
           unattributed: trips({closed: 1})}, style: {}},
  sessions: [session('2026-10-06', 'FLAT'), session('2026-10-07', 'KILLED'),
             session('2026-10-08', 'FAILED_SAFE', {end_nlv_usd: null, return_pct: null})],
};

test('unknown renders as a dash and zero renders as zero', () => {
  assert.equal(formatMoney(null), '—');
  assert.equal(formatMoney(NaN), '—');
  assert.equal(formatMoney(undefined), '—');
  assert.equal(formatMoney(0), '$0.00');
  assert.equal(formatPct(undefined), '—');
  assert.equal(formatPct(0), '0.00%');
  assert.equal(formatNumber(null, 2), '—');
  assert.equal(formatNumber(1.234, 2), '1.23');
});

test('negative money keeps its sign and thousands separators', () => {
  assert.equal(formatMoney(-12.5), '-$12.50');
  assert.equal(formatMoney(1234567.891), '$1,234,567.89');
});

test('an empty report renders without throwing and still says paper', () => {
  const html = renderScoreboard(EMPTY_REPORT);
  assert.match(html, /PAPER/);
  assert.match(html, /proof of live edge/);
  assert.doesNotMatch(html, /NaN|undefined|null/);
});

test('the paper banner comes first', () => {
  assert.ok(renderScoreboard(FULL_REPORT).indexOf('PAPER') < renderScoreboard(FULL_REPORT).indexOf('Account'));
});

test('a null report renders a no-experiment notice', () => {
  assert.match(renderScoreboard(null), /No experiment/);
  assert.match(renderScoreboard({...EMPTY_REPORT, experiment: null}), /No experiment/);
  assert.match(renderScoreboard(null), /PAPER/);
});

test('small-sample warning is shown', () => {
  assert.match(renderScoreboard(FULL_REPORT), /fewer than 60 sessions/);
});

test('simulated baseline is labelled simulated', () => {
  const report = {...FULL_REPORT, benchmarks: {...FULL_REPORT.benchmarks,
    simulated: {label: 'simulated', status: 'AVAILABLE', rows: 2, pnl_usd: 7}}};
  assert.match(renderScoreboard(report), /simulated/);
  assert.match(renderScoreboard(report), /\$7\.00/);
});

test('unavailable ai cost and simulated book say unavailable, not zero', () => {
  const html = renderScoreboard(EMPTY_REPORT);
  assert.match(html, /AI cost[^<]*unavailable/i);
  assert.doesNotMatch(html, /AI cost[^<]*\$0\.00/);
  assert.match(html, /Simulated baseline[^<]*unavailable/i);
});

test('labels for SPY and drawdown are shown', () => {
  const html = renderScoreboard(FULL_REPORT);
  assert.match(html, /dividends excluded/);
  assert.match(html, /intraday lows may be missed/);
});

test('killed and failed-safe sessions are visibly different from flat', () => {
  const html = renderScoreboard(FULL_REPORT);
  for (const state of ['FLAT', 'KILLED', 'FAILED_SAFE']) {
    assert.match(html, new RegExp(`data-end-state="${state}"`));
  }
});

test('report strings are escaped', () => {
  const report = {...FULL_REPORT, warnings: [{code: 'X', detail: '<script>alert(1)</script>'}],
                  experiment: {...FULL_REPORT.experiment, id: '<script>'}};
  assert.doesNotMatch(renderScoreboard(report), /<script/i);
  assert.equal(escapeHtml('<a href="x">&\''), '&lt;a href=&quot;x&quot;&gt;&amp;&#39;');
});

test('unknown group keys render as unattributed', () => {
  assert.match(renderScoreboard(FULL_REPORT), /unattributed/);
});

test('telegram line shows disabled, pending and last sent', () => {
  assert.match(renderScoreboard(EMPTY_REPORT), /Telegram: disabled/);
  const report = {...EMPTY_REPORT, outbox: {enabled: true, pending: 2, last_sent_at: '2026-10-06T21:00:00+00:00'}};
  const html = renderScoreboard(report);
  assert.match(html, /2 pending/);
  assert.match(html, /last sent 2026-10-06T21:00:00\+00:00/);
});

test('an error body from the trader is shown as an error', () => {
  assert.match(renderScoreboard({label: 'PAPER', error_code: 'EXPERIMENT_NOT_FOUND'}), /EXPERIMENT_NOT_FOUND/);
});

test('incidents are listed', () => {
  const report = {...EMPTY_REPORT, incidents: [{kind: 'SESSION_MISSING', key: 'k', detail: 'd',
                                               recorded_at: '2026-10-06'}]};
  assert.match(renderScoreboard(report), /SESSION_MISSING/);
});

console.log(`command_center_scoreboard.test.js: ${passed} tests passed`);

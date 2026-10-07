/* The read-only Scoreboard tab: render the trader's PAPER report. Unknown is '—', never 0. */
'use strict';

(function (root) {
  const DASH = '—';

  function known(value) {
    return typeof value === 'number' && Number.isFinite(value);
  }

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, (character) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[character]);
  }

  function formatNumber(value, digits) {
    if (!known(value)) return DASH;
    return value.toLocaleString('en-US', {minimumFractionDigits: digits, maximumFractionDigits: digits});
  }

  function formatMoney(value) {
    if (!known(value)) return DASH;
    return (value < 0 ? '-$' : '$') + formatNumber(Math.abs(value), 2);
  }

  function formatPct(value) {
    return known(value) ? `${formatNumber(value, 2)}%` : DASH;
  }

  function text(value) {
    return value === null || value === undefined || value === '' ? DASH : escapeHtml(value);
  }

  function row(label, value) {
    return `<tr><th scope="row">${escapeHtml(label)}</th><td>${value}</td></tr>`;
  }

  function section(title, body) {
    return `<section class="sb-section"><h3>${escapeHtml(title)}</h3>${body}</section>`;
  }

  function banner(report) {
    const disclaimer = (report && report.disclaimer) || 'Paper trading. Nothing here is proof of live edge.';
    return `<div class="sb-banner" role="note"><strong>PAPER</strong> <span>${escapeHtml(disclaimer)}</span></div>`;
  }

  function account(report) {
    const a = report.account;
    const sharpe = `${formatNumber(a.sharpe_daily, 3)} daily, ${formatNumber(a.sharpe_annualised, 3)} annualised`
      + (a.sharpe_warning === 'SMALL_SAMPLE' ? ' <span class="sb-warn">small sample: fewer than 60 sessions</span>' : '');
    return section('Account', `<table class="sb-kv">${[
      row('Sessions', `${escapeHtml(a.sessions)} (unknown net liquidation: ${escapeHtml(a.unknown_nlv_sessions)})`),
      row('Return', `${formatPct(a.return_pct)} as of ${text(a.end_date)}`),
      row('P&L', formatMoney(a.pnl_usd)),
      row('End-of-day drawdown', `${formatPct(a.eod_drawdown_pct)} <span class="sb-label">${
        escapeHtml(a.eod_drawdown_label)}</span>`),
      row('Sharpe', sharpe),
    ].join('')}</table>`);
  }

  function benchmarks(report) {
    const b = report.benchmarks;
    const simulated = b.simulated.status === 'UNAVAILABLE' ? 'unavailable'
      : `${escapeHtml(b.simulated.rows)} rows, P&L ${formatMoney(b.simulated.pnl_usd)}`;
    const aiCost = b.ai_costs_status === 'UNAVAILABLE' ? 'unavailable'
      : `${formatMoney(b.ai_cost_usd)} over ${escapeHtml(b.ai_calls)} calls`;
    return section('Benchmarks', `<table class="sb-kv">${[
      row('SPY return', `${formatPct(b.spy.return_pct)} from the ${text(b.spy.base_date)} close <span class="sb-label">${
        escapeHtml(b.spy.label)}</span>`),
      row('vs SPY', known(b.vs_spy_pp) ? `${formatNumber(b.vs_spy_pp, 2)} pp` : DASH),
      row('Baseline', `Simulated baseline (labelled simulated): ${simulated}`),
      row('AI', `AI cost: ${aiCost}`),
      row('P&L minus AI cost', formatMoney(b.pnl_minus_ai_cost_usd)),
    ].join('')}</table>`);
  }

  function tripRows(t) {
    return [
      row('Closed / open', `${escapeHtml(t.closed)} / ${escapeHtml(t.open)}`),
      row('Win rate', known(t.win_rate) ? formatPct(t.win_rate * 100) : DASH),
      row('Profit factor', formatNumber(t.profit_factor, 2)),
      row('Net P&L', formatMoney(t.net_pnl_usd)),
      row('Fees', formatMoney(t.fees_usd)),
      row('Turnover', known(t.turnover) ? `${formatNumber(t.turnover, 2)}x` : DASH),
      row('Trips with unknown fees', escapeHtml(t.unresolved_fee_trips)),
    ].join('');
  }

  function splitTable(name, groups) {
    const keys = Object.keys(groups || {});
    if (!keys.length) return `<p class="dim">${escapeHtml(name)}: no trips yet.</p>`;
    const body = keys.map((key) => {
      const t = groups[key];
      return `<tr><td>${escapeHtml(key || 'unattributed')}</td><td>${escapeHtml(t.closed)}</td>`
        + `<td>${known(t.win_rate) ? formatPct(t.win_rate * 100) : DASH}</td>`
        + `<td>${formatNumber(t.profit_factor, 2)}</td><td>${formatMoney(t.net_pnl_usd)}</td></tr>`;
    }).join('');
    return `<table class="sb-table"><caption>${escapeHtml(name)}</caption><thead><tr><th>Group</th><th>Closed</th>`
      + `<th>Win rate</th><th>Profit factor</th><th>Net P&amp;L</th></tr></thead><tbody>${body}</tbody></table>`;
  }

  function sessions(report) {
    if (!report.sessions.length) return section('Sessions', '<p class="dim">No finished session yet.</p>');
    const body = report.sessions.map((s) => `<tr><td>${text(s.date)}</td>`
      + `<td><span class="sb-state" data-end-state="${escapeHtml(s.end_state)}">${escapeHtml(s.end_state)}</span></td>`
      + `<td>${formatPct(s.return_pct)}</td><td>${formatMoney(s.end_nlv_usd)}</td>`
      + `<td>${formatMoney(s.realized_pnl_usd)}</td><td>${formatMoney(s.commissions_usd)}</td>`
      + `<td>${formatMoney(s.peak_gross_exposure_usd)}</td><td>${text(s.trade_count)}</td>`
      + `<td>${text(s.open_positions)}</td></tr>`).join('');
    return section('Sessions', '<table class="sb-table"><thead><tr><th>Date</th><th>End</th><th>Return</th>'
      + '<th>End value</th><th>Realized</th><th>Fees</th><th>Peak gross</th><th>Trades</th><th>Open</th></tr>'
      + `</thead><tbody>${body}</tbody></table>`);
  }

  function notes(report) {
    const warnings = report.warnings.map((w) => `<li class="sb-warn">${escapeHtml(w.code)}: ${escapeHtml(w.detail)}</li>`);
    const incidents = report.incidents.map((i) => `<li class="sb-incident">${escapeHtml(i.kind)} ${
      escapeHtml(i.key)}: ${escapeHtml(i.detail)}</li>`);
    const items = warnings.concat(incidents);
    return section('Warnings and incidents', items.length ? `<ul>${items.join('')}</ul>` : '<p class="dim">None.</p>');
  }

  function telegram(report) {
    const outbox = report.outbox || {};
    const line = outbox.enabled
      ? `${text(outbox.pending)} pending, last sent ${text(outbox.last_sent_at)}`
      : 'disabled';
    return `<p class="sb-telegram">Telegram: ${line}</p>`;
  }

  function renderScoreboard(report) {
    if (report && report.error_code) {
      return banner(report) + `<p class="sb-error">${escapeHtml(report.error_code)}</p>`;
    }
    if (!report || !report.experiment) {
      return banner(report) + '<p class="sb-empty">No experiment yet. Start one with mmr experiment start.</p>';
    }
    const e = report.experiment;
    const header = `<p class="sb-experiment">Experiment ${escapeHtml(e.id)} ${escapeHtml(e.state)} since ${
      escapeHtml(e.started_at)} (base ${text(e.base_currency)}). Kill-line status: see mmr experiment status.</p>`;
    const splits = section('Splits (trips only)', ['strategy_version', 'decider', 'style']
      .map((key) => splitTable(key, report.splits[key])).join(''));
    return banner(report) + header + account(report) + benchmarks(report)
      + section('Trades', `<table class="sb-kv">${tripRows(report.trips)}</table>`)
      + splits + sessions(report) + notes(report) + telegram(report);
  }

  const api = {formatMoney, formatPct, formatNumber, escapeHtml, renderScoreboard};
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
    return;
  }
  root.CCScoreboard = api;

  const REFRESH_MS = 60000;
  let lastReport = null;
  let timer = null;

  function pane() {
    return document.getElementById('dash-scoreboard');
  }

  function show(html, stale) {
    const target = document.getElementById('scoreboard-root');
    if (!target) return;
    target.innerHTML = html;
    target.classList.toggle('sb-stale', Boolean(stale));
  }

  async function load() {
    try {
      const response = await fetch('/api/scoreboard', {credentials: 'same-origin', headers: {Accept: 'application/json'}});
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = body.error || {code: `HTTP_${response.status}`, message: response.statusText};
        throw Object.assign(new Error(error.message || ''), {code: error.code});
      }
      lastReport = body;
      show(renderScoreboard(body), false);
    } catch (error) {
      const notice = `<p class="sb-error">Scoreboard unavailable: ${escapeHtml(error.code || 'NETWORK_ERROR')} ${
        escapeHtml(error.message || '')}</p>`;
      show(notice + (lastReport ? renderScoreboard(lastReport) : banner(null)), Boolean(lastReport));
    }
  }

  function activeNow() {
    const element = pane();
    return Boolean(element && element.classList.contains('active'));
  }

  function tick() {
    if (activeNow()) load();
  }

  document.addEventListener('DOMContentLoaded', () => {
    if (!pane()) return;
    document.querySelectorAll('[data-dash-tab="scoreboard"]').forEach((button) => {
      button.addEventListener('click', () => setTimeout(tick, 0));
    });
    if (activeNow() || root.location.hash === '#scoreboard') load();
    timer = setInterval(tick, REFRESH_MS);
  });
  root.addEventListener('beforeunload', () => clearInterval(timer));
})(typeof window !== 'undefined' ? window : globalThis);

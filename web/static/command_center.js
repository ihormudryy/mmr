/* MMR Command Center client (read-only [M1-R]).
 * Store + reducer mirror the server contract: replace by entity key, apply
 * tombstones, reject nothing client-side (the server already rejected
 * revision regressions). SSE with Last-Event-ID resume; 15 s disconnect ->
 * 5 s snapshot polling behind a persistent banner; back to SSE only after a
 * coherent snapshot. */
'use strict';

const CFG = {
  degradedAfterMs: parseInt(document.body.dataset.degradedAfterMs, 10) || 15000,
  pollIntervalMs: parseInt(document.body.dataset.pollIntervalMs, 10) || 5000,
  staleAfterS: 30,
};

const DOMAIN_EVENT_TYPES = [
  'account.updated', 'position.updated', 'proposal.updated', 'command.updated',
  'trading_control.updated', 'order.updated', 'fill.received', 'fill.updated',
  'strategy.updated', 'risk.updated', 'reconciliation.updated', 'service.health',
];
const ACTIVE_PROPOSAL = new Set(['PENDING', 'APPROVED']);
const TERMINAL_ORDER = new Set(
  ['FILLED', 'CANCELLED', 'CANCELLED_AFTER_PARTIAL', 'REJECTED', 'INACTIVE']);
const DISPATCHABLE_STRATEGY = new Set(['RUNNING', 'WAITING_HISTORICAL_DATA']);

const store = {
  view: null, streamId: null, sequence: 0,
  quotes: {}, quoteReceivedAt: {},
  connection: { mode: 'connecting', degradedSince: null },
};

/* ---------------- reducer (mirrors web/command_center/state.py) ---------- */
function collectionFor(v, type) {
  return {
    account: v.accounts, position: v.positions, strategy: v.strategies,
    reconciliation: v.reconciliation, trading_control: v.trading_control,
    command: v.commands,
  }[type];
}

function replaceById(list, row) {
  const i = list.findIndex(r => r.entity_id === row.entity_id);
  if (i >= 0) list[i] = row; else list.push(row);
}

function removeById(list, id) {
  const i = list.findIndex(r => r.entity_id === id);
  if (i >= 0) list.splice(i, 1);
}

function applyEvent(env) {
  if (env.stream_id !== store.streamId) { resync(); return; }
  store.sequence = env.sequence;
  const v = store.view;
  if (!v) return;
  const row = env.operation === 'upsert'
    ? Object.assign({ entity_id: env.entity_id,
                      entity_revision: env.entity_revision }, env.payload)
    : null;
  const type = env.entity_type;
  if (type === 'proposal') {
    removeById(v.proposals.active, env.entity_id);
    removeById(v.proposals.terminal, env.entity_id);
    if (row) (ACTIVE_PROPOSAL.has(String(row.status || '').toUpperCase())
      ? v.proposals.active : v.proposals.terminal).push(row);
  } else if (type === 'order') {
    removeById(v.orders.active, env.entity_id);
    removeById(v.orders.terminal, env.entity_id);
    if (row) (TERMINAL_ORDER.has(String(row.status || '').toUpperCase())
      ? v.orders.terminal : v.orders.active).push(row);
  } else if (type === 'fill') {
    removeById(v.fills, env.entity_id);
    if (row) v.fills.push(row);
  } else if (type === 'risk') {
    if (row) v.risk[env.entity_id] = row; else delete v.risk[env.entity_id];
  } else {
    const list = collectionFor(v, type);
    if (list) { if (row) replaceById(list, row); else removeById(list, env.entity_id); }
  }
  v.last_event_at = env.source_timestamp;
  renderAll();
}

function applyQuotes(batch) {
  const now = Date.now();
  for (const [id, quote] of Object.entries(batch)) {
    store.quotes[id] = quote;
    store.quoteReceivedAt[id] = now;
  }
  renderPositions();
}

function applySnapshot(view) {
  store.view = view;
  store.streamId = view.stream_id;
  store.sequence = view.sequence;
  store.quotes = view.quotes || {};
  const now = Date.now();
  Object.keys(store.quotes).forEach(id => { store.quoteReceivedAt[id] = now; });
  renderAll();
}

/* ---------------- connection management ---------------------------------- */
let es = null, pollTimer = null, disconnectedAt = null;

async function fetchSnapshot() {
  const response = await fetch('/api/snapshot', { credentials: 'same-origin' });
  if (response.status === 401) { window.location.href = '/cc/login'; return null; }
  if (!response.ok) return null;
  return response.json();
}

async function resync() {
  if (es) { es.close(); es = null; }
  const view = await fetchSnapshot();
  if (view) { applySnapshot(view); connectSse(); }
  else setTimeout(resync, CFG.pollIntervalMs);
}

function connectSse() {
  if (es) es.close();
  const after = store.streamId ? `?after=${store.streamId}:${store.sequence}` : '';
  es = new EventSource('/api/events' + after);
  es.onopen = () => { disconnectedAt = null; stopPolling(); setBanner(false); };
  es.onerror = () => { if (disconnectedAt === null) disconnectedAt = Date.now(); };
  DOMAIN_EVENT_TYPES.forEach(t =>
    es.addEventListener(t, e => applyEvent(JSON.parse(e.data))));
  es.addEventListener('quote.updated',
    e => applyQuotes(JSON.parse(e.data).quotes));
  es.addEventListener('quotes.snapshot', e => {
    store.quotes = JSON.parse(e.data).quotes || {};
    const now = Date.now();
    Object.keys(store.quotes).forEach(id => { store.quoteReceivedAt[id] = now; });
    renderPositions();
  });
  es.addEventListener('resync_required', () => resync());
}

function startPolling() {
  if (pollTimer) return;
  setBanner(true);
  store.connection.mode = 'polling';
  pollTimer = setInterval(async () => {
    const view = await fetchSnapshot();
    if (view) {
      applySnapshot(view);
      // Return to SSE only after this coherent snapshot (spec §11).
      stopPolling();
      connectSse();
    }
  }, CFG.pollIntervalMs);
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  store.connection.mode = 'sse';
}

function setBanner(visible) {
  document.getElementById('degraded-banner').hidden = !visible;
}

setInterval(() => {
  if (es && es.readyState !== EventSource.OPEN && disconnectedAt !== null
      && Date.now() - disconnectedAt >= CFG.degradedAfterMs && !pollTimer) {
    es.close(); es = null;
    startPolling();
  }
}, 1000);

/* ---------------- rendering ---------------------------------------------- */
const fmt = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const money = (x, ccy) => (x === null || x === undefined || Number.isNaN(x))
  ? '—' : `${fmt.format(x)}${ccy ? ' ' + ccy : ''}`;
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const ageOf = iso => iso ? (Date.now() - Date.parse(iso)) / 1000 : null;
const fmtAge = s => s === null ? 'no data' : s < 60 ? `${Math.round(s)}s`
  : s < 3600 ? `${Math.round(s / 60)}m` : `${(s / 3600).toFixed(1)}h`;

function renderStatusBar() {
  const v = store.view; if (!v) return;
  const account = v.accounts[0] || {};
  const badge = document.getElementById('mode-badge');
  const mode = String(account.mode || 'unknown').toLowerCase();
  badge.textContent = mode.toUpperCase();
  badge.className = 'badge ' + (mode === 'live' ? 'live' : 'paper');
  document.getElementById('account-id').textContent =
    account.entity_id || account.account_id || '—';
  const chips = document.getElementById('dependency-chips');
  const sources = (v.health && v.health.sources) || {};
  chips.innerHTML = Object.entries(sources).map(([name, s]) =>
    `<span class="chip" data-state="${esc(s.state)}">${esc(name)}: ${esc(s.state)}` +
    (s.last_success_age_seconds !== null && s.last_success_age_seconds !== undefined
      ? ` (${fmtAge(s.last_success_age_seconds)})` : '') + '</span>').join('');
  chips.innerHTML += `<span class="chip" data-state="${
    v.health && v.health.lifecycle === 'live' ? 'ok' : 'error'}">bridge: ${
    esc(v.health ? v.health.lifecycle : 'unknown')}</span>`;
  document.querySelector('#last-event-time .v').textContent =
    v.last_event_at ? `${v.last_event_at} (${fmtAge(ageOf(v.last_event_at))} ago)` : '—';
}

function renderAccountCards() {
  const v = store.view; if (!v) return;
  const account = v.accounts[0] || {};
  // Net liquidation must render for a flat account too (spec §8.1): the value
  // comes from the account entity, never derived from positions.
  const cards = [
    ['Net liquidation', money(account.net_liquidation, account.currency)],
    ['Daily P&L', money(account.daily_pnl, account.currency)],
    ['Exposure', money(account.gross_exposure, account.currency)],
    ['Buying power', money(account.buying_power, account.currency)],
    ['Margin cushion', account.margin_cushion !== undefined && account.margin_cushion !== null
      ? `${fmt.format(account.margin_cushion * 100)}%` : '—'],
  ];
  document.getElementById('account-cards').innerHTML = cards.map(([k, val]) =>
    `<div class="card"><div class="k">${k}</div><div class="v">${esc(val)}</div></div>`
  ).join('');
}

function renderPositions() {
  const v = store.view; if (!v) return;
  const body = document.getElementById('positions-body');
  body.innerHTML = v.positions.map(p => {
    const conid = String(p.conid ?? (p.entity_id || '').split(':').pop());
    const quote = store.quotes[conid];
    const last = quote ? quote.last : null;
    const quoteAge = store.quoteReceivedAt[conid]
      ? (Date.now() - store.quoteReceivedAt[conid]) / 1000 : null;
    const stale = quoteAge === null || quoteAge > CFG.staleAfterS;
    const pnl = p.unrealized_pnl;
    return `<tr class="${stale ? 'stale' : ''}" data-entity="${esc(p.entity_id)}">
      <td>${esc(p.symbol || conid)}</td>
      <td class="num">${fmt.format(p.quantity ?? 0)}</td>
      <td class="num">${money(p.avg_cost)}</td>
      <td class="num">${money(last)}</td>
      <td>${esc(p.currency || '')}</td>
      <td class="num">${money(p.market_value, p.currency)}</td>
      <td class="num">${p.base_market_value !== undefined
        ? money(p.base_market_value, p.base_currency) : '— (no conversion)'}</td>
      <td class="num ${pnl >= 0 ? 'pos' : 'neg'}">${money(pnl)}</td>
      <td class="num">${money(p.daily_pnl)}</td>
      <td><span class="age">${fmtAge(quoteAge)}</span></td>
    </tr>`;
  }).join('');
}

function renderProposals() {
  const v = store.view; if (!v) return;
  const rail = document.getElementById('proposal-cards');
  const cards = v.proposals.active.map(p => {
    const age = ageOf(p.created_at);
    return `<div class="proposal-card" tabindex="0" role="button"
        data-proposal="${esc(p.entity_id)}"
        aria-label="Proposal ${esc(p.entity_id)} details">
      <strong>#${esc(p.entity_id)} ${esc(p.action || p.side || '')}
        ${esc(p.symbol || '')}</strong>
      <div class="dim">qty ${esc(p.quantity ?? 'auto')} · notional
        ${money(p.amount, p.currency)} · conf ${esc(p.confidence ?? '—')}</div>
      <div class="dim">status ${esc(p.status)} · expires ${esc(p.expires_at || '—')}
        · <span class="age">${fmtAge(age)} old</span></div>
    </div>`;
  });
  rail.innerHTML = cards.join('')
    || '<div class="proposal-card dim">No pending proposals.</div>';
}

function renderOrders() {
  const v = store.view; if (!v) return;
  const orders = [...v.orders.active, ...v.orders.terminal];
  const groups = new Map();
  for (const o of orders) {
    const gid = o.order_group_id || `solo:${o.entity_id}`;
    if (!groups.has(gid)) groups.set(gid, []);
    groups.get(gid).push(o);
  }
  // Aggregate group status without hiding per-leg state (spec §8.4).
  document.getElementById('order-groups').innerHTML =
    [...groups.entries()].map(([gid, legs]) => {
      const statuses = [...new Set(legs.map(l => String(l.status || '')))];
      const filled = legs.reduce((n, l) => n + (l.filled_quantity || 0), 0);
      return `<div class="order-group">
        <div class="group-head">${esc(gid)} — ${legs.length} leg(s),
          ${esc(statuses.join(' / '))}, filled ${fmt.format(filled)}</div>
        ${legs.map(l => `<div class="leg"><span>${esc(l.symbol || l.conid || '')}
          ${esc(l.action || '')} ${fmt.format(l.quantity ?? 0)}
          @ ${esc(l.order_type || '')}</span>
          <span>${esc(l.status || '')} · filled ${fmt.format(l.filled_quantity || 0)}
          ${l.avg_fill_price ? '@ ' + money(l.avg_fill_price) : ''}</span></div>`
        ).join('')}
      </div>`;
    }).join('') || '<div class="order-group group-head dim">No orders.</div>';
}

function renderFills() {
  const v = store.view; if (!v) return;
  document.getElementById('fills-body').innerHTML = v.fills.slice(-50).reverse()
    .map(f => `<tr><td>${esc(f.time || '')}</td><td>${esc(f.symbol || f.conid || '')}</td>
      <td>${esc(f.side || '')}</td><td class="num">${fmt.format(f.quantity ?? 0)}</td>
      <td class="num">${money(f.price)}</td>
      <td class="num">${money(f.commission)}</td></tr>`).join('');
}

function renderStrategies() {
  const v = store.view; if (!v) return;
  document.getElementById('strategies-body').innerHTML = v.strategies.map(s => {
    const state = String(s.runtime_state || s.state || '').toUpperCase();
    const enabled = DISPATCHABLE_STRATEGY.has(state);
    return `<tr><td>${esc(s.name || s.entity_id)}</td><td>${esc(state)}</td>
      <td>${enabled ? '● enabled' : '○ not dispatchable'}</td>
      <td><span class="age">${fmtAge(ageOf(s.last_activity_at))}</span></td>
      <td class="${s.last_error ? 'neg' : 'dim'}">${
        s.last_error ? '⚠ ' + esc(s.last_error) : '—'}</td></tr>`;
  }).join('');
}

function renderRisk() {
  const v = store.view; if (!v) return;
  const el = document.getElementById('risk-body');
  const projections = Object.entries(v.risk || {})
    .filter(([key]) => key.startsWith('projection:'));
  const journal = v.health && v.health.sources && v.health.sources.journal;
  const degraded = !journal || journal.state !== 'ok';
  if (projections.length === 0 || degraded) {
    // Never render green from missing data (spec §8.4).
    el.dataset.state = 'unavailable';
    el.textContent = '⚠ Risk unavailable — '
      + (degraded ? 'journal source degraded.' : 'no authoritative risk projection.');
    return;
  }
  const rows = projections.map(([key, r]) => {
    const warnings = r.warnings || [];
    return `<div><strong>${esc(key)}</strong> — ${
      warnings.length ? '⚠ ' + warnings.map(esc).join('; ')
                      : 'no active warnings'}</div>`;
  });
  const anyWarning = projections.some(([, r]) => (r.warnings || []).length);
  el.dataset.state = anyWarning ? 'warning' : 'ok';
  el.innerHTML = rows.join('')
    + (v.reconciliation || []).map(r =>
      `<div class="dim">reconciliation ${esc(r.entity_id)}: ${
        esc(r.discrepancy_count ?? 0)} discrepancies</div>`).join('');
}

function renderAll() {
  renderStatusBar(); renderAccountCards(); renderPositions(); renderProposals();
  renderOrders(); renderFills(); renderStrategies(); renderRisk();
}

/* ---------------- drawer (keyboard + focus managed) ----------------------- */
let drawerInvoker = null;

function openDrawer(html, invoker) {
  drawerInvoker = invoker || null;
  document.getElementById('drawer-content').innerHTML = html;
  document.getElementById('drawer').hidden = false;
  document.getElementById('drawer-close').focus();
}

function closeDrawer() {
  document.getElementById('drawer').hidden = true;
  if (drawerInvoker) { drawerInvoker.focus(); drawerInvoker = null; }
}

document.getElementById('drawer-close').addEventListener('click', closeDrawer);
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !document.getElementById('drawer').hidden) closeDrawer();
});

document.getElementById('proposal-cards').addEventListener('click', e => {
  const card = e.target.closest('[data-proposal]');
  if (card) showProposalDrawer(card.dataset.proposal, card);
});
document.getElementById('proposal-cards').addEventListener('keydown', e => {
  const card = e.target.closest('[data-proposal]');
  if (card && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    showProposalDrawer(card.dataset.proposal, card);
  }
});

function showProposalDrawer(id, invoker) {
  const p = store.view.proposals.active.find(x => String(x.entity_id) === String(id))
    || store.view.proposals.terminal.find(x => String(x.entity_id) === String(id));
  if (!p) return;
  // Full sizing-reasoning chain, not a one-line preview (spec §8.3).
  const sizing = p.sizing_result || {};
  const reasoning = Array.isArray(sizing.reasoning) ? sizing.reasoning
    : (sizing.reasoning ? [sizing.reasoning] : []);
  openDrawer(`<h3>Proposal #${esc(p.entity_id)} — ${esc(p.action || '')}
      ${esc(p.symbol || '')}</h3>
    <p>status <strong>${esc(p.status)}</strong> · source ${esc(p.source || '—')}
      · confidence ${esc(p.confidence ?? '—')} · expires ${esc(p.expires_at || '—')}</p>
    <h4>Position sizing reasoning</h4>
    ${reasoning.length
      ? '<ol>' + reasoning.map(step => `<li>${esc(step)}</li>`).join('') + '</ol>'
      : '<p class="dim">No sizing reasoning recorded.</p>'}
    <h4>Rationale</h4>
    <p>${esc(p.reasoning || '—')}</p>`, invoker);
}

/* ---------------- freshness ticker ---------------------------------------- */
setInterval(() => { renderStatusBar(); renderPositions(); }, 1000);

/* ---------------- boot ----------------------------------------------------- */
resync();

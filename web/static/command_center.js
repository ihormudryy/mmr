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

/* ===================== [M1-C] command surfaces ===================== */
/* 202 == received only. Success renders ONLY once a matching command_id
 * shows up in store.view.commands with a terminal state -- the exact same
 * command.updated SSE reducer path applyEvent() (above) already wires via
 * collectionFor(v, 'command') -> v.commands, since "command" is already one
 * of DOMAIN_EVENT_TYPES. A POST that never gets an ack renders "Outcome
 * unknown — reconciling" instead of ever inferring success from the HTTP
 * response.
 *
 * Source-vs-brief note: this section assumes no CC.* namespace and no
 * CC.onEvent hook -- neither exists anywhere above (the M1-R client store
 * is bare globals: `store`, `resync`, `renderAll`, ...) -- so command
 * resolution is read off the already-reduced `store.view.commands` via a
 * small poll (ccCheckPendingCommands) instead of re-subscribing to SSE or
 * restructuring applyEvent()/renderAll(). Likewise there is no
 * `<meta name="cc-csrf-token">` anywhere in the page (session.py's cookie
 * is HttpOnly and [M1-R] never minted a CSRF token to begin with) --
 * ccCsrfToken() fetches-and-caches it from the
 * `GET /api/commands/csrf-token` route this task adds instead. */

const CC = {
  commands: {
    pending: new Map(),   // command_id -> {label}
    unknown: new Map(),   // command_id -> {label}
    availability: {},     // command kind -> {enabled, reason} (later task)
  },
  csrfToken: null,
};

function ccNewCommandId() {
  // Created BEFORE submission; reused across confirmation and every retry.
  return crypto.randomUUID();
}

async function ccCsrfToken(force = false) {
  // `force` bypasses the cache -- used after a `CSRF_REJECTED` 403 (see
  // ccPost below), since the process-local `_CSRF_SECRET` that derives this
  // token rotates on every web restart, so a token cached from before a
  // restart 403s until refreshed.
  if (CC.csrfToken && !force) return CC.csrfToken;
  const res = await fetch('/api/commands/csrf-token', { credentials: 'same-origin' });
  if (!res.ok) throw new Error('could not fetch a CSRF token for this session');
  const data = await res.json();
  CC.csrfToken = data.csrf_token;
  return CC.csrfToken;
}

function ccToast(kind, text) {
  const el = document.createElement('div');
  el.className = `cc-toast cc-toast-${kind}`;
  el.textContent = text;
  document.getElementById('cc-toasts').appendChild(el);
  setTimeout(() => el.remove(), 8000);
}

async function ccPost(url, body, { okStatus = 202 } = {}) {
  const timeoutMs = window.CC_COMMAND_TIMEOUT_MS || 8000;
  const attempt = async () => {
    const csrf = await ccCsrfToken();
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      credentials: 'same-origin',
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
  };
  let res;
  try {
    res = await attempt();
    if (res.status === 403) {
      const peek = await res.clone().json().catch(() => ({}));
      if (peek.code === 'CSRF_REJECTED') {
        // Refetch the token (bypassing the cache) and retry ONCE, reusing
        // the SAME `body` -- and therefore the SAME `body.command_id` (see
        // ccNewCommandId) -- so the coordinator's idempotent replay dedupes
        // this as one logical command rather than creating two proposals.
        await ccCsrfToken(true);
        res = await attempt();
      }
    }
  } catch (err) {
    return {
      ok: false, outcomeUnknown: true,
      error: {
        code: 'OUTCOME_UNKNOWN', message: 'no acknowledgement — reconciling',
        retryable: false, correlation_id: body.command_id,
      },
    };
  }
  const data = await res.json().catch(() => ({}));
  if (res.status === okStatus) return { ok: true, data };
  const unknown = res.status === 504 || data.code === 'OUTCOME_UNKNOWN';
  return { ok: false, outcomeUnknown: unknown, error: data };
}

function ccRenderPending() {
  const box = document.getElementById('cc-pending-commands');
  box.replaceChildren(...[...CC.commands.pending.entries()].map(([id, p]) => {
    const chip = document.createElement('div');
    chip.className = 'cc-pending';
    chip.dataset.commandId = id;
    chip.textContent = `${p.label} — Pending confirmation`;
    return chip;
  }));
}

function ccShowOutcomeUnknown(commandId, label) {
  const banner = document.getElementById('cc-outcome-unknown');
  CC.commands.unknown.set(commandId, { label });
  banner.hidden = false;
  banner.textContent =
      `Outcome unknown — reconciling: ${
        [...CC.commands.unknown.values()].map((u) => u.label).join(', ')}`;
}

function ccClearOutcomeUnknown(commandId) {
  CC.commands.unknown.delete(commandId);
  if (CC.commands.unknown.size === 0) {
    document.getElementById('cc-outcome-unknown').hidden = true;
  }
}

async function ccReconcileCommand(commandId, label) {
  // Authoritative refresh: poll the ledger until a terminal state, or until
  // ccCheckPendingCommands() clears it first from a command.updated event
  // that already arrived over SSE.
  for (let i = 0; i < 12 && CC.commands.unknown.has(commandId); i += 1) {
    await new Promise((r) => setTimeout(r, window.CC_RECONCILE_MS || 5000));
    try {
      const res = await fetch(`/api/commands/${commandId}`,
                              { credentials: 'same-origin' });
      if (!res.ok) continue;
      const receipt = await res.json();
      if (['SUBMITTED', 'REJECTED', 'RESOLVED'].includes(receipt.state)) {
        ccResolveCommand(commandId, receipt.state, receipt.error_code, label);
        return;
      }
    } catch (err) { /* keep reconciling */ }
  }
}

function ccResolveCommand(commandId, state, errorCode, label) {
  const pending = CC.commands.pending.get(commandId);
  const name = label || (pending && pending.label) || commandId;
  CC.commands.pending.delete(commandId);
  ccClearOutcomeUnknown(commandId);
  ccRenderPending();
  if (state === 'REJECTED') {
    ccToast('error', `${name}: rejected (${errorCode || 'no code'})`);
  } else {
    ccToast('ok', `${name}: ${state.toLowerCase()}`);
  }
}

function ccCheckPendingCommands() {
  const v = typeof store !== 'undefined' ? store.view : null;
  if (!v || !Array.isArray(v.commands)) return;
  const ids = new Set([...CC.commands.pending.keys(), ...CC.commands.unknown.keys()]);
  ids.forEach((id) => {
    const row = v.commands.find((c) => c.entity_id === id);
    if (!row) return;
    const state = String(row.state || '').toUpperCase();
    if (['SUBMITTED', 'REJECTED', 'RESOLVED'].includes(state)) {
      ccResolveCommand(id, state, row.error_code, null);
    }
  });
}
setInterval(ccCheckPendingCommands, 400);

function ccRequireAvailable(kind) {
  const a = CC.commands.availability[kind];
  if (a && !a.enabled) {
    ccToast('error',
            `Command unavailable — ${a.reason}. Commands are never queued.`);
    return false;
  }
  return true;
}

async function ccSubmitCommand(kind, label, url, body) {
  if (!ccRequireAvailable(kind)) return;
  CC.commands.pending.set(body.command_id, { label });
  ccRenderPending();
  const result = await ccPost(url, body);
  if (result.ok) return;  // stays "Pending confirmation" until command.updated
  CC.commands.pending.delete(body.command_id);
  ccRenderPending();
  if (result.outcomeUnknown) {
    ccShowOutcomeUnknown(body.command_id, label);
    ccReconcileCommand(body.command_id, label);
    return;
  }
  ccToast('error', `${label}: ${result.error.message} (${result.error.code})`);
}

/* ---- New proposal drawer ---- */

function ccOpenProposalDrawer() {
  document.getElementById('cc-proposal-drawer').hidden = false;
}

function ccProposalBody(form, commandId) {
  const f = new FormData(form);
  const num = (k) => (f.get(k) ? Number(f.get(k)) : null);
  return {
    command_id: commandId,
    conid: Number(f.get('conid')),
    action: f.get('action'),
    quantity: num('quantity'),       // both empty -> server auto-sizing
    amount: num('amount'),
    confidence: f.get('confidence') ? Number(f.get('confidence')) : 0.0,
    group: String(f.get('group') || '').trim(),
    thesis: String(f.get('thesis') || '').trim(),
    reasoning: String(f.get('reasoning') || ''),
  };
}

document.getElementById('cc-proposal-form').addEventListener('submit',
    async (evt) => {
      evt.preventDefault();
      // Minted ONCE here, at command initiation -- reused for the CSRF
      // retry inside ccPost (M-3) and any future resubmit of this same
      // logical command; never re-minted (see ccNewCommandId).
      const commandId = ccNewCommandId();
      const body = ccProposalBody(evt.target, commandId);
      document.getElementById('cc-proposal-drawer').hidden = true;
      await ccSubmitCommand('create_proposal',
          `New proposal ${body.action} conId ${body.conid}`,
          '/api/commands/proposals', body);
    });

/* ---- Close position drawer (pre-filled reducing proposal) ---- */

function ccOpenCloseDrawer(position) {
  const d = document.getElementById('cc-close-drawer');
  d.querySelector('[data-field=instrument]').textContent =
      `${position.symbol || position.conid} (${position.conid})`;
  const action = position.quantity > 0 ? 'SELL' : 'BUY';  // opposite, reducing
  d.querySelector('[data-field=side]').textContent = action;
  const qty = d.querySelector('input[name=quantity]');
  qty.value = Math.abs(position.quantity);
  qty.max = Math.abs(position.quantity);  // never exceed reducible quantity
  d.dataset.account = position.account_id
      || String(position.entity_id || '').split(':')[0] || '';
  d.dataset.action = action;
  d.dataset.conid = position.conid;
  d.hidden = false;
}

document.getElementById('cc-close-form').addEventListener('submit',
    async (evt) => {
      evt.preventDefault();
      const d = document.getElementById('cc-close-drawer');
      // Minted ONCE at initiation -- see the proposal-form handler above
      // for the same reuse contract (CSRF retry + any future resubmit).
      const commandId = ccNewCommandId();
      const body = {
        command_id: commandId,
        action: d.dataset.action,
        quantity: Number(d.querySelector('input[name=quantity]').value),
        reasoning: d.querySelector('textarea[name=reasoning]').value,
      };
      d.hidden = true;
      await ccSubmitCommand('create_proposal',
          `Close position conId ${d.dataset.conid}`,
          `/api/commands/positions/${d.dataset.account}/${d.dataset.conid}/close`,
          body);
    });

/* ===================== [M1-C] Task 4: approve / reject / preflight ======
 * Live two-stage ceremony (spec 9.1): one command_id minted BEFORE
 * preflight, reused across the confirmation drawer and any retry -- the
 * gateway/coordinator dedupes on it. `/api/preflight` is a same-origin GET
 * of a summary, never treated as an approval itself; the drawer below
 * repeats that authoritative summary so the human confirms what will
 * actually transmit, not what the browser assumed. The approve POST itself
 * is still 202-received-only, exactly like every other command in this
 * file -- the outcome resolves from `command.updated` via
 * ccCheckPendingCommands, never from this response.
 *
 * Wiring note: these functions are exposed per this task's interface (a
 * proposal-row "Approve"/"Reject" control triggering them) but are not
 * themselves wired into `renderProposals()`/`showProposalDrawer()` here --
 * same deferred-wiring boundary `ccOpenCloseDrawer` already documents above
 * ("invoked ... once a later task wires that control into the positions
 * table"): the M1-R proposal-card rendering is out of this task's scope to
 * restructure. */

function ccIsLive(accountMode) {
  return String(accountMode || '').toLowerCase() === 'live';
}

async function ccRequestPreflight(commandId, action, params, expectedVersion) {
  const result = await ccPost('/api/preflight', {
    command_id: commandId, action, params,
    expected_version: expectedVersion,
  }, {okStatus: 200});
  if (!result.ok) {
    ccToast('error',
            `Preflight failed: ${result.error.message} (${result.error.code})`);
    return null;
  }
  return result.data;  // {command_id, nonce, expires_at, summary}
}

function ccOpenConfirmDrawer(ticket, onConfirm, onExpired) {
  const d = document.getElementById('cc-confirm-drawer');
  const s = ticket.summary;
  const set = (name, value) => {
    d.querySelector(`[data-field=${name}]`).textContent =
        value === null || value === undefined ? '—' : String(value);
  };
  set('side', s.side);
  set('instrument', s.instrument);
  set('quantity', s.quantity);
  set('notional', s.notional);
  set('order_type', s.order_type);
  set('latest_price', s.latest_price);
  set('drift_bps', s.drift_bps);
  set('account', `${s.account_id} (${String(s.account_mode).toUpperCase()})`);
  const warnings = d.querySelector('[data-field=warnings]');
  warnings.replaceChildren(...(s.warnings || []).map((w) => {
    const li = document.createElement('li');
    li.textContent = w;
    return li;
  }));

  const confirmBtn = d.querySelector('#cc-confirm-button');
  const countdown = d.querySelector('#cc-confirm-countdown');
  confirmBtn.disabled = false;
  const expiresAt = Date.parse(ticket.expires_at);
  const timer = setInterval(() => {
    const left = Math.max(0, Math.round((expiresAt - Date.now()) / 1000));
    countdown.textContent = `${left}s`;
    if (left <= 0) {
      clearInterval(timer);
      confirmBtn.disabled = true;
      countdown.textContent = 'Preflight expired — re-run to confirm';
      if (onExpired) onExpired();
    }
  }, 250);

  confirmBtn.onclick = () => {
    clearInterval(timer);
    d.hidden = true;
    onConfirm(ticket.nonce);
  };
  d.querySelector('#cc-confirm-cancel').onclick = () => {
    clearInterval(timer);
    d.hidden = true;
  };
  d.hidden = false;
}

async function ccRunLiveCeremony(kind, label, action, params,
                                 expectedVersion, submit) {
  if (!ccRequireAvailable(kind)) return;
  const commandId = ccNewCommandId();  // reused across retries
  const run = async () => {
    const ticket = await ccRequestPreflight(commandId, action, params,
                                            expectedVersion);
    if (!ticket) return;
    ccOpenConfirmDrawer(ticket,
        (nonce) => submit(commandId, nonce),
        () => ccToast('warn', `${label}: preflight expired — reopen to retry`));
  };
  await run();
}

/* ---- Proposal approve / reject actions (spec 9.2) ---- */

async function ccApproveProposal(proposal) {
  const expectedVersion = proposal.entity_revision;
  const url = `/api/commands/proposals/${proposal.id}/approve`;
  if (!ccIsLive(proposal.account_mode)) {
    await ccSubmitCommand('approve_proposal', `Approve #${proposal.id}`, url, {
      command_id: ccNewCommandId(),
      expected_version: expectedVersion,
      preflight_nonce: null,
    });
    return;
  }
  await ccRunLiveCeremony('approve_proposal', `Approve #${proposal.id}`,
      'approve_proposal', {proposal_id: proposal.id}, expectedVersion,
      (commandId, nonce) => ccSubmitCommand('approve_proposal',
          `Approve #${proposal.id}`, url, {
            command_id: commandId,
            expected_version: expectedVersion,
            preflight_nonce: nonce,
          }));
}

async function ccRejectProposal(proposal) {
  // Immediate, idempotent, risk-reducing in both modes.
  await ccSubmitCommand('reject_proposal', `Reject #${proposal.id}`,
      `/api/commands/proposals/${proposal.id}/reject`, {
        command_id: ccNewCommandId(),
        reason: '',
      });
}

/* ===================== [M1-C] Task 5: order cancel + cancel-all ==========
 * Working-order cancel (spec 9.7). An entry-leg cancel just removes PENDING
 * exposure -- risk-REDUCING, immediate single POST in both modes, same as
 * reject_proposal above. A protective-leg cancel strips protection from an
 * already-open position -- risk-INCREASING -- so it runs a confirmation
 * ceremony that names the position left unprotected: a plain confirm()
 * dialog on paper, the signed two-stage live ceremony (ccRunLiveCeremony,
 * already built for approve_proposal) on live. The SERVER is still the real
 * authority: `classify_cancel` (trader/trading/command_coordinator.py)
 * re-derives this from durable order-group state and enforces the nonce
 * requirement itself -- this classification only drives which UX ceremony
 * the browser runs, it is never trusted for the actual gate.
 *
 * Source-vs-brief drift: the plan assumed `CC.entities('order')` /
 * `CC.entity('position', id)` accessors and an `order.leg_role` field with
 * values `entry|parent|take_profit|stop|trailing_stop|null`. Neither the
 * `CC.entities`/`CC.entity` helpers nor a `leg_role` field exist anywhere in
 * the landed [M1-R] client store or the real [M1-F2]/[M1-F3] order payload
 * (`trader/data/broker_state.py`'s `BrokerOrderRow.to_payload()`) -- the
 * real field is `leg`, produced ONLY by `order_correlation.classify_leg`
 * with values `"entry" | "stop" | "take_profit" | f"child-{id}" | null`
 * (mirrored server-side by `classify_cancel`, the real authority this
 * ceremony defers to). This section reads the real store shape instead --
 * `store.view.orders.active`/`.terminal`, `store.view.positions`,
 * `store.view.accounts` -- the exact same shape `renderOrders`/
 * `renderPositions`/`showProposalDrawer` above already read, rather than
 * inventing the accessors the brief assumed. Orders also carry no
 * `account_mode` of their own (unlike proposals): live/paper is resolved
 * from the account entity matching the order's `account_id`, the same way
 * `renderStatusBar` derives the status bar's LIVE/PAPER badge.
 *
 * Wiring note: same deferred-wiring boundary `ccOpenCloseDrawer`/
 * `ccApproveProposal` above already document -- these functions are exposed
 * per this task's interface (a working-order row's "Cancel" control and a
 * "Cancel all" button trigger them) but are not themselves wired into
 * `renderOrders()` here; that is a later UI-wiring pass, not a restructure
 * of the M1-R client store. */

const CC_ENTRY_LEGS = new Set(['entry']);

function ccClassifyOrder(order) {
  // Fail-safe: an unclassifiable leg (null, external, or any non-entry
  // value such as a protective stop/take-profit/child leg) is PROTECTIVE,
  // never entry -- mirrors classify_cancel's "any non-entry leg, including
  // a missing one, is INCREASING" rule exactly.
  const leg = String(order.leg || '').toLowerCase();
  return CC_ENTRY_LEGS.has(leg) ? 'entry' : 'protective';
}

function ccOrderAccountMode(order) {
  const v = store.view;
  if (!v || !v.accounts) return null;
  const acct = v.accounts.find((a) =>
      a.account_id === order.account_id || a.entity_id === order.account_id)
      || v.accounts[0];
  return acct ? acct.mode : null;
}

function ccFindPosition(order) {
  const v = store.view;
  if (!v || !v.positions) return null;
  const id = `${order.account_id}:${order.conid}`;
  return v.positions.find((p) => p.entity_id === id) || null;
}

function ccOrderEntityId(order) {
  return order.order_entity_id || order.entity_id;
}

function ccUnprotectedPositionLabel(order) {
  const position = ccFindPosition(order);
  const symbol = order.symbol || (position && position.symbol) || order.conid;
  const qty = position ? position.quantity : '?';
  return `${symbol} (${order.account_id}, qty ${qty})`;
}

async function ccCancelOrder(order) {
  const orderEntityId = ccOrderEntityId(order);
  const url = `/api/commands/orders/${encodeURIComponent(orderEntityId)}/cancel`;
  const label = `Cancel order ${orderEntityId}`;

  if (ccClassifyOrder(order) === 'entry') {
    // Risk-reducing: immediate, idempotent, both modes -- no ceremony.
    await ccSubmitCommand('cancel_order', label, url,
        {command_id: ccNewCommandId(), preflight_nonce: null});
    return;
  }

  const positionLabel = ccUnprotectedPositionLabel(order);
  if (!ccIsLive(ccOrderAccountMode(order))) {
    // Paper ceremony: one authenticated POST, but an explicit confirm
    // dialog that names the position left unprotected.
    const ok = window.confirm(
        `Cancel PROTECTIVE order ${orderEntityId}?\n` +
        `This leaves ${positionLabel} unprotected.`);
    if (!ok) return;
    await ccSubmitCommand('cancel_order', label, url,
        {command_id: ccNewCommandId(), preflight_nonce: null});
    return;
  }

  // Live: signed two-stage preflight; the drawer's authoritative summary/
  // warnings name the unprotected position (the coordinator includes it
  // server-side in summary.warnings).
  await ccRunLiveCeremony('cancel_order', label, 'cancel_order',
      {order_entity_id: orderEntityId}, null,
      (commandId, nonce) => ccSubmitCommand('cancel_order', label, url,
          {command_id: commandId, preflight_nonce: nonce}));
}

/* ---- Cancel all: one confirmation listing every working order with its
 * classification, fanned out server-side to per-order commands under one
 * correlation id. Mirrors `CancelCommandService.cancel_orders`' outcome
 * shape `{child_command_ids, children: {order_entity_id: {command_id,
 * state, error_code, classification}}, partial_failure}` -- the per-child
 * truth resolves later from `command.updated` events, same as every other
 * command in this file; this dialog only drives the ONE up-front
 * confirmation spec 9.7 requires for the whole batch. */

function ccOpenCancelAllDialog() {
  const v = store.view;
  const orders = v && v.orders ? v.orders.active : [];
  if (!orders || orders.length === 0) {
    ccToast('warn', 'No working orders to cancel');
    return;
  }
  const d = document.getElementById('cc-cancel-all-dialog');
  const list = d.querySelector('#cc-cancel-all-list');
  list.replaceChildren(...orders.map((o) => {
    const li = document.createElement('li');
    const cls = ccClassifyOrder(o);
    li.textContent = `${ccOrderEntityId(o)} — ${o.symbol || o.conid || ''} ` +
        `${o.action || ''} ${o.total_quantity ?? ''} [${cls.toUpperCase()}]` +
        (cls === 'protective'
            ? ` — leaves ${ccUnprotectedPositionLabel(o)} unprotected` : '');
    return li;
  }));
  const orderIds = orders.map(ccOrderEntityId);
  d.dataset.orderIds = JSON.stringify(orderIds);
  d.dataset.hasProtective =
      String(orders.some((o) => ccClassifyOrder(o) === 'protective'));
  d.dataset.hasLive =
      String(orders.some((o) => ccIsLive(ccOrderAccountMode(o))));
  d.hidden = false;
}

function ccCancelAll() {
  ccOpenCancelAllDialog();
}

document.getElementById('cc-cancel-all-confirm').addEventListener('click',
    async () => {
      const d = document.getElementById('cc-cancel-all-dialog');
      d.hidden = true;
      const orderIds = JSON.parse(d.dataset.orderIds || '[]');
      if (orderIds.length === 0) return;
      const riskIncreasing = d.dataset.hasProtective === 'true' &&
                             d.dataset.hasLive === 'true';
      const submit = (commandId, nonce) => ccSubmitCommand('cancel_orders',
          `Cancel all (${orderIds.length} orders)`,
          '/api/commands/orders/cancel-all', {
            command_id: commandId,
            order_entity_ids: orderIds,
            preflight_nonce: nonce,
          });
      if (riskIncreasing) {
        await ccRunLiveCeremony('cancel_orders',
            `Cancel all (${orderIds.length} orders)`, 'cancel_orders',
            {order_entity_ids: orderIds}, null, submit);
      } else {
        await submit(ccNewCommandId(), null);
      }
    });
document.getElementById('cc-cancel-all-abort').addEventListener('click',
    () => { document.getElementById('cc-cancel-all-dialog').hidden = true; });

/* ===================== [M1-C] Task 6: strategy control + pause/resume =====
 * Spec 9.3 (strategy enable/disable/params) / 9.4 (pause new trading).
 * Strategy enable and resume (unpause) let new risk on -- they mirror
 * approve_proposal/T4's live-gate + two-stage ceremony. Strategy disable and
 * pause are risk-reducing and immediate in both modes, same as
 * reject_proposal / an entry-leg cancel above -- no ceremony, no live-gate,
 * even if a nonce happens to be passed through.
 *
 * Source-vs-brief drift: the plan assumed a `strategy.account_mode` field, a
 * `strategy.owns_exposure` field, and a `CC.entity('trading_control',
 * accountId)` accessor. None exist. The real "strategy" entity payload
 * (`StrategyControlCommandService.acknowledge_state`,
 * trader/trading/command_coordinator.py) is `{strategy_name, action,
 * strategy_state, control_revision, state_revision, error}` plus the
 * generic `entity_id`/`entity_revision` every row gets -- no `account_mode`,
 * no `owns_exposure`, no `account_id` at all. Since a CommandFlags-
 * configured dashboard already binds to exactly one account/mode (see
 * ccApproveProposal's docstring above), a strategy's live/paper-ness is read
 * off the single dashboard account (`store.view.accounts[0].mode`, the same
 * value renderStatusBar's LIVE/PAPER badge already uses) rather than a
 * per-strategy field that doesn't exist. The `trading_control` entity IS
 * real (trader/trading/trading_control.py) and does carry
 * `new_exposure_paused`/`revision`, but its account/mode is likewise
 * resolved by matching its `account_id` against `store.view.accounts`,
 * mirroring `ccOrderAccountMode` above -- there is no `CC.entities`/
 * `CC.entity` helper (same gap Task 5's section above already documents).
 *
 * F3 wire-contract drift: `EnableStrategyRequest`/`DisableStrategyRequest`/
 * `UpdateStrategyParamsRequest` (trader/messaging/production_api.py) have no
 * `preflight_nonce` field at all (`requires_preflight=False, saga=True` at
 * the coordinator) -- routes_commands.py accepts `preflight_nonce` on the
 * enable/params bodies purely as ITS OWN live-gate signal and never forwards
 * it past that layer. That's invisible at this JS boundary: the shape this
 * file posts to those routes is unchanged from the brief.
 *
 * Wiring note: same deferred-wiring boundary `ccOpenCloseDrawer`/
 * `ccApproveProposal`/`ccCancelOrder` above already document -- these
 * functions are exposed per this task's interface (a strategy row's
 * "Enable"/"Disable"/"Edit params" controls and a pause/resume toggle
 * trigger them) but are not themselves wired into `renderStrategies()` or a
 * pause control here; that is a later UI-wiring pass, not a restructure of
 * the M1-R client store or renderer. */

function ccDashboardAccountMode() {
  const v = store.view;
  return v && v.accounts && v.accounts[0] ? v.accounts[0].mode : null;
}

function ccAccountModeFor(accountId) {
  const v = store.view;
  if (!v || !v.accounts) return null;
  const acct = v.accounts.find((a) =>
      a.account_id === accountId || a.entity_id === accountId) || v.accounts[0];
  return acct ? acct.mode : null;
}

function ccStrategyName(strategy) {
  return strategy.name || strategy.strategy_name || strategy.entity_id;
}

async function ccEnableStrategy(strategy) {
  const name = ccStrategyName(strategy);
  const url = `/api/commands/strategies/${encodeURIComponent(name)}/enable`;
  const label = `Enable ${name}`;
  const revision = strategy.control_revision;
  if (!ccIsLive(ccDashboardAccountMode())) {
    await ccSubmitCommand('enable_strategy', label, url,
        {command_id: ccNewCommandId(), expected_version: revision,
         preflight_nonce: null});
    return;
  }
  await ccRunLiveCeremony('enable_strategy', label, 'enable_strategy',
      {strategy_name: name}, revision,
      (commandId, nonce) => ccSubmitCommand('enable_strategy', label, url,
          {command_id: commandId, expected_version: revision, preflight_nonce: nonce}));
}

async function ccDisableStrategy(strategy) {
  // Risk-reducing (spec 9.3): immediate single POST in both modes, no
  // ceremony -- mirrors ccRejectProposal / an entry-leg ccCancelOrder above.
  // Exposure-ownership validation (whether disabling orphans an open exit)
  // is the coordinator's, never decided here.
  const name = ccStrategyName(strategy);
  const url = `/api/commands/strategies/${encodeURIComponent(name)}/disable`;
  const label = `Disable ${name}`;
  await ccSubmitCommand('disable_strategy', label, url,
      {command_id: ccNewCommandId(), expected_version: strategy.control_revision,
       preflight_nonce: null});
}

async function ccUpdateStrategyParams(strategy, params) {
  const name = ccStrategyName(strategy);
  const url = `/api/commands/strategies/${encodeURIComponent(name)}/params`;
  const label = `Update ${name} params`;
  const revision = strategy.control_revision;
  // Any parameter whose risk effect is unspecified is treated as
  // risk-increasing (the same fail-safe default ccClassifyOrder uses
  // above): live parameter changes take the ceremony, paper takes the
  // single CAS POST.
  if (!ccIsLive(ccDashboardAccountMode())) {
    await ccSubmitCommand('update_strategy_params', label, url,
        {command_id: ccNewCommandId(), expected_version: revision, params,
         preflight_nonce: null});
    return;
  }
  await ccRunLiveCeremony('update_strategy_params', label, 'update_strategy_params',
      {strategy_name: name, params}, revision,
      (commandId, nonce) => ccSubmitCommand('update_strategy_params', label, url,
          {command_id: commandId, expected_version: revision, params,
           preflight_nonce: nonce}));
}

/* ---- Pause new trading (spec 9.4) ----
 * paused=true is risk-reducing: immediate single POST, no revision, both
 * modes. paused=false (resume) is risk-increasing: paper sends the exact
 * current revision in one POST; live runs the signed ceremony. */

async function ccSetPause(accountId, paused, revision) {
  const label = paused ? `Pause new trading (${accountId})`
                       : `Resume new trading (${accountId})`;
  const url = '/api/commands/pause';
  const reason = paused ? 'operator pause' : 'operator resume';
  if (paused) {
    await ccSubmitCommand('set_trading_pause', label, url,
        {command_id: ccNewCommandId(), paused: true, expected_version: null,
         reason, preflight_nonce: null});
    return;
  }
  if (!ccIsLive(ccAccountModeFor(accountId))) {
    await ccSubmitCommand('set_trading_pause', label, url,
        {command_id: ccNewCommandId(), paused: false, expected_version: revision,
         reason, preflight_nonce: null});
    return;
  }
  await ccRunLiveCeremony('set_trading_pause', label, 'set_trading_pause',
      {paused: false}, revision,
      (commandId, nonce) => ccSubmitCommand('set_trading_pause', label, url,
          {command_id: commandId, paused: false, expected_version: revision,
           reason, preflight_nonce: nonce}));
}

/* ---------------- boot ----------------------------------------------------- */
resync();

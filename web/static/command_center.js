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
  // [M1-C] UI-wiring pass: mirrors data-commands-enabled (command_center.html
  // body tag, set from routes_read.py's /cc handler off app.state.command_
  // flags.commands_enabled). Every [M1-C] action affordance below checks
  // this before rendering -- false (the default, read-only deployment)
  // means the M1-R render functions behave exactly as before.
  commandsEnabled: document.body.dataset.commandsEnabled === 'true',
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
  // Quotes carry their own server_received_timestamp; freshness is computed
  // from that (corrected onto the client clock via serverClockOffsetMs learned
  // from the snapshot's generated_at), NOT from client arrival time -- that's
  // what let stale quotes look fresh after a snapshot/reconnect.
  quotes: {}, serverClockOffsetMs: 0,
  // Authoritative bridge health, refreshed by an independent /api/cc-health
  // poll (see refreshHealth) so the degraded banner reflects whether the
  // server actually has live trader data -- not merely that the SSE socket to
  // the dashboard process is open.
  health: null,
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
  const sequence = env.sequence;
  // EventSource can redeliver the last event while reconnecting.  Ignore
  // those duplicates, but never skip forward: a non-contiguous sequence
  // means at least one state transition was missed and only a fenced
  // snapshot can restore a coherent view.
  if (Number.isSafeInteger(sequence) && sequence <= store.sequence) return;
  if (!Number.isSafeInteger(sequence) || sequence !== store.sequence + 1) {
    resync();
    return;
  }
  store.sequence = sequence;
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
  // Merge latest-value; each quote already carries its own
  // server_received_timestamp (no client arrival stamping).
  Object.assign(store.quotes, batch);
  renderPositions();
}

function applySnapshot(view) {
  // Reject a late/overlapping snapshot that would roll state backward (a slow
  // older fetch resolving after a newer one). Returns whether it was applied.
  if (!ccSnapshotSupersedes(store.streamId, store.sequence, view)) return false;
  store.view = view;
  store.streamId = view.stream_id;
  store.sequence = view.sequence;
  store.quotes = view.quotes || {};
  // Calibrate the client<->server clock offset from this snapshot's
  // server-stamped generated_at so quote ages are skew-corrected.
  store.serverClockOffsetMs = ccServerClockOffsetMs(view.generated_at, Date.now());
  if (view.health) store.health = view.health;
  const boot = document.getElementById('boot-banner');
  if (boot) {
    boot.hidden = true;
    if (typeof boot.setAttribute === 'function') boot.setAttribute('hidden', '');
  }
  renderAll();
  return true;
}

/* ---------------- connection management ---------------------------------- */
let es = null, pollTimer = null, disconnectedAt = null, everConnected = false;
let snapshotGen = 0, snapshotInFlight = false;
const SNAPSHOT_TIMEOUT_MS = 8000;

async function fetchSnapshot() {
  // Bounded fetch: a hung snapshot must never wedge recovery forever.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), SNAPSHOT_TIMEOUT_MS);
  try {
    const response = await fetch('/api/snapshot',
      { credentials: 'same-origin', signal: ctrl.signal });
    if (response.status === 401) { window.location.href = '/cc/login'; return null; }
    if (!response.ok) return null;      // 503 not-ready etc.: stay degraded, retry
    return await response.json();
  } catch (err) {
    return null;                        // timeout / abort / network -> no snapshot
  } finally {
    clearTimeout(timer);
  }
}

// Single in-flight snapshot fetch guarded by a generation token: only the most
// recently initiated fetch may apply, so a slow older response can never roll
// state back over a newer one (applySnapshot also rejects a stale sequence).
async function fetchAndApplySnapshot() {
  if (snapshotInFlight) return false;
  snapshotInFlight = true;
  const gen = ++snapshotGen;
  try {
    const view = await fetchSnapshot();
    if (gen !== snapshotGen || !view) return false;
    return applySnapshot(view);
  } finally {
    snapshotInFlight = false;
  }
}

// Enter the degraded/recovering state and drive ONE recovery loop: fetch a
// coherent snapshot, then reconnect SSE. Idempotent -- a second call while
// already recovering is a no-op (guarded on connection.mode), so a
// resync_required event, a stream mismatch, and the transport watchdog can all
// funnel here without spawning overlapping loops. Shows the banner immediately
// because the readyState watchdog can't fire while es is null mid-fetch.
function resync() {
  if (store.connection.mode === 'polling') return;
  store.connection.mode = 'polling';
  updateBanner();
  if (es) { es.close(); es = null; }
  recoverTick();
}

async function recoverTick() {
  if (store.connection.mode !== 'polling') return;
  if (await fetchAndApplySnapshot()) {
    stopPolling();                      // return to SSE only after a coherent snapshot
    connectSse();
    return;
  }
  if (store.connection.mode === 'polling') {
    pollTimer = setTimeout(recoverTick, CFG.pollIntervalMs);
  }
}

function connectSse() {
  if (es) es.close();
  // A fresh connection attempt gets a fresh grace window: clear the
  // "disconnected since" clock so the 1s watchdog can't fire mid-handshake and
  // abort this reconnect (onerror re-arms it if THIS attempt fails).
  disconnectedAt = null;
  const after = store.streamId ? `?after=${store.streamId}:${store.sequence}` : '';
  es = new EventSource('/api/events' + after);
  es.onopen = () => {
    everConnected = true; disconnectedAt = null; stopPolling(); updateBanner();
  };
  es.onerror = () => {
    if (disconnectedAt === null) disconnectedAt = Date.now();
    updateBanner();
  };
  DOMAIN_EVENT_TYPES.forEach(t =>
    es.addEventListener(t, e => applyEvent(JSON.parse(e.data))));
  es.addEventListener('quote.updated',
    e => applyQuotes(JSON.parse(e.data).quotes));
  es.addEventListener('quotes.snapshot', e => {
    // Replace (not merge): the reconnect baseline is authoritative; freshness
    // still comes from each quote's server_received_timestamp.
    store.quotes = JSON.parse(e.data).quotes || {};
    renderPositions();
  });
  es.addEventListener('resync_required', () => resync());
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
  store.connection.mode = 'sse';
}

function setBanner(visible) {
  document.getElementById('degraded-banner').hidden = !visible;
}

function currentSseState() {
  // A normal first connection attempt gets a clean grace state to avoid a
  // startup flicker.  Snapshot polling is different: it means the client has
  // no coherent live baseline yet, so it must remain visibly degraded even
  // before EventSource has connected successfully for the first time.
  if (!everConnected) {
    return { open: true, disconnectedForMs: null,
             degradedAfterMs: CFG.degradedAfterMs,
             polling: store.connection.mode === 'polling' };
  }
  return {
    open: !!es && es.readyState === EventSource.OPEN,
    disconnectedForMs: disconnectedAt === null ? null : Date.now() - disconnectedAt,
    degradedAfterMs: CFG.degradedAfterMs,
    polling: store.connection.mode === 'polling',
  };
}

function updateBanner() {
  const lifecycle = store.health && store.health.lifecycle;
  setBanner(ccIsDegraded(lifecycle, currentSseState()));
}

async function refreshHealth() {
  // Authoritative source-freshness poll, independent of the SSE socket: an open
  // EventSource to the dashboard process says nothing about whether that
  // process still has live trader data. Drives the banner + dependency chips.
  try {
    const res = await fetch('/api/cc-health', { credentials: 'same-origin' });
    if (res.status === 401) { window.location.href = '/cc/login'; return; }
    if (!res.ok) return;
    const h = await res.json();
    store.health = { lifecycle: h.lifecycle, sources: h.sources,
                     reconnects: h.reconnects, cursor: h.cursor };
    renderStatusBar();
    updateBanner();
  } catch (err) { /* transient; the next tick retries */ }
}

// Transport watchdog: an SSE that errored and stayed non-open past the grace
// window falls back to snapshot polling via resync(). Also re-evaluates the
// banner every tick so crossing the grace window (or a health change) is
// reflected even with no other event.
setInterval(() => {
  if (es && es.readyState !== EventSource.OPEN && disconnectedAt !== null
      && Date.now() - disconnectedAt >= CFG.degradedAfterMs
      && store.connection.mode !== 'polling') {
    resync();
  }
  updateBanner();
}, 1000);

// Independent health poll (issue #6): keep source freshness current even while
// SSE is open.
setInterval(refreshHealth, CFG.pollIntervalMs);

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
  const mode = ccAccountModeValue(account) || 'unknown';
  badge.textContent = mode.toUpperCase();
  badge.className = 'badge ' + (mode === 'live' ? 'live' : mode === 'paper' ? 'paper' : '');
  // LIVE-mode red command band (design 1c): tint the whole band on a real
  // account so a live book can never be mistaken for paper.
  const band = document.getElementById('status-bar');
  if (band) band.classList.toggle('live', mode === 'live');
  document.getElementById('account-id').textContent =
    account.entity_id || account.account_id || '—';
  const chips = document.getElementById('dependency-chips');
  // Prefer the independently-polled store.health (issue #6) so the chips + the
  // bridge lifecycle stay current during live SSE, not frozen at snapshot time.
  const health = store.health || v.health || {};
  const sources = health.sources || {};
  chips.innerHTML = Object.entries(sources).map(([name, s]) =>
    `<span class="chip" data-state="${esc(s.state)}">${esc(name)}: ${esc(s.state)}` +
    (s.last_success_age_seconds !== null && s.last_success_age_seconds !== undefined
      ? ` (${fmtAge(s.last_success_age_seconds)})` : '') + '</span>').join('');
  chips.innerHTML += `<span class="chip" data-state="${
    health.lifecycle === 'live' ? 'ok' : 'error'}">bridge: ${
    esc(health.lifecycle || 'unknown')}</span>`;
  document.querySelector('#last-event-time .v').textContent =
    v.last_event_at ? `${v.last_event_at} (${fmtAge(ageOf(v.last_event_at))} ago)` : '—';
}

function renderAccountCards() {
  const v = store.view; if (!v) return;
  const account = v.accounts[0] || {};
  // Net liquidation must render for a flat account too (spec §8.1): the value
  // comes from the account entity, never derived from positions. The two
  // headline figures live in the command band; the rest fills the
  // quick-stats row under the action queue.
  const netEl = document.getElementById('band-netliq');
  if (netEl) netEl.textContent = money(account.net_liquidation, account.currency);
  const dayEl = document.getElementById('band-daypnl');
  if (dayEl) {
    const pnl = account.daily_pnl;
    dayEl.textContent = (pnl > 0 ? '+' : '') + money(pnl, account.currency);
    dayEl.className = 'band-v' + (pnl > 0 ? ' pos' : pnl < 0 ? ' neg' : '');
  }
  const stats = [
    ['Exposure', money(account.gross_exposure, account.currency)],
    ['Buying power', money(account.buying_power, account.currency)],
    ['Margin cushion', account.margin_cushion !== undefined && account.margin_cushion !== null
      ? `${fmt.format(account.margin_cushion * 100)}%` : '—'],
    ['Open positions', fmt.format((v.positions || []).length)],
    ['Working orders', fmt.format(((v.orders || {}).active || []).length)],
  ];
  const quick = document.getElementById('quick-stats');
  if (quick) {
    quick.innerHTML = stats.map(([k, val]) =>
      `<div class="q"><div class="k">${k}</div><div class="v">${esc(val)}</div></div>`
    ).join('');
  }
}

function renderPositions() {
  const v = store.view; if (!v) return;
  const meta = document.getElementById('positions-meta');
  if (meta) {
    const account = v.accounts[0] || {};
    meta.textContent = `${v.positions.length} open`
      + (account.gross_exposure !== undefined && account.gross_exposure !== null
        ? ` · ${money(account.gross_exposure, account.currency)} gross` : '');
  }
  const body = document.getElementById('positions-body');
  body.innerHTML = v.positions.map(p => {
    const conid = String(p.conid ?? (p.entity_id || '').split(':').pop());
    const quote = store.quotes[conid];
    const last = quote ? quote.last : null;
    // Age from the quote's own server_received_timestamp (skew-corrected via
    // the snapshot clock offset), never from client arrival time -- so a stale
    // quote reads stale immediately after a snapshot/reconnect. null == unknown
    // -> stale, never silently "fresh".
    const quoteAge = ccQuoteAgeSeconds(quote, store.serverClockOffsetMs, Date.now());
    const stale = quoteAge === null || quoteAge > CFG.staleAfterS;
    const pnl = p.unrealized_pnl;
    return `<tr class="${stale ? 'stale' : ''}" data-entity="${esc(p.entity_id)}">
      <td class="sym">${esc(p.symbol || conid)}</td>
      <td class="num">${fmt.format(p.quantity ?? 0)}</td>
      <td class="num">${money(p.avg_cost)}</td>
      <td class="num">${money(last)}</td>
      <td>${esc(p.currency || '')}</td>
      <td class="num">${money(p.market_value, p.currency)}</td>
      <td class="num">${p.base_market_value !== undefined
        ? money(p.base_market_value, p.base_currency) : '— (no conversion)'}</td>
      <td class="num ${pnl >= 0 ? 'pos' : 'neg'}">${money(pnl)}</td>
      <td class="num ${p.daily_pnl > 0 ? 'pos' : p.daily_pnl < 0 ? 'neg' : ''}">${
        money(p.daily_pnl)}</td>
      <td><span class="age">${fmtAge(quoteAge)}</span></td>
      ${CFG.commandsEnabled ? `<td><button type="button"
        data-cc-close-position="${esc(p.entity_id)}">Close</button></td>` : ''}
    </tr>`;
  }).join('');
}

const PROPOSAL_FILTER = { mode: 'pending' };  // pending | all | terminal

function proposalFilterRows() {
  const v = store.view; if (!v) return [];
  const active = v.proposals.active || [];
  const terminal = v.proposals.terminal || [];
  if (PROPOSAL_FILTER.mode === 'terminal') return terminal;
  if (PROPOSAL_FILTER.mode === 'all') return [...active, ...terminal];
  return active;
}

function renderProposals() {
  const v = store.view; if (!v) return;
  const rail = document.getElementById('proposal-cards');
  if (!rail) return;
  const pendingBtn = document.querySelector('[data-proposal-filter="pending"]');
  if (pendingBtn) pendingBtn.textContent = `Pending ${(v.proposals.active || []).length}`;
  const rows = proposalFilterRows();
  const cards = rows.map(p => {
    const age = ageOf(p.created_at);
    const status = String(p.status || '').toUpperCase();
    const side = String(p.action || p.side || '').toUpperCase();
    const sideCls = side === 'BUY' ? 'buy' : side === 'SELL' ? 'sell' : '';
    const thesis = p.thesis || p.reasoning || '';
    // Inline Approve/Reject reuse the exact drawer command path
    // (ccApproveProposal runs the live preflight ceremony when required);
    // Details falls through to the card click -> detail drawer.
    const actions = (CFG.commandsEnabled && status === 'PENDING')
      ? `<div class="prop-actions">
          <button type="button" class="primary"
            data-cc-approve="${esc(p.entity_id)}">Approve</button>
          <button type="button" class="reject"
            data-cc-reject="${esc(p.entity_id)}">Reject</button>
          <button type="button">Details</button>
        </div>` : '';
    return `<div class="proposal-card" tabindex="0" role="button"
        data-proposal="${esc(p.entity_id)}"
        aria-label="Proposal ${esc(p.entity_id)} details">
      <div class="prop-top">
        <span class="side ${sideCls}">${esc(side || '—')}</span>
        <span class="prop-sym">${esc(p.symbol || '')}</span>
        <span class="prop-age"><span class="age">${fmtAge(age)} old</span></span>
      </div>
      <div class="prop-meta"><span>#${esc(p.entity_id)}</span>
        <span>qty <b>${esc(p.quantity ?? 'auto')}</b></span>
        <span>notional <b>${money(p.amount, p.currency)}</b></span>
        <span>conf <b>${esc(p.confidence ?? '—')}</b></span>
        <span>status <b>${esc(status)}</b></span>
        <span>source ${esc(p.source || '—')}</span></div>
      ${thesis ? `<div class="prop-thesis">${esc(thesis)}</div>` : ''}
      ${actions}
    </div>`;
  });
  const emptyMsg = PROPOSAL_FILTER.mode === 'pending'
    ? 'No pending proposals.'
    : (PROPOSAL_FILTER.mode === 'terminal'
      ? 'No terminal proposals in the live feed yet.'
      : 'No proposals.');
  rail.innerHTML = cards.join('')
    || `<div class="proposal-card dim">${emptyMsg}</div>`;
}

function renderOrders() {
  const v = store.view; if (!v) return;
  // Cancellable == currently active (not yet terminal) -- mirrors the same
  // active/terminal split state.py's _place() already enforces server-side.
  const activeIds = new Set(v.orders.active.map(o => o.entity_id));
  const orders = [...v.orders.active, ...v.orders.terminal];
  const groups = new Map();
  for (const o of orders) {
    const gid = o.order_group_id || `solo:${o.entity_id}`;
    if (!groups.has(gid)) groups.set(gid, []);
    groups.get(gid).push(o);
  }
  // Leg classification chip from the real `leg` field
  // (order_correlation.classify_leg): entry vs protective (stop/take-profit/
  // child legs). Unclassified legs get no chip — never a guessed one.
  const kindOf = (leg) => {
    const l = String(leg || '').toLowerCase();
    if (l === 'entry') return ['ENTRY', 'entry'];
    if (l === 'stop' || l === 'take_profit' || l.startsWith('child')) {
      return ['PROTECTIVE', 'prot'];
    }
    return null;
  };
  // Aggregate group status without hiding per-leg state (spec §8.4).
  document.getElementById('order-groups').innerHTML =
    [...groups.entries()].map(([gid, legs]) => {
      const statuses = [...new Set(legs.map(l => String(l.status || '')))];
      const filled = legs.reduce((n, l) => n + (l.filled_quantity || 0), 0);
      return `<div class="order-group">
        <div class="group-head">${esc(gid)}<span class="n">${legs.length} leg(s) ·
          ${esc(statuses.join(' / '))} · filled ${fmt.format(filled)}</span></div>
        ${legs.map(l => {
          const kind = kindOf(l.leg);
          return `<div class="leg">
          ${kind ? `<span class="kind ${kind[1]}">${kind[0]}</span>` : ''}
          <span>${esc(l.symbol || l.conid || '')}
          ${esc(l.action || '')} ${fmt.format(l.quantity ?? 0)}
          @ ${esc(l.order_type || '')}</span>
          <span class="leg-r">${esc(l.status || '')} · filled ${fmt.format(l.filled_quantity || 0)}
          ${l.avg_fill_price ? '@ ' + money(l.avg_fill_price) : ''}
          ${CFG.commandsEnabled && activeIds.has(l.entity_id)
            ? ` <button type="button"
                data-cc-cancel-order="${esc(l.entity_id)}">Cancel</button>` : ''}
          </span></div>`;
        }).join('')}
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
    // `strategy_state` is the REAL field on the strategy row (the only
    // producer is StrategyControlCommandService.acknowledge_state,
    // trader/trading/command_coordinator.py -- see this file's Task 5/6
    // drift notes below); `runtime_state`/`state` are kept as fallbacks for
    // any other future producer rather than dropped outright.
    const state = String(s.strategy_state || s.runtime_state || s.state || '')
        .toUpperCase();
    const enabled = DISPATCHABLE_STRATEGY.has(state);
    const name = esc(ccStrategyName(s));
    const actions = CFG.commandsEnabled ? `<td>
        ${enabled
          ? `<button type="button" class="reject" data-cc-strategy-action="disable"
               data-cc-strategy="${name}">Disable</button>`
          : `<button type="button" class="primary" data-cc-strategy-action="enable"
               data-cc-strategy="${name}">Enable</button>`}
        <button type="button" data-cc-strategy-action="params"
          data-cc-strategy="${name}">Edit params</button>
      </td>` : '';
    const stateCls = state === 'ERROR' ? 'err' : enabled ? 'run' : '';
    return `<tr><td class="sym">${name}</td>
      <td><span class="state-chip ${stateCls}">${esc(state)}</span></td>
      <td><span class="dotstate ${enabled ? 'on' : 'off'}"><i class="d"></i>${
        enabled ? 'enabled' : 'not dispatchable'}</span></td>
      <td><span class="age">${fmtAge(ageOf(s.last_activity_at))}</span></td>
      <td class="err-note ${s.last_error ? 'neg' : 'dim'}">${
        s.last_error ? '⚠ ' + esc(s.last_error) : '—'}</td>${actions}</tr>`;
  }).join('');
  renderPauseControl();
  renderStrategyAlert();
}

// Strategy-error alert banner (design 1c error-prevention layer): a red band
// under the tabs whenever a strategy row is in ERROR, naming the first one and
// counting the rest. Elevates a failure that would otherwise only show as one
// red row in the Strategies panel below the fold.
function renderStrategyAlert() {
  const el = document.getElementById('strategy-alert');
  if (!el) return;
  const v = store.view;
  const errs = ((v && v.strategies) || []).filter(s =>
    String(s.strategy_state || s.runtime_state || s.state || '').toUpperCase() === 'ERROR');
  if (!errs.length) { el.hidden = true; return; }
  const n = errs.length;
  el.querySelector('.sa-badge').textContent = `${n} ${n === 1 ? 'error' : 'errors'}`;
  const first = errs[0];
  const detail = first.last_error ? ` — ${first.last_error}` : '';
  const more = n > 1 ? ` · +${n - 1} more` : '';
  el.querySelector('.sa-msg').textContent =
    `${ccStrategyName(first)} halted${detail}${more}`;
  el.hidden = false;
}

/* ---- [M1-C] UI-wiring pass: account-level pause/resume control ----------
 * Placed here (rather than a standalone entry in renderAll()) per the
 * wiring task's own placement ("renderStrategies(...): ... and the
 * pause/resume control"), even though trading_control is account-scoped,
 * not per-strategy. */
function ccFindTradingControl(view, accountId) {
  const controls = (view && view.trading_control) || [];
  if (!controls.length) return null;
  const accounts = (view && view.accounts) || [];
  const account = accounts[0] || {};
  const candidates = [accountId, account.entity_id, account.account_id]
    .filter((value) => value !== null && value !== undefined && value !== '')
    .map((value) => String(value));
  if (candidates.length) {
    const match = controls.find((tc) => {
      const ids = [tc.account_id, tc.entity_id]
        .filter((value) => value !== null && value !== undefined && value !== '')
        .map((value) => String(value));
      return ids.some((id) => candidates.includes(id));
    });
    if (match) return match;
  }
  // Single-account books often have one control row; prefer it over an
  // infinite "waiting…" state when the account id field naming drifts.
  if (controls.length === 1) return controls[0];
  return null;
}

function renderPauseControl() {
  if (!CFG.commandsEnabled) return;
  const toggle = document.getElementById('cc-pause-toggle');
  const stateEl = document.getElementById('cc-pause-state');
  if (!toggle || !stateEl) return;
  const v = store.view; if (!v) return;
  const account = (v.accounts || [])[0] || {};
  const accountId = account.entity_id || account.account_id;
  const control = ccFindTradingControl(v, accountId);
  if (!control) {
    const controls = v.trading_control || [];
    stateEl.textContent = controls.length
      ? 'trading control account mismatch'
      : 'trading control unavailable';
    toggle.textContent = 'Pause new trading';
    toggle.disabled = true;
    toggle.onclick = null;
    return;
  }
  toggle.disabled = false;
  const paused = !!control.new_exposure_paused;
  const revision = control.revision;
  const pauseAccountId = control.account_id || control.entity_id || accountId;
  stateEl.textContent = paused ? '⏸ paused' : '● active';
  toggle.textContent = paused ? 'Resume new trading' : 'Pause new trading';
  toggle.onclick = () => CCCommands.setPause(pauseAccountId, !paused, revision);
}
globalThis.ccFindTradingControl = ccFindTradingControl;

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

function _pctCeiling(value) {
  if (value == null || !Number.isFinite(Number(value))) return '—';
  return (Number(value) * 100).toFixed(2) + '%';
}

function renderScaling() {
  const panel = document.getElementById('scaling-panel');
  if (!panel) return;
  const v = store.view;
  const scaling = (v && v.scaling) || {
    status: 'unknown', lifecycle: 'unknown',
    message: 'Snapshot not ready', authorities: [],
  };
  const lifecycle = scaling.lifecycle || scaling.status || 'unknown';
  const badge = document.getElementById('scaling-lifecycle');
  badge.dataset.lifecycle = lifecycle;
  badge.textContent = lifecycle;
  document.getElementById('scaling-message').textContent =
    scaling.message || 'No allocation message';
  document.getElementById('scaling-stage').textContent = scaling.stage || '—';
  document.getElementById('scaling-ceiling').textContent =
    _pctCeiling(scaling.max_gross_allocation);
  document.getElementById('scaling-event').textContent = scaling.event || '—';
  document.getElementById('scaling-expires').textContent =
    scaling.expires_at ? esc(String(scaling.expires_at)) : '—';

  const suspendBtn = document.getElementById('scaling-suspend');
  if (suspendBtn) {
    const gross = Number(scaling.max_gross_allocation);
    const canSuspend = (
      (lifecycle === 'active' || lifecycle === 'authorized')
      && Number.isFinite(gross) && gross > 0
    );
    suspendBtn.disabled = !canSuspend;
    suspendBtn.title = canSuspend
      ? ''
      : 'Suspend needs an active/authorized allocation with a positive ceiling';
  }

  const body = document.getElementById('scaling-authorities-body');
  const rows = scaling.authorities || [];
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="6" class="dim">No allocation authority rows yet — paste a signed attestation above for live capital, or use Paper automation below for paper.</td></tr>';
    return;
  }
  body.innerHTML = rows.slice().sort((a, b) =>
      (b.entity_revision || 0) - (a.entity_revision || 0)).map(row => {
    const sid = esc(row.entity_id || row.strategy_id || '—');
    return `<tr>
      <td>${sid}</td>
      <td>${esc(row.stage || '—')}</td>
      <td class="num">${_pctCeiling(row.max_gross_allocation)}</td>
      <td>${esc(row.event || '—')}</td>
      <td>${esc(row.expires_at || '—')}</td>
      <td class="num">${esc(row.entity_revision ?? '—')}</td>
    </tr>`;
  }).join('');
}

function _checklistItem(state, label, detail) {
  const mark = state === 'ok' ? '✓' : (state === 'wait' ? '…' : '✗');
  return `<li data-state="${esc(state)}"><span class="mark" aria-hidden="true">${mark}</span>`
    + `<span><strong>${esc(label)}</strong> — ${detail}</span></li>`;
}

function renderPaperAutomation() {
  const panel = document.getElementById('paper-auto-panel');
  if (!panel) return;
  const v = store.view;
  const pa = (v && v.paper_automation) || null;
  const lifecycle = (pa && pa.lifecycle) || 'unknown';
  const badge = document.getElementById('paper-auto-lifecycle');
  badge.dataset.lifecycle = lifecycle;
  badge.textContent = lifecycle;

  const restartBanner = document.getElementById('paper-auto-banner-restart');
  const partialBanner = document.getElementById('paper-auto-banner-partial');
  const showRestart = !!(pa && (pa.restart_required || lifecycle === 'restart_required'));
  const showPartial = !!(pa && (pa.armed_unpersisted || lifecycle === 'armed_unpersisted'));
  restartBanner.hidden = !showRestart;
  partialBanner.hidden = !showPartial;

  const dashboardMode = CCCommands.dashboardAccountMode();
  const paperOk = CCCommands.paperAutomationAllowed();
  const statusKnown = pa != null;
  const caReady = !!(pa && pa.command_authority_ready === true);

  let message = 'Waiting for paper automation status…';
  if (dashboardMode == null) {
    message = 'Waiting for account mode from the live snapshot…';
  } else if (!paperOk) {
    message = 'Paper automation is only available on paper accounts';
  } else if (!statusKnown) {
    message = 'Paper automation status unavailable — usually means '
      + 'command_authority.enabled is false (or trader query is down). '
      + 'Set it in ~/.config/mmr/trader.yaml and restart trader.';
  } else if (pa.last_error) {
    message = pa.last_error;
  } else if (showPartial) {
    message = 'Activation partially succeeded — click Activate again to complete.';
  } else if (showRestart) {
    message = 'Config written. Restart trader and strategy services to arm.';
  } else if (lifecycle === 'failed') {
    message = 'Last activate/deactivate failed — see last_error; automation is off';
  } else if (lifecycle === 'preparing') {
    message = 'Paper automation activation in progress…';
  } else if (lifecycle === 'disabled') {
    message = caReady
      ? 'Paper automation is disabled — select a strategy, enter a reason, then Activate.'
      : 'Paper automation is disabled — enable command_authority first (see checklist).';
  } else if (lifecycle === 'armed') {
    message = 'Paper automation is armed (hot-arm active)';
  } else if (lifecycle === 'degraded') {
    message = 'Paper automation is degraded — check materials and YAML';
  }
  document.getElementById('paper-auto-message').textContent = message;

  document.getElementById('paper-auto-strategy-value').textContent =
    (pa && pa.strategy_name) || '—';
  document.getElementById('paper-auto-account-mode').textContent =
    (pa && pa.account_mode)
      ? String(pa.account_mode).toUpperCase()
      : (dashboardMode ? String(dashboardMode).toUpperCase() : '—');
  document.getElementById('paper-auto-ca-ready').textContent =
    !statusKnown ? 'unknown' : (caReady ? 'ready' : 'not ready');
  document.getElementById('paper-auto-last-activated').textContent =
    (pa && pa.last_activated_at) ? String(pa.last_activated_at) : '—';

  const select = document.getElementById('paper-auto-strategy');
  if (select) {
    const previous = select.value;
    const live = ((v && v.strategies) || []).map(ccStrategyName).filter(Boolean);
    // Journaled strategy rows are empty until a control command acknowledges
    // state; Activate still needs YAML-deployed names — merge both.
    const deployed = (v && v.deployed_strategy_names) || [];
    const names = Array.from(new Set(live.concat(deployed))).filter(Boolean)
        .sort((a, b) => String(a).localeCompare(String(b)));
    const bound = (pa && pa.strategy_name) || '';
    const placeholder = names.length
      ? 'Select a strategy…'
      : 'No deployed strategies — use the Deploy tab first';
    const options = [`<option value="">${esc(placeholder)}</option>`]
        .concat(names.map(name =>
          `<option value="${esc(name)}">${esc(name)}</option>`));
    select.innerHTML = options.join('');
    const prefer = previous || bound;
    if (prefer && names.includes(prefer)) {
      select.value = prefer;
    } else if (bound && names.includes(bound)) {
      select.value = bound;
    }
  }

  const strategySelected = !!(select && select.value);
  const reasonEl = document.getElementById('paper-auto-reason');
  const reasonFilled = !!(reasonEl && (reasonEl.value || '').trim());

  const checklist = document.getElementById('paper-auto-checklist');
  if (checklist) {
    const items = [];
    if (dashboardMode == null) {
      items.push(_checklistItem('wait', 'Paper account',
        'Waiting for broker account mode in the snapshot.'));
    } else if (paperOk) {
      items.push(_checklistItem('ok', 'Paper account',
        `Mode is ${esc(String(dashboardMode).toUpperCase())}.`));
    } else {
      items.push(_checklistItem('blocked', 'Paper account',
        `Mode is ${esc(String(dashboardMode).toUpperCase())} — Activate is paper-only.`));
    }
    if (!statusKnown) {
      items.push(_checklistItem('blocked', 'Trader status',
        'No <code>get_paper_automation_status</code> response. Enable '
        + '<code>command_authority.enabled: true</code> in '
        + '<code>~/.config/mmr/trader.yaml</code> (keep '
        + '<code>live_enabled: false</code>), then restart trader.'));
    } else {
      items.push(_checklistItem('ok', 'Trader status',
        `Lifecycle <code>${esc(lifecycle)}</code>.`));
    }
    if (!statusKnown) {
      items.push(_checklistItem('blocked', 'Command authority',
        'Unknown until trader serves status — usually still disabled.'));
    } else if (caReady) {
      items.push(_checklistItem('ok', 'Command authority',
        'Ready — approve / automation dispatch is wired.'));
    } else {
      items.push(_checklistItem('blocked', 'Command authority',
        'Not ready. Set <code>command_authority.enabled: true</code> and '
        + '<code>live_enabled: false</code>, restart trader. See '
        + '<code>docs/PAPER_AUTOMATION_SETUP.md</code>.'));
    }
    if (strategySelected) {
      items.push(_checklistItem('ok', 'Strategy selected',
        `Using <code>${esc(select.value)}</code>.`));
    } else if (select && ((v && v.deployed_strategy_names) || []).length === 0
               && ((v && v.strategies) || []).length === 0) {
      items.push(_checklistItem('blocked', 'Strategy selected',
        'No strategies in live feed or <code>strategy_runtime.yaml</code>. '
        + 'Deploy one on the Deploy tab, then reload.'));
    } else {
      items.push(_checklistItem('wait', 'Strategy selected',
        'Pick the one strategy to arm (must not use propose while automated).'));
    }
    if (reasonFilled) {
      items.push(_checklistItem('ok', 'Reason', 'Operator reason entered.'));
    } else {
      items.push(_checklistItem('wait', 'Reason',
        'Enter a short reason before Activate / Deactivate.'));
    }
    checklist.innerHTML = items.join('');
  }

  // Keep controls visible so operators can always see why buttons are locked.
  const controls = document.getElementById('paper-auto-controls');
  if (controls) controls.hidden = false;

  const canActivate = paperOk && statusKnown && caReady
      && strategySelected && reasonFilled;
  const activateBtn = document.getElementById('paper-auto-activate');
  if (activateBtn) {
    activateBtn.disabled = !canActivate;
    if (!paperOk) {
      activateBtn.title = 'Paper automation is only available on paper accounts';
    } else if (!statusKnown) {
      activateBtn.title = 'Enable command_authority.enabled in trader.yaml and restart trader';
    } else if (!caReady) {
      activateBtn.title = 'Command authority must be enabled before Activate';
    } else if (!strategySelected) {
      activateBtn.title = 'Select a strategy first';
    } else if (!reasonFilled) {
      activateBtn.title = 'Enter a reason first';
    } else {
      activateBtn.title = '';
    }
  }
  const deactivateBtn = document.getElementById('paper-auto-deactivate');
  if (deactivateBtn) {
    const enabledLifecycle = lifecycle === 'restart_required'
        || lifecycle === 'armed'
        || lifecycle === 'armed_unpersisted'
        || lifecycle === 'degraded';
    const canDeactivate = paperOk && statusKnown && enabledLifecycle && reasonFilled;
    deactivateBtn.disabled = !canDeactivate;
    if (!paperOk) {
      deactivateBtn.title = 'Paper automation is only available on paper accounts';
    } else if (!statusKnown) {
      deactivateBtn.title = 'Status unavailable — nothing to deactivate safely';
    } else if (!enabledLifecycle) {
      deactivateBtn.title = `Nothing armed to deactivate (lifecycle=${lifecycle})`;
    } else if (!reasonFilled) {
      deactivateBtn.title = 'Enter a reason first';
    } else {
      deactivateBtn.title = '';
    }
  }
}

function renderAll() {
  renderStatusBar(); renderAccountCards(); renderPositions(); renderProposals();
  renderOrders(); renderFills(); renderStrategies(); renderRisk(); renderScaling();
  renderPaperAutomation();
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
  if (e.key !== 'Escape') return;
  if (!document.getElementById('drawer').hidden) {
    closeDrawer();
    return;
  }
  if (CFG.commandsEnabled) CCCommands.closeCommandDrawers();
});

document.getElementById('proposal-cards').addEventListener('click', e => {
  // Inline card actions first (rendered only when commands are enabled);
  // anything else on the card — including the Details button — opens the
  // detail drawer exactly as before.
  const approveBtn = e.target.closest('[data-cc-approve]');
  if (approveBtn) {
    const p = ccFindProposalRow(approveBtn.dataset.ccApprove);
    if (p) CCCommands.approveProposal(p);
    return;
  }
  const rejectBtn = e.target.closest('[data-cc-reject]');
  if (rejectBtn) {
    const p = ccFindProposalRow(rejectBtn.dataset.ccReject);
    if (p) CCCommands.rejectProposal(p);
    return;
  }
  const card = e.target.closest('[data-proposal]');
  if (card) showProposalDrawer(card.dataset.proposal, card);
});
document.getElementById('proposal-cards').addEventListener('keydown', e => {
  if (e.target.closest('button')) return;  // let inline actions keep native keys
  const card = e.target.closest('[data-proposal]');
  if (card && (e.key === 'Enter' || e.key === ' ')) {
    e.preventDefault();
    showProposalDrawer(card.dataset.proposal, card);
  }
});
document.querySelectorAll('[data-proposal-filter]').forEach(btn => {
  btn.addEventListener('click', () => {
    PROPOSAL_FILTER.mode = btn.getAttribute('data-proposal-filter') || 'pending';
    document.querySelectorAll('[data-proposal-filter]').forEach(b => {
      b.classList.toggle('active', b === btn);
    });
    renderProposals();
  });
});

// Strategy-error alert "Review strategies →": switch to the Trading tab
// (dash_admin.js owns the .dash-tab click) and scroll the Strategies panel
// into view. Guarded — the banner only exists once the page has rendered.
(() => {
  const link = document.querySelector('#strategy-alert .sa-link');
  if (!link) return;
  link.addEventListener('click', () => {
    const tab = document.querySelector('.dash-tab[data-dash-tab="trading"]');
    if (tab) tab.click();
    const panel = document.getElementById('strategies-panel');
    if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
})();

function ccFindProposalRow(id) {
  const v = store.view; if (!v) return null;
  return v.proposals.active.find(x => String(x.entity_id) === String(id))
    || v.proposals.terminal.find(x => String(x.entity_id) === String(id))
    || null;
}

function showProposalDrawer(id, invoker) {
  const local = ccFindProposalRow(id);
  const render = (p) => {
    if (!p) return;
    const sizing = p.sizing_result || {};
    const reasoning = Array.isArray(sizing.reasoning) ? sizing.reasoning
      : (sizing.reasoning ? [sizing.reasoning] : []);
    const status = String(p.status || '').toUpperCase();
    const actions = (CFG.commandsEnabled && status === 'PENDING')
      ? `<div class="cc-actions">
          <button type="button" data-cc-approve="${esc(p.entity_id || p.id)}" class="primary">Approve</button>
          <button type="button" data-cc-reject="${esc(p.entity_id || p.id)}" class="reject">Reject</button>
        </div>` : '';
    const orderIds = p.order_ids || p.broker_order_ids || [];
    openDrawer(`<h3>Proposal #${esc(p.entity_id || p.id)} — ${esc(p.action || '')}
        ${esc(p.symbol || '')}</h3>
      <p>status <strong>${esc(status)}</strong> · source ${esc(p.source || '—')}
        · confidence ${esc(p.confidence ?? '—')} · expires ${esc(p.expires_at || '—')}</p>
      <p class="dim">conId ${esc(p.conid ?? p.instrument_id ?? '—')}
        · qty ${esc(p.quantity ?? 'auto')} · amount ${money(p.amount, p.currency)}
        · group ${esc(p.group || '—')}</p>
      ${orderIds.length
        ? `<p class="dim">orders: ${esc(Array.isArray(orderIds) ? orderIds.join(', ') : orderIds)}</p>`
        : ''}
      <h4>Position sizing reasoning</h4>
      ${reasoning.length
        ? '<ol>' + reasoning.map(step => `<li>${esc(step)}</li>`).join('') + '</ol>'
        : '<p class="dim">No sizing reasoning recorded.</p>'}
      <h4>Rationale</h4>
      <p>${esc(p.reasoning || '—')}</p>
      ${p.thesis ? `<h4>Thesis</h4><p>${esc(p.thesis)}</p>` : ''}
      ${actions}`, invoker);
  };
  render(local);
  // Enrich from trader when possible (history / fields missing from SSE row).
  const numericId = String(id).replace(/^proposal:/, '');
  if (!/^\d+$/.test(numericId)) return;
  fetch(`/api/proposals/${encodeURIComponent(numericId)}`, {
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json' },
  }).then(r => r.ok ? r.json() : null).then(body => {
    if (!body || body.error) return;
    const merged = Object.assign({}, local || {}, body, {
      entity_id: (local && local.entity_id) || body.id || id,
    });
    render(merged);
  }).catch(() => {});
}

/* ---------------- freshness ticker ---------------------------------------- */
setInterval(() => { renderStatusBar(); renderPositions(); }, 1000);

// [M1-C] UI-wiring pass: `cc-proposal-form` (and every other cc-* static
// element below) only exists in the DOM when commands_enabled is true (see
// command_center.html's `{% if commands_enabled %}` wrapper) -- guard the
// listener registration itself so a commands-disabled load never throws on
// a null getElementById(...) and aborts the rest of this script (which
// would also skip the resync() boot call at the very end of the file).
if (CFG.commandsEnabled) {
document.getElementById('cc-proposal-form').addEventListener('submit',
    async (evt) => {
      evt.preventDefault();
      // Minted ONCE here, at command initiation -- reused for the CSRF
      // retry inside ccPost (M-3) and any future resubmit of this same
      // logical command; never re-minted (see ccNewCommandId).
      const commandId = CCCommands.newCommandId();
      const body = CCCommands.proposalBody(evt.target, commandId);
      CCCommands.closeProposalDrawer();
      await CCCommands.submitCommand('create_proposal',
          `New proposal ${body.action} conId ${body.conid}`,
          '/api/commands/proposals', body);
    });
document.getElementById('cc-proposal-form').addEventListener(
    'input', CCCommands.invalidateResolvedConId);
document.getElementById('cc-proposal-cancel').addEventListener('click',
    CCCommands.closeProposalDrawer);
document.getElementById('cc-proposal-close').addEventListener('click',
    CCCommands.closeProposalDrawer);
const resolveBtn = document.getElementById('cc-resolve-symbol');
if (resolveBtn) {
  resolveBtn.addEventListener('click', (ev) => {
    ev.preventDefault();
    CCCommands.resolveSymbol();
  });
}
}

/* ---- Close position drawer (pre-filled reducing proposal) ---- */

if (CFG.commandsEnabled) {
document.getElementById('cc-close-form').addEventListener('submit',
    async (evt) => {
      evt.preventDefault();
      const d = document.getElementById('cc-close-drawer');
      // Minted ONCE at initiation -- see the proposal-form handler above
      // for the same reuse contract (CSRF retry + any future resubmit).
      const commandId = CCCommands.newCommandId();
      const body = {
        command_id: commandId,
        action: d.dataset.action,
        quantity: Number(d.querySelector('input[name=quantity]').value),
        reasoning: d.querySelector('textarea[name=reasoning]').value,
      };
      d.hidden = true;
      await CCCommands.submitCommand('create_proposal',
          `Close position conId ${d.dataset.conid}`,
          `/api/commands/positions/${d.dataset.account}/${d.dataset.conid}/close`,
          body);
    });
const ccCloseCloseDrawer = () => {
  document.getElementById('cc-close-drawer').hidden = true;
};
document.getElementById('cc-close-cancel').addEventListener('click',
    ccCloseCloseDrawer);
document.getElementById('cc-close-drawer-x').addEventListener('click',
    ccCloseCloseDrawer);
}

if (CFG.commandsEnabled) {
document.getElementById('cc-cancel-all-confirm').addEventListener('click',
    async () => {
      const d = document.getElementById('cc-cancel-all-dialog');
      d.hidden = true;
      const orderIds = JSON.parse(d.dataset.orderIds || '[]');
      if (orderIds.length === 0) return;
      const riskIncreasing = d.dataset.hasProtective === 'true' &&
                             d.dataset.hasLive === 'true';
      const submit = (commandId, nonce) => CCCommands.submitCommand('cancel_orders',
          `Cancel all (${orderIds.length} orders)`,
          '/api/commands/orders/cancel-all', {
            command_id: commandId,
            order_entity_ids: orderIds,
            preflight_nonce: nonce,
          });
      if (riskIncreasing) {
        await CCCommands.runLiveCeremony('cancel_orders',
            `Cancel all (${orderIds.length} orders)`, 'cancel_orders',
            {order_entity_ids: orderIds}, null, submit);
      } else {
        await submit(CCCommands.newCommandId(), null);
      }
    });
document.getElementById('cc-cancel-all-abort').addEventListener('click',
    () => { document.getElementById('cc-cancel-all-dialog').hidden = true; });
}

/* ===================== [M1-C] UI-wiring pass ==============================
 * The consolidating pass Tasks 3-6 each deferred (their comments above still
 * say "a later task wires that control ... "): binds the already-built cc*
 * action functions into the M1-R render/drawer functions as additive
 * affordances, gated on CFG.commandsEnabled. Nothing here restructures
 * applyEvent()/the store shape -- every lookup below reads the exact same
 * store.view collections renderPositions()/renderOrders()/renderStrategies()/
 * showProposalDrawer() already read.
 *
 * Reuses the event-delegation pattern the M1-R `#proposal-cards` listener
 * (above) established: one listener per stable container, matched by a
 * `data-cc-*` attribute on the actual (plain <button>) target -- real
 * <button> elements are natively keyboard-operable, so no extra keydown
 * shim is needed the way the div/role=button proposal cards required. */

function ccFindPositionRow(entityId) {
  const v = store.view;
  if (!v || !v.positions) return null;
  return v.positions.find((p) => String(p.entity_id) === String(entityId)) || null;
}

function ccFindOrderRow(entityId) {
  const v = store.view;
  if (!v || !v.orders) return null;
  return v.orders.active.find((o) => String(o.entity_id) === String(entityId))
      || v.orders.terminal.find((o) => String(o.entity_id) === String(entityId))
      || null;
}

function ccFindStrategy(name) {
  const v = store.view;
  if (!v || !v.strategies) return null;
  return v.strategies.find((s) => String(ccStrategyName(s)) === String(name)) || null;
}

if (CFG.commandsEnabled) {
  // [M1-C] fix wave I-1: `/cc` is served with a strict CSP (`script-src
  // 'self'`, no `'unsafe-inline'` -- web/command_center/session.py's
  // `_STRICT_CSP`), so these two command-initiation buttons can no longer be
  // wired via inline `onclick=` (a real browser silently drops the handler,
  // leaving the button dead) -- bind them here instead, same
  // addEventListener convention as every other control in this block.
  document.getElementById('cc-cancel-all-open')
      ?.addEventListener('click', () => CCCommands.cancelAll());
  document.getElementById('cc-open-proposal')
      ?.addEventListener('click', () => CCCommands.openProposalDrawer());

  // Proposal drawer: Approve / Reject (Task 4).
  document.getElementById('drawer-content').addEventListener('click', (e) => {
    const approveBtn = e.target.closest('[data-cc-approve]');
    if (approveBtn) {
      const p = ccFindProposalRow(approveBtn.dataset.ccApprove);
      if (p) CCCommands.approveProposal(p);
      return;
    }
    const rejectBtn = e.target.closest('[data-cc-reject]');
    if (rejectBtn) {
      const p = ccFindProposalRow(rejectBtn.dataset.ccReject);
      if (p) CCCommands.rejectProposal(p);
    }
  });

  // Positions: Close (Task 3).
  document.getElementById('positions-body').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-cc-close-position]');
    if (!btn) return;
    const position = ccFindPositionRow(btn.dataset.ccClosePosition);
    if (position) CCCommands.openCloseDrawer(CCCommands.positionForClose(position));
  });

  // Working orders: per-leg Cancel (Task 5). "Cancel all" is the static
  // #cc-cancel-all-open button (command_center.html), bound via
  // addEventListener above (I-1 fix) -- same convention as #cc-open-proposal.
  document.getElementById('order-groups').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-cc-cancel-order]');
    if (!btn) return;
    const order = ccFindOrderRow(btn.dataset.ccCancelOrder);
    if (order) CCCommands.cancelOrder(order);
  });

  // Strategies: Enable / Disable / Edit params (Task 6).
  document.getElementById('strategies-body').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-cc-strategy-action]');
    if (!btn) return;
    const strategy = ccFindStrategy(btn.dataset.ccStrategy);
    if (!strategy) return;
    const action = btn.dataset.ccStrategyAction;
    if (action === 'enable') CCCommands.enableStrategy(strategy);
    else if (action === 'disable') CCCommands.disableStrategy(strategy);
    else if (action === 'params') CCCommands.openStrategyParamsDrawer(strategy);
  });

  // Strategy params dialog: Apply / Cancel. The form is intentionally empty
  // (no tunables schema yet -- see command_center.html's comment on
  // #cc-strategy-params-dialog); Apply collects whatever it holds today
  // (nothing) and still exercises the real ccUpdateStrategyParams(strategy,
  // params) call so the CAS/live-ceremony path works once fields exist.
  document.getElementById('cc-params-apply').addEventListener('click', () => {
    const d = document.getElementById('cc-strategy-params-dialog');
    const strategy = ccFindStrategy(d.dataset.strategyName) || {
      strategy_name: d.dataset.strategyName,
      control_revision: d.dataset.controlRevision ? Number(d.dataset.controlRevision) : null,
    };
    const form = document.getElementById('cc-strategy-params-form');
    const params = Object.fromEntries(new FormData(form).entries());
    d.hidden = true;
    CCCommands.updateStrategyParams(strategy, params);
  });
  document.getElementById('cc-params-cancel').addEventListener('click', () => {
    document.getElementById('cc-strategy-params-dialog').hidden = true;
  });
}

if (CFG.commandsEnabled) {
  const activateBtn = document.getElementById('scaling-activate');
  const suspendBtn = document.getElementById('scaling-suspend');
  const fileInput = document.getElementById('scaling-attestation-file');
  if (activateBtn) {
    activateBtn.addEventListener('click', () => { CCCommands.activateAllocation(); });
  }
  if (suspendBtn) {
    suspendBtn.addEventListener('click', () => { CCCommands.suspendAllocation(); });
  }
  if (fileInput) {
    fileInput.addEventListener('change', async () => {
      const file = fileInput.files && fileInput.files[0];
      if (!file) return;
      try {
        const text = await file.text();
        document.getElementById('scaling-attestation').value = text.trim();
      } catch (err) {
        CCCommands.toast('error', `Failed to read file: ${err.message || err}`);
      }
    });
  }
}

if (CFG.commandsEnabled) {
  const activateBtn = document.getElementById('paper-auto-activate');
  const deactivateBtn = document.getElementById('paper-auto-deactivate');
  if (activateBtn) {
    activateBtn.addEventListener('click', () => { CCCommands.activatePaperAutomation(); });
  }
  if (deactivateBtn) {
    deactivateBtn.addEventListener('click', () => {
      CCCommands.deactivatePaperAutomation();
    });
  }
  const strategySelect = document.getElementById('paper-auto-strategy');
  const reasonInput = document.getElementById('paper-auto-reason');
  if (strategySelect) {
    strategySelect.addEventListener('change', () => renderPaperAutomation());
  }
  if (reasonInput) {
    reasonInput.addEventListener('input', () => renderPaperAutomation());
  }
}

/* ---------------- boot ----------------------------------------------------- */
if (CFG.commandsEnabled) {
  // Inject live state + start the pending-command poll now that the store and
  // command flags exist; also re-exposes globalThis.ccOpenResearchProposal.
  CCCommands.init({ view: () => store.view, config: CFG });
}
resync();

/* [M1-C] command surface for the Command Center client.
 *
 * Loaded (defer) BETWEEN cc_util.js and command_center.js: command_center.js's
 * thin wiring calls CCCommands.* and its boot calls CCCommands.init(). Kept as
 * a separate, injectable namespace -- mirroring command_center_research.js's
 * globalThis.CCResearch -- so the correctness-critical command / preflight /
 * reconcile logic can be unit-tested without the reducer/render stack.
 *
 * State access is injected via CCCommands.init({view, config}); this module
 * never reaches for the command_center.js globals `store`/`CFG` directly.
 * `_view()` returns the current reduced view and `_config` the command flags
 * (defaults keep every method inert until init() wires them). */
'use strict';

let _view = () => ({});
let _config = {};

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
  // Authoritative ledger reconcile for an accepted command whose terminal
  // state hasn't arrived over SSE. Drives BOTH the outcome-unknown path AND a
  // normal 202 (issue #9): a 202 that the feed never confirms -- because SSE
  // or the bridge is degraded -- would otherwise stay "Pending confirmation"
  // forever. Re-checks after each wait so a command.updated event (applied by
  // ccCheckPendingCommands) short-circuits this without a needless ledger poll.
  for (let i = 0; i < 12; i += 1) {
    await new Promise((r) => setTimeout(r, window.CC_RECONCILE_MS || 5000));
    if (!(CC.commands.pending.has(commandId) || CC.commands.unknown.has(commandId))) {
      return;  // resolved over SSE while we waited
    }
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
  const v = _view();
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
  if (result.ok) {
    // 202 accepted: normally an SSE command.updated resolves the pending chip
    // within ~1s. But if SSE/the feed is degraded that event may never arrive,
    // so ALSO reconcile against the ledger after a grace interval (silent -- no
    // outcome-unknown banner). ccReconcileCommand re-checks after each wait, so
    // the SSE path still wins when it's healthy and no ledger poll is wasted.
    ccReconcileCommand(body.command_id, label);
    return;
  }
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

function ccCloseCommandDrawers() {
  const ids = [
    'cc-proposal-drawer', 'cc-close-drawer', 'cc-confirm-drawer',
    'cc-cancel-all-dialog', 'cc-strategy-params-dialog',
  ];
  for (const id of ids) {
    const el = document.getElementById(id);
    if (el && !el.hidden) el.hidden = true;
  }
}

function ccOpenProposalDrawer() {
  const d = document.getElementById('cc-proposal-drawer');
  const status = document.getElementById('cc-resolve-status');
  if (status) {
    status.textContent = '';
    status.className = 'cc-resolve-status';
  }
  d.hidden = false;
}

async function ccOpenResearchProposal(instrument) {
  const form = document.getElementById('cc-proposal-form');
  if (!form) return;
  const selected = instrument || {};
  const setValue = (name, value) => {
    if (!form[name]) return;
    form[name].value = value;
  };
  form.reset();
  setValue('resolve_symbol', String(selected.ticker || selected.symbol || '')
      .trim().toUpperCase());
  setValue('resolve_exchange', String(selected.exchange || '').trim());
  setValue('resolve_currency', String(selected.currency || '').trim());
  setValue('conid', '');
  const action = String(selected.action || 'BUY').trim().toUpperCase();
  setValue('action', (action === 'SELL') ? 'SELL' : 'BUY');
  // Leave quantity/amount blank so the server auto-sizes from confidence.
  setValue('quantity', '');
  setValue('amount', '');
  if (selected.confidence != null && selected.confidence !== '') {
    const confidence = Number(selected.confidence);
    setValue('confidence', Number.isFinite(confidence)
      ? String(Math.min(1, Math.max(0, confidence)))
      : '');
  } else {
    setValue('confidence', '');
  }
  setValue('group', String(selected.group || '').trim());
  setValue('thesis', String(selected.thesis || '').trim());
  setValue('reasoning', String(selected.reasoning || ''));
  ccOpenProposalDrawer();
  await ccResolveSymbol();
}

function ccCloseProposalDrawer() {
  document.getElementById('cc-proposal-drawer').hidden = true;
}

let ccResolveGeneration = 0;
let ccResolveAbortController = null;

async function ccResolveSymbol() {
  const form = document.getElementById('cc-proposal-form');
  const status = document.getElementById('cc-resolve-status');
  const generation = ++ccResolveGeneration;
  if (ccResolveAbortController) ccResolveAbortController.abort();
  const controller = new AbortController();
  ccResolveAbortController = controller;
  const isCurrent = () => generation === ccResolveGeneration;
  const sym = String(form.resolve_symbol.value || '').trim().toUpperCase();
  const exchange = String(form.resolve_exchange.value || '').trim();
  const currency = String(form.resolve_currency.value || '').trim();
  if (!sym) {
    status.textContent = 'Enter a symbol to resolve.';
    status.className = 'cc-resolve-status err';
    form.resolve_symbol.focus();
    if (isCurrent()) ccResolveAbortController = null;
    return;
  }
  status.textContent = `Resolving ${sym}…`;
  status.className = 'cc-resolve-status';
  const qs = new URLSearchParams({ symbol: sym });
  if (exchange) qs.set('exchange', exchange);
  if (currency) qs.set('currency', currency);
  try {
    const res = await fetch('/api/resolve?' + qs.toString(), {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
      signal: controller.signal,
    });
    const body = await res.json();
    if (!isCurrent()) return;
    if (!res.ok) {
      form.conid.value = '';
      status.textContent = body.error || (`HTTP ${res.status}`);
      status.className = 'cc-resolve-status err';
      return;
    }
    const instruments = body.instruments || [];
    if (!instruments.length) {
      form.conid.value = '';
      status.textContent = `No contract found for ${sym}`
        + (exchange ? ` on ${exchange}` : '')
        + ' — try exchange/currency hints.';
      status.className = 'cc-resolve-status err';
      return;
    }
    const first = instruments[0];
    const conid = first.instrument_id || first.conId || first.conid;
    form.conid.value = conid;
    const more = instruments.length > 1
      ? ` (${instruments.length} matches — using first; set exchange if wrong)`
      : '';
    status.textContent = `${first.symbol || sym} → conId ${conid}`
      + ` · ${first.primary_exchange || first.exchange || '—'}`
      + ` · ${first.currency || '—'}${more}`;
    status.className = 'cc-resolve-status ok';
  } catch (err) {
    if (!isCurrent()) return;
    form.conid.value = '';
    status.textContent = String(err.message || err);
    status.className = 'cc-resolve-status err';
  } finally {
    if (isCurrent()) ccResolveAbortController = null;
  }
}

function ccInvalidateResolvedConId(event) {
  const form = document.getElementById('cc-proposal-form');
  if (!form) return;
  const target = event && event.target;
  const field = target && (
    target.name
    || (target === form.resolve_symbol ? 'resolve_symbol' : null)
    || (target === form.resolve_exchange ? 'resolve_exchange' : null)
    || (target === form.resolve_currency ? 'resolve_currency' : null)
  );
  if (field !== 'resolve_symbol'
      && field !== 'resolve_exchange'
      && field !== 'resolve_currency') {
    return;
  }
  ccResolveGeneration += 1;
  if (ccResolveAbortController) {
    ccResolveAbortController.abort();
    ccResolveAbortController = null;
  }
  form.conid.value = '';
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

function ccIsPaper(accountMode) {
  return String(accountMode || '').toLowerCase() === 'paper';
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
  const s = ticket.summary || {};
  const set = (name, value) => {
    const el = d.querySelector(`[data-field=${name}]`);
    if (!el) return;
    el.textContent = value === null || value === undefined ? '—' : String(value);
  };
  // Order fields (qty / notional / price / drift) only apply to trade
  // preflights. Control commands (paper automation, resume, cancel, …) leave
  // them null — hide the rows instead of showing a wall of dashes.
  const showTrade = [s.quantity, s.notional, s.latest_price, s.drift_bps]
      .some((v) => v !== null && v !== undefined && v !== '');
  d.querySelectorAll('.cc-confirm-trade').forEach((el) => {
    el.hidden = !showTrade;
  });
  const reason = (s.reason || '').trim();
  d.querySelectorAll('.cc-confirm-reason').forEach((el) => {
    el.hidden = !reason;
  });

  const title = d.querySelector('#cc-confirm-title');
  const instrumentLabel = d.querySelector('[data-label-for="instrument"]');
  if (s.order_type === 'PAPER_AUTOMATION') {
    if (title) title.textContent = 'Confirm paper automation';
    if (instrumentLabel) instrumentLabel.textContent = 'Strategy';
  } else if (showTrade) {
    if (title) title.textContent = 'Confirm live command';
    if (instrumentLabel) instrumentLabel.textContent = 'Instrument';
  } else {
    if (title) title.textContent = 'Confirm command';
    if (instrumentLabel) instrumentLabel.textContent =
        (s.order_type === 'CONTROL' || s.order_type === 'ORDER CONTROL')
          ? 'Target' : 'Instrument';
  }

  set('side', s.side);
  set('instrument', s.instrument);
  if (showTrade) {
    set('quantity', s.quantity);
    set('notional', s.notional);
    set('latest_price', s.latest_price);
    set('drift_bps', s.drift_bps);
  }
  set('order_type', s.order_type);
  if (reason) set('reason', reason);
  set('account', `${s.account_id} (${String(s.account_mode || '').toUpperCase()})`);
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
  const v = _view();
  if (!v || !v.accounts) return null;
  const acct = v.accounts.find((a) =>
      a.account_id === order.account_id || a.entity_id === order.account_id)
      || v.accounts[0];
  return acct ? acct.mode : null;
}

function ccFindPosition(order) {
  const v = _view();
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
  const v = _view();
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
  const v = _view();
  return v && v.accounts && v.accounts[0] ? ccAccountModeValue(v.accounts[0]) : null;
}

function ccPaperAutomationAllowed() {
  const dashboardMode = ccDashboardAccountMode();
  if (!ccIsPaper(dashboardMode)) return false;
  const pa = _view() && _view().paper_automation;
  if (pa && pa.account_mode != null && String(pa.account_mode).trim() !== '') {
    return ccIsPaper(pa.account_mode);
  }
  return true;
}

function ccAccountModeFor(accountId) {
  const v = _view();
  if (!v || !v.accounts) return null;
  const acct = v.accounts.find((a) =>
      a.account_id === accountId || a.entity_id === accountId) || v.accounts[0];
  return ccAccountModeValue(acct);
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
  const reason = paused ? 'operator pause' : 'operator resume';
  if (paused) {
    await ccSubmitCommand('pause_trading', label, '/api/commands/pause',
        {command_id: ccNewCommandId(), reason});
    return;
  }
  if (!ccIsLive(ccAccountModeFor(accountId))) {
    await ccSubmitCommand('resume_trading', label, '/api/commands/resume',
        {command_id: ccNewCommandId(), expected_control_revision: revision,
         reason, preflight_nonce: null});
    return;
  }
  await ccRunLiveCeremony('resume_trading', label, 'resume_trading',
      {reason}, revision,
      (commandId, nonce) => ccSubmitCommand('resume_trading', label,
          '/api/commands/resume',
          {command_id: commandId, expected_control_revision: revision,
           reason, preflight_nonce: nonce}));
}

function ccPositionForClose(position) {
  // ccOpenCloseDrawer (Task 3, above) reads `.conid` directly with no
  // fallback; the real BrokerPositionRow payload always carries `conid`
  // (trader/data/broker_state.py), but a synthetic/seed row keyed only by
  // `entity_id` ("ACCOUNT:CONID") would not -- derive it the same way
  // renderPositions() already does rather than teaching ccOpenCloseDrawer
  // a new fallback.
  if (position.conid !== undefined && position.conid !== null) return position;
  const conid = Number(String(position.entity_id || '').split(':').pop());
  return Object.assign({}, position, {conid});
}

/* ---- Strategy params drawer open (Apply/Cancel wiring is below, guarded
 * with the rest of the cc-* static elements) ---- */
function ccOpenStrategyParamsDrawer(strategy) {
  const d = document.getElementById('cc-strategy-params-dialog');
  if (!d) return;
  const name = ccStrategyName(strategy);
  document.getElementById('cc-params-strategy-name').textContent = name;
  d.dataset.strategyName = name;
  d.dataset.controlRevision = strategy.control_revision ?? '';
  d.hidden = false;
}

/* ===================== Allocation activate / suspend (Scaling tab) =========
 * Activate always runs the preflight confirm ceremony (coordinator
 * requires_preflight=True — same as the CLI/SDK path). Signing stays
 * offline; this UI only pastes already-signed attestation JSON. Suspend is
 * risk-reducing and a single POST, mirroring deactivate_live_canary. */

function ccParseAttestationJson(raw) {
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    ccToast('error', `Attestation JSON is invalid: ${err.message || err}`);
    return null;
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    ccToast('error', 'Attestation must be a JSON object');
    return null;
  }
  return parsed;
}

async function ccActivateAllocation() {
  const raw = (document.getElementById('scaling-attestation').value || '').trim();
  const reason = (document.getElementById('scaling-reason').value || '').trim();
  if (!raw) {
    ccToast('error', 'Paste or upload a signed attestation JSON first');
    return;
  }
  if (!reason) {
    ccToast('error', 'Reason is required');
    return;
  }
  const attestation = ccParseAttestationJson(raw);
  if (!attestation) return;

  const url = '/api/commands/allocation/activate';
  const label = 'Activate allocation';
  // Always ceremony — activate_allocation requires a consumed preflight nonce
  // in both paper and live (unlike approve_proposal's paper single-POST path).
  await ccRunLiveCeremony(
      'activate_allocation', label, 'activate_allocation',
      {attestation, reason}, null,
      (commandId, nonce) => ccSubmitCommand('activate_allocation', label, url, {
        command_id: commandId,
        attestation,
        reason,
        preflight_nonce: nonce,
      }));
}

async function ccSuspendAllocation() {
  const reason = (document.getElementById('scaling-reason').value || '').trim();
  if (!reason) {
    ccToast('error', 'Reason is required to suspend allocation');
    return;
  }
  if (!window.confirm(
      `Suspend the active allocation authority?\n\nReason: ${reason}`)) {
    return;
  }
  await ccSubmitCommand('suspend_allocation', 'Suspend allocation',
      '/api/commands/allocation/suspend', {
        command_id: ccNewCommandId(),
        reason,
      });
}

/* ===================== Paper automation activate / deactivate (Scaling) ====
 * Activate always runs the preflight confirm ceremony (coordinator
 * requires_preflight=True). Deactivate is risk-reducing and a single POST.
 * Phase 1 returns restart_required — operator restarts trader + strategy. */

async function ccActivatePaperAutomation() {
  if (!ccPaperAutomationAllowed()) {
    ccToast('error', 'Paper automation is only available on paper accounts');
    return;
  }
  const strategy = (document.getElementById('paper-auto-strategy').value || '')
      .trim();
  const reason = (document.getElementById('paper-auto-reason').value || '')
      .trim();
  if (!strategy) {
    ccToast('error', 'Select a strategy first');
    return;
  }
  if (!reason) {
    ccToast('error', 'Reason is required');
    return;
  }
  const pa = (_view() && _view().paper_automation) || null;
  if (pa && pa.command_authority_ready !== true) {
    ccToast('error', 'Command authority must be enabled before Activate');
    return;
  }
  if (!window.confirm(
      `Activate paper automation for ${strategy}?\n\n`
      + 'Will arm trader + strategy in-process (no restart when hot-arm '
      + 'succeeds). Private keys stay on the host filesystem.\n\n'
      + 'Reason: ' + reason)) {
    return;
  }

  const url = '/api/commands/paper-automation/activate';
  const label = 'Activate paper automation';
  const params = {strategy_name: strategy, reason};
  await ccRunLiveCeremony(
      'activate_paper_automation', label, 'activate_paper_automation',
      params, null,
      (commandId, nonce) => ccSubmitCommand(
          'activate_paper_automation', label, url, {
            command_id: commandId,
            strategy_name: strategy,
            reason,
            preflight_nonce: nonce,
          }));
}

async function ccDeactivatePaperAutomation() {
  if (!ccPaperAutomationAllowed()) {
    ccToast('error', 'Paper automation is only available on paper accounts');
    return;
  }
  const reason = (document.getElementById('paper-auto-reason').value || '')
      .trim();
  if (!reason) {
    ccToast('error', 'Reason is required to deactivate paper automation');
    return;
  }
  if (!window.confirm(
      `Deactivate paper automation?\n\n`
      + 'Clears in-memory arm immediately and writes automation.enabled=false.\n\n'
      + `Reason: ${reason}`)) {
    return;
  }
  await ccSubmitCommand(
      'deactivate_paper_automation', 'Deactivate paper automation',
      '/api/commands/paper-automation/deactivate', {
        command_id: ccNewCommandId(),
        reason,
      });
}

function init(options) {
  // Wire live state + start the 400ms pending-command poll. Called once from
  // command_center.js's boot (gated on commands being enabled), and re-exposes
  // ccOpenResearchProposal as the global hook command_center_research.js calls.
  const opts = options || {};
  _view = opts.view || (() => ({}));
  _config = opts.config || {};
  globalThis.ccOpenResearchProposal = ccOpenResearchProposal;
  setInterval(ccCheckPendingCommands, 400);
}

globalThis.CCCommands = {
  init,
  submitCommand: ccSubmitCommand,
  post: ccPost,
  toast: ccToast,
  csrfToken: ccCsrfToken,
  newCommandId: ccNewCommandId,
  checkPendingCommands: ccCheckPendingCommands,
  openProposalDrawer: ccOpenProposalDrawer,
  openResearchProposal: ccOpenResearchProposal,
  closeProposalDrawer: ccCloseProposalDrawer,
  resolveSymbol: ccResolveSymbol,
  invalidateResolvedConId: ccInvalidateResolvedConId,
  proposalBody: ccProposalBody,
  openCloseDrawer: ccOpenCloseDrawer,
  approveProposal: ccApproveProposal,
  rejectProposal: ccRejectProposal,
  requestPreflight: ccRequestPreflight,
  runLiveCeremony: ccRunLiveCeremony,
  cancelOrder: ccCancelOrder,
  cancelAll: ccCancelAll,
  openCancelAllDialog: ccOpenCancelAllDialog,
  classifyOrder: ccClassifyOrder,
  enableStrategy: ccEnableStrategy,
  disableStrategy: ccDisableStrategy,
  updateStrategyParams: ccUpdateStrategyParams,
  setPause: ccSetPause,
  openStrategyParamsDrawer: ccOpenStrategyParamsDrawer,
  positionForClose: ccPositionForClose,
  activateAllocation: ccActivateAllocation,
  suspendAllocation: ccSuspendAllocation,
  activatePaperAutomation: ccActivatePaperAutomation,
  deactivatePaperAutomation: ccDeactivatePaperAutomation,
  closeCommandDrawers: ccCloseCommandDrawers,
  reconcileCommand: ccReconcileCommand,
  resolveCommand: ccResolveCommand,
  dashboardAccountMode: ccDashboardAccountMode,
  paperAutomationAllowed: ccPaperAutomationAllowed,
  isLive: ccIsLive,
  isPaper: ccIsPaper,
};

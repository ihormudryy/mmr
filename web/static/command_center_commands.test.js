/* Sharp-path tests for command_center_commands.js (the CCCommands module).
 *
 * The command surface was extracted from command_center.js behind a small
 * namespace + an injected view() so the capital-adjacent logic — CSRF retry,
 * live-vs-paper approval gating, the preflight ceremony, order classification,
 * and the pending/reconcile state machine — can be exercised WITHOUT a browser.
 * The module is evaluated in a VM context with a stubbed document, a
 * queue-based fake fetch, controllable (inert) timers, and cc_util's shared
 * helpers — the same "stub the platform globals" approach research_test_harness
 * uses for CCResearch, plus a fake clock so no real timer keeps node alive.
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const util = require('./cc_util.js');

function makeEl(id) {
  const children = [];
  const qcache = new Map();
  const el = {
    id, hidden: true, innerHTML: '', textContent: '', className: '',
    value: '', disabled: false, dataset: {}, onclick: null, style: {},
    children,
    classList: {
      _s: new Set(),
      add(n) { this._s.add(n); }, remove(n) { this._s.delete(n); },
      toggle(n, f) { if (f) this._s.add(n); else this._s.delete(n); },
      contains(n) { return this._s.has(n); },
    },
    setAttribute(name, value) { if (name === 'hidden') el.hidden = true; el['attr_' + name] = value; },
    removeAttribute(name) { if (name === 'hidden') el.hidden = false; },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { children.push(c); return c; },
    replaceChildren(...c) { children.length = 0; children.push(...c); },
    remove() {}, focus() {}, reset() {},
    closest() { return null; },
    querySelector(sel) {
      if (!qcache.has(sel)) qcache.set(sel, makeEl(id + '::' + sel));
      return qcache.get(sel);
    },
    querySelectorAll() { return []; },
  };
  return el;
}

function makeFetch() {
  const queued = [];
  const calls = [];
  const mkRes = (reply) => {
    const r = {
      ok: reply.status >= 200 && reply.status < 300,
      status: reply.status,
      async json() { return reply.body; },
      clone() { return r; },
    };
    return r;
  };
  const fetch = async (url, options) => {
    let body;
    try { body = options && options.body ? JSON.parse(options.body) : undefined; }
    catch (e) { body = undefined; }
    calls.push({ url: String(url), options: options || {}, body });
    if (!queued.length) throw new Error(`unexpected fetch: ${url}`);
    const reply = queued.shift();
    if (reply.networkError) throw reply.networkError;
    return mkRes(reply);
  };
  fetch.enqueue = (status, body) => { queued.push({ status, body }); return fetch; };
  fetch.reject = (message) => { queued.push({ networkError: new Error(message) }); return fetch; };
  fetch.calls = calls;
  return fetch;
}

// Inert fake timers: store callbacks, never fire on their own (so a detached
// reconcile loop can't keep node alive). tick() runs pending setTimeout
// callbacks once, for the rare test that wants to advance time.
function makeClock() {
  const timeouts = [];
  return {
    setTimeout(fn) { timeouts.push(fn); return timeouts.length; },
    clearTimeout() {},
    setInterval() { return 0; },
    clearInterval() {},
    tick() { const due = timeouts.splice(0); due.forEach((fn) => fn()); },
  };
}

function makeHarness() {
  const elements = new Map();
  const getEl = (id) => {
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  };
  const fetch = makeFetch();
  const clock = makeClock();
  let uuidN = 0;
  const state = { view: {}, confirmReturn: true };

  const document = {
    getElementById: getEl,
    createElement: () => makeEl('el'),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
  };

  const context = vm.createContext({
    ...util,
    console, document, fetch, Date, JSON, Promise, Error,
    URLSearchParams, FormData, AbortController, AbortSignal,
    crypto: { randomUUID: () => `id-${++uuidN}` },
    setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval, clearInterval: clock.clearInterval,
    window: { confirm: () => state.confirmReturn, location: { href: '' } },
    localStorage: {
      _store: Object.create(null),
      getItem(k) { return Object.prototype.hasOwnProperty.call(this._store, k)
        ? this._store[k] : null; },
      setItem(k, v) { this._store[k] = String(v); },
      removeItem(k) { delete this._store[k]; },
    },
  });

  const source = fs.readFileSync(
    path.join(__dirname, 'command_center_commands.js'), 'utf8');
  vm.runInContext(source, context, { filename: 'command_center_commands.js' });
  const api = context.CCCommands;
  api.init({ view: () => state.view, config: { commandsEnabled: true } });

  return {
    api, context, fetch, clock, elements, getEl,
    setView(v) { state.view = v; },
    setConfirm(v) { state.confirmReturn = v; },
    posts: () => fetch.calls.filter((c) => c.options && c.options.method === 'POST'),
  };
}

const flush = async () => { for (let i = 0; i < 6; i += 1) await new Promise((r) => setImmediate(r)); };

let passed = 0;
async function test(name, fn) {
  try { await fn(); passed += 1; }
  catch (error) { error.message = `${name}: ${error.message}`; throw error; }
}

(async () => {
  // 1 — order classification: entry vs the fail-safe PROTECTIVE default.
  await test('classifyOrder: entry is entry; everything else is protective (fail-safe)', () => {
    const { api } = makeHarness();
    assert.equal(api.classifyOrder({ leg: 'entry' }), 'entry');
    assert.equal(api.classifyOrder({ leg: 'stop' }), 'protective');
    assert.equal(api.classifyOrder({ leg: 'take_profit' }), 'protective');
    assert.equal(api.classifyOrder({ leg: 'child-7' }), 'protective');
    assert.equal(api.classifyOrder({ leg: null }), 'protective');
    assert.equal(api.classifyOrder({}), 'protective');
  });

  // 2 — the live/paper fork keys off injected state.
  await test('account-mode gating reads the injected view', () => {
    const h = makeHarness();
    h.setView({ accounts: [{ account_mode: 'live' }] });
    assert.equal(h.api.dashboardAccountMode(), 'live');
    assert.equal(h.api.isLive('live'), true);
    assert.equal(h.api.isPaper('live'), false);
    h.setView({ accounts: [{ account_mode: 'paper' }] });
    assert.equal(h.api.dashboardAccountMode(), 'paper');
    assert.equal(h.api.isPaper('paper'), true);
  });

  // 3 — CSRF 403 refetches the token and retries ONCE with the SAME command_id.
  await test('ccPost: CSRF_REJECTED retries once, same command_id, refreshed token', async () => {
    const h = makeHarness();
    h.fetch.enqueue(200, { csrf_token: 't1' });        // first token
    h.fetch.enqueue(403, { code: 'CSRF_REJECTED' });    // POST rejected
    h.fetch.enqueue(200, { csrf_token: 't2' });         // forced refetch
    h.fetch.enqueue(202, {});                            // POST accepted
    const res = await h.api.post('/api/commands/x', { command_id: 'cmd-1', foo: 1 });
    assert.equal(res.ok, true);
    const posts = h.posts();
    assert.equal(posts.length, 2, 'retried exactly once');
    assert.equal(posts[0].body.command_id, 'cmd-1');
    assert.equal(posts[1].body.command_id, 'cmd-1');    // idempotent: same id
    assert.equal(posts[0].options.headers['X-CSRF-Token'], 't1');
    assert.equal(posts[1].options.headers['X-CSRF-Token'], 't2');  // token refreshed
  });

  // 4 — paper approve is a single POST, no preflight ceremony.
  await test('approveProposal (paper): single POST, no /api/preflight', async () => {
    const h = makeHarness();
    h.fetch.enqueue(200, { csrf_token: 't' });
    h.fetch.enqueue(202, {});
    await h.api.approveProposal({ id: 41, entity_revision: 2, account_mode: 'paper' });
    const posts = h.posts();
    assert.equal(posts.length, 1);
    assert.equal(posts[0].url, '/api/commands/proposals/41/approve');
    assert.equal(posts[0].body.preflight_nonce, null);
    assert.ok(!h.fetch.calls.some((c) => String(c.url).includes('/api/preflight')),
      'no preflight on paper');
  });

  // 5 — live approve runs the two-stage ceremony: preflight first, the order is
  // NOT routed until the human confirms, then it carries the preflight nonce
  // and the ceremony's command_id.
  await test('approveProposal (live): preflight first, approve deferred until confirm, carries nonce', async () => {
    const h = makeHarness();
    h.fetch.enqueue(200, { csrf_token: 't' });          // csrf for preflight
    h.fetch.enqueue(200, {                                // preflight ticket
      command_id: 'ignored', nonce: 'NONCE-9',
      expires_at: '2999-01-01T00:00:00Z',
      summary: { side: 'BUY', instrument: 'AAPL', warnings: [] },
    });
    await h.api.approveProposal({ id: 41, entity_revision: 2, account_mode: 'live' });
    assert.ok(h.fetch.calls.some((c) => c.url === '/api/preflight' && c.options.method === 'POST'),
      'preflight requested');
    assert.ok(!h.fetch.calls.some((c) => String(c.url).includes('/approve')),
      'nothing routed to the broker before confirm');

    // Human confirms via the confirm drawer's button.
    h.fetch.enqueue(200, { csrf_token: 't2' });         // csrf for approve
    h.fetch.enqueue(202, {});                            // approve accepted
    const confirmBtn = h.getEl('cc-confirm-drawer').querySelector('#cc-confirm-button');
    assert.equal(typeof confirmBtn.onclick, 'function', 'confirm wired');
    confirmBtn.onclick();
    await flush();

    const approve = h.fetch.calls.find((c) => c.url === '/api/commands/proposals/41/approve');
    const preflight = h.fetch.calls.find((c) => c.url === '/api/preflight');
    assert.ok(approve, 'approve routed after confirm');
    assert.equal(approve.body.preflight_nonce, 'NONCE-9');
    assert.equal(approve.body.command_id, preflight.body.command_id, 'one command_id across the ceremony');
  });

  // 6 — a 202 leaves a pending chip up; resolveCommand clears it and toasts the
  // terminal state (ok vs rejected).
  await test('202 registers a pending chip; resolveCommand clears it and toasts', async () => {
    const h = makeHarness();
    h.fetch.enqueue(200, { csrf_token: 't' });
    h.fetch.enqueue(202, {});
    await h.api.submitCommand('approve_proposal', 'Approve #41',
      '/api/commands/proposals/41/approve', { command_id: 'cmd-9' });
    const pending = h.getEl('cc-pending-commands');
    assert.equal(pending.children.length, 1, 'pending chip shown after 202');
    assert.match(pending.children[0].textContent, /Approve #41 — Pending confirmation/);

    h.api.resolveCommand('cmd-9', 'SUBMITTED', null, 'Approve #41');
    assert.equal(h.getEl('cc-pending-commands').children.length, 0, 'chip cleared');
    const okToast = h.getEl('cc-toasts').children.at(-1);
    assert.match(okToast.textContent, /Approve #41: submitted/);
    assert.match(okToast.className, /cc-toast-ok/);

    h.api.resolveCommand('cmd-9', 'REJECTED', 'RISK', 'Approve #41');
    const errToast = h.getEl('cc-toasts').children.at(-1);
    assert.match(errToast.textContent, /rejected \(RISK\)/);
    assert.match(errToast.className, /cc-toast-error/);
  });

  // 7 — unsigned allocation payload builder (Scaling tab JSON helper).
  await test('buildUnsignedAllocationPayload: happy path + stage ceiling', async () => {
    const h = makeHarness();
    const base = {
      strategy_id: 'orb_googl',
      account_id: 'DU123',
      account_mode: 'paper',
      stage: 'SCALE_1',
      max_gross_allocation: 0.09,
      ttl_days: 30,
      artifact_digest: 'sha256:a',
      allowlist_digest: 'sha256:b',
      ruleset_digest: 'sha256:c',
      evidence_digest: 'sha256:d',
      operator: 'alice',
      reason: 'scale after canary',
      public_key_id: 'key-1',
      issued_at: '2026-07-23T12:00:00.000Z',
    };
    const ok = h.api.buildUnsignedAllocationPayload(base);
    assert.equal(ok.ok, true);
    assert.equal(ok.payload.stage, 'SCALE_1');
    assert.equal(ok.payload.max_gross_allocation, 0.09);
    assert.equal(ok.payload.issued_at, '2026-07-23T12:00:00Z');
    assert.equal(ok.payload.expires_at, '2026-08-22T12:00:00Z');
    assert.equal(ok.payload.signature, undefined);

    const over = h.api.buildUnsignedAllocationPayload({
      ...base, max_gross_allocation: 0.10,
    });
    assert.equal(over.ok, false);
    assert.ok(over.errors.some((e) => /ceiling/i.test(e)));

    const missing = h.api.buildUnsignedAllocationPayload({ stage: 'SCALE_1' });
    assert.equal(missing.ok, false);
    assert.ok(missing.errors.length >= 3);
  });

  await test('prefillScalingPayloadForm: digests from paper_automation + evidence from authority', () => {
    const h = makeHarness();
    const strategy = h.getEl('scaling-payload-strategy');
    strategy.options = [{ value: 'orb_googl' }, { value: '' }];
    strategy.value = '';
    h.setView({
      accounts: [{ account_id: 'DU999', account_mode: 'paper' }],
      paper_automation: {
        strategy_name: 'orb_googl',
        artifact_digest: 'afe3e4e54c3f6d37',
        allowlist_digest: 'allowlist-1',
        ruleset_digest: 'ruleset-abc',
        public_key_id: 'ed25519-deadbeef',
      },
      scaling: {
        stage: 'SCALE_1',
        strategy_id: 'orb_googl',
        authorities: [{
          entity_revision: 2,
          strategy_id: 'orb_googl',
          evidence_digest: 'sha256:evidence-from-authority',
          operator: 'alice',
        }],
      },
    });
    h.api.prefillScalingPayloadForm();
    assert.equal(h.getEl('scaling-payload-account').value, 'DU999');
    assert.equal(h.getEl('scaling-payload-mode').value, 'paper');
    assert.equal(h.getEl('scaling-payload-artifact').value, 'afe3e4e54c3f6d37');
    assert.equal(h.getEl('scaling-payload-allowlist').value, 'allowlist-1');
    assert.equal(h.getEl('scaling-payload-ruleset').value, 'ruleset-abc');
    assert.equal(h.getEl('scaling-payload-key-id').value, 'ed25519-deadbeef');
    assert.equal(h.getEl('scaling-payload-evidence').value, 'sha256:evidence-from-authority');
    assert.equal(h.getEl('scaling-payload-operator').value, 'alice');
    assert.match(h.getEl('scaling-payload-reason').value, /SCALE_1/);
  });

  console.log(`command_center_commands.test.js: ${passed} tests passed`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

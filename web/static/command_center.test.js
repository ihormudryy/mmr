/* Stateful browser-client tests for command_center.js.
 *
 * The production script is evaluated in a small VM-backed DOM shim.  This
 * keeps the tests dependency-free while exercising the real reducer and
 * recovery code rather than a copied implementation.
 */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const util = require('./cc_util.js');

const source = fs.readFileSync(path.join(__dirname, 'command_center.js'), 'utf8')
  .replace(/\nresync\(\);\s*$/, '\n');

function element() {
  const handlers = new Map();
  return {
    hidden: true,
    innerHTML: '',
    textContent: '',
    className: '',
    value: '',
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(type, callback) {
      if (!handlers.has(type)) handlers.set(type, []);
      handlers.get(type).push(callback);
    },
    dispatch(type, event = {}) {
      for (const callback of handlers.get(type) || []) callback(event);
    },
    appendChild() {},
    replaceChildren() {},
    focus() {},
    remove() {},
    closest() { return null; },
    querySelector() { return element(); },
  };
}

function makeContext({commandsEnabled = false} = {}) {
  const elements = new Map();
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const document = {
    body: { dataset: {
      commandsEnabled: commandsEnabled ? 'true' : 'false',
      degradedAfterMs: '15000', pollIntervalMs: '5000',
    } },
    getElementById: getElement,
    querySelector: () => element(),
    querySelectorAll: (sel) => {
      if (sel === '[data-proposal-filter]') return [];
      return [];
    },
    addEventListener() {},
    createElement: () => element(),
  };
  class FakeEventSource {
    static OPEN = 1;
    constructor(url) { this.url = url; this.readyState = 0; }
    addEventListener() {}
    close() { this.readyState = 2; }
  }
  const context = vm.createContext({
    ...util,
    AbortController,
    AbortSignal,
    console,
    crypto: { randomUUID: () => '00000000-0000-4000-8000-000000000000' },
    document,
    EventSource: FakeEventSource,
    fetch: async () => { throw new Error('unexpected fetch'); },
    FormData: class { entries() { return []; } get() { return null; } },
    URLSearchParams,
    setInterval: () => 1,
    clearInterval() {},
    setTimeout,
    clearTimeout,
    window: { location: { href: '' } },
  });
  vm.runInContext(source, context, { filename: 'command_center.js' });
  return { context, elements, run: (code) => vm.runInContext(code, context) };
}

function emptyView() {
  return {
    stream_id: 'stream-a', sequence: 5, generated_at: '2026-07-15T12:00:00Z',
    last_event_at: null, accounts: [], positions: [],
    proposals: { active: [], terminal: [] },
    orders: { active: [], terminal: [] }, fills: [], strategies: [],
    reconciliation: [], trading_control: [], commands: [], risk: {},
  };
}

let passed = 0;
async function test(name, fn) {
  try {
    await fn();
    passed += 1;
  } catch (error) {
    error.message = `${name}: ${error.message}`;
    throw error;
  }
}

(async () => {
  await test('a forward sequence gap triggers resync without applying the event', () => {
    const { run } = makeContext();
    const view = emptyView();
    view.accounts.push({ entity_id: 'acct', entity_revision: 1, marker: 'before' });
    run(`store.view = ${JSON.stringify(view)};
         store.streamId = 'stream-a'; store.sequence = 5;
         globalThis.resyncCalls = 0;
         resync = () => { globalThis.resyncCalls += 1; };
         renderAll = () => {};`);

    run(`applyEvent({stream_id: 'stream-a', sequence: 7,
      entity_type: 'account', entity_id: 'acct', entity_revision: 2,
      operation: 'upsert', payload: {marker: 'after'}, source_timestamp: 'now'});`);

    assert.equal(run('globalThis.resyncCalls'), 1);
    assert.equal(run('store.sequence'), 5);
    assert.equal(run("store.view.accounts[0].marker"), 'before');
  });

  await test('a duplicate sequence is ignored, while the exact successor applies', () => {
    const { run } = makeContext();
    const view = emptyView();
    view.accounts.push({ entity_id: 'acct', entity_revision: 1, marker: 'before' });
    run(`store.view = ${JSON.stringify(view)};
         store.streamId = 'stream-a'; store.sequence = 5;
         globalThis.resyncCalls = 0;
         resync = () => { globalThis.resyncCalls += 1; };
         renderAll = () => {};`);

    run(`applyEvent({stream_id: 'stream-a', sequence: 5,
      entity_type: 'account', entity_id: 'acct', entity_revision: 2,
      operation: 'upsert', payload: {marker: 'duplicate'}, source_timestamp: 'old'});`);
    assert.equal(run('globalThis.resyncCalls'), 0);
    assert.equal(run("store.view.accounts[0].marker"), 'before');

    run(`applyEvent({stream_id: 'stream-a', sequence: 6,
      entity_type: 'account', entity_id: 'acct', entity_revision: 2,
      operation: 'upsert', payload: {marker: 'successor'}, source_timestamp: 'now'});`);
    assert.equal(run('store.sequence'), 6);
    assert.equal(run("store.view.accounts[0].marker"), 'successor');
  });

  await test('a malformed sequence cannot be mistaken for a duplicate', () => {
    const { run } = makeContext();
    run(`store.view = ${JSON.stringify(emptyView())};
         store.streamId = 'stream-a'; store.sequence = 5;
         globalThis.resyncCalls = 0;
         resync = () => { globalThis.resyncCalls += 1; };
         renderAll = () => {};`);

    run(`applyEvent({stream_id: 'stream-a', sequence: null,
      entity_type: 'account', entity_id: 'acct', entity_revision: 2,
      operation: 'upsert', payload: {}, source_timestamp: 'now'});`);
    assert.equal(run('globalThis.resyncCalls'), 1);
    assert.equal(run('store.sequence'), 5);
  });

  await test('initial snapshot recovery is visibly degraded while polling', () => {
    const { elements, run } = makeContext();
    run(`everConnected = false; store.health = null;
         store.connection.mode = 'polling'; updateBanner();`);
    assert.equal(elements.get('degraded-banner').hidden, false);
  });

  await test('snapshot fetch is aborted at its deadline and reports failure', async () => {
    const { context, run } = makeContext();
    let suppliedSignal = null;
    context.setTimeout = (callback) => { callback(); return 1; };
    context.clearTimeout = () => {};
    context.fetch = (_url, options) => {
      suppliedSignal = options.signal;
      return new Promise((_resolve, reject) => {
        if (options.signal.aborted) reject(new Error('aborted'));
        else options.signal.addEventListener('abort', () => reject(new Error('aborted')));
      });
    };

    assert.equal(await run('fetchSnapshot()'), null);
    assert.ok(suppliedSignal, 'fetch must receive an AbortSignal');
    assert.equal(suppliedSignal.aborted, true);
  });

  await test('snapshot arrival does not reset a stale server-timestamped quote', () => {
    const { elements, run } = makeContext();
    const generatedAt = '2026-07-15T12:00:00.000Z';
    const generatedMs = Date.parse(generatedAt);
    const view = emptyView();
    view.generated_at = generatedAt;
    view.positions.push({ entity_id: 'acct:101', conid: 101, symbol: 'STALE', quantity: 1 });
    view.quotes = {
      101: { last: 42, server_received_timestamp:
        new Date(generatedMs - 45_000).toISOString() },
    };
    run(`Date.now = () => ${generatedMs}; renderAll = () => {};
         applySnapshot(${JSON.stringify(view)}); renderPositions();`);

    const html = elements.get('positions-body').innerHTML;
    assert.match(html, /<tr class="stale"/);
    assert.match(html, />45s<\/span>/);
  });

  await test('research proposal resets stale intent and resolves only the instrument', async () => {
    const {elements, run} = makeContext();
    const form = run("document.getElementById('cc-proposal-form')");
    const fieldNames = [
      'resolve_symbol', 'resolve_exchange', 'resolve_currency', 'conid',
      'action', 'quantity', 'amount', 'confidence', 'group', 'thesis',
      'reasoning',
    ];
    for (const name of fieldNames) form[name] = element();
    for (const name of fieldNames) form[name].value = `stale-${name}`;
    form.resetCalls = 0;
    form.reset = () => {
      form.resetCalls += 1;
      for (const name of fieldNames) form[name].value = '';
      form.action.value = 'BUY';
    };
    run(`globalThis.resolveCalls = 0;
         globalThis.submitCalls = 0;
         ccResolveSymbol = async () => { globalThis.resolveCalls += 1; };
         ccSubmitCommand = async () => { globalThis.submitCalls += 1; };`);

    await run(`ccOpenResearchProposal({
      ticker: ' aapl ', exchange: ' NASDAQ ', currency: ' USD ',
      action: 'SELL', quantity: 100, amount: 999, confidence: 1,
      thesis: 'provider intent', reasoning: 'provider reasoning'
    })`);

    assert.equal(form.resetCalls, 1);
    assert.equal(form.resolve_symbol.value, 'AAPL');
    assert.equal(form.resolve_exchange.value, 'NASDAQ');
    assert.equal(form.resolve_currency.value, 'USD');
    assert.equal(form.conid.value, '');
    assert.equal(form.action.value, 'BUY');
    for (const name of [
      'quantity', 'amount', 'confidence', 'group', 'thesis', 'reasoning',
    ]) assert.equal(form[name].value, '');
    assert.equal(elements.get('cc-proposal-drawer').hidden, false);
    assert.equal(run('globalThis.resolveCalls'), 1);
    assert.equal(run('globalThis.submitCalls'), 0);
  });

  await test('a stale symbol resolution cannot overwrite a newer instrument conId', async () => {
    const {context, run} = makeContext();
    const form = run("document.getElementById('cc-proposal-form')");
    for (const name of [
      'resolve_symbol', 'resolve_exchange', 'resolve_currency', 'conid',
    ]) form[name] = element();
    form.reset = () => {
      form.resolve_symbol.value = '';
      form.resolve_exchange.value = '';
      form.resolve_currency.value = '';
      form.conid.value = '';
    };
    const pending = [];
    context.fetch = (_url, _options) => new Promise((resolve) => pending.push(resolve));

    const first = run("ccOpenResearchProposal({ticker: 'AAPL'})");
    const second = run("ccOpenResearchProposal({ticker: 'MSFT'})");
    assert.equal(pending.length, 2);

    pending[1]({ok: true, async json() { return {instruments: [{
      symbol: 'MSFT', instrument_id: 202, primary_exchange: 'NASDAQ', currency: 'USD',
    }]}; }});
    await second;
    pending[0]({ok: true, async json() { return {instruments: [{
      symbol: 'AAPL', instrument_id: 101, primary_exchange: 'NASDAQ', currency: 'USD',
    }]}; }});
    await first;

    assert.equal(form.resolve_symbol.value, 'MSFT');
    assert.equal(form.conid.value, 202);
    assert.match(run("document.getElementById('cc-resolve-status').textContent"), /MSFT/);
  });

  await test('editing any resolved instrument hint clears its conId', async () => {
    const {context, run} = makeContext({commandsEnabled: true});
    const form = run("document.getElementById('cc-proposal-form')");
    for (const name of [
      'resolve_symbol', 'resolve_exchange', 'resolve_currency', 'conid',
    ]) form[name] = element();
    form.resolve_symbol.value = 'AAPL';
    form.resolve_exchange.value = 'NASDAQ';
    form.resolve_currency.value = 'USD';
    context.fetch = async () => ({ok: true, async json() { return {instruments: [{
      symbol: 'AAPL', instrument_id: 101, primary_exchange: 'NASDAQ', currency: 'USD',
    }]}; }});

    await run('ccResolveSymbol()');
    assert.equal(form.conid.value, 101);
    for (const name of ['resolve_symbol', 'resolve_exchange', 'resolve_currency']) {
      form.conid.value = 101;
      form.dispatch('input', {target: form[name]});
      assert.equal(form.conid.value, '', `${name} edit must invalidate conId`);
    }
  });

  await test('failed and no-match resolution retries cannot retain a prior conId', async () => {
    const {context, run} = makeContext({commandsEnabled: true});
    const form = run("document.getElementById('cc-proposal-form')");
    for (const name of [
      'resolve_symbol', 'resolve_exchange', 'resolve_currency', 'conid',
    ]) form[name] = element();
    form.resolve_symbol.value = 'AAPL';
    form.resolve_exchange.value = 'NASDAQ';
    form.resolve_currency.value = 'USD';
    const responses = [
      {ok: true, async json() { return {instruments: []}; }},
      {ok: false, status: 502, async json() { return {error: 'resolve failed'}; }},
    ];
    context.fetch = async () => responses.shift();

    form.conid.value = 101;
    await run('ccResolveSymbol()');
    assert.equal(form.conid.value, '');

    form.conid.value = 202;
    await run('ccResolveSymbol()');
    assert.equal(form.conid.value, '');
  });

  console.log(`command_center.test.js: ${passed} tests passed`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

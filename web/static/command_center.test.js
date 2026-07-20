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
  return {
    hidden: true,
    innerHTML: '',
    textContent: '',
    className: '',
    value: '',
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {},
    appendChild() {},
    replaceChildren() {},
    focus() {},
    remove() {},
    closest() { return null; },
    querySelector() { return element(); },
  };
}

function makeContext() {
  const elements = new Map();
  const getElement = (id) => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  const document = {
    body: { dataset: {
      commandsEnabled: 'false', degradedAfterMs: '15000', pollIntervalMs: '5000',
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

  console.log(`command_center.test.js: ${passed} tests passed`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

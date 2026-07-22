'use strict';

const assert = require('node:assert/strict');
const {makeHarness} = require('./research_test_harness.js');

let passed = 0;
async function test(name, callback) {
  try {
    await callback();
    passed += 1;
  } catch (error) {
    error.message = `${name}: ${error.message}`;
    throw error;
  }
}

function response(data, title, meta = {}) {
  return {data, title, meta: {provider: 'massive', ...meta}};
}

function proposalHarness(enabled) {
  const h = makeHarness();
  const root = {dataset: {researchProposeEnabled: enabled ? 'true' : 'false'}};
  h.document.querySelector = (selector) => (
    selector === '[data-research-propose-enabled]' ? root : null
  );
  h.context.proposalCalls = [];
  h.context.ccOpenResearchProposal = (instrument) => {
    h.context.proposalCalls.push(instrument);
  };
  h.loadProductionScript();
  return h;
}

function proposalTarget() {
  const button = {dataset: {researchPropose: ''}};
  return {closest(selector) {
    return selector === '[data-research-propose]' ? button : null;
  }};
}

(async () => {
  await test('initialization fetches presets only and does not scan', async () => {
    const h = makeHarness();
    h.fetch.enqueue(200, response([{preset: 'momentum'}, {preset: 'value'}], 'Presets', {
      tool: 'presets', provider: 'local',
    }));
    h.loadProductionScript();
    await h.start();

    assert.deepEqual(h.fetch.calls.map((call) => call.url), ['/api/research/presets']);
    assert.equal(h.api.state.presets.data.length, 2);
    assert.match(h.elements.get('research-controls').innerHTML, /momentum/);
    assert.equal(h.api.state.ideas.data, null);
  });

  await test('Ideas controls include all filters and repeat normalized tickers', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.api.renderControls();
    const html = h.elements.get('research-controls').innerHTML;
    for (const name of [
      'preset', 'source', 'tickers', 'universe', 'num', 'min_price', 'max_price',
      'min_volume', 'min_change', 'max_change', 'fundamentals', 'news', 'names',
    ]) assert.match(html, new RegExp(`name="${name}"`));

    const params = h.api.paramsFromForm(h.form('ideas', [
      ['source', 'tickers'], ['tickers', ' aapl, msft  AAPL '], ['num', '12'],
    ], [{name: 'fundamentals', checked: true}, {name: 'news', checked: false}]));
    assert.deepEqual(params.getAll('tickers'), ['AAPL', 'MSFT', 'AAPL']);
    assert.equal(params.get('fundamentals'), 'true');
    assert.equal(params.get('news'), 'false');
  });

  await test('tool state and selection survive switching and a failed refresh', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.fetch.enqueue(200, response([
      {ticker: 'AAPL', score: 5}, {ticker: 'MSFT', score: 4},
    ], 'Ideas: momentum', {tool: 'ideas', observed_at: '2026-07-22T10:00:00Z'}));
    await h.api.run('ideas', new URLSearchParams({preset: 'momentum'}));
    h.api.selectRow(1);
    const priorMeta = h.api.state.ideas.meta;

    h.api.selectTool('movers');
    h.api.selectTool('ideas');
    assert.equal(h.api.state.ideas.selected.ticker, 'MSFT');
    assert.match(h.elements.get('research-results').innerHTML, /AAPL/);

    h.fetch.enqueue(502, {error: {code: 'RESEARCH_UPSTREAM_ERROR',
      message: 'Ideas provider request failed.', retryable: true}});
    await h.api.run('ideas', new URLSearchParams({preset: 'momentum'}));
    assert.equal(h.api.state.ideas.data.length, 2);
    assert.equal(h.api.state.ideas.selected.ticker, 'MSFT');
    assert.equal(h.api.state.ideas.meta, priorMeta);
    assert.match(h.elements.get('research-status').textContent, /provider request failed/i);
  });

  await test('network failure preserves last successful result', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.api.selectTool('movers');
    h.fetch.enqueue(200, response([{ticker: 'NVDA'}], 'Movers', {tool: 'movers'}));
    await h.api.run('movers', new URLSearchParams());
    h.fetch.reject('offline');
    await h.api.run('movers', new URLSearchParams());

    assert.equal(h.api.state.movers.data[0].ticker, 'NVDA');
    assert.equal(h.api.state.movers.error.code, 'NETWORK_ERROR');
    assert.match(h.elements.get('research-status').textContent, /offline/i);
  });

  await test('a latest same-tool response wins when an older success arrives later', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    const older = h.fetch.defer();
    const newer = h.fetch.defer();
    const oldRun = h.api.run('ideas', new URLSearchParams({preset: 'old'}));
    const newRun = h.api.run('ideas', new URLSearchParams({preset: 'new'}));

    newer.resolve(200, response([{ticker: 'NEW'}], 'Ideas: new', {
      tool: 'ideas', observed_at: 'new-time',
    }));
    await newRun;
    older.resolve(200, response([{ticker: 'OLD'}], 'Ideas: old', {
      tool: 'ideas', observed_at: 'old-time',
    }));
    await oldRun;

    assert.equal(h.api.state.ideas.data[0].ticker, 'NEW');
    assert.equal(h.api.state.ideas.title, 'Ideas: new');
    assert.equal(h.api.state.ideas.meta.observed_at, 'new-time');
    assert.equal(h.api.state.ideas.selected.ticker, 'NEW');
    assert.equal(h.api.state.ideas.error, null);
    assert.equal(h.api.state.ideas.loading, false);
  });

  await test('a stale error cannot replace the latest successful same-tool state', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    const older = h.fetch.defer();
    const newer = h.fetch.defer();
    const oldRun = h.api.run('movers', new URLSearchParams({direction: 'losers'}));
    const newRun = h.api.run('movers', new URLSearchParams({direction: 'gainers'}));

    newer.resolve(200, response([{ticker: 'NEW'}], 'New movers', {tool: 'movers'}));
    await newRun;
    older.resolve(502, {error: {code: 'RESEARCH_UPSTREAM_ERROR',
      message: 'Old request failed.', retryable: true}});
    await oldRun;

    assert.equal(h.api.state.movers.data[0].ticker, 'NEW');
    assert.equal(h.api.state.movers.error, null);
    assert.equal(h.api.state.movers.loading, false);
  });

  await test('a stale settlement leaves loading true until the current request settles', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    const older = h.fetch.defer();
    const newer = h.fetch.defer();
    const oldRun = h.api.run('ideas', new URLSearchParams({preset: 'old'}));
    const newRun = h.api.run('ideas', new URLSearchParams({preset: 'new'}));

    older.resolve(200, response([{ticker: 'OLD'}], 'Ideas: old', {tool: 'ideas'}));
    await oldRun;
    assert.equal(h.api.state.ideas.loading, true);
    assert.equal(h.api.state.ideas.data, null);

    newer.resolve(200, response([{ticker: 'NEW'}], 'Ideas: new', {tool: 'ideas'}));
    await newRun;
    assert.equal(h.api.state.ideas.loading, false);
    assert.equal(h.api.state.ideas.data[0].ticker, 'NEW');
  });

  await test('valid empty response is rendered as no results rather than an error', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.api.selectTool('movers');
    h.fetch.enqueue(200, response([], 'Movers', {tool: 'movers'}));
    await h.api.run('movers', new URLSearchParams());

    assert.deepEqual(h.api.state.movers.data, []);
    assert.equal(h.api.state.movers.error, null);
    assert.match(h.elements.get('research-status').textContent, /no results/i);
    assert.equal(h.elements.get('research-results').innerHTML.includes('research-error'), false);
  });

  await test('missing configuration is shown safely in a visible banner', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.fetch.enqueue(503, {error: {code: 'MASSIVE_NOT_CONFIGURED',
      message: '<img src=x onerror=alert(1)> Massive API key is not configured.',
      retryable: false}});
    await h.api.run('movers', new URLSearchParams());

    const banner = h.elements.get('research-config-banner');
    assert.equal(banner.hidden, false);
    assert.equal(banner.innerHTML, '');
    assert.match(banner.textContent, /^<img/);
    assert.equal(h.api.state.movers.error.code, 'MASSIVE_NOT_CONFIGURED');
  });

  await test('every provider field rendered through innerHTML is escaped', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.fetch.enqueue(200, response([{
      ticker: '<script>alert(1)</script>',
      label: '<img src=x onerror=alert(2)>',
      nested: {value: '<svg onload=alert(3)>'},
    }], '<b>unsafe title</b>', {
      tool: 'ideas', provider: '<i>bad provider</i>', observed_at: '<time>bad</time>',
    }));
    await h.api.run('ideas', new URLSearchParams());
    h.api.selectRow(0);

    const html = h.elements.get('research-results').innerHTML
      + h.elements.get('research-detail').innerHTML;
    assert.doesNotMatch(html, /<script>|<img|<svg|<b>|<i>|<time>/);
    assert.match(html, /&lt;script&gt;/);
    assert.match(html, /&lt;img/);
    assert.match(html, /&lt;svg/);
    assert.match(html, /&lt;b&gt;/);
    assert.match(html, /&lt;i&gt;/);
  });

  await test('result rows support click and keyboard Enter selection', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.fetch.enqueue(200, response([], 'Presets', {tool: 'presets', provider: 'local'}));
    await h.start();
    h.fetch.enqueue(200, response([{ticker: 'AAPL'}, {ticker: 'MSFT'}], 'Ideas'));
    await h.api.run('ideas', new URLSearchParams());
    const results = h.elements.get('research-results');

    results.dispatch('click', {target: h.rowTarget(1)});
    assert.equal(h.api.state.ideas.selected.ticker, 'MSFT');
    results.dispatch('keydown', {key: 'Enter', preventDefault() {}, target: h.rowTarget(0)});
    assert.equal(h.api.state.ideas.selected.ticker, 'AAPL');
    assert.match(results.innerHTML, /tabindex="0"/);
  });

  await test('Lookup snapshot success and News failure remain independent', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.api.selectTool('lookup');
    h.fetch.enqueue(200, response({ticker: 'AAPL', price: 222}, 'Snapshot', {
      tool: 'snapshot', observed_at: '2026-07-22T10:00:00Z',
    }));
    h.fetch.enqueue(502, {error: {code: 'RESEARCH_UPSTREAM_ERROR',
      message: 'News provider request failed.', retryable: true}});
    await h.api.run('lookup', new URLSearchParams({symbol: 'AAPL', limit: '5', source: 'benzinga'}));

    assert.equal(h.api.state.lookup.snapshot.data.price, 222);
    assert.equal(h.api.state.lookup.snapshot.error, null);
    assert.equal(h.api.state.lookup.news.data, null);
    assert.equal(h.api.state.lookup.news.error.code, 'RESEARCH_UPSTREAM_ERROR');
    const html = h.elements.get('research-results').innerHTML;
    assert.match(html, /data-lookup-part="snapshot"/);
    assert.match(html, /AAPL/);
    assert.match(html, /data-lookup-part="news"/);
    assert.match(html, /News provider request failed/);
    assert.match(h.fetch.calls[0].url, /\/snapshot\?symbol=AAPL$/);
    assert.match(h.fetch.calls[1].url, /\/news\?ticker=AAPL&limit=5&source=benzinga$/);
  });

  await test('provider and observed-time labels render with results and detail', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.fetch.enqueue(200, response([{ticker: 'AAPL'}], 'Ideas: momentum', {
      tool: 'ideas', observed_at: '2026-07-22T10:00:00Z',
    }));
    await h.api.run('ideas', new URLSearchParams());
    const results = h.elements.get('research-results').innerHTML;
    const detail = h.elements.get('research-detail').innerHTML;
    assert.match(results, /Ideas: momentum/);
    assert.match(results, /massive/);
    assert.match(results, /2026-07-22T10:00:00Z/);
    assert.match(detail, /massive/);
  });

  await test('Propose is absent when the server-rendered partial disables it', async () => {
    const h = proposalHarness(false);
    h.fetch.enqueue(200, response([{ticker: 'AAPL'}], 'Ideas', {tool: 'ideas'}));
    await h.api.run('ideas', new URLSearchParams());

    assert.doesNotMatch(h.elements.get('research-detail').innerHTML,
      /data-research-propose/);
  });

  await test('eligible Ideas proposal delegates only the instrument and never posts', async () => {
    const h = proposalHarness(true);
    h.fetch.enqueue(200, response([], 'Presets', {
      tool: 'presets', provider: 'local',
    }));
    await h.start();
    h.fetch.enqueue(200, response([{
      ticker: 'AAPL', exchange: 'NASDAQ', currency: 'USD', action: 'SELL',
      quantity: 100, confidence: 1, thesis: 'provider thesis',
    }], 'Ideas', {tool: 'ideas'}));
    await h.api.run('ideas', new URLSearchParams());
    const fetchCount = h.fetch.calls.length;

    assert.match(h.elements.get('research-detail').innerHTML,
      /data-research-propose/);
    h.elements.get('research-detail').dispatch('click', {
      target: proposalTarget(),
    });

    assert.equal(h.context.proposalCalls.length, 1);
    assert.deepEqual(
      JSON.parse(JSON.stringify(h.context.proposalCalls[0])),
      {ticker: 'AAPL', exchange: 'NASDAQ', currency: 'USD'},
    );
    assert.equal(h.fetch.calls.length, fetchCount);
    assert.equal(h.fetch.calls.some((call) =>
      call.url === '/api/commands/proposals'), false);
  });

  await test('Propose is limited to stock Movers and rejects non-equity records', async () => {
    const h = proposalHarness(true);
    h.api.selectTool('movers');
    for (const market of ['crypto', 'indices', 'options', 'futures']) {
      h.fetch.enqueue(200, response([{ticker: 'NOPE', market}], 'Movers', {
        tool: 'movers',
      }));
      await h.api.run('movers', new URLSearchParams({market}));
      assert.doesNotMatch(h.elements.get('research-detail').innerHTML,
        /data-research-propose/);
    }

    h.fetch.enqueue(200, response([{
      ticker: 'NVDA', market: 'stocks', exchange: 'NASDAQ', currency: 'USD',
    }], 'Movers', {tool: 'movers'}));
    await h.api.run('movers', new URLSearchParams({market: 'stocks'}));
    assert.match(h.elements.get('research-detail').innerHTML,
      /data-research-propose/);
  });

  await test('Lookup stock snapshot can propose while explicit non-equity data cannot', async () => {
    const h = proposalHarness(true);
    h.api.selectTool('lookup');
    h.fetch.enqueue(200, response({
      ticker: 'AAPL', exchange: 'NASDAQ', currency: 'USD',
    }, 'Snapshot', {tool: 'snapshot'}));
    h.fetch.enqueue(200, response([], 'News', {tool: 'news'}));
    await h.api.run('lookup', new URLSearchParams({symbol: 'AAPL'}));
    assert.match(h.elements.get('research-detail').innerHTML,
      /data-research-propose/);

    h.fetch.enqueue(200, response({
      ticker: 'SPX', asset_class: 'index',
    }, 'Snapshot', {tool: 'snapshot'}));
    h.fetch.enqueue(200, response([], 'News', {tool: 'news'}));
    await h.api.run('lookup', new URLSearchParams({symbol: 'SPX'}));
    assert.doesNotMatch(h.elements.get('research-detail').innerHTML,
      /data-research-propose/);
  });

  await test('disabled Later tools cannot select or request', async () => {
    const h = makeHarness();
    h.loadProductionScript();
    h.fetch.enqueue(200, response([], 'Presets', {tool: 'presets', provider: 'local'}));
    await h.start();
    h.fetch.calls.length = 0;
    const disabled = h.tools.find((tool) => tool.dataset.researchTool === 'scan');
    disabled.dispatch('click');
    const result = await h.api.run('scan', new URLSearchParams());

    assert.equal(result, null);
    assert.equal(h.fetch.calls.length, 0);
    assert.equal(h.api.activeTool(), 'ideas');
  });

  console.log(`command_center_research.test.js: ${passed} tests passed`);
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});

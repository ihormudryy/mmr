/* Browser state and rendering for the read-only Command Center Research tab. */
'use strict';

(() => {
  const supportedTools = new Set(['ideas', 'movers', 'lookup']);
  const slot = () => ({
    data: null,
    title: null,
    meta: null,
    selected: null,
    loading: false,
    error: null,
  });
  const state = {
    presets: slot(),
    ideas: slot(),
    movers: slot(),
    lookup: {snapshot: slot(), news: slot()},
  };
  let currentTool = 'ideas';

  function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, (character) => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
    })[character]);
  }

  function paramsFromForm(form) {
    const params = new URLSearchParams();
    for (const [name, value] of new FormData(form).entries()) {
      if (name === 'tickers') {
        String(value).split(/[\s,]+/).filter(Boolean).forEach((ticker) => {
          params.append('tickers', ticker.toUpperCase());
        });
      } else if (value !== '') {
        params.append(name, value);
      }
    }
    for (const checkbox of form.querySelectorAll('input[type="checkbox"]')) {
      params.set(checkbox.name, checkbox.checked ? 'true' : 'false');
    }
    return params;
  }

  function errorMessage(body, status) {
    return body && body.error && body.error.message
      ? String(body.error.message)
      : `Research request failed (HTTP ${status})`;
  }

  function setConfigurationError(error) {
    if (!error || error.code !== 'MASSIVE_NOT_CONFIGURED') return;
    const banner = document.getElementById('research-config-banner');
    if (!banner) return;
    banner.textContent = error.message;
    banner.hidden = false;
  }

  function clearConfigurationError() {
    const banner = document.getElementById('research-config-banner');
    if (!banner || banner.hidden) return;
    banner.textContent = '';
    banner.hidden = true;
  }

  async function request(target, path, params) {
    target.loading = true;
    target.error = null;
    render();
    const query = params.toString();
    const url = `/api/research/${path}${query ? `?${query}` : ''}`;
    try {
      const response = await fetch(url, {
        credentials: 'same-origin',
        headers: {Accept: 'application/json'},
      });
      const body = await response.json();
      if (!response.ok) {
        target.error = {
          code: body && body.error && body.error.code
            ? String(body.error.code)
            : 'HTTP_ERROR',
          message: errorMessage(body, response.status),
          retryable: Boolean(body && body.error && body.error.retryable),
        };
        setConfigurationError(target.error);
        return null;
      }
      target.data = body.data;
      target.title = body.title ?? null;
      target.meta = body.meta ?? null;
      target.selected = Array.isArray(body.data)
        ? (body.data[0] ?? null)
        : (body.data ?? null);
      if (path !== 'presets') clearConfigurationError();
      return body;
    } catch (error) {
      target.error = {
        code: 'NETWORK_ERROR',
        message: String(error && error.message ? error.message : error),
        retryable: true,
      };
      return null;
    } finally {
      target.loading = false;
      render();
    }
  }

  async function run(tool, params = new URLSearchParams()) {
    if (!supportedTools.has(tool)) return null;
    if (tool === 'lookup') {
      const symbol = params.get('symbol') || '';
      const snapshotParams = new URLSearchParams({symbol});
      const newsParams = new URLSearchParams({
        ticker: symbol,
        limit: params.get('limit') || '10',
        source: params.get('source') || 'polygon',
      });
      const [snapshot, news] = await Promise.all([
        request(state.lookup.snapshot, 'snapshot', snapshotParams),
        request(state.lookup.news, 'news', newsParams),
      ]);
      return {snapshot, news};
    }
    return request(state[tool], tool, params);
  }

  function loadPresets() {
    return request(state.presets, 'presets', new URLSearchParams());
  }

  function selectTool(tool) {
    if (!supportedTools.has(tool)) return false;
    currentTool = tool;
    for (const button of document.querySelectorAll('[data-research-tool]')) {
      const active = button.dataset.researchTool === tool;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
    }
    renderControls();
    render();
    return true;
  }

  function selectRow(index) {
    if (currentTool === 'lookup') return false;
    const target = state[currentTool];
    if (!Array.isArray(target.data) || !target.data[index]) return false;
    target.selected = target.data[index];
    renderResults();
    renderDetail();
    return true;
  }

  function renderControls() {
    const root = document.getElementById('research-controls');
    if (!root) return '';
    const presetRows = Array.isArray(state.presets.data) ? state.presets.data : [];
    const presetOptions = presetRows.map((row) => {
      const value = row && (row.preset ?? row.name) ? (row.preset ?? row.name) : '';
      return `<option value="${esc(value)}">${esc(value)}</option>`;
    }).join('');
    const forms = {
      ideas: `<form data-research-form="ideas" class="research-form">
        <label>Preset <select name="preset" id="research-preset">${presetOptions}</select></label>
        <label>Source <select name="source"><option value="movers">Movers</option><option value="tickers">Tickers</option><option value="universe">Universe</option></select></label>
        <label>Tickers <input name="tickers" placeholder="AAPL MSFT"></label>
        <label>Universe <input name="universe"></label>
        <label>Results <input name="num" type="number" min="1" max="50" value="15"></label>
        <label>Min price <input name="min_price" type="number" min="0" step="any"></label>
        <label>Max price <input name="max_price" type="number" min="0" step="any"></label>
        <label>Min volume <input name="min_volume" type="number" min="0" step="1"></label>
        <label>Min change % <input name="min_change" type="number" step="any"></label>
        <label>Max change % <input name="max_change" type="number" step="any"></label>
        <label><input name="fundamentals" type="checkbox"> Fundamentals</label>
        <label><input name="news" type="checkbox"> News</label>
        <label><input name="names" type="checkbox"> Names</label>
        <button type="submit">Run Ideas</button>
      </form>`,
      movers: `<form data-research-form="movers" class="research-form">
        <label>Market <select name="market"><option>stocks</option><option>crypto</option><option>indices</option><option>options</option><option>futures</option></select></label>
        <label>Direction <select name="direction"><option>gainers</option><option>losers</option></select></label>
        <label>Results <input name="num" type="number" min="1" max="100" value="20"></label>
        <label><input name="detail" type="checkbox"> Detail</label>
        <button type="submit">Run Movers</button>
      </form>`,
      lookup: `<form data-research-form="lookup" class="research-form">
        <label>Ticker <input name="symbol" required maxlength="32"></label>
        <label>News source <select name="source"><option>polygon</option><option>benzinga</option></select></label>
        <label>News limit <input name="limit" type="number" min="1" max="50" value="10"></label>
        <button type="submit">Lookup</button>
      </form>`,
    };
    root.innerHTML = forms[currentTool];
    return root.innerHTML;
  }

  function statusFor(target) {
    if (target.loading) return 'Loading…';
    if (target.error) return target.error.message;
    if (Array.isArray(target.data) && target.data.length === 0) return 'No results.';
    return '';
  }

  function valueText(value) {
    if (value !== null && typeof value === 'object') return JSON.stringify(value);
    return value;
  }

  function metaMarkup(target) {
    if (!target.title && !target.meta) return '';
    const provider = target.meta && target.meta.provider;
    const observedAt = target.meta && target.meta.observed_at;
    return `<header class="research-result-meta">
      ${target.title ? `<strong>${esc(target.title)}</strong>` : ''}
      ${provider ? `<span data-research-provider>${esc(provider)}</span>` : ''}
      ${observedAt ? `<time data-research-observed>${esc(observedAt)}</time>` : ''}
    </header>`;
  }

  function objectMarkup(data) {
    if (data === null || data === undefined) return '';
    if (typeof data !== 'object') return `<p>${esc(data)}</p>`;
    return `<dl>${Object.entries(data).map(([key, value]) =>
      `<dt>${esc(key)}</dt><dd>${esc(valueText(value))}</dd>`).join('')}</dl>`;
  }

  function lookupPart(name, target) {
    const status = statusFor(target);
    let content = '';
    if (name === 'news' && Array.isArray(target.data)) {
      content = target.data.map((row) => `<article>${objectMarkup(row)}</article>`).join('');
    } else if (target.data !== null) {
      content = objectMarkup(target.data);
    }
    return `<section data-lookup-part="${name}">
      <h3>${name === 'snapshot' ? 'Snapshot' : 'News'}</h3>
      ${metaMarkup(target)}
      ${status ? `<p class="${target.error ? 'research-error' : 'research-part-status'}">${esc(status)}</p>` : ''}
      ${content}
    </section>`;
  }

  function renderResults() {
    const root = document.getElementById('research-results');
    const status = document.getElementById('research-status');
    if (!root || !status) return '';
    if (currentTool === 'lookup') {
      const snapshot = state.lookup.snapshot;
      const news = state.lookup.news;
      status.textContent = [statusFor(snapshot), statusFor(news)].filter(Boolean).join(' ');
      root.innerHTML = lookupPart('snapshot', snapshot) + lookupPart('news', news);
      return root.innerHTML;
    }

    const target = state[currentTool];
    status.textContent = statusFor(target);
    const rows = Array.isArray(target.data) ? target.data : [];
    const rowMarkup = rows.map((row, index) => {
      const name = row && (row.ticker ?? row.symbol ?? row.name ?? '');
      const measure = row && (row.change_pct ?? row.change_percent ?? row.score ?? row.price ?? '');
      const selected = target.selected === row;
      return `<button type="button" class="research-row${selected ? ' selected' : ''}" data-research-row="${index}" tabindex="0" aria-pressed="${selected ? 'true' : 'false'}"><strong>${esc(name)}</strong><span>${esc(measure)}</span></button>`;
    }).join('');
    root.innerHTML = metaMarkup(target) + rowMarkup;
    return root.innerHTML;
  }

  function renderDetail() {
    const root = document.getElementById('research-detail');
    if (!root) return '';
    const target = currentTool === 'lookup' ? state.lookup.snapshot : state[currentTool];
    if (!target.selected) {
      root.innerHTML = '<p class="dim">Select a result to inspect it.</p>';
      return root.innerHTML;
    }
    root.innerHTML = objectMarkup(target.selected) + metaMarkup(target);
    return root.innerHTML;
  }

  function render() {
    renderResults();
    renderDetail();
  }

  function rowFromEvent(event) {
    return event.target && event.target.closest
      ? event.target.closest('[data-research-row]')
      : null;
  }

  globalThis.CCResearch = {
    state,
    selectTool,
    run,
    selectRow,
    activeTool: () => currentTool,
    paramsFromForm,
    escapeHtml: esc,
    renderControls,
    renderResults,
    renderDetail,
  };

  document.addEventListener('DOMContentLoaded', () => {
    for (const button of document.querySelectorAll('[data-research-tool]:not([disabled])')) {
      button.addEventListener('click', () => selectTool(button.dataset.researchTool));
    }
    const controls = document.getElementById('research-controls');
    const results = document.getElementById('research-results');
    if (controls) {
      controls.addEventListener('submit', (event) => {
        event.preventDefault();
        const form = event.target;
        run(form.dataset.researchForm, paramsFromForm(form));
      });
    }
    if (results) {
      results.addEventListener('click', (event) => {
        const row = rowFromEvent(event);
        if (row) selectRow(Number(row.dataset.researchRow));
      });
      results.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        const row = rowFromEvent(event);
        if (!row) return;
        event.preventDefault();
        selectRow(Number(row.dataset.researchRow));
      });
    }
    renderControls();
    render();
    loadPresets().finally(() => {
      renderControls();
      render();
    });
  });
})();

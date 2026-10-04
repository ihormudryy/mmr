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
    generation: 0,
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
    const entries = Array.from(new FormData(form).entries());
    const source = String(
      (entries.find(([name]) => name === 'source') || [])[1] || '',
    );
    for (const [name, value] of entries) {
      if (name === 'tickers') {
        // Ideas API rejects tickers unless source=tickers.
        if (source !== 'tickers' || value === '') continue;
        String(value).split(/[\s,]+/).filter(Boolean).forEach((ticker) => {
          params.append('tickers', ticker.toUpperCase());
        });
      } else if (name === 'universe') {
        // Ideas API rejects universe unless source=universe.
        if (source !== 'universe' || value === '') continue;
        params.append(name, value);
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
    const generation = target.generation + 1;
    target.generation = generation;
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
      if (target.generation !== generation) return null;
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
      if (target.generation !== generation) return null;
      target.error = {
        code: 'NETWORK_ERROR',
        message: String(error && error.message ? error.message : error),
        retryable: true,
      };
      return null;
    } finally {
      if (target.generation !== generation) return;
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
      ideas: `<form data-research-form="ideas" class="param-form research-form">
        <label>Preset <select name="preset" id="research-preset">${presetOptions}</select></label>
        <label>Source <select name="source"><option value="movers">Movers</option><option value="tickers">Tickers</option><option value="universe">Universe</option></select></label>
        <label>Tickers <input name="tickers" class="w-symbols" placeholder="AAPL MSFT"></label>
        <label>Universe <input name="universe" class="w-name"></label>
        <label>Results <input name="num" class="w-xs" type="number" min="1" max="50" value="15"></label>
        <label>Min price <input name="min_price" class="w-sm" type="number" min="0" step="any"></label>
        <label>Max price <input name="max_price" class="w-sm" type="number" min="0" step="any"></label>
        <label>Min volume <input name="min_volume" class="w-sm" type="number" min="0" step="1"></label>
        <label>Min change % <input name="min_change" class="w-sm" type="number" step="any"></label>
        <label>Max change % <input name="max_change" class="w-sm" type="number" step="any"></label>
        <label><input name="fundamentals" type="checkbox"> Fundamentals</label>
        <label><input name="news" type="checkbox"> News</label>
        <label><input name="names" type="checkbox"> Names</label>
        <button type="submit">Run Ideas</button>
      </form>`,
      movers: `<form data-research-form="movers" class="param-form research-form">
        <label>Market <select name="market"><option>stocks</option><option>crypto</option><option>indices</option><option>options</option><option>futures</option></select></label>
        <label>Direction <select name="direction"><option>gainers</option><option>losers</option></select></label>
        <label>Results <input name="num" class="w-xs" type="number" min="1" max="100" value="20"></label>
        <label><input name="detail" type="checkbox"> Detail</label>
        <button type="submit">Run Movers</button>
      </form>`,
      lookup: `<form data-research-form="lookup" class="param-form research-form">
        <label>Ticker <input name="symbol" class="w-name" required maxlength="32"></label>
        <label>News source <select name="source"><option>polygon</option><option>benzinga</option></select></label>
        <label>News limit <input name="limit" class="w-xs" type="number" min="1" max="50" value="10"></label>
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
    if (target.data !== null && typeof target.data === 'object'
        && !Array.isArray(target.data)
        && Object.keys(target.data).length === 0) {
      return 'No results.';
    }
    return '';
  }

  function isPlainObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function hasEntries(value) {
    return isPlainObject(value) && Object.keys(value).length > 0;
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function formatNumber(value, digits = 2) {
    const number = toNumber(value);
    if (number === null) return '—';
    return number.toLocaleString(undefined, {
      maximumFractionDigits: digits,
      minimumFractionDigits: Number.isInteger(number) ? 0 : Math.min(digits, 2),
    });
  }

  function formatPct(value) {
    const number = toNumber(value);
    if (number === null) return '—';
    const sign = number > 0 ? '+' : '';
    return `${sign}${formatNumber(number, 2)}%`;
  }

  function formatVolume(value) {
    const number = toNumber(value);
    if (number === null) return '—';
    const abs = Math.abs(number);
    if (abs >= 1e9) return `${formatNumber(number / 1e9, 2)}B`;
    if (abs >= 1e6) return `${formatNumber(number / 1e6, 2)}M`;
    if (abs >= 1e3) return `${formatNumber(number / 1e3, 1)}K`;
    return formatNumber(number, 0);
  }

  function formatMoney(value) {
    const number = toNumber(value);
    if (number === null) return '—';
    const abs = Math.abs(number);
    if (abs >= 1e12) return `$${formatNumber(number / 1e12, 2)}T`;
    if (abs >= 1e9) return `$${formatNumber(number / 1e9, 2)}B`;
    if (abs >= 1e6) return `$${formatNumber(number / 1e6, 2)}M`;
    return `$${formatNumber(number, 2)}`;
  }

  function signedClass(value) {
    const number = toNumber(value);
    if (number === null || number === 0) return '';
    return number > 0 ? ' research-pos' : ' research-neg';
  }

  function labelize(key) {
    return String(key || '')
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (ch) => ch.toUpperCase());
  }

  function formatFieldValue(key, value) {
    if (value === null || value === undefined || value === '') return '—';
    const lower = String(key).toLowerCase();
    if (lower.includes('pct') || lower.includes('percent') || lower.endsWith('_change')) {
      return formatPct(value);
    }
    if (lower.includes('volume') || lower === 'last_size' || lower.endsWith('_size')) {
      return formatVolume(value);
    }
    if (lower.includes('market_cap') || lower === 'mkt_cap') {
      return formatMoney(value);
    }
    if (typeof value === 'number' || (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value)))) {
      return formatNumber(value, lower.includes('score') ? 1 : 2);
    }
    return String(value);
  }

  function metaMarkup(target) {
    if (!target.title && !target.meta) return '';
    const provider = target.meta && target.meta.provider;
    const observedAt = target.meta && target.meta.observed_at;
    const notice = target.meta && target.meta.notice;
    return `<header class="research-result-meta">
      ${target.title ? `<strong>${esc(target.title)}</strong>` : ''}
      ${provider ? `<span data-research-provider>${esc(provider)}</span>` : ''}
      ${observedAt ? `<time data-research-observed>${esc(observedAt)}</time>` : ''}
      ${notice ? `<span data-research-notice>${esc(notice)}</span>` : ''}
    </header>`;
  }

  function metricCell(label, value, className = '') {
    return `<div class="research-metric${className}">
      <span class="research-metric-label">${esc(label)}</span>
      <span class="research-metric-value">${esc(value)}</span>
    </div>`;
  }

  function fieldListMarkup(entries) {
    if (!entries.length) return '';
    return `<dl class="research-fields">${entries.map(([key, value]) =>
      `<dt>${esc(labelize(key))}</dt><dd>${esc(formatFieldValue(key, value))}</dd>`
    ).join('')}</dl>`;
  }

  function nestedObjectMarkup(title, data) {
    if (!hasEntries(data)) return '';
    const entries = Object.entries(data).filter(([, value]) =>
      value !== null && value !== undefined && value !== ''
      && !isPlainObject(value) && !Array.isArray(value));
    if (!entries.length) return '';
    return `<section class="research-section">
      <h4>${esc(title)}</h4>
      ${fieldListMarkup(entries)}
    </section>`;
  }

  function detailsMarkup(details) {
    if (!hasEntries(details)) return '';
    const cap = details.market_cap != null
      ? `<p class="research-company-cap">Market cap ${esc(formatMoney(details.market_cap))}</p>`
      : '';
    const description = details.description
      ? `<p class="research-company-desc">${esc(details.description)}</p>`
      : '';
    const rest = Object.entries(details).filter(([key]) =>
      !['name', 'market_cap', 'description'].includes(key));
    if (!cap && !description && !rest.length) return '';
    return `<section class="research-section" data-research-details>
      <h4>Company</h4>
      ${cap}${description}
      ${fieldListMarkup(rest)}
    </section>`;
  }

  function ratiosMarkup(ratios) {
    if (!hasEntries(ratios)) return '';
    const cells = Object.entries(ratios)
      .filter(([, value]) => value !== null && value !== undefined && value !== '')
      .map(([key, value]) => metricCell(labelize(key), formatFieldValue(key, value)))
      .join('');
    if (!cells) return '';
    return `<section class="research-section" data-research-ratios>
      <h4>Ratios</h4>
      <div class="research-metrics">${cells}</div>
    </section>`;
  }

  function newsItemMarkup(news) {
    if (!hasEntries(news) && !Array.isArray(news)) return '';
    const items = Array.isArray(news) ? news : [news];
    const articles = items.filter((item) => hasEntries(item) || (item && item.title)).map((item) => {
      const title = item.title || 'Untitled';
      const meta = [item.published, item.author, item.sentiment]
        .filter((part) => part !== null && part !== undefined && String(part).trim())
        .map((part) => esc(part))
        .join(' · ');
      const body = item.teaser ? `<p>${esc(item.teaser)}</p>` : '';
      const link = item.url
        ? `<a href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${esc(title)}</a>`
        : esc(title);
      return `<article class="research-news-item"><strong>${link}</strong>
        ${meta ? `<p class="research-news-meta">${meta}</p>` : ''}${body}</article>`;
    }).join('');
    if (!articles) return '';
    return `<section class="research-section" data-research-news>
      <h4>News</h4>${articles}
    </section>`;
  }

  function objectMarkup(data) {
    if (data === null || data === undefined) return '';
    if (typeof data !== 'object') return `<p>${esc(data)}</p>`;
    if (Array.isArray(data)) {
      return data.map((row) => `<article>${detailMarkup(row)}</article>`).join('');
    }
    return detailMarkup(data);
  }

  function detailMarkup(data) {
    if (!isPlainObject(data)) {
      return typeof data === 'object' ? '' : `<p>${esc(data)}</p>`;
    }

    const ticker = String(data.ticker ?? data.symbol ?? '').trim();
    const details = isPlainObject(data.details) ? data.details : null;
    const ratios = isPlainObject(data.ratios) ? data.ratios : null;
    const news = data.news;
    const day = isPlainObject(data.day) ? data.day : null;
    const previousDay = isPlainObject(data.previous_day) ? data.previous_day : null;
    const changePct = data.change_pct ?? data.change_percent ?? data.percent_change;
    const change = data.change;
    const companyName = (details && details.name) || data.name || '';

    const consumed = new Set([
      'ticker', 'symbol', 'details', 'ratios', 'news', 'day', 'previous_day',
      'name', 'market', 'change_pct', 'change_percent', 'percent_change', 'change',
      'open', 'close', 'high', 'low', 'volume', 'last', 'price', 'bid', 'ask',
      'bid_size', 'ask_size', 'last_size', 'score', 'vwap',
    ]);

    const headlineMetrics = [];
    const close = data.close ?? data.last ?? data.price ?? (day && day.close);
    if (close != null) headlineMetrics.push(metricCell('Price', formatNumber(close)));
    if (change != null) {
      headlineMetrics.push(metricCell('Change', formatNumber(change), signedClass(change)));
    }
    if (changePct != null) {
      headlineMetrics.push(metricCell('Change %', formatPct(changePct), signedClass(changePct)));
    }
    if (data.score != null) headlineMetrics.push(metricCell('Score', formatNumber(data.score, 1)));
    const volume = data.volume ?? (day && day.volume);
    if (volume != null) headlineMetrics.push(metricCell('Volume', formatVolume(volume)));
    if (data.open != null || (day && day.open != null)) {
      headlineMetrics.push(metricCell('Open', formatNumber(data.open ?? day.open)));
    }
    if (data.bid != null) headlineMetrics.push(metricCell('Bid', formatNumber(data.bid)));
    if (data.ask != null) headlineMetrics.push(metricCell('Ask', formatNumber(data.ask)));

    const leftovers = Object.entries(data).filter(([key, value]) => {
      if (consumed.has(key)) return false;
      if (value === null || value === undefined || value === '') return false;
      if (isPlainObject(value) || Array.isArray(value)) return false;
      return true;
    });

    const nestedSections = Object.entries(data)
      .filter(([key, value]) => !consumed.has(key) && hasEntries(value))
      .map(([key, value]) => nestedObjectMarkup(labelize(key), value))
      .join('');

    const header = ticker || companyName ? `<header class="research-detail-head">
      <div>
        ${ticker ? `<h3 class="research-ticker">${esc(ticker)}</h3>` : ''}
        ${companyName ? `<p class="research-company-name">${esc(companyName)}</p>` : ''}
        ${data.market ? `<p class="research-market">${esc(labelize(data.market))}</p>` : ''}
      </div>
      ${changePct != null ? `<span class="research-change-badge${signedClass(changePct)}">${esc(formatPct(changePct))}</span>` : ''}
    </header>` : '';

    return `<div class="research-detail-card">
      ${header}
      ${headlineMetrics.length ? `<div class="research-metrics">${headlineMetrics.join('')}</div>` : ''}
      ${nestedObjectMarkup('Session', day)}
      ${nestedObjectMarkup('Previous day', previousDay)}
      ${detailsMarkup(details)}
      ${ratiosMarkup(ratios)}
      ${newsItemMarkup(news)}
      ${nestedSections}
      ${fieldListMarkup(leftovers)}
    </div>`;
  }

  function proposalsEnabled() {
    if (typeof document.querySelector !== 'function') return false;
    const root = document.querySelector('[data-research-propose-enabled]');
    return Boolean(root && root.dataset.researchProposeEnabled === 'true');
  }

  function equityResult(data, tool) {
    if (!data || typeof data !== 'object') return false;
    if (!String(data.ticker ?? data.symbol ?? '').trim()) return false;
    const allowed = new Set([
      'stock', 'stocks', 'equity', 'equities', 'stk', 'common stock',
    ]);
    const classifications = [
      data.market, data.market_type, data.asset_class, data.asset_type,
      data.security_type, data.sec_type,
    ].filter((value) => value !== null && value !== undefined && String(value).trim());
    if (classifications.some((value) =>
      !allowed.has(String(value).trim().toLowerCase()))) return false;
    if (tool === 'movers') {
      return allowed.has(String(data.market ?? '').trim().toLowerCase());
    }
    return tool === 'ideas' || tool === 'lookup';
  }

  function proposalConfidence(data) {
    const score = Number(data && data.score);
    if (Number.isFinite(score) && score > 0) {
      return Math.round(Math.min(0.95, Math.max(0.35, score / 100)) * 100) / 100;
    }
    const changePct = Math.abs(Number(
      data && (data.change_pct ?? data.change_percent ?? data.percent_change)));
    if (Number.isFinite(changePct) && changePct > 0) {
      return Math.round(Math.min(0.85, Math.max(0.4, 0.45 + (changePct / 25))) * 100) / 100;
    }
    return 0.55;
  }

  function proposalAction(data) {
    const signal = String(data && data.signal || '').trim().toUpperCase();
    if (signal === 'BUY' || signal === 'SELL') return signal;
    return 'BUY';
  }

  function proposalThesis(data, tool) {
    const ticker = String(data.ticker ?? data.symbol ?? '').trim().toUpperCase();
    const company = data.details && data.details.name
      ? String(data.details.name).trim()
      : String(data.name || '').trim();
    const label = tool === 'ideas' ? 'Ideas scan'
      : (tool === 'movers' ? 'Movers' : 'Lookup');
    if (company) return `${label}: ${ticker} — ${company}`.slice(0, 4000);
    return `${label}: ${ticker}`.slice(0, 4000);
  }

  function proposalReasoning(data, tool) {
    const lines = [];
    const ticker = String(data.ticker ?? data.symbol ?? '').trim().toUpperCase();
    lines.push(`Prefill from Research ${tool || 'result'} for ${ticker}.`);
    if (data.signal != null && String(data.signal).trim()) {
      lines.push(`Scanner signal: ${String(data.signal).trim()}.`);
    }
    if (data.score != null && data.score !== '') {
      lines.push(`Score: ${formatNumber(data.score, 1)}.`);
    }
    const changePct = data.change_pct ?? data.change_percent ?? data.percent_change;
    if (changePct != null && changePct !== '') {
      lines.push(`Change: ${formatPct(changePct)}.`);
    }
    const price = data.close ?? data.last ?? data.price
      ?? (data.day && data.day.close);
    if (price != null && price !== '') {
      lines.push(`Price: ${formatNumber(price)}.`);
    }
    const volume = data.volume ?? (data.day && data.day.volume);
    if (volume != null && volume !== '') {
      lines.push(`Volume: ${formatVolume(volume)}.`);
    }
    if (data.gap_pct != null && data.gap_pct !== '') {
      lines.push(`Gap: ${formatPct(data.gap_pct)}.`);
    }
    if (data.rel_vol != null && data.rel_vol !== '') {
      lines.push(`Rel volume: ${formatNumber(data.rel_vol, 2)}×.`);
    }
    if (data.details && data.details.description) {
      lines.push(String(data.details.description).trim().slice(0, 400));
    }
    if (data.news && data.news.title) {
      const sentiment = data.news.sentiment ? ` (${data.news.sentiment})` : '';
      lines.push(`News: ${String(data.news.title).trim()}${sentiment}`);
    }
    lines.push('Quantity/amount left blank for automatic position sizing.');
    return lines.join('\n').slice(0, 8000);
  }

  function proposalInstrument(data, tool) {
    const row = data || {};
    return {
      ticker: String(row.ticker ?? row.symbol ?? '').trim().toUpperCase(),
      exchange: String(row.exchange ?? row.primary_exchange ?? '').trim(),
      currency: String(row.currency ?? '').trim(),
      action: proposalAction(row),
      confidence: proposalConfidence(row),
      group: 'research',
      thesis: proposalThesis(row, tool),
      reasoning: proposalReasoning(row, tool),
    };
  }

  function proposalMarkup(data, tool) {
    if (!proposalsEnabled() || !equityResult(data, tool)) return '';
    return '<button type="button" class="research-propose" '
      + 'data-research-propose>Propose</button>';
  }

  function lookupPart(name, target) {
    const status = statusFor(target);
    let content = '';
    if (status === 'No results.') {
      content = '';
    } else if (name === 'news' && Array.isArray(target.data)) {
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
      const changePct = row && (row.change_pct ?? row.change_percent ?? row.percent_change);
      const measureRaw = changePct ?? (row && (row.score ?? row.price ?? row.close ?? ''));
      const measure = changePct != null ? formatPct(changePct)
        : (measureRaw === '' || measureRaw == null ? '' : formatFieldValue('score', measureRaw));
      const selected = target.selected === row;
      return `<button type="button" class="research-row${selected ? ' selected' : ''}" data-research-row="${index}" tabindex="0" aria-pressed="${selected ? 'true' : 'false'}"><strong>${esc(name)}</strong><span class="${signedClass(changePct).trim()}">${esc(measure)}</span></button>`;
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
    root.innerHTML = metaMarkup(target) + detailMarkup(target.selected)
      + proposalMarkup(target.selected, currentTool);
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

  function proposalFromEvent(event) {
    return event.target && event.target.closest
      ? event.target.closest('[data-research-propose]')
      : null;
  }

  function openSelectedProposal() {
    const target = currentTool === 'lookup' ? state.lookup.snapshot : state[currentTool];
    if (!proposalsEnabled() || !equityResult(target.selected, currentTool)) return false;
    const hook = globalThis.ccOpenResearchProposal;
    if (typeof hook !== 'function') return false;
    hook(proposalInstrument(target.selected, currentTool));
    return true;
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
    const detail = document.getElementById('research-detail');
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
    if (detail) {
      detail.addEventListener('click', (event) => {
        if (proposalFromEvent(event)) openSelectedProposal();
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

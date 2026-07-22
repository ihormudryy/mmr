'use strict';

const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function makeElement({id = '', tool = '', disabled = false} = {}) {
  const listeners = new Map();
  const classes = new Set(tool === 'ideas' ? ['active'] : []);
  return {
    id,
    disabled,
    hidden: id !== 'research-config-banner' ? false : true,
    innerHTML: '',
    textContent: '',
    value: '',
    dataset: tool ? {researchTool: tool} : {},
    classList: {
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
    },
    setAttribute(name, value) {
      if (name === 'aria-pressed') this.ariaPressed = value;
    },
    addEventListener(name, callback) {
      if (!listeners.has(name)) listeners.set(name, []);
      listeners.get(name).push(callback);
    },
    dispatch(name, event = {}) {
      for (const callback of listeners.get(name) || []) callback(event);
    },
    querySelectorAll() { return []; },
  };
}

function makeFetch() {
  const queued = [];
  const calls = [];
  const fetch = async (url, options) => {
    calls.push({url: String(url), options});
    if (!queued.length) throw new Error(`unexpected fetch: ${url}`);
    const reply = queued.shift();
    if (reply.networkError) throw reply.networkError;
    return {
      ok: reply.status >= 200 && reply.status < 300,
      status: reply.status,
      async json() { return reply.body; },
    };
  };
  fetch.enqueue = (status, body) => queued.push({status, body});
  fetch.reject = (message) => queued.push({networkError: new Error(message)});
  fetch.calls = calls;
  fetch.queued = queued;
  return fetch;
}

function makeHarness() {
  const ids = [
    'research-config-banner',
    'research-controls',
    'research-status',
    'research-results',
    'research-detail',
  ];
  const elements = new Map(ids.map((id) => [id, makeElement({id})]));
  const tools = ['ideas', 'movers', 'lookup', 'scan', 'depth', 'options', 'forex']
    .map((tool) => makeElement({tool, disabled: !['ideas', 'movers', 'lookup'].includes(tool)}));
  const readyListeners = [];
  const fetch = makeFetch();

  const document = {
    getElementById(id) { return elements.get(id) || null; },
    querySelectorAll(selector) {
      if (selector === '[data-research-tool]:not([disabled])') {
        return tools.filter((tool) => !tool.disabled);
      }
      if (selector === '[data-research-tool]') return tools;
      return [];
    },
    addEventListener(name, callback) {
      if (name === 'DOMContentLoaded') readyListeners.push(callback);
    },
  };

  class FakeFormData {
    constructor(form) { this.form = form; }
    entries() { return (this.form.fields || [])[Symbol.iterator](); }
  }

  const context = vm.createContext({
    console,
    document,
    fetch,
    FormData: FakeFormData,
    URLSearchParams,
  });

  function loadProductionScript() {
    const source = fs.readFileSync(
      path.join(__dirname, 'command_center_research.js'), 'utf8');
    vm.runInContext(source, context, {filename: 'command_center_research.js'});
    return context.CCResearch;
  }

  async function start() {
    for (const callback of readyListeners) callback();
    await new Promise((resolve) => setImmediate(resolve));
  }

  function form(tool, fields, checkboxes = []) {
    return {
      dataset: {researchForm: tool},
      fields,
      querySelectorAll(selector) {
        return selector === 'input[type="checkbox"]' ? checkboxes : [];
      },
    };
  }

  function rowTarget(index) {
    const row = {dataset: {researchRow: String(index)}};
    return {closest(selector) {
      return selector === '[data-research-row]' ? row : null;
    }};
  }

  return {
    context,
    document,
    elements,
    fetch,
    tools,
    loadProductionScript,
    start,
    form,
    rowTarget,
    get api() { return context.CCResearch; },
  };
}

module.exports = {makeHarness};

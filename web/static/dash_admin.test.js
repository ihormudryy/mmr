/* Focused tab-routing regression tests for dash_admin.js. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, 'dash_admin.js'), 'utf8');

function element({id = '', dashTab = ''} = {}) {
  const classes = new Set();
  const listeners = new Map();
  const attrs = {};
  return {
    id,
    dataset: dashTab ? {dashTab} : {},
    style: {},
    hidden: false,
    classList: {
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
    },
    setAttribute(name, value) { attrs[name] = String(value); },
    getAttribute(name) { return attrs[name]; },
    addEventListener(name, callback) { listeners.set(name, callback); },
    dispatch(name) { listeners.get(name)?.(); },
    querySelector() { return null; },
    closest() { return null; },
  };
}

function makeContext(hash) {
  const tabs = ['trading', 'research'].map((name) => element({dashTab: name}));
  const panes = ['trading', 'research'].map((name) => element({id: `dash-${name}`}));
  const byId = new Map(panes.map((pane) => [pane.id, pane]));
  const banners = [];
  const location = {
    hash,
    href: 'http://localhost/cc?flash=hello%20world#trading',
    reload() {},
  };
  const document = {
    getElementById(id) { return byId.get(id) || null; },
    querySelector() { return null; },
    querySelectorAll(selector) {
      if (selector === '.dash-tab') return tabs;
      if (selector === '.dash-pane') return panes;
      if (selector === '[data-flash-banner]') return banners.slice();
      return [];
    },
    addEventListener(name, callback) {
      document._listeners = document._listeners || {};
      document._listeners[name] = callback;
    },
  };
  const window = {
    innerHeight: 800,
    innerWidth: 1200,
    addEventListener() {},
    confirm() { return true; },
    alert() {},
    location,
  };
  const history = {
    replaceState(_state, _title, next) {
      const value = String(next || '');
      if (value.startsWith('#')) {
        location.hash = value;
        return;
      }
      location.href = value.startsWith('http') ? value : ('http://localhost' + value);
      const hashIdx = value.indexOf('#');
      location.hash = hashIdx >= 0 ? value.slice(hashIdx) : '';
    },
  };
  const context = vm.createContext({
    console, document, history, location, setInterval() { return 1; }, window,
    URL,
  });
  window.window = window;
  vm.runInContext(source, context, {filename: 'dash_admin.js'});
  return {tabs, panes, location, document, banners, history};
}

const {tabs, panes, location} = makeContext('#research');
assert.equal(location.hash, '#research');
assert.equal(tabs[1].classList.contains('active'), true);
assert.equal(tabs[0].classList.contains('active'), false);
assert.equal(panes[1].classList.contains('active'), true);
assert.equal(panes[0].classList.contains('active'), false);

{
  const ctx = makeContext('#trading');
  const banner = element({id: 'flash'});
  banner.hidden = false;
  banner.remove = function () { banner.removed = true; };
  ctx.banners.push(banner);
  const btn = element();
  btn.closest = (sel) => (sel === '[data-flash-dismiss]' ? btn : null);
  ctx.document._listeners.click({target: btn, preventDefault() {}});
  assert.equal(banner.hidden, true);
  assert.equal(banner.removed, true);
  assert.equal(ctx.location.href.includes('flash='), false);
}

console.log('dash_admin.test.js: 2 tests passed');

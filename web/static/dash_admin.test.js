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
  return {
    id,
    dataset: dashTab ? {dashTab} : {},
    style: {},
    classList: {
      contains(name) { return classes.has(name); },
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
    },
    addEventListener(name, callback) { listeners.set(name, callback); },
    dispatch(name) { listeners.get(name)?.(); },
    querySelector() { return null; },
  };
}

function makeContext(hash) {
  const tabs = ['trading', 'research'].map((name) => element({dashTab: name}));
  const panes = ['trading', 'research'].map((name) => element({id: `dash-${name}`}));
  const byId = new Map(panes.map((pane) => [pane.id, pane]));
  const location = {hash, reload() {}};
  const document = {
    getElementById(id) { return byId.get(id) || null; },
    querySelector() { return null; },
    querySelectorAll(selector) {
      if (selector === '.dash-tab') return tabs;
      if (selector === '.dash-pane') return panes;
      return [];
    },
  };
  const window = {
    innerHeight: 800,
    innerWidth: 1200,
    addEventListener() {},
    confirm() { return true; },
    alert() {},
  };
  const history = {
    replaceState(_state, _title, nextHash) { location.hash = nextHash; },
  };
  const context = vm.createContext({
    console, document, history, location, setInterval() { return 1; }, window,
  });
  window.window = window;
  vm.runInContext(source, context, {filename: 'dash_admin.js'});
  return {tabs, panes, location};
}

const {tabs, panes, location} = makeContext('#research');
assert.equal(location.hash, '#research');
assert.equal(tabs[1].classList.contains('active'), true);
assert.equal(tabs[0].classList.contains('active'), false);
assert.equal(panes[1].classList.contains('active'), true);
assert.equal(panes[0].classList.contains('active'), false);

console.log('dash_admin.test.js: 1 test passed');

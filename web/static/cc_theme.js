/* Theme boot + toggle for the /cc dashboard.
 *
 * Loaded synchronously in <head> (the strict CSP forbids inline scripts) so
 * the effective theme is stamped on <html> before first paint — no flash.
 *
 * Resolution order: an explicit stored choice wins; with no stored choice we
 * follow the OS via prefers-color-scheme. The effective theme is written as
 * an explicit data-theme="light|dark", so the stylesheet needs only a single
 * [data-theme="dark"] rule and no prefers-color-scheme @media duplication.
 * (The OS is read once at load; it is not live-listened for mid-session.)
 * Directions from the design doc: light = 1c, dark = 1b.
 */
'use strict';

(function () {
  var KEY = 'cc-theme';
  var root = document.documentElement;

  function storedChoice() {
    try { return localStorage.getItem(KEY); } catch (err) { return null; }
  }
  function osPrefersDark() {
    try { return window.matchMedia('(prefers-color-scheme: dark)').matches; }
    catch (err) { return false; }
  }

  var choice = storedChoice();
  var dark = choice ? choice === 'dark' : osPrefersDark();
  root.dataset.theme = dark ? 'dark' : 'light';

  function syncButton(btn) {
    var isDark = root.dataset.theme === 'dark';
    btn.textContent = isDark ? 'Light' : 'Dark';
    btn.setAttribute('aria-pressed', String(isDark));
  }

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    syncButton(btn);
    btn.addEventListener('click', function () {
      var toDark = root.dataset.theme !== 'dark';
      root.dataset.theme = toDark ? 'dark' : 'light';
      try { localStorage.setItem(KEY, toDark ? 'dark' : 'light'); }
      catch (err) { /* preference just won't persist */ }
      syncButton(btn);
    });
  });
})();

/* Theme boot + toggle for the /cc dashboard.
 *
 * Loaded synchronously in <head> (the strict CSP forbids inline scripts) so
 * a stored dark preference applies before first paint — no light flash.
 * Light is the default (design direction 1c); "dark" is design direction 1b.
 */
'use strict';

(function () {
  var KEY = 'cc-theme';
  var root = document.documentElement;

  var stored = null;
  try { stored = localStorage.getItem(KEY); } catch (err) { /* private mode */ }
  if (stored === 'dark') root.dataset.theme = 'dark';

  function syncButton(btn) {
    var dark = root.dataset.theme === 'dark';
    btn.textContent = dark ? 'Light' : 'Dark';
    btn.setAttribute('aria-pressed', String(dark));
  }

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (!btn) return;
    syncButton(btn);
    btn.addEventListener('click', function () {
      var toDark = root.dataset.theme !== 'dark';
      if (toDark) root.dataset.theme = 'dark';
      else delete root.dataset.theme;
      try { localStorage.setItem(KEY, toDark ? 'dark' : 'light'); }
      catch (err) { /* preference just won't persist */ }
      syncButton(btn);
    });
  });
})();

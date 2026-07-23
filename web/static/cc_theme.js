/* Theme + text-scale boot for the /cc dashboard.
 *
 * Loaded synchronously in <head> (the strict CSP forbids inline scripts) so
 * both the effective theme and the stored text scale are stamped on <html>
 * before first paint — no flash of the wrong theme or size.
 *
 * Theme resolution: an explicit stored choice wins; with no stored choice
 * the dashboard defaults to dark (design doc: the 1c cockpit page on the
 * 1b warm-dark ground). The effective theme is written as an explicit
 * data-theme="light|dark", so the stylesheet needs only a single
 * [data-theme="dark"] rule and no prefers-color-scheme @media duplication.
 *
 * Text scale: the slider in the tab bar scales the whole page via CSS
 * `zoom` on <html> — the same geometry browser page-zoom applies, so text,
 * layout and rules all grow together. Persisted in localStorage. Any JS
 * that positions fixed elements from getBoundingClientRect() must divide
 * by the effective zoom (see positionSetupTip in dash_admin.js).
 */
'use strict';

(function () {
  var KEY = 'cc-theme';
  var ZOOM_KEY = 'cc-zoom';
  var ZOOM_MIN = 80;
  var ZOOM_MAX = 150;
  var root = document.documentElement;

  function storedChoice() {
    try { return localStorage.getItem(KEY); } catch (err) { return null; }
  }

  var choice = storedChoice();
  var dark = choice ? choice === 'dark' : true;
  root.dataset.theme = dark ? 'dark' : 'light';

  function clampZoom(v) {
    v = parseInt(v, 10);
    if (!isFinite(v)) return 100;
    return Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, v));
  }
  function storedZoom() {
    try { return clampZoom(localStorage.getItem(ZOOM_KEY)); }
    catch (err) { return 100; }
  }
  function applyZoom(pct) {
    root.style.zoom = pct + '%';
  }

  applyZoom(storedZoom());

  function syncButton(btn) {
    var isDark = root.dataset.theme === 'dark';
    btn.textContent = isDark ? 'Light' : 'Dark';
    btn.setAttribute('aria-pressed', String(isDark));
  }

  document.addEventListener('DOMContentLoaded', function () {
    var btn = document.getElementById('theme-toggle');
    if (btn) {
      syncButton(btn);
      btn.addEventListener('click', function () {
        var toDark = root.dataset.theme !== 'dark';
        root.dataset.theme = toDark ? 'dark' : 'light';
        try { localStorage.setItem(KEY, toDark ? 'dark' : 'light'); }
        catch (err) { /* preference just won't persist */ }
        syncButton(btn);
      });
    }

    var slider = document.getElementById('zoom-slider');
    var readout = document.getElementById('zoom-value');
    if (!slider) return;

    function setZoom(pct) {
      pct = clampZoom(pct);
      applyZoom(pct);
      slider.value = String(pct);
      if (readout) readout.textContent = pct + '%';
      try { localStorage.setItem(ZOOM_KEY, String(pct)); }
      catch (err) { /* preference just won't persist */ }
    }

    setZoom(storedZoom());
    slider.addEventListener('input', function () { setZoom(slider.value); });
    if (readout) {
      readout.addEventListener('click', function () { setZoom(100); });
    }
  });
})();

/* Deploy + Watchlists tab wiring for /cc (CSP-safe — no inline script).
 * Tab clicks, hash routing, deploy-row unfold, watchlist delete confirm,
 * tooltip positioning, and the 30s admin-pane auto-refresh. */
'use strict';

(function () {
  var setupOpenEditors = 0;

  window.toggleSetupParams = function (id, btn) {
    var el = document.getElementById(id);
    if (!el) return;
    var open = el.style.display !== 'none';
    el.style.display = open ? 'none' : 'table-row';
    setupOpenEditors = Math.max(0, setupOpenEditors + (open ? -1 : 1));
    if (btn) {
      var label = btn.dataset.label || 'params';
      btn.textContent = (open ? '▸' : '▾') + ' ' + label;
    }
  };

  function positionSetupTip(anchor, tip) {
    tip.style.display = 'block';
    var r = anchor.getBoundingClientRect();
    var margin = 8, gap = 7;
    var tw = tip.offsetWidth, th = tip.offsetHeight;
    var left = r.left + r.width / 2 - tw / 2;
    left = Math.max(margin, Math.min(left, window.innerWidth - tw - margin));
    var top = r.bottom + gap;
    if (top + th > window.innerHeight - margin) top = r.top - th - gap;
    tip.style.left = left + 'px';
    tip.style.top = Math.max(top, margin) + 'px';
  }

  function hideAdminTips() {
    document.querySelectorAll('.dash-admin-pane .tip').forEach(function (t) {
      t.style.display = 'none';
    });
  }

  document.querySelectorAll('.dash-admin-pane .info, .dash-admin-pane .hover-tip')
    .forEach(function (el) {
      var tip = el.querySelector('.tip');
      if (!tip) return;
      var show = function () { positionSetupTip(el, tip); };
      var hide = function () { tip.style.display = 'none'; };
      el.addEventListener('mouseenter', show);
      el.addEventListener('mouseleave', hide);
      el.addEventListener('focus', show);
      el.addEventListener('blur', hide);
    });
  window.addEventListener('scroll', hideAdminTips, true);

  var ADMIN_TABS = { deploy: true, watchlists: true };

  function normalizeDashTab(raw) {
    if (!raw || raw === 'trading') return 'trading';
    if (raw === 'scaling') return 'scaling';
    if (raw === 'manage' || raw === 'setup' || raw === 'setup-strategies') return 'deploy';
    if (raw === 'setup-watchlists') return 'watchlists';
    if (ADMIN_TABS[raw]) return raw;
    return 'trading';
  }

  function activateDashTab(name) {
    name = normalizeDashTab(name);
    if (!document.getElementById('dash-' + name)) name = 'trading';
    document.querySelectorAll('.dash-tab').forEach(function (b) {
      b.classList.toggle('active', b.dataset.dashTab === name);
    });
    document.querySelectorAll('.dash-pane').forEach(function (p) {
      p.classList.toggle('active', p.id === 'dash-' + name);
    });
    hideAdminTips();
    history.replaceState(null, '', '#' + name);
  }

  document.querySelectorAll('.dash-tab').forEach(function (b) {
    b.addEventListener('click', function () {
      activateDashTab(b.dataset.dashTab);
    });
  });

  document.querySelectorAll('.dash-admin-pane .setup-unfold').forEach(function (btn) {
    btn.addEventListener('click', function () {
      toggleSetupParams(btn.dataset.target, btn);
    });
  });

  document.querySelectorAll('.dash-admin-pane .setup-delete-form').forEach(function (form) {
    form.addEventListener('submit', function (ev) {
      var msg = form.dataset.confirm || 'Delete this watchlist?';
      if (!window.confirm(msg)) ev.preventDefault();
    });
  });

  document.querySelectorAll('.param-form[action="/strategies/deploy"]').forEach(function (form) {
    form.addEventListener('submit', function (ev) {
      var symEl = form.querySelector('[name="symbols"]');
      var wlEl = form.querySelector('[name="watchlist"]');
      var sym = symEl && symEl.value ? symEl.value.trim() : '';
      var wl = wlEl && wlEl.value ? wlEl.value.trim() : '';
      if (!sym && !wl) {
        ev.preventDefault();
        window.alert('Enter at least one symbol (e.g. AAPL, MSFT) or choose a watchlist.');
        if (symEl) symEl.focus();
      }
    });
  });

  function parseHash() {
    var raw = (location.hash || '#trading').replace('#', '');
    activateDashTab(raw);
  }
  parseHash();
  window.addEventListener('hashchange', parseHash);

  setInterval(function () {
    var active = document.querySelector('.dash-pane.dash-admin-pane.active');
    if (!active || setupOpenEditors !== 0) return;
    location.reload();
  }, 30000);
})();

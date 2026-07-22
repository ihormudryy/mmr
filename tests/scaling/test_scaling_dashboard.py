"""P5 Task 8 — scaling observability in dashboard snapshot + page markup."""
from __future__ import annotations

from pathlib import Path

from web.command_center.state import DashboardState

ROOT = Path(__file__).resolve().parents[2]


def test_scaling_view_unknown_when_no_authorities():
    state = DashboardState()
    view = state.snapshot_view()
    assert view["scaling"]["status"] == "unknown"
    assert view["scaling"]["lifecycle"] == "unknown"
    assert view["scaling"]["authorities"] == []


def test_scaling_view_active_from_allocation_authority_events():
    state = DashboardState()
    now = 1_700_000_000.0
    state._place(
        "allocation_authority",
        "orb_breakout",
        {
            "entity_id": "orb_breakout",
            "entity_revision": 1,
            "stage": "SCALE_1",
            "max_gross_allocation": 0.08,
            "event": "ACTIVATED",
            "expires_at": "2026-08-01T00:00:00+00:00",
            "signature": "SHOULD_NOT_LEAK",
            "public_key": "SHOULD_NOT_LEAK",
        },
        now,
    )
    scaling = state.snapshot_view()["scaling"]
    assert scaling["status"] == "active"
    assert scaling["lifecycle"] == "active"
    assert scaling["stage"] == "SCALE_1"
    assert scaling["max_gross_allocation"] == 0.08
    assert "signature" not in scaling["authorities"][0]
    assert "public_key" not in scaling["authorities"][0]


def test_scaling_view_suspended_on_zero_override():
    state = DashboardState()
    now = 1_700_000_000.0
    state._place(
        "allocation_authority",
        "orb_breakout",
        {
            "entity_id": "orb_breakout",
            "entity_revision": 2,
            "stage": "SCALE_1",
            "max_gross_allocation": 0.0,
            "event": "OVERRIDE",
            "expires_at": "2026-08-01T00:00:00+00:00",
        },
        now,
    )
    scaling = state.snapshot_view()["scaling"]
    assert scaling["lifecycle"] == "suspended"
    assert scaling["status"] == "suspended"


def test_command_center_includes_scaling_tab_markup():
    html = (ROOT / "web" / "templates" / "command_center.html").read_text()
    assert 'data-dash-tab="scaling"' in html
    assert 'id="dash-scaling"' in html
    assert 'id="scaling-authorities-body"' in html
    js = (ROOT / "web" / "static" / "command_center.js").read_text()
    assert "function renderScaling()" in js
    assert "renderScaling()" in js

"""P5 Task 8 — scaling observability in dashboard snapshot."""
from __future__ import annotations

from web.command_center.state import DashboardState


def test_scaling_view_unknown_when_no_authorities():
    state = DashboardState()
    view = state.snapshot_view()
    assert view["scaling"]["status"] == "unknown"
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
        },
        now,
    )
    scaling = state.snapshot_view()["scaling"]
    assert scaling["status"] == "active"
    assert scaling["stage"] == "SCALE_1"
    assert scaling["max_gross_allocation"] == 0.08

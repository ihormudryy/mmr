"""Command-band operating mode (auto / semi / manual)."""

from web.command_center.routes_read import compute_operating_mode


def test_armed_paper_automation_is_auto():
    assert compute_operating_mode({"lifecycle": "armed"}) == "auto"
    assert compute_operating_mode({"lifecycle": "armed_unpersisted"}) == "auto"
    assert compute_operating_mode({"lifecycle": "degraded"}) == "auto"
    assert compute_operating_mode({"lifecycle": "restart_required"}) == "auto"
    assert compute_operating_mode({"lifecycle": "preparing"}) == "auto"


def test_one_full_auto_strategy_is_auto_even_among_propose():
    rows = [
        {"name": "manualish", "auto_execute": "propose"},
        {"name": "bot", "auto_execute": True},
    ]
    assert compute_operating_mode({"lifecycle": "disabled"}, rows) == "auto"
    assert compute_operating_mode(None, [{"auto_execute": "true"}]) == "auto"


def test_propose_strategies_are_semi_when_not_armed():
    rows = [{"name": "orb", "auto_execute": "propose"}]
    assert compute_operating_mode({"lifecycle": "disabled"}, rows) == "semi"
    assert compute_operating_mode(None, rows) == "semi"


def test_no_automation_and_no_propose_is_manual():
    assert compute_operating_mode(None, []) == "manual"
    assert compute_operating_mode(
        {"lifecycle": "disabled"}, [{"auto_execute": False}]
    ) == "manual"


def test_armed_wins_over_propose():
    rows = [{"name": "other", "auto_execute": "propose"}]
    assert compute_operating_mode({"lifecycle": "armed"}, rows) == "auto"

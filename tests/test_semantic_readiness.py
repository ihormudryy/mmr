import datetime as dt

from trader.trading.semantic_readiness import SemanticReadiness


NOW = dt.datetime(2026, 7, 20, 14, 30, tzinfo=dt.timezone.utc)


def _ready(**overrides):
    checks = dict(
        ib_connected=lambda: True,
        account_pinned=lambda: True,
        broker_current=lambda: True,
        journal_writable=lambda: True,
        reconciliation_safe=lambda: True,
        control_readable=lambda: True,
        breaker_clear=lambda: True,
        session_open=lambda now: True,
        command_stack_active=lambda: True,
        quotes_ready=lambda: True,
    )
    checks.update(overrides)
    return SemanticReadiness(**checks)


def test_all_checks_are_required_and_reported():
    report = _ready().evaluate(NOW)
    assert report.ready is True
    assert all(report.checks.values())
    assert set(report.checks) == {
        "ib_connected", "account_pinned", "broker_generation_current",
        "journal_writable", "reconciliation_safe", "control_readable",
        "breaker_clear", "xnys_session_open", "command_stack_active", "quotes_ready",
    }


def test_each_failed_or_raising_check_blocks_automation():
    names = {
        "ib_connected": "ib_connected",
        "account_pinned": "account_pinned",
        "broker_current": "broker_generation_current",
        "journal_writable": "journal_writable",
        "reconciliation_safe": "reconciliation_safe",
        "control_readable": "control_readable",
        "breaker_clear": "breaker_clear",
        "command_stack_active": "command_stack_active",
        "quotes_ready": "quotes_ready",
    }
    for argument, report_name in names.items():
        report = _ready(**{argument: lambda: False}).evaluate(NOW)
        assert report.ready is False and report.checks[report_name] is False
    report = _ready(session_open=lambda now: False).evaluate(NOW)
    assert report.ready is False and report.checks["xnys_session_open"] is False
    report = _ready(journal_writable=lambda: (_ for _ in ()).throw(OSError("disk"))).evaluate(NOW)
    assert report.ready is False
    assert "disk" not in report.to_payload()["failed"]  # safe names, no exception leakage

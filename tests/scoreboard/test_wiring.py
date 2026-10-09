"""SP1 Plan 5 Task 10: the scoreboard inside the real command stack.

Only the broker-facing authorities are fakes (as in tests/test_ai_paper_rpc.py); the Telegram transport is a fake
sender with a made-up token. No network.
"""
import datetime as dt
import logging

import pytest

from tests.automation.ai_paper_fixtures import NOW
from tests.automation.experiment_fixtures import armed_record
from tests.scoreboard.telegram_fakes import FakePost, token_file
from tests.test_ai_paper_rpc import FakeBroker, FakeMargin, FakeOrders, FakeOrphanEvidence, FakeQuotes
from tests.test_command_stack import _trader
from trader.automation.ai_paper_config import load_ai_paper_config
from trader.automation.kill_monitor import KillSessionEnd
from trader.automation.session_controller import SessionControllerState
from trader.scoreboard.ports import FxEvidence
from trader.scoreboard.summary_text import summary_event_id
from trader.scoreboard.telegram_config import TelegramConfigError
from trader.trading.command_policy import CommandAuthorityPolicy

ACCOUNT = "DU111111"
D = NOW.date()                 # Friday 2026-07-17


class FixedFx:
    def evidence(self):
        return FxEvidence("USD", 1.0, "base_is_usd", NOW)


def _build(tmp_path, monkeypatch, telegram=None):
    import trader.trading.command_stack as command_stack
    import trader.trading.trading_runtime as trading_runtime

    broker = FakeBroker()
    monkeypatch.setattr(command_stack, "TraderBrokerRiskSnapshotAuthority", lambda **kw: broker)
    monkeypatch.setattr(command_stack, "TraderQuoteAuthority", FakeQuotes)
    monkeypatch.setattr(command_stack, "TraderBrokerAuthority", FakeMargin)
    monkeypatch.setattr(command_stack, "BrokerStateOrphanEvidence", FakeOrphanEvidence)
    monkeypatch.setattr(trading_runtime, "TradingRuntimeOrderDispatch", lambda *a, **k: FakeOrders())
    yaml_path = tmp_path / "trader.yaml"
    yaml_path.write_text("{}\n")
    monkeypatch.setenv("TRADER_CONFIG", str(yaml_path))
    trader = _trader(tmp_path)
    section = {"enabled": True}
    if telegram is not None:
        section["telegram"] = telegram
    trader.ai_paper_config = load_ai_paper_config(section, trading_mode="paper")
    stack = command_stack.build_command_stack(trader, CommandAuthorityPolicy(enabled=True, max_drift_bps=50.0),
                                              now=lambda: NOW)
    stack.scoreboard.ledger.fx = FixedFx()
    if stack.scoreboard.sender is not None:
        stack.scoreboard.sender._post = FakePost()
    return trader, stack


def _telegram(tmp_path):
    return {"enabled": True, "chat_id": 5, "token_secret_file": str(token_file(tmp_path))}


@pytest.fixture
def prod_stack(tmp_path, monkeypatch):
    return _build(tmp_path, monkeypatch)


@pytest.fixture
def telegram_on(tmp_path, monkeypatch):
    return _build(tmp_path, monkeypatch, telegram=_telegram(tmp_path))


def _arm(stack):
    record = armed_record(account=ACCOUNT, started_at=NOW - dt.timedelta(hours=1))
    stack.experiments.store.insert_armed(record, principal="cli", reason="test")
    return record.experiment_id


def _flat_state(state="FLAT"):
    return SessionControllerState(
        account_id=ACCOUNT, session_date=D, calendar_name="XNYS", calendar_version="x", state=state,
        open_utc=None, close_utc=None, entry_cutoff_utc=None, cancel_entries_utc=None, flatten_start_utc=None,
        flat_deadline_utc=None, entry_cutoff_reached=True, flatten_command_id=None, flat_generation=None,
        incident=None)


def _rows(trader, table):
    return trader.journal_db.execute(f"SELECT * FROM {table}", fetch="all")


def test_build_stack_exposes_the_scoreboard_service_and_listener(prod_stack):
    trader, stack = prod_stack
    assert trader.scoreboard_service is stack.scoreboard.service and stack.session_controller._on_terminal is not None
    assert trader.session_ledger is stack.scoreboard.ledger


def test_flat_session_through_the_controller_listener_writes_a_row_and_one_summary(telegram_on):
    trader, stack = telegram_on
    exp = _arm(stack)
    stack.session_controller._notify_terminal(_flat_state())
    stack.session_controller._notify_terminal(_flat_state())
    assert len(_rows(trader, "equity_daily")) == 1
    assert [r.event_id for r in stack.scoreboard.outbox.due()] == [summary_event_id(exp, D)]


def test_failed_safe_session_also_sends_a_summary(telegram_on):
    trader, stack = telegram_on
    exp = _arm(stack)
    stack.session_controller._notify_terminal(_flat_state("INCIDENT"))
    row = stack.scoreboard.outbox.row(summary_event_id(exp, D))
    assert row.text.startswith("PAPER — FAILED_SAFE")


def test_restart_after_row_written_still_enqueues_the_summary(telegram_on, tmp_path, monkeypatch):
    trader, stack = telegram_on
    exp = _arm(stack)
    stack.scoreboard.ledger.on_row_written = None              # the process died before the hook ran
    stack.session_controller._notify_terminal(_flat_state())
    assert stack.scoreboard.outbox.due() == []
    stack.scoreboard.recover(NOW)                              # what trader_service runs at startup
    stack.scoreboard.tick()
    assert len(_rows(trader, "equity_daily")) == 1
    assert len(_rows(trader, "telegram_outbox")) == 1
    assert stack.scoreboard.outbox.row(summary_event_id(exp, D)).status == "SENT"


def test_telegram_off_writes_rows_but_no_outbox_rows_and_no_sender(prod_stack):
    trader, stack = prod_stack
    _arm(stack)
    stack.session_controller._notify_terminal(_flat_state())
    stack.scoreboard.tick()
    assert stack.scoreboard.sender is None and stack.scoreboard.outbox is None
    assert len(_rows(trader, "equity_daily")) == 1 and _rows(trader, "telegram_outbox") == []
    assert trader.scoreboard_service.report()["outbox"]["enabled"] is False


def test_telegram_enabled_without_a_chat_id_stops_stack_build(tmp_path, monkeypatch):
    with pytest.raises(TelegramConfigError):
        _build(tmp_path, monkeypatch, telegram={"enabled": True, "token_secret_file": str(token_file(tmp_path))})


def test_benchmark_failure_does_not_stop_the_loop_and_is_logged(prod_stack, caplog):
    trader, stack = prod_stack
    _arm(stack)
    with caplog.at_level(logging.WARNING):
        stack.scoreboard.tick()
    assert "SPY benchmark not refreshed" in caplog.text
    assert _rows(trader, "equity_session_peak") != []          # the observation step still ran


def test_loop_idles_without_an_experiment(telegram_on):
    trader, stack = telegram_on
    stack.scoreboard.tick()
    stack.session_controller._notify_terminal(_flat_state())
    assert _rows(trader, "equity_daily") == [] and stack.scoreboard.sender._post.calls == []


def test_kill_notices_are_attached_to_the_plan4_monitor(telegram_on):
    trader, stack = telegram_on
    monitor = stack.experiments.monitor
    assert monitor._alerts is stack.scoreboard.outbox and monitor._session_end is stack.scoreboard.ledger
    exp = _arm(stack)
    stack.experiments.store.transition(exp, expected=frozenset({"ARMED"}), to="KILLED", principal="kill_monitor",
                                       command_id=None, reason="test", changes={"killed_at": NOW})
    assert monitor._send_alert(f"kill_started:{exp}:1", "kill_started", "PAPER experiment KILLED") == "ENQUEUED"
    stack.scoreboard.ledger.record_session_end(KillSessionEnd(ACCOUNT, D, "KILLED", NOW))
    assert _rows(trader, "equity_daily")[0][3] == "KILLED"
    assert {r.event_id for r in stack.scoreboard.outbox.due()} == {f"kill_started:{exp}:1", summary_event_id(exp, D)}


def test_kill_alert_without_telegram_is_logged_no_outbox(prod_stack):
    _trader_, stack = prod_stack
    assert stack.experiments.monitor._alerts is None and stack.experiments.monitor._session_end is not None


def test_ingest_reads_enter_sizes_from_the_command_ledger_and_sizes_with_sp1(prod_stack):    # SP2 Plan 2
    from trader.automation.ai_baseline_sizing import AiPaperBaselineSizer
    from trader.scoreboard.ports import DecisionStoreFacts
    from trader.trading.command_coordinator import CommandLedger
    trader, stack = prod_stack
    ingest = trader.ai_ingest
    assert ingest is stack.scoreboard.ingest
    assert isinstance(ingest._decisions, DecisionStoreFacts) and isinstance(ingest._decisions._ledger, CommandLedger)
    assert isinstance(ingest._sizer, AiPaperBaselineSizer) and ingest._sizer is stack.ai_paper.baseline_sizer
    assert ingest._sizer._scope is stack.ai_paper.decisions._scope is not None       # issue #85
    from trader.scoreboard.close_fills import JournalCloseFills
    from trader.scoreboard.ports import DecisionStoreCloseLinks
    close_fills = stack.scoreboard.close_fills
    assert isinstance(close_fills, JournalCloseFills) and isinstance(close_fills._links, DecisionStoreCloseLinks)
    assert close_fills._executions == stack.scoreboard.ledger.trip_executions
    assert stack.scoreboard.simulator._close_fills is close_fills


def test_shadow_ingest_shares_the_scoreboard_store_the_judgments_and_the_versions(prod_stack):   # SP2c Plan 3
    from trader.scoreboard.shadow_ingest import ShadowIngest
    trader, stack = prod_stack
    ingest = trader.shadow_ingest
    assert isinstance(ingest, ShadowIngest) and ingest._store is stack.scoreboard.store
    assert ingest._judgments is stack.ai_paper.judgments and ingest._versions is trader.ai_deployment_versions
    assert ingest._config is trader.ai_paper_config.backtest_judge
    assert stack.scoreboard.service._shadow_owed() == {}                   # the same judgments: none recorded yet


def test_no_shadow_ingest_without_an_ai_paper_stack():
    from types import SimpleNamespace
    from trader.trading.command_stack import _build_shadow_ingest, _shadow_owed
    assert _build_shadow_ingest(SimpleNamespace(ai_paper_config=object()), SimpleNamespace(), None) is None
    assert _shadow_owed(SimpleNamespace(ai_paper_config=object()), None, lambda: None) is None


def test_scoreboard_tables_live_in_the_journal_db_not_the_research_db(prod_stack):
    trader, _stack = prod_stack
    tables = {r[0] for r in trader.journal_db.execute(
        "SELECT table_name FROM information_schema.tables", fetch="all")}
    assert {"equity_daily", "round_trips", "benchmark_prices", "ai_costs", "simulated_decisions", "simulated_outcomes",
            "telegram_outbox", "shadow_results",
            "scoreboard_seals", "scoreboard_incidents"} <= tables
    assert {60, 61, 62, 63, 64, 95, 96} <= {r[0] for r in trader.journal_db.execute(
        "SELECT version FROM schema_migrations", fetch="all")}


def test_a_sender_failure_never_blocks_the_tick_or_the_session_controller(telegram_on, caplog):
    trader, stack = telegram_on
    _arm(stack)

    def boom():
        raise RuntimeError("telegram down")
    stack.scoreboard.sender.drain = boom
    stack.scoreboard.tick()
    stack.session_controller._notify_terminal(_flat_state())
    assert "telegram drain failed" in caplog.text and len(_rows(trader, "equity_daily")) == 1


def test_the_tick_runs_the_simulator_and_survives_its_failure(prod_stack, caplog):
    _trader_, stack = prod_stack
    calls = []

    def boom():
        calls.append("simulate")
        raise RuntimeError("bars down")
    stack.scoreboard.simulator.run_due = boom
    stack.scoreboard.tick()                       # must not raise
    assert calls == ["simulate"] and "scoreboard simulation failed" in caplog.text

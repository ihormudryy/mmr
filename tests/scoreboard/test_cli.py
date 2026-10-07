"""SP1 Plan 5 Task 7: `mmr scoreboard` over a fake SDK."""
import json

import pytest

from trader import mmr_cli
from trader.mmr_cli import _LOCAL_ONLY_COMMANDS, build_parser

REPORT = {
    "label": "PAPER", "disclaimer": "Paper trading. Nothing here is proof of live edge.",
    "experiment": {"id": "exp-" + "a" * 20, "state": "ARMED", "started_at": "2026-10-06T13:35:00+00:00",
                   "base_currency": "USD"},
    "account": {"sessions": 2, "start_nlv_usd": 100000.0, "end_nlv_usd": None, "end_date": None, "pnl_usd": None,
                "return_pct": None, "eod_drawdown_pct": 1.5,
                "eod_drawdown_label": "drawdown from end-of-day equity; intraday lows may be missed",
                "sharpe_daily": None, "sharpe_annualised": None, "sharpe_warning": "SMALL_SAMPLE",
                "unknown_nlv_sessions": 1},
    "benchmarks": {"spy": {"return_pct": 0.0, "base_date": "2026-10-05", "last_date": None, "version": 1,
                           "provider": "history_duckdb", "label": "SPY price only; dividends excluded"},
                   "vs_spy_pp": None, "simulated": {"label": "simulated", "status": "UNAVAILABLE", "rows": None,
                                                    "pnl_usd": None},
                   "ai_cost_usd": None, "ai_calls": None, "ai_costs_status": "UNAVAILABLE",
                   "pnl_minus_ai_cost_usd": None},
    "trips": {"closed": 0, "open": 0, "unresolved_fee_trips": 0, "net_pnl_usd": 0.0, "net_pnl_complete": True,
              "fees_usd": 0.0, "fees_complete": True, "win_rate": None, "profit_factor": None, "turnover": 0.0},
    "splits": {"strategy_version": {}, "decider": {}, "style": {}},
    "sessions": [], "warnings": [], "incidents": [],
    "outbox": {"enabled": False, "pending": None, "last_sent_at": None},
}


class FakeSdk:
    def __init__(self):
        self.verify_result = {"ok": True, "checked": {"seals": 3, "round_trips": 1, "sessions": 1},
                              "mismatches": [], "incidents": []}
        self.calls = []

    def scoreboard(self, experiment_id=None):
        self.calls.append(("scoreboard", experiment_id))
        return REPORT

    def verify_scoreboard(self, experiment_id=None):
        self.calls.append(("verify", experiment_id))
        return self.verify_result


@pytest.fixture
def sdk():
    return FakeSdk()


@pytest.fixture
def cli(sdk, capsys, monkeypatch):
    def run(line, json_mode=False):
        monkeypatch.setattr(mmr_cli, "_json_mode", json_mode)
        mmr_cli._handle_scoreboard(sdk, build_parser().parse_args(line.split()))
        return capsys.readouterr().out
    return run


def test_json_output_wraps_data_and_title(cli, sdk):
    out = json.loads(cli("scoreboard --experiment exp-aaaaaaaaaaaaaaaaaaaa", json_mode=True))
    assert out == {"data": REPORT, "title": "Scoreboard (paper)"}
    assert sdk.calls == [("scoreboard", "exp-aaaaaaaaaaaaaaaaaaaa")]


def test_table_output_prints_paper_label_first_and_dash_for_unknown(cli):
    out = cli("scoreboard")
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].startswith("PAPER") and "proof of live edge" in out
    assert "return: -" in out and "AI cost: unavailable" in out and "SPY price only; dividends excluded" in out
    assert "intraday lows may be missed" in out and "fewer than 60 sessions" in out
    assert "nan" not in out.lower() and "None" not in out


def test_verify_exits_1_on_mismatch_and_0_when_clean(cli, sdk):
    assert "ok" in cli("scoreboard verify").lower()
    sdk.verify_result = {"ok": False, "checked": {}, "incidents": [],
                         "mismatches": [{"check": "ROW_EDITED", "table": "equity_daily", "key": "k"}]}
    with pytest.raises(SystemExit) as exc:
        cli("scoreboard verify")
    assert exc.value.code == 1


def test_verify_prints_the_mismatches(cli, sdk, capsys):
    sdk.verify_result = {"ok": False, "checked": {}, "incidents": [],
                         "mismatches": [{"check": "ROW_EDITED", "table": "equity_daily", "key": "k"}]}
    with pytest.raises(SystemExit):
        cli("scoreboard verify")
    out = capsys.readouterr().out
    assert "ROW_EDITED" in out and "equity_daily" in out


def test_scoreboard_does_not_probe_ib_upstream():
    import inspect
    source = inspect.getsource(mmr_cli.dispatch)
    ib_block = source[source.index("_ib_commands = {"):source.index("}", source.index("_ib_commands = {"))]
    assert "'scoreboard'" not in ib_block and "scoreboard" in _LOCAL_ONLY_COMMANDS


def test_sdk_reads_over_typed_query():
    from trader.sdk import MMR

    class Query:
        def __init__(self):
            self.calls = []

        def call(self, method, body, model, **kwargs):
            self.calls.append((method, body))
            return {"ok": True}

    query = Query()
    mmr = MMR.__new__(MMR)
    mmr._ensure_typed_clients = lambda: None
    mmr._typed_query_client = query
    mmr.scoreboard()
    mmr.scoreboard("exp-" + "b" * 20)
    mmr.verify_scoreboard()
    mmr.experiment_trips("exp-" + "b" * 20)
    assert query.calls == [("get_scoreboard", {}),
                                      ("get_scoreboard", {"experiment_id": "exp-" + "b" * 20}),
                                      ("verify_scoreboard", {}),
                                      ("get_experiment_trips", {"experiment_id": "exp-" + "b" * 20})]

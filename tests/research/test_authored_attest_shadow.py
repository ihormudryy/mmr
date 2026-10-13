# tests/research/test_authored_attest_shadow.py
"""Plan 4 ruling 23 (attest) and spec 7.6 (shadow replay through the sandbox) for authored keys."""
import datetime as dt
from types import SimpleNamespace

import pytest

from tests.authored.fake_sandbox import fault
from tests.research.renewal_fixtures import V1, forward_view
from tests.research.service_fakes import FakeTrader
from trader.automation.backtest_judge_config import BacktestJudgeConfig
from trader.authored.keys import authored_key
from trader.authored.sandbox_client import SandboxUnavailable
from trader.research.evaluation import EvaluationError
from trader.research.judgment_attest import JudgmentAttest, _Refused
from trader.research.renewal_service import RenewalRequests
from trader.research.shadow_replay import ShadowReplay, replay_failure_code
from trader.research.strategy_key import authored_strategy_path
from trader.sandbox.protocol import RESEARCH_SOCKET, SANDBOX_ERROR

DIGEST = "sha256:" + "c" * 64
KEY = authored_key(DIGEST, "OpeningRangeBreakout")
PATH = authored_strategy_path(DIGEST)


def attest(tmp_path):
    instance = object.__new__(JudgmentAttest)
    instance._repo_root = tmp_path
    return instance


def test_an_authored_source_is_checked_by_its_digest_without_a_read(tmp_path):
    attest(tmp_path)._require_judged_source(PATH, DIGEST)       # no file exists anywhere


def test_an_authored_source_with_another_digest_is_refused(tmp_path):
    with pytest.raises(_Refused) as refused:
        attest(tmp_path)._require_judged_source(PATH, "sha256:" + "d" * 64)
    assert refused.value.code == "STRATEGY_SOURCE_CHANGED"


def test_a_catalogue_source_is_still_read_from_the_repo(tmp_path):
    with pytest.raises(_Refused) as refused:
        attest(tmp_path)._require_judged_source("strategies/gone.py", DIGEST)
    assert refused.value.code == "STRATEGY_SOURCE_CHANGED"


def shadow(tmp_path):
    instance = object.__new__(ShadowReplay)
    instance._paths = SimpleNamespace(repo_root=tmp_path, history_db="h", universe_db="u", universe_library="U",
                                      execution_costs="c.yaml", sandbox_socket=RESEARCH_SOCKET)
    return instance


def case(file_hash=DIGEST):
    return SimpleNamespace(strategy_key=KEY, strategy_file_hash=file_hash, conids=[1001, 1002], bar_size="1 min")


def test_the_shadow_replay_of_an_authored_case_runs_behind_the_research_socket(tmp_path):
    family = SimpleNamespace(cost_model={"order_notional": 1900.0, "account_equity": 100000.0,
                                         "max_gross_allocation": 0.05})
    env = shadow(tmp_path)._environment(case(), family)
    assert (env.authored_digest, env.sandbox_socket, env.strategy_file) == (DIGEST, RESEARCH_SOCKET, PATH)


def test_the_shadow_source_check_uses_the_digest(tmp_path):
    assert shadow(tmp_path)._source_problem(case()) is None
    assert shadow(tmp_path)._source_problem(case("sha256:" + "d" * 64)).startswith("STRATEGY_SOURCE_CHANGED")


def test_an_unreachable_sandbox_names_itself_in_the_shadow_row():
    assert replay_failure_code(EvaluationError("x")) == "REPLAY_ERROR"
    try:
        raise EvaluationError("backtest failed") from SandboxUnavailable("gone")
    except EvaluationError as wrapped:
        assert replay_failure_code(wrapped) == "SANDBOX_UNAVAILABLE"
    assert replay_failure_code(fault(SANDBOX_ERROR)) == "REPLAY_ERROR"


def test_a_shadow_replay_failure_is_an_incomplete_row_never_a_zero_row(tmp_path):
    """spec 7.6: a sandbox failure while replaying is named in the row (or waits); it never becomes numbers."""
    def failing_run(env, job):
        raise EvaluationError("backtest failed") from SandboxUnavailable("gone")

    instance = shadow(tmp_path)
    instance._run_job = failing_run
    instance._config = SimpleNamespace(shadow_incomplete_after_hours=16)
    instance._now = lambda: dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    instance._family = lambda case: SimpleNamespace(cost_model={})
    instance._source_problem = lambda case: None
    instance._family_problem = lambda case, family: None
    instance._costs_problem = lambda family: None
    instance._bar_problem = lambda *args, **kwargs: None
    instance._environment = lambda case, family: SimpleNamespace()
    authored_case = SimpleNamespace(
        strategy_key=KEY, evidence={"warmup_sessions": 1, "replay_index": 0, "points": [{"params": {"A": 1}}]},
        conids=[1001], bar_size="1 min", cohort=[{"A": 1}])
    member = {"judgment_id": "jdg-1", "case_digest": "sha256:" + "e" * 64, "verdict": "DEPLOY",
              "first_session": dt.date(2024, 4, 2)}
    row = instance._row(member, authored_case, dt.date(2024, 4, 3))
    assert row["status"] == "INCOMPLETE" and row["reason"].startswith("SANDBOX_UNAVAILABLE: EvaluationError")
    assert row["pnl_usd"] is None and row["trades"] is None and row["end_equity_usd"] is None


def test_an_authored_renewal_is_refused_not_allowed_and_raises_nothing(tmp_path):
    """C1 carry: RenewalRequests._file_hash still calls split_strategy_key; the allowlist check must come first."""
    judge = BacktestJudgeConfig(strategy_allowlist=("strategies/time_of_day.py:TimeOfDay",))
    trader = FakeTrader()
    trader.forward[V1] = authored_view()
    renewals = RenewalRequests(store=SimpleNamespace(get=lambda request_id: None), trader=trader, signer=None,
                               artifacts_root=tmp_path, repo_root=tmp_path, judge=judge, warmup_sessions=5,
                               incomplete_after_hours=16,
                               now=lambda: dt.datetime(2024, 4, 4, 21, 0, tzinfo=dt.timezone.utc))
    reply = renewals.submit(V1)
    assert (reply["status"], reply["code"], reply["retryable"]) == ("REFUSED", "STRATEGY_NOT_ALLOWED", False)


def authored_view() -> dict:
    view = forward_view(file_hash=DIGEST)
    view["binding"]["strategy_key"] = KEY
    view["binding"]["strategy_path"] = PATH
    return view

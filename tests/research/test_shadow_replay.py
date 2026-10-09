import dataclasses
import datetime as dt
import hashlib
import json
import threading
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from tests.research.case_fixtures import pre_holdout_result
from tests.research.evaluation_fixtures import (CONIDS, TIME_OF_DAY_STRATEGY, write_costs_config, write_trend_bars,
                                                write_universe)
from tests.research.service_fakes import FakeTrader, judgment_view
from trader.data.duckdb_store import DuckDBConnection, DuckDBDataStore
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.typed_rpc import TypedRpcRemoteError, canonical_json
from trader.research import evaluation_jobs, shadow_replay
from trader.research.case_builder import build_initial_case
from trader.research.artifact import ExperimentFamily
from trader.research.evaluation import EvaluationPaths, _costs_config_digest
from trader.research.evaluation_case import case_path, write_evaluation_case
from trader.research.evaluation_jobs import WindowOutcome
from trader.research.evaluation_request import EvaluationRequestBody, evaluation_request_id
from trader.research.experiment_registry import ExperimentRegistry
from trader.research.schema import apply_research_migrations
from trader.research.service_config import ResearchServiceConfig
from trader.research.service_store import ResearchStore
from trader.research.shadow_replay import ShadowReplay, before_close
from trader.research.shadow_window import shadow_window
from trader.research.signing import AttestationSigner
from trader.research.trader_port import TraderUnavailable
from trader.scoreboard.schema import apply_scoreboard_migrations
from trader.scoreboard.shadow_ingest import RecordShadowResultRequest, ShadowIngest
from trader.scoreboard.store import ScoreboardStore

UTC = dt.timezone.utc
NEW_YORK = shadow_replay.NEW_YORK
JUDGE = SimpleNamespace(deploy_expiry_sessions=20, family_cooldown_sessions=10, shadow_warmup_sessions=5)
JUDGMENT = "jdg-00000001"


@pytest.fixture
def world(tmp_path, tmp_duckdb_path):
    write_universe(tmp_duckdb_path)
    write_trend_bars(tmp_duckdb_path, drift=0.0006)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "time_of_day.py").write_text(TIME_OF_DAY_STRATEGY)
    costs = write_costs_config(tmp_path / "execution_costs.yaml")
    paths = EvaluationPaths(tmp_duckdb_path, tmp_duckdb_path, "Universes", str(costs), tmp_path,
                            tmp_path / "reports", tmp_path / "artifacts" / "evaluations")
    signer = AttestationSigner.generate()
    file_hash = "sha256:" + hashlib.sha256((tmp_path / "strategies" / "time_of_day.py").read_bytes()).hexdigest()

    def make(db_name, verdict="DEPLOY", decided_at="2024-03-15T21:00:00+00:00", bar_size="15 mins",
             bound_bar_size=None, run_job=None, family_cost_digest="current"):
        """``family_cost_digest=None`` leaves the case's family out of the registry."""
        db = DuckDBConnection.get_instance(str(tmp_path / db_name))
        apply_research_migrations(SchemaMigrator(db))
        store, trader = ResearchStore(db), FakeTrader()
        params = {"ENTRY_MINUTE": 600, "EXIT_MINUTE": 660}
        body = EvaluationRequestBody.model_validate({"strategy_key": "strategies/time_of_day.py:TimeOfDay",
                                                     "cohort": [params], "conids": CONIDS, "bar_size": bar_size,
                                                     "research_day": "2024-03-15"})
        spec = SimpleNamespace(request_id=evaluation_request_id(body), body=body, strategy_key=body.strategy_key,
                               cohort=(params,), file_hash=file_hash)
        result = pre_holdout_result((params,))
        if family_cost_digest is not None:
            digest = _costs_config_digest(costs) if family_cost_digest == "current" else family_cost_digest
            result = dataclasses.replace(result, family_id=_judged_family(db, digest))
        case = build_initial_case(spec, "2024-03-15", result,
                                  created_at=dt.datetime(2024, 3, 15, 21, tzinfo=UTC), warmup_sessions=5)
        digest = write_evaluation_case(tmp_path / "artifacts" / "cases", case, signer)
        store.record_case(digest, case.request_id, case.stage, dt.datetime(2024, 3, 15, 21, tzinfo=UTC))
        trader.judgments[JUDGMENT] = judgment_view(JUDGMENT, digest, verdict, decided_at=decided_at,
                                                   binding={"bar_size": bound_bar_size or bar_size})
        clock = {"now": dt.datetime(2024, 3, 23, 12, tzinfo=UTC)}
        jobs = []

        def spy(env, job):
            jobs.append((env, job))
            return (run_job or evaluation_jobs.run_window_job)(env, job)
        replay = ShadowReplay(store=store, trader=trader, signer=signer, artifacts_root=tmp_path / "artifacts",
                              paths=paths, registry=ExperimentRegistry(db), config=ResearchServiceConfig(),
                              judge=JUDGE, now=lambda: clock["now"], run_job=spy)
        return SimpleNamespace(replay=replay, trader=trader, clock=clock, jobs=jobs, store=store, digest=digest)
    return SimpleNamespace(make=make, db_path=tmp_duckdb_path, cases_dir=tmp_path / "artifacts" / "cases",
                           costs=costs)


def _judged_family(db, cost_digest):
    """The registry family a judged case names; its cost model carries the digest the judgment saw."""
    config = ResearchServiceConfig()
    return ExperimentRegistry(db).create_family(ExperimentFamily(
        strategy_path="strategies/time_of_day.py", class_name="TimeOfDay", repository_commit="unknown",
        source_tree_digest="s", dependency_lock_digest="d", container_digest="unpinned",
        dataset_manifest_digest="m", search_space={},
        cost_model={"model": "realistic", "config_digest": cost_digest, "order_notional": config.order_notional,
                    "account_equity": config.account_equity, "max_gross_allocation": config.max_gross_allocation},
        validation_protocol={}), created_at=dt.datetime(2024, 3, 15, 21, tzinfo=UTC))


def rows(trader):
    return {session: row for (_, session), row in sorted(trader.shadow_rows.items())}


def ny_midnight(day):
    return dt.datetime.combine(day, dt.time.min, tzinfo=NEW_YORK)


@pytest.mark.timeout(120)
def test_every_due_session_is_replayed_on_the_bound_bar_size_with_warm_up(world):
    w = world.make("a.duckdb")
    w.replay.tick()
    got = rows(w.trader)
    assert list(got) == ["2024-03-18", "2024-03-19", "2024-03-20", "2024-03-21", "2024-03-22"]
    assert {env.bar_size for env, _ in w.jobs} == {"15 mins"} and {r["bar_size"] for r in got.values()} == {"15 mins"}
    first = got["2024-03-18"]
    assert first["status"] == "COMPLETE" and first["end_equity_usd"] - first["pnl_usd"] == pytest.approx(100_000.0)
    job = w.jobs[0][1]
    assert job.trading_start == ny_midnight(dt.date(2024, 3, 18)) and job.start < job.trading_start
    w.replay.tick()
    assert len(w.jobs) == 5                                                   # a repeat tick sends nothing


@pytest.mark.timeout(120)
def test_the_nightly_rows_equal_one_continuous_run(world):
    nightly = world.make("nightly.duckdb")
    nightly.clock["now"] = dt.datetime(2024, 3, 20, 23, tzinfo=UTC)
    nightly.replay.tick()
    nightly.clock["now"] = dt.datetime(2024, 3, 23, 12, tzinfo=UTC)
    nightly.replay.tick()
    continuous = world.make("continuous.duckdb")
    continuous.replay.tick()
    assert rows(nightly.trader) == rows(continuous.trader)


@pytest.mark.timeout(120)
def test_the_rows_add_up_to_one_continuous_run(world):
    w = world.make("one-run.duckdb")
    w.replay.tick()
    got = rows(w.trader)
    env, first_job = w.jobs[0]
    whole = evaluation_jobs.run_window_job(env, dataclasses.replace(first_job, end=w.jobs[-1][1].end))
    curve = whole.equity_series()
    last_by_day = {}
    for day, value in zip(shadow_replay._ny_dates(curve.index), curve.values):
        last_by_day[day.isoformat()] = float(value)
    assert sorted(last_by_day) == sorted(got)
    for session, row in got.items():
        assert row["end_equity_usd"] == pytest.approx(last_by_day[session], rel=1e-12), session
    assert sum(row["pnl_usd"] for row in got.values()) == pytest.approx(float(curve.iloc[-1]) - env.account_equity,
                                                                       rel=1e-9)


def _one_minute_bars(duckdb_path, conid, session):
    index = pd.date_range(f"{session} 13:30", f"{session} 19:59", freq="1min", tz="UTC")
    frame = pd.DataFrame({"open": 100.0, "high": 100.1, "low": 99.9, "close": 100.0, "volume": 1000.0,
                          "bar_size": "15 mins"}, index=index)
    frame.index.name = "date"
    DuckDBDataStore(duckdb_path).write(str(conid), frame)


@pytest.mark.timeout(120)
def test_wrong_size_or_missing_bars_make_an_incomplete_row_after_the_deadline(world):
    _one_minute_bars(world.db_path, CONIDS[0], "2024-03-21")
    w = world.make("b.duckdb", decided_at="2024-03-19T21:00:00+00:00")
    w.clock["now"] = dt.datetime(2024, 4, 2, 6, tzinfo=UTC)                  # 04-01 close + 16 h not reached
    w.replay.tick()
    got = rows(w.trader)
    assert got["2024-03-21"]["status"] == "INCOMPLETE" and got["2024-03-21"]["reason"].startswith("BAR_SIZE_MISMATCH")
    later = got["2024-03-28"]                                                # one run: 03-21 is still its input
    assert later["status"] == "INCOMPLETE" and later["reason"] == (
        f"BAR_SIZE_MISMATCH: conid {CONIDS[0]} has bars closer than 15 mins on 2024-03-21")
    assert "2024-04-01" not in got
    w.clock["now"] = dt.datetime(2024, 4, 2, 13, tzinfo=UTC)
    w.replay.tick()
    assert rows(w.trader)["2024-04-01"]["reason"].startswith("BARS_MISSING")


def test_no_verdict_never_joins(world):
    w = world.make("c.duckdb", verdict="NO_VERDICT")
    w.replay.tick()
    assert w.trader.shadow_rows == {} and w.store.shadow_members() == []


@pytest.mark.timeout(120)
def test_a_final_refusal_is_a_visible_failure_and_never_marked_sent(world):      # ruling 25, PR #91 4218219293
    w = world.make("d.duckdb")
    first = (JUDGMENT, "2024-03-18")
    w.trader.refuse_shadow[first] = ("SHADOW_SESSION_OUTSIDE_WINDOW", False)
    w.replay.tick()
    w.replay.tick()
    assert w.trader.shadow_calls.count(first) == 1                          # final: never sent again
    assert dt.date(2024, 3, 18) not in w.store.sent_sessions(JUDGMENT)
    (failure,) = w.store.shadow_failures()
    assert (str(failure["session_date"]), failure["code"]) == ("2024-03-18", "SHADOW_SESSION_OUTSIDE_WINDOW")
    assert w.replay.status()["failed_rows"] == 1
    assert sorted(rows(w.trader)) == ["2024-03-19", "2024-03-20", "2024-03-21", "2024-03-22"]


@pytest.mark.timeout(120)
def test_a_retryable_refusal_leaves_the_row_pending(world):
    w = world.make("e.duckdb")
    first = (JUDGMENT, "2024-03-18")
    w.trader.refuse_shadow[first] = ("JUDGMENT_UNKNOWN", True)
    w.replay.tick()
    assert w.store.sent_sessions(JUDGMENT) == set() and w.store.shadow_failures() == []
    del w.trader.refuse_shadow[first]
    w.replay.tick()
    assert len(w.store.sent_sessions(JUDGMENT)) == 5 and w.replay.status()["failed_rows"] == 0


# -- (a) the window comes from the trader's sealed judgment --------------------------------------------------


@pytest.mark.timeout(120)
def test_the_window_uses_the_sealed_recorded_at_and_cooldown_not_the_body(world):
    w = world.make("f.duckdb", verdict="REJECT")
    view = w.trader.judgments[JUDGMENT]
    view["recorded_at"] = "2024-03-18T21:00:00+00:00"          # sealed by the trader; the body says 03-15
    view["cooldown_until_session"] = "2024-03-19"
    w.replay.tick()
    (member,) = w.store.shadow_members()
    expected = shadow_window(dt.datetime(2024, 3, 18, 21, tzinfo=UTC), "REJECT", deploy_expiry_sessions=20,
                             cooldown_until_session=dt.date(2024, 3, 19))
    assert (member["first_session"], member["last_session"]) == expected == (dt.date(2024, 3, 19),
                                                                             dt.date(2024, 4, 3))
    assert list(rows(w.trader)) == ["2024-03-19", "2024-03-20", "2024-03-21", "2024-03-22"]


def test_a_reject_without_its_sealed_cooldown_is_parked_not_guessed(world):
    w = world.make("g.duckdb", verdict="REJECT")
    w.replay.tick()
    assert w.store.shadow_members() == [] and w.trader.shadow_calls == []
    assert w.replay.status()["parked"][w.digest].startswith("SHADOW_WINDOW_INVALID")


# -- (b) trader refusals and loud errors -----------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_a_session_is_sent_only_after_its_close_and_a_not_closed_refusal_stays_pending(world):
    w = world.make("h.duckdb")
    w.clock["now"] = dt.datetime(2024, 3, 20, 20, 10, tzinfo=UTC)            # 03-20 closed 10 minutes ago
    w.replay.tick()
    assert list(rows(w.trader)) == ["2024-03-18", "2024-03-19"]
    w.clock["now"] = dt.datetime(2024, 3, 20, 23, tzinfo=UTC)
    key = (JUDGMENT, "2024-03-20")
    w.trader.refuse_shadow[key] = ("SHADOW_SESSION_NOT_CLOSED", True)     # a trader clock behind ours
    w.replay.tick()
    assert w.store.shadow_failures() == [] and dt.date(2024, 3, 20) not in w.store.sent_sessions(JUDGMENT)
    del w.trader.refuse_shadow[key]
    w.replay.tick()
    assert list(rows(w.trader)) == ["2024-03-18", "2024-03-19", "2024-03-20"]


def test_a_judgment_bound_to_another_bar_size_is_parked(world):
    w = world.make("i.duckdb", bound_bar_size="5 mins")
    w.replay.tick()
    assert w.store.shadow_members() == [] and w.trader.shadow_calls == []
    assert w.replay.status()["parked"][w.digest].startswith("SHADOW_BAR_SIZE_MISMATCH")


def test_a_tampered_judgment_parks_the_case_and_the_worker_keeps_running(world, caplog):
    w = world.make("j.duckdb")
    calls = []

    def tampered(**kwargs):
        calls.append(kwargs)
        raise TypedRpcRemoteError("JUDGMENT_TAMPERED", "seal mismatch")
    w.trader.judgment = tampered
    w.replay.tick()
    w.replay.tick()
    assert len(calls) == 1 and w.store.shadow_members() == []
    assert w.replay.status()["parked"] == {w.digest: "JUDGMENT_TAMPERED: seal mismatch"}
    assert any(r.levelname == "ERROR" and "JUDGMENT_TAMPERED" in r.getMessage() for r in caplog.records)


@pytest.mark.timeout(120)
def test_a_loud_error_while_recording_parks_the_member(world):
    w = world.make("k.duckdb")

    def loud(body):
        w.trader.shadow_calls.append((body["judgment_id"], body["session_date"]))
        raise TypedRpcRemoteError("CASE_TAMPERED", "case file changed")
    w.trader.record_shadow = loud
    w.replay.tick()
    w.replay.tick()
    assert w.trader.shadow_calls == [(JUDGMENT, "2024-03-18")]
    assert w.store.sent_sessions(JUDGMENT) == set() and w.store.shadow_failures() == []
    assert w.replay.status()["parked"][w.digest].startswith("CASE_TAMPERED")


def test_a_missing_case_file_parks_the_case(world):
    w = world.make("l.duckdb")
    case_path(world.cases_dir, w.digest).unlink()
    w.replay.tick()
    assert w.store.shadow_members() == [] and w.replay.status()["parked"][w.digest].startswith("CASE_NOT_FOUND")


def test_an_infrastructure_error_ends_the_tick_and_the_worker(world):
    w = world.make("m.duckdb")

    def broken(**kwargs):
        raise TypedRpcRemoteError("INTERNAL_ERROR", "boom")
    w.trader.judgment = broken
    with pytest.raises(TypedRpcRemoteError):
        w.replay.tick()
    assert w.replay.status()["parked"] == {}
    with pytest.raises(TypedRpcRemoteError):
        w.replay.serve_forever(threading.Event())


def test_an_unreachable_trader_only_waits(world, monkeypatch):
    monkeypatch.setattr(shadow_replay, "TICK_SECONDS", 0.0)
    w = world.make("n.duckdb")
    stop, ticks = threading.Event(), []

    def unreachable(**kwargs):
        ticks.append(1)
        if len(ticks) == 3:
            stop.set()
        raise TraderUnavailable("get_backtest_judgment: TimeoutError")
    w.trader.judgment = unreachable
    w.replay.serve_forever(stop)
    assert len(ticks) == 3


# -- (c) no trading bars is never a zero-P&L COMPLETE row ------------------------------------------------------


def test_a_replay_without_trading_bars_is_incomplete_never_zero(world):
    def no_trading_bars(env, job):
        return WindowOutcome(job, (), (), 0.0, 0.0, "", {})
    w = world.make("o.duckdb", run_job=no_trading_bars)
    w.clock["now"] = dt.datetime(2024, 3, 18, 21, tzinfo=UTC)                # closed, deadline not reached
    w.replay.tick()
    assert w.trader.shadow_calls == []
    w.clock["now"] = dt.datetime(2024, 3, 19, 13, tzinfo=UTC)
    w.replay.tick()
    (row,) = rows(w.trader).values()
    assert row["status"] == "INCOMPLETE" and row["reason"].startswith("NO_TRADING_BARS")
    assert row["pnl_usd"] is None and row["trades"] is None


# -- (d) a sent row is final ----------------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_a_session_sent_incomplete_is_never_sent_again_with_numbers(world):
    w = world.make("p.duckdb", decided_at="2024-03-27T21:00:00+00:00")
    w.clock["now"] = dt.datetime(2024, 4, 2, 13, tzinfo=UTC)
    w.replay.tick()
    assert rows(w.trader)["2024-04-01"]["status"] == "INCOMPLETE"
    sent_before, calls_before = rows(w.trader), list(w.trader.shadow_calls)
    write_trend_bars(world.db_path, drift=0.0006, start="2024-04-01", end="2024-04-01")
    w.replay.tick()
    assert rows(w.trader) == sent_before and w.trader.shadow_calls == calls_before


@pytest.mark.timeout(120)
def test_a_lost_reply_resends_the_same_body_even_after_bars_arrive(world):
    w = world.make("q.duckdb", decided_at="2024-03-27T21:00:00+00:00")
    w.clock["now"] = dt.datetime(2024, 4, 2, 13, tzinfo=UTC)
    record = w.trader.record_shadow

    def stored_then_lost(body):
        record(body)
        if body["session_date"] == "2024-04-01":
            raise TraderUnavailable("record_shadow_result: TimeoutError")
        return {"status": "INSERTED", "code": None, "retryable": False}
    w.trader.record_shadow = stored_then_lost
    with pytest.raises(TraderUnavailable):
        w.replay.tick()
    assert dt.date(2024, 4, 1) not in w.store.sent_sessions(JUDGMENT)
    write_trend_bars(world.db_path, drift=0.0006, start="2024-04-01", end="2024-04-01")
    w.trader.record_shadow = record
    w.replay.tick()
    assert rows(w.trader)["2024-04-01"]["status"] == "INCOMPLETE"
    assert dt.date(2024, 4, 1) in w.store.sent_sessions(JUDGMENT) and w.store.shadow_failures() == []


# -- fix round 1 -------------------------------------------------------------------------------------------------


def _after_hours_bars(duckdb_path, session):
    """1-minute bars from the close to 19:59 ET at a far price: no row may see them, not even the bar check."""
    index = pd.date_range(f"{session} 16:00", f"{session} 19:59", freq="1min", tz="America/New_York")
    for conid in CONIDS:
        frame = pd.DataFrame({"open": 2000.0, "high": 2000.0, "low": 2000.0, "close": 2000.0, "volume": 50_000.0,
                              "bar_size": "15 mins"}, index=index.tz_convert("UTC"))
        frame.index.name = "date"
        DuckDBDataStore(duckdb_path).write(str(conid), frame)


@pytest.mark.timeout(120)
def test_a_row_ends_at_the_close_and_never_sees_after_hours_bars(world):
    before = world.make("before.duckdb")
    before.clock["now"] = dt.datetime(2024, 3, 19, 13, tzinfo=UTC)          # past the INCOMPLETE deadline
    before.replay.tick()
    _after_hours_bars(world.db_path, "2024-03-18")
    after = world.make("after.duckdb")
    after.clock["now"] = dt.datetime(2024, 3, 19, 13, tzinfo=UTC)
    after.replay.tick()
    assert rows(after.trader) == rows(before.trader) and rows(after.trader)["2024-03-18"]["status"] == "COMPLETE"
    # Live rules allow no entry after the close and flatten before it, so the job end is checked directly.
    assert after.jobs[0][1].end == before_close(dt.date(2024, 3, 18))


def _reject_first_body(w, error):
    record = w.trader.record_shadow

    def reject(body):
        if body["session_date"] == "2024-03-18":
            w.trader.shadow_calls.append((body["judgment_id"], body["session_date"]))
            raise error
        return record(body)
    w.trader.record_shadow = reject


@pytest.mark.timeout(120)
def test_a_row_body_the_trader_rejects_is_a_final_failure_and_later_sessions_go(world):
    w = world.make("r.duckdb")
    _reject_first_body(w, TypedRpcRemoteError("VALIDATION_ERROR", "invalid request body: 1 validation error"))
    w.replay.tick()
    (failure,) = w.store.shadow_failures()
    assert (str(failure["session_date"]), failure["code"]) == ("2024-03-18", "VALIDATION_ERROR")
    assert w.store.queued_row(JUDGMENT, dt.date(2024, 3, 18)) is None
    assert sorted(rows(w.trader)) == ["2024-03-19", "2024-03-20", "2024-03-21", "2024-03-22"]
    assert w.replay.status()["parked"] == {}
    w.replay.tick()
    assert w.trader.shadow_calls.count((JUDGMENT, "2024-03-18")) == 1


@pytest.mark.parametrize("code,message", [
    ("VALIDATION_ERROR", "handler returned an invalid response: bad reply"),
    ("INTERNAL_ERROR", "boom"), ("AUTHENTICATION_ERROR", "bad signature"), ("PERMISSION_DENIED", "not research"),
    ("METHOD_NOT_ALLOWED", "unknown method"), ("REPLAY_ERROR", "nonce reused"), ("UNKNOWN_ERROR", "?"),
])
def test_a_service_level_rpc_error_while_recording_ends_the_tick(world, code, message):
    w = world.make(f"s-{code}.duckdb")
    w.clock["now"] = dt.datetime(2024, 3, 18, 21, tzinfo=UTC)
    _reject_first_body(w, TypedRpcRemoteError(code, message))
    with pytest.raises(TypedRpcRemoteError):
        w.replay.tick()
    assert w.store.shadow_failures() == [] and w.replay.status()["parked"] == {}
    assert w.store.queued_row(JUDGMENT, dt.date(2024, 3, 18)) is not None


def test_a_rejected_judgment_lookup_still_ends_the_worker(world):
    w = world.make("t.duckdb")

    def rejected(**kwargs):
        raise TypedRpcRemoteError("VALIDATION_ERROR", "invalid request body: 1 validation error")
    w.trader.judgment = rejected
    with pytest.raises(TypedRpcRemoteError):
        w.replay.tick()
    assert w.replay.status()["parked"] == {}


# -- final review fixes ------------------------------------------------------------------------------------------


@pytest.mark.timeout(120)
@pytest.mark.parametrize("call", ["judgment", "record_shadow"])
def test_a_trader_without_ai_paper_is_logged_and_waited_for_not_a_death(world, monkeypatch, caplog, call):
    monkeypatch.setattr(shadow_replay, "TICK_SECONDS", 0.0)
    w = world.make(f"u-{call}.duckdb")
    w.clock["now"] = dt.datetime(2024, 3, 18, 21, tzinfo=UTC)
    stop, ticks = threading.Event(), []

    def not_served(*args, **kwargs):
        ticks.append(1)
        if len(ticks) == 3:
            stop.set()
        raise TypedRpcRemoteError("METHOD_NOT_ALLOWED", "method is not registered on the 'command' socket")
    setattr(w.trader, call, not_served)
    w.replay.serve_forever(stop)
    assert len(ticks) == 3 and w.replay.status()["parked"] == {} and w.store.shadow_failures() == []
    errors = [r for r in caplog.records if r.levelname == "ERROR" and "METHOD_NOT_ALLOWED" in r.getMessage()]
    assert len(errors) == 3


def _drop_bars(duckdb_path, conid, start_utc, end_utc):
    """Remove one conid's bars in [start, end) (a delete first: a write only replaces its own date span)."""
    store = DuckDBDataStore(duckdb_path)
    frame = store.read(str(conid))
    kept = frame[(frame.index < pd.Timestamp(start_utc)) | (frame.index >= pd.Timestamp(end_utc))]
    store.delete(str(conid))
    store.write(str(conid), kept)


@pytest.mark.timeout(120)
@pytest.mark.parametrize("hole,reason", [
    (("2024-03-20 15:00", "2024-03-20 17:00"), "gap"),                 # two hours inside the session
    (("2024-03-20 13:30", "2024-03-20 14:30"), "first"),           # the first bar comes an hour late
])
def test_a_hole_or_a_late_open_in_one_conid_waits_then_is_incomplete(world, hole, reason):
    _drop_bars(world.db_path, CONIDS[1], f"{hole[0]}+00:00", f"{hole[1]}+00:00")
    w = world.make(f"v-{reason.replace(' ', '-')}.duckdb")
    w.clock["now"] = dt.datetime(2024, 3, 21, 6, tzinfo=UTC)                 # 03-20 closed, deadline not reached
    w.replay.tick()
    assert list(rows(w.trader)) == ["2024-03-18", "2024-03-19"]
    w.clock["now"] = dt.datetime(2024, 3, 21, 13, tzinfo=UTC)
    w.replay.tick()
    row = rows(w.trader)["2024-03-20"]
    assert row["status"] == "INCOMPLETE" and row["reason"].startswith("BARS_MISSING")
    assert str(CONIDS[1]) in row["reason"] and reason in row["reason"]


def _pre_market_bar(duckdb_path, conid, stamp_utc):
    frame = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 10.0,
                          "bar_size": "15 mins"}, index=pd.DatetimeIndex([pd.Timestamp(stamp_utc)], name="date"))
    DuckDBDataStore(duckdb_path).write(str(conid), frame)


@pytest.mark.timeout(120)
def test_a_pre_market_bar_neither_opens_a_gap_nor_hides_a_late_open(world):
    _pre_market_bar(world.db_path, CONIDS[3], "2024-03-20 12:00+00:00")            # 08:00 New York
    _pre_market_bar(world.db_path, CONIDS[4], "2024-03-20 12:00+00:00")
    _drop_bars(world.db_path, CONIDS[4], "2024-03-20 13:30+00:00", "2024-03-20 14:30+00:00")
    w = world.make("pre-market.duckdb")
    w.clock["now"] = dt.datetime(2024, 3, 21, 13, tzinfo=UTC)
    w.replay.tick()
    row = rows(w.trader)["2024-03-20"]
    assert row["status"] == "INCOMPLETE" and row["reason"].startswith(f"BARS_MISSING: conid {CONIDS[4]} has its first")
    _drop_bars(world.db_path, CONIDS[4], "2024-03-20 12:00+00:00", "2024-03-20 12:15+00:00")
    write_trend_bars(world.db_path, drift=0.0006, start="2024-03-20", end="2024-03-20", conids=[CONIDS[4]])
    fresh = world.make("pre-market-2.duckdb")
    fresh.clock["now"] = dt.datetime(2024, 3, 21, 6, tzinfo=UTC)
    fresh.replay.tick()
    assert rows(fresh.trader)["2024-03-20"]["status"] == "COMPLETE"


@pytest.mark.timeout(120)
def test_one_missing_bar_or_a_first_bar_one_bar_late_is_still_complete(world):
    _drop_bars(world.db_path, CONIDS[1], "2024-03-20 13:30+00:00", "2024-03-20 13:45+00:00")
    _drop_bars(world.db_path, CONIDS[2], "2024-03-20 16:00+00:00", "2024-03-20 16:15+00:00")
    w = world.make("w.duckdb")
    w.clock["now"] = dt.datetime(2024, 3, 21, 6, tzinfo=UTC)
    w.replay.tick()
    assert rows(w.trader)["2024-03-20"]["status"] == "COMPLETE"


@pytest.mark.timeout(120)
def test_a_cost_model_changed_since_the_judgment_waits_then_is_incomplete(world):
    w = world.make("x.duckdb")
    w.clock["now"] = dt.datetime(2024, 3, 19, 13, tzinfo=UTC)                # 03-18 past its deadline
    w.replay.tick()
    assert rows(w.trader)["2024-03-18"]["status"] == "COMPLETE"
    changed = yaml.safe_load(world.costs.read_text())
    changed["impact_k"] = 0.2
    world.costs.write_text(yaml.safe_dump(changed))
    w.clock["now"] = dt.datetime(2024, 3, 20, 6, tzinfo=UTC)                 # 03-19 closed, deadline not reached
    w.replay.tick()
    assert list(rows(w.trader)) == ["2024-03-18"]
    w.clock["now"] = dt.datetime(2024, 3, 20, 13, tzinfo=UTC)
    w.replay.tick()
    row = rows(w.trader)["2024-03-19"]
    assert row["status"] == "INCOMPLETE" and row["reason"].startswith("COSTS_CHANGED")


def _ingest_over_the_wire(tmp_path, w, body, now):
    """What the trader does with a row: decode the signed JSON, validate the wire model, store it."""
    request = RecordShadowResultRequest.model_validate(json.loads(canonical_json(body)))
    db = DuckDBConnection.get_instance(str(tmp_path / f"journal-{body['status']}.duckdb"))
    apply_scoreboard_migrations(SchemaMigrator(db))
    view = w.trader.judgments[JUDGMENT]
    judgment = SimpleNamespace(case_digest=view["case_digest"], verdict=view["verdict"], binding=view["binding"],
                               recorded_at=dt.datetime.fromisoformat(view["recorded_at"]),
                               cooldown_until_session=None)
    ingest = ShadowIngest(store=ScoreboardStore(db, now=lambda: now), judgments={JUDGMENT: judgment},
                          versions=None, config=JUDGE, now=lambda: now)
    return request, ingest.record(request, SimpleNamespace(principal="research"))


def test_real_complete_and_incomplete_rows_pass_the_wire_model_and_the_ingest(world, tmp_path):
    def flat(env, job):
        stamp = dt.datetime.combine(job.end.date(), dt.time(15), tzinfo=UTC)
        return WindowOutcome(job, (), ((stamp, env.account_equity),), 0.0, 0.0, "", {})
    complete = world.make("y.duckdb", run_job=flat)
    complete.clock["now"] = dt.datetime(2024, 3, 18, 21, tzinfo=UTC)
    complete.replay.tick()
    (body,) = rows(complete.trader).values()
    assert body["status"] == "COMPLETE" and body["pnl_usd"] == 0.0 and type(body["pnl_usd"]) is float
    request, reply = _ingest_over_the_wire(tmp_path, complete, body, complete.clock["now"])
    assert request.pnl_usd == 0.0 and request.trades == 0 and reply["status"] == "INSERTED"

    def no_trading_bars(env, job):
        return WindowOutcome(job, (), (), 0.0, 0.0, "", {})
    incomplete = world.make("z.duckdb", run_job=no_trading_bars)
    incomplete.clock["now"] = dt.datetime(2024, 3, 19, 13, tzinfo=UTC)
    incomplete.replay.tick()
    (body,) = rows(incomplete.trader).values()
    assert body["status"] == "INCOMPLETE"
    request, reply = _ingest_over_the_wire(tmp_path, incomplete, body, incomplete.clock["now"])
    assert request.reason.startswith("NO_TRADING_BARS") and reply["status"] == "INSERTED"


@pytest.mark.timeout(120)
def test_a_case_whose_family_is_not_in_the_registry_waits_then_is_incomplete(world):
    w = world.make("family-unknown.duckdb", family_cost_digest=None)
    w.clock["now"] = dt.datetime(2024, 3, 18, 21, tzinfo=UTC)                # 03-18 closed, deadline not reached
    w.replay.tick()
    assert w.trader.shadow_calls == [] and w.jobs == []
    w.clock["now"] = dt.datetime(2024, 3, 19, 13, tzinfo=UTC)
    w.replay.tick()
    (row,) = rows(w.trader).values()
    assert row["status"] == "INCOMPLETE" and row["reason"].startswith("FAMILY_UNKNOWN")


@pytest.mark.timeout(120)
def test_an_after_hours_bar_never_stands_in_for_a_missing_closing_bar(world):
    _drop_bars(world.db_path, CONIDS[1], "2024-03-20 19:45+00:00", "2024-03-20 20:00+00:00")   # last bar due
    _pre_market_bar(world.db_path, CONIDS[1], "2024-03-20 20:00+00:00")                       # 16:00 New York
    w = world.make("after-hours-close.duckdb")
    assert w.replay._bar_problem(CONIDS, "15 mins", dt.date(2024, 3, 20)).startswith(
        f"BARS_MISSING: conid {CONIDS[1]} has no 15 mins bar up to the close")
    w.clock["now"] = dt.datetime(2024, 3, 21, 13, tzinfo=UTC)
    w.replay.tick()
    row = rows(w.trader)["2024-03-20"]
    assert row["status"] == "INCOMPLETE" and row["reason"].startswith(f"BARS_MISSING: conid {CONIDS[1]}")


def test_the_bar_check_ignores_a_close_stamped_bar_even_when_the_read_returns_it(world, monkeypatch):
    """An interval read (every earlier input session) returns after-hours bars; only [open, close) counts."""
    _drop_bars(world.db_path, CONIDS[1], "2024-03-20 19:45+00:00", "2024-03-20 20:00+00:00")
    _pre_market_bar(world.db_path, CONIDS[1], "2024-03-20 20:00+00:00")
    w = world.make("after-hours-read.duckdb")
    monkeypatch.setattr(shadow_replay, "before_close", shadow_replay.session_close_utc)    # read includes 20:00
    assert w.replay._bar_problem(CONIDS, "15 mins", dt.date(2024, 3, 20)).startswith(
        f"BARS_MISSING: conid {CONIDS[1]} has no 15 mins bar up to the close")


@pytest.mark.timeout(120)
def test_a_hole_in_an_earlier_session_keeps_every_later_row_incomplete(world):
    _drop_bars(world.db_path, CONIDS[2], "2024-03-18 00:00+00:00", "2024-03-19 00:00+00:00")   # first session
    w = world.make("earlier-hole.duckdb")
    w.replay.tick()                                                       # 03-23 12:00: every deadline passed
    got = rows(w.trader)
    assert got["2024-03-18"]["reason"] == f"BARS_MISSING: no 15 mins bars for conid {CONIDS[2]} on 2024-03-18"
    for session in ("2024-03-19", "2024-03-20", "2024-03-21", "2024-03-22"):
        assert got[session]["status"] == "INCOMPLETE" and got[session]["reason"] == got["2024-03-18"]["reason"]
    assert w.jobs == []                                                   # no row was built on the hole


@pytest.mark.timeout(120)
def test_a_hole_in_a_warm_up_session_keeps_the_rows_incomplete(world):
    _drop_bars(world.db_path, CONIDS[0], "2024-03-13 15:00+00:00", "2024-03-13 17:00+00:00")   # warm-up
    w = world.make("warm-up-hole.duckdb")
    w.clock["now"] = dt.datetime(2024, 3, 19, 13, tzinfo=UTC)
    w.replay.tick()
    row = rows(w.trader)["2024-03-18"]
    assert row["status"] == "INCOMPLETE" and row["reason"].startswith(f"BARS_MISSING: conid {CONIDS[0]} has a gap")
    assert row["reason"].endswith("on 2024-03-13")


@pytest.mark.timeout(120)
def test_an_after_hours_bar_in_an_earlier_session_does_not_hide_its_missing_closing_bar(world):
    _drop_bars(world.db_path, CONIDS[1], "2024-03-20 19:45+00:00", "2024-03-20 20:00+00:00")
    _pre_market_bar(world.db_path, CONIDS[1], "2024-03-20 20:00+00:00")
    w = world.make("earlier-after-hours.duckdb")
    w.clock["now"] = dt.datetime(2024, 3, 22, 13, tzinfo=UTC)
    w.replay.tick()
    got = rows(w.trader)
    assert got["2024-03-19"]["status"] == "COMPLETE"
    for session in ("2024-03-20", "2024-03-21"):
        assert got[session]["status"] == "INCOMPLETE"
        assert got[session]["reason"] == f"BARS_MISSING: conid {CONIDS[1]} has no 15 mins bar up to the close on 2024-03-20"

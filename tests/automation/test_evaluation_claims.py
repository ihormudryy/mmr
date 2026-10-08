"""SP2c Plan 1 Task 3: evaluation claims at the trader (spec 5.1 steps 3, 5, 6, 8; 6.1; 9 daily cap)."""
from __future__ import annotations

import datetime as dt
import threading

import pytest

from tests.automation.backtest_judge_fixtures import (
    KEY, NOW, ZERO, Clock, ClockMovesWhileWaitingForTheLock, count_claims, insert_reject, journal, judge_config,
    request_body,
)
from trader.automation.evaluation_claims import ClaimRefused, EvaluationClaims
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.research.evaluation_request import evaluation_request_id


@pytest.fixture
def clock():
    return Clock(NOW)


@pytest.fixture
def db(tmp_path):
    return journal(tmp_path)


@pytest.fixture
def claims(db, clock):
    return EvaluationClaims(db, config=judge_config(), now=clock)


def claim(claims, body=None):
    body = body or request_body()
    return claims.claim(evaluation_request_id(body), body, principal="research")


def refusal(fn) -> str:
    with pytest.raises(ClaimRefused) as exc:
        fn()
    return exc.value.code


def test_migrations_110_and_111_create_the_claim_and_judgment_tables(db):
    tables = {r[0] for r in db.execute("SELECT table_name FROM information_schema.tables", fetch="all")}
    assert {"evaluation_claims", "backtest_judgments"} <= tables
    assert {110, 111} <= SchemaMigrator(db).applied_versions()


def test_a_new_claim_is_queued_on_its_new_york_day(claims):
    result = claim(claims)
    assert result.status == "ACCEPTED"
    assert (result.claim.state, result.claim.ny_day, result.claim.strategy_key) == ("QUEUED", dt.date(2026, 10, 8), KEY)
    assert result.claim.body() == request_body()


def test_a_retry_with_the_same_body_returns_the_claim_and_takes_no_slot(claims, db):    # review focus 2
    first = claim(claims)
    again = claim(claims)
    assert (again.status, again.claim) == ("EXISTING", first.claim) and count_claims(db) == 1


def test_the_same_id_with_another_body_is_a_conflict(claims, db):
    body = request_body()
    claims.claim(evaluation_request_id(body), body, principal="research")
    other = request_body(bar_size="1 min")
    assert refusal(lambda: claims.claim(evaluation_request_id(body), other, principal="research")) \
        == "EVALUATION_REQUEST_CONFLICT"
    assert count_claims(db) == 1


def test_a_caller_chosen_request_id_is_refused(claims, db):
    assert refusal(lambda: claims.claim(ZERO, request_body(), principal="research")) == "EVALUATION_REQUEST_ID_MISMATCH"
    assert count_claims(db) == 0


def test_the_trader_checks_the_allowlist_and_the_cohort_size_itself(db, clock):
    assert refusal(lambda: claim(EvaluationClaims(db, config=judge_config(max_cohort_points=1), now=clock))) \
        == "COHORT_TOO_LARGE"
    assert refusal(lambda: claim(EvaluationClaims(db, config=judge_config(strategy_allowlist=()), now=clock))) \
        == "STRATEGY_NOT_ALLOWED"
    assert count_claims(db) == 0


def test_the_day_cap_counts_every_claim_of_the_day_whatever_its_state(db, clock):
    claims = EvaluationClaims(db, config=judge_config(evaluations_per_day=2), now=clock)
    first = claim(claims, request_body(research_day="2026-10-01"))
    claims.update(first.claim.request_id, "FAILED")
    claim(claims, request_body(research_day="2026-10-02"))
    assert refusal(lambda: claim(claims, request_body(research_day="2026-10-03"))) == "EVALUATION_LIMIT_REACHED"


def test_the_cap_resets_at_new_york_midnight_not_at_utc_midnight(db, clock):
    claims = EvaluationClaims(db, config=judge_config(evaluations_per_day=1), now=clock)
    clock.now = dt.datetime(2026, 10, 9, 3, 59, tzinfo=dt.timezone.utc)          # 23:59 ET on Oct 8
    assert claim(claims, request_body(research_day="2026-10-01")).claim.ny_day == dt.date(2026, 10, 8)
    clock.now = dt.datetime(2026, 10, 9, 4, 1, tzinfo=dt.timezone.utc)           # 00:01 ET on Oct 9
    assert claim(claims, request_body(research_day="2026-10-02")).claim.ny_day == dt.date(2026, 10, 9)


def test_claim_cap_uses_transaction_day_not_stale_prelock_day(db, clock):        # PR #93 round 1
    config = judge_config(evaluations_per_day=1)
    EvaluationClaims(db, config=config, now=clock).claim(
        evaluation_request_id(request_body(research_day="2026-10-01")), request_body(research_day="2026-10-01"),
        principal="research")                                                    # Oct 8's only slot
    clock.now = dt.datetime(2026, 10, 9, 3, 59, 59, tzinfo=dt.timezone.utc)     # 23:59:59 ET on Oct 8
    after_midnight = dt.datetime(2026, 10, 9, 4, 0, 1, tzinfo=dt.timezone.utc)  # 00:00:01 ET on Oct 9
    waiting = EvaluationClaims(ClockMovesWhileWaitingForTheLock(db, clock, after_midnight), config=config,
                               now=clock)
    stored = claim(waiting, request_body(research_day="2026-10-02")).claim
    assert (stored.ny_day, stored.claimed_at) == (dt.date(2026, 10, 9), after_midnight)
    assert refusal(lambda: claim(waiting, request_body(research_day="2026-10-03"))) == "EVALUATION_LIMIT_REACHED"


def test_two_concurrent_claims_for_the_last_slot_accept_exactly_one(db, clock):     # review focus 1
    claims = EvaluationClaims(db, config=judge_config(evaluations_per_day=2), now=clock)
    claim(claims, request_body(research_day="2026-10-01"))
    barrier, outcomes = threading.Barrier(2), []

    def attempt(body):
        barrier.wait()
        try:
            outcomes.append(claim(claims, body).status)
        except ClaimRefused as refused:
            outcomes.append(refused.code)

    threads = [threading.Thread(target=attempt, args=(request_body(research_day=day),))
               for day in ("2026-10-02", "2026-10-03")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert sorted(outcomes) == ["ACCEPTED", "EVALUATION_LIMIT_REACHED"] and count_claims(db) == 2


def test_a_rejected_strategy_key_cools_down_every_parameter_set(db, claims, clock):
    insert_reject(db, KEY, dt.date(2026, 10, 22))
    other_point = request_body(cohort=[{"RANGE_MINUTES": 45}])
    assert refusal(lambda: claim(claims, other_point)) == "FAMILY_COOLING_DOWN"
    clock.now = dt.datetime(2026, 10, 22, 20, 0, tzinfo=dt.timezone.utc)
    assert refusal(lambda: claim(claims, other_point)) == "FAMILY_COOLING_DOWN"
    clock.now = dt.datetime(2026, 10, 23, 14, 0, tzinfo=dt.timezone.utc)
    assert claim(claims, other_point).status == "ACCEPTED" and count_claims(db) == 1


def test_a_cooldown_of_another_strategy_key_does_not_block(db, claims):
    insert_reject(db, "strategies/opening_range_breakout.py:Other", dt.date(2026, 10, 22))
    assert claim(claims).status == "ACCEPTED"


def test_states_only_move_forward_and_a_repeat_is_a_no_op(claims):
    request_id = claim(claims).claim.request_id
    assert claims.update(request_id, "RUNNING").status == "UPDATED"
    assert claims.update(request_id, "RUNNING").status == "UNCHANGED"
    assert claims.update(request_id, "DONE").claim.state == "DONE"
    for backward in ("RUNNING", "FAILED"):
        assert refusal(lambda: claims.update(request_id, backward)) == "CLAIM_STATE_BACKWARD"
    assert refusal(lambda: claims.update(ZERO, "RUNNING")) == "CLAIM_UNKNOWN"


def test_a_restarted_trader_reads_queued_and_running_claims_back(tmp_path, claims, clock):
    queued = claim(claims, request_body(research_day="2026-10-01")).claim
    running = claim(claims, request_body(research_day="2026-10-02")).claim
    claims.update(running.request_id, "RUNNING")
    fresh = EvaluationClaims(DuckDBConnection(str(tmp_path / "journal.duckdb")), config=judge_config(), now=clock)
    assert fresh.get(queued.request_id).state == "QUEUED"
    assert fresh.get(running.request_id).state == "RUNNING"
    assert fresh.get(ZERO) is None

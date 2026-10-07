import json
import pytest
from trader.ai.journal import (
    COST_CONFIRMED, COST_CORRECTION, RECONCILED, STARTED, SUCCEEDED, AttemptJournal, JournalError,
    canonical_request_json,
)
from trader.ai.model_client import ChatMessage, ModelRequest, ModelResponse, Usage


def make_request(key="d1/jev/1", text="hello") -> ModelRequest:
    return ModelRequest(request_key=key, messages=(ChatMessage("user", text),), max_output_tokens=10)


def response() -> ModelResponse:
    return ModelResponse("answer", Usage(10, 5), "m", "openrouter", "stop", "gen-1")


@pytest.fixture
def journal(store) -> AttemptJournal:
    return AttemptJournal(store)


def begin(store, journal, clock, request=None, reservation="r1"):
    request = request or make_request()
    return store.transaction(lambda conn: journal.begin_in_tx(
        conn, request=request, role="jev", backend="openrouter", model="m", reservation_id=reservation, now=clock.now()))


def fail(store, journal, clock, attempt_key, outcome="UNKNOWN", code="HTTP_500", detail="x"):
    store.transaction(lambda conn: journal.finish_failure_in_tx(
        conn, attempt_key, outcome=outcome, error_code=code, error_detail=detail, now=clock.now()))


def test_restart_turns_started_into_unknown_and_unknown_never_becomes_success(store, journal, clock):
    record = begin(store, journal, clock)
    marked = store.transaction(lambda conn: journal.mark_started_as_unknown_in_tx(conn, clock.now()))
    assert [m.attempt_key for m in marked] == [record.attempt_key]
    assert journal.get(record.attempt_key).status == "UNKNOWN"
    assert journal.get(record.attempt_key).error_code == "PROCESS_RESTARTED"
    with pytest.raises(JournalError):
        store.transaction(lambda conn: journal.finish_success_in_tx(conn, record.attempt_key, response(), clock.now()))
    assert [a.attempt_key for a in journal.unknown_attempts()] == [record.attempt_key]


def test_begin_records_the_request_before_any_call(store, journal, clock):
    record = begin(store, journal, clock, make_request(text="what is up"))
    assert record.status == STARTED and record.attempt_key == "d1/jev/1#1" and record.attempt_no == 1
    assert record.response_text is None and record.finished_at is None
    assert record.started_at == clock.now()
    body = json.loads(record.request_json)
    assert body["messages"] == [{"role": "user", "content": "what is up"}]
    assert "authorization" not in record.request_json.lower() and "api" not in record.request_json.lower()
    assert len(record.request_sha256) == 64


def test_attempt_numbers_count_per_request_key(store, journal, clock):
    first = begin(store, journal, clock, make_request("d1/jev/1"), "r1")
    second = begin(store, journal, clock, make_request("d1/jev/1"), "r2")
    other = begin(store, journal, clock, make_request("d1/jev/2"), "r3")
    assert (first.attempt_key, second.attempt_key, other.attempt_key) == ("d1/jev/1#1", "d1/jev/1#2", "d1/jev/2#1")
    assert [a.attempt_key for a in journal.attempts_with_prefix("d1/")] == ["d1/jev/1#1", "d1/jev/1#2", "d1/jev/2#1"]
    assert [a.attempt_key for a in journal.attempts_with_prefix("d1/jev/2")] == ["d1/jev/2#1"]
    assert journal.attempts_with_prefix("zz/") == []


def test_hash_ignores_the_attempt_key():
    plain = make_request()
    tagged = ModelRequest(request_key="d1/jev/1", messages=plain.messages, max_output_tokens=10, attempt_key="d1/jev/1#4")
    assert canonical_request_json(plain) == canonical_request_json(tagged)


def test_success_stores_the_response_and_blocks_a_second_finish(store, journal, clock):
    record = begin(store, journal, clock)
    clock.advance(2)
    store.transaction(lambda conn: journal.finish_success_in_tx(conn, record.attempt_key, response(), clock.now()))
    done = journal.get(record.attempt_key)
    assert done.status == SUCCEEDED and done.response_text == "answer"
    assert (done.input_tokens, done.output_tokens) == (10, 5)
    assert done.finish_reason == "stop" and done.provider_request_id == "gen-1"
    assert done.finished_at == clock.now()
    with pytest.raises(JournalError):
        store.transaction(lambda conn: journal.finish_success_in_tx(conn, record.attempt_key, response(), clock.now()))
    with pytest.raises(JournalError):
        fail(store, journal, clock, record.attempt_key)
    with pytest.raises(JournalError) as caught:
        store.transaction(lambda conn: journal.finish_success_in_tx(conn, "nope#1", response(), clock.now()))
    assert caught.value.code == "ATTEMPT_UNKNOWN"


def test_failure_outcomes_are_validated_and_detail_is_bounded(store, journal, clock):
    record = begin(store, journal, clock)
    with pytest.raises(JournalError) as caught:
        fail(store, journal, clock, record.attempt_key, outcome="SUCCEEDED")
    assert caught.value.code == "OUTCOME_INVALID"
    assert journal.get(record.attempt_key).status == STARTED
    fail(store, journal, clock, record.attempt_key, outcome="REJECTED", code="HTTP_400", detail="y" * 5000)
    stored = journal.get(record.attempt_key)
    assert stored.status == "REJECTED" and stored.error_code == "HTTP_400" and len(stored.error_detail) == 500


def test_late_usage_reconciles_once(store, journal, clock):
    record = begin(store, journal, clock)
    fail(store, journal, clock, record.attempt_key, outcome="UNKNOWN")
    first = store.transaction(lambda conn: journal.reconcile_late_usage_in_tx(conn, record.attempt_key, Usage(8, 2), clock.now()))
    again = store.transaction(lambda conn: journal.reconcile_late_usage_in_tx(conn, record.attempt_key, Usage(8, 2), clock.now()))
    assert (first, again) == (True, False)
    reconciled = journal.get(record.attempt_key)
    assert reconciled.status == RECONCILED and (reconciled.input_tokens, reconciled.output_tokens) == (8, 2)
    with pytest.raises(JournalError) as caught:
        store.transaction(lambda conn: journal.reconcile_late_usage_in_tx(conn, record.attempt_key, Usage(9, 2), clock.now()))
    assert caught.value.code == "LATE_USAGE_CONFLICT"
    assert journal.unknown_attempts() == []


def test_late_usage_is_refused_for_a_succeeded_attempt(store, journal, clock):
    record = begin(store, journal, clock)
    store.transaction(lambda conn: journal.finish_success_in_tx(conn, record.attempt_key, response(), clock.now()))
    with pytest.raises(JournalError) as caught:
        store.transaction(lambda conn: journal.reconcile_late_usage_in_tx(conn, record.attempt_key, Usage(1, 1), clock.now()))
    assert caught.value.code == "ATTEMPT_STATE"
    started = begin(store, journal, clock, make_request("d1/jev/9"), "r9")
    with pytest.raises(JournalError):
        store.transaction(lambda conn: journal.reconcile_late_usage_in_tx(conn, started.attempt_key, Usage(1, 1), clock.now()))


@pytest.mark.asyncio
async def test_cost_events_have_stable_ids_and_a_cursor(store, journal, clock):
    first = begin(store, journal, clock, make_request("d1/jev/1"), "r1")
    second = begin(store, journal, clock, make_request("d1/jev/2"), "r2")

    def write(conn):
        journal.add_cost_event_in_tx(conn, attempt=first, kind=COST_CONFIRMED, cost_micros=250, usage=Usage(10, 5), now=clock.now())
        journal.add_cost_event_in_tx(conn, attempt=second, kind=COST_CORRECTION, cost_micros=0, usage=None, now=clock.now())

    store.transaction(write)
    events = await journal.acost_events_after(0)
    assert [e.event_id for e in events] == ["d1/jev/1#1:CONFIRMED", "d1/jev/2#1:CORRECTION"]
    assert events[0].cost_micros == 250 and (events[0].input_tokens, events[0].output_tokens) == (10, 5)
    assert events[0].role == "jev" and events[0].occurred_at == clock.now()
    assert events[1].input_tokens is None
    assert events[0].event_seq < events[1].event_seq
    assert [e.event_id for e in await journal.acost_events_after(events[0].event_seq)] == ["d1/jev/2#1:CORRECTION"]
    assert await journal.acost_events_after(events[1].event_seq) == []
    assert len(await journal.acost_events_after(0, limit=1)) == 1
    with pytest.raises(Exception):  # the same event cannot be written twice
        store.transaction(lambda conn: journal.add_cost_event_in_tx(
            conn, attempt=first, kind=COST_CONFIRMED, cost_micros=250, usage=Usage(10, 5), now=clock.now()))

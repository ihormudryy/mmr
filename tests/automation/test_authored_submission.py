"""SP3 Plan 3 Task 5: submit_authored_strategy (spec 7.3, 8, 10 Authority and Cap; rulings 5-6)."""
from __future__ import annotations

import datetime as dt
import json
import threading
import warnings

import pytest
from pydantic import ValidationError

from tests.automation.authored_fixtures import (
    TUNABLES, VALID_SOURCE, authored_world, digest_of, source, stored, stored_files, submit_request, with_config,
)
from tests.automation.backtest_judge_fixtures import NOW
from trader.automation.authored_files import AuthoredFileStore
from trader.automation.authored_strategies import AuthoredStrategyStore
from trader.automation.authored_submission import class_constants


def test_a_clean_file_is_stored_with_its_metadata(tmp_path):
    w = authored_world(tmp_path)
    reply = w.store.submit(submit_request())
    assert (reply["status"], reply["code"]) == ("STORED", None)
    record, digest = reply["record"], digest_of(VALID_SOURCE)
    assert (record["digest"], record["key"]) == (digest, f"authored:{digest}:RangeBreak")
    assert (record["state"], record["attempts"], record["bar_size"], record["reason"]) == ("STORED", 0, "5 mins", None)
    assert record["tunables"] == TUNABLES
    hex_digest = digest.removeprefix("sha256:")
    assert (tmp_path / "store" / f"{hex_digest}.py").read_bytes() == VALID_SOURCE.encode("utf-8")
    meta = json.loads((tmp_path / "store" / f"{hex_digest}.json").read_text())
    assert (meta["class_name"], meta["bar_size"], meta["author_model"], meta["tunables"]) == (
        "RangeBreak", "5 mins", "openrouter/author-1", TUNABLES)


def test_a_retry_after_a_lost_reply_is_existing_even_with_a_full_cap_and_the_switch_off(tmp_path):  # focus 2
    w = authored_world(tmp_path, max_active=1)
    first = w.store.submit(submit_request())
    again = with_config(w, enabled=False, max_active=1).submit(submit_request(note="another note"))
    assert again["status"] == "EXISTING"
    assert again["record"] == first["record"]                         # ruling 6: the stored note wins


def test_the_switch_off_refuses_and_writes_nothing(tmp_path):              # spec 10 Authority
    w = authored_world(tmp_path, enabled=False)
    reply = w.store.submit(submit_request())
    assert (reply["status"], reply["code"], reply["record"]) == ("REFUSED", "AUTHORED_STRATEGIES_DISABLED", None)
    assert stored_files(w) == []


def test_a_full_cap_refuses_and_writes_nothing(tmp_path):                  # spec 10 Cap
    w = authored_world(tmp_path, max_active=2)
    stored(w, 10)
    stored(w, 11)
    reply = w.store.submit(submit_request(source(12)))
    assert (reply["status"], reply["code"]) == ("REFUSED", "AUTHORED_CAP_FULL")
    assert len(stored_files(w)) == 2


def test_two_submits_for_the_last_slot_store_exactly_one(tmp_path):        # review focus 1
    w = authored_world(tmp_path, max_active=1)
    barrier, outcomes = threading.Barrier(2), []

    def attempt(range_bars: int) -> None:
        barrier.wait()
        reply = w.store.submit(submit_request(source(range_bars)))
        outcomes.append(reply["code"] or reply["status"])
    threads = [threading.Thread(target=attempt, args=(n,)) for n in (20, 21)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["AUTHORED_CAP_FULL", "STORED"]
    assert w.store.overview()["counted"] == 1


@pytest.mark.parametrize("second_store", ["the same store", "another store on the same files"])
def test_two_submits_of_the_same_file_at_once_leave_the_metadata_of_the_stored_record(tmp_path, monkeypatch,
                                                                                       second_store):
    """The bad order: the second submit finds no record, the first commits, then the second writes its .json."""
    w = authored_world(tmp_path)
    stores = {"first note": w.store,
              "second note": w.store if second_store == "the same store" else with_config(w)}
    real_write = AuthoredFileStore.write
    first_wrote, second_writing, first_done = threading.Event(), threading.Event(), threading.Event()

    def write(self, digest, data, meta):
        if not first_wrote.is_set():
            real_write(self, digest, data, meta)
            first_wrote.set()
            second_writing.wait(timeout=0.5)    # a fix may keep the second submit away from the files
        else:
            second_writing.set()
            first_done.wait(timeout=10)
            real_write(self, digest, data, meta)
    monkeypatch.setattr(AuthoredFileStore, "write", write)
    replies = {}

    def attempt(note: str) -> None:
        replies[note] = stores[note].submit(submit_request(note=note))
        if note == "first note":
            first_done.set()
    first = threading.Thread(target=attempt, args=("first note",))
    first.start()
    assert first_wrote.wait(timeout=10)
    second = threading.Thread(target=attempt, args=("second note",))
    second.start()
    for thread in (first, second):
        thread.join(timeout=30)
    assert (replies["first note"]["status"], replies["second note"]["status"]) == ("STORED", "EXISTING")
    record = w.store.get(digest_of(VALID_SOURCE))
    assert record.note == "first note"
    meta_path = tmp_path / "store" / f"{record.digest.removeprefix('sha256:')}.json"
    assert json.loads(meta_path.read_text()) == record.file_metadata()


def test_a_file_the_static_check_refuses_is_rejected_with_its_rule(tmp_path):
    w = authored_world(tmp_path)
    reply = w.store.submit(submit_request(VALID_SOURCE.replace("import pandas as pd", "import os")))
    assert (reply["status"], reply["code"]) == ("REFUSED", "AUTHORED_STATIC_REJECTED")
    assert "@" in reply["detail"]                                     # rule@line: detail
    assert stored_files(w) == []


def test_a_bar_size_outside_the_owner_list_is_refused(tmp_path):
    w = authored_world(tmp_path, bar_sizes=("1 min",))
    assert w.store.submit(submit_request())["code"] == "AUTHORED_BAD_BAR_SIZE"


@pytest.mark.parametrize("tunables, hint", [
    ({"WINDOW": {"low": 1, "high": 2, "default": 1}}, "not a numeric class constant"),
    ({"LOOKBACK_BARS": {"low": 10, "high": 50, "default": 40}}, "fixed by the API"),
    ({"RANGE_BARS": {"low": 30, "high": 5, "default": 15}}, "not a finite, ordered range"),
    ({"RANGE_BARS": {"low": float("nan"), "high": 30, "default": 15}}, "not a finite, ordered range"),
    ({"RANGE_BARS": {"low": 20, "high": 30, "default": 15}}, "the class value 15 is outside [20, 30]"),
    ({"RANGE_BARS": {"low": 5, "high": 30, "default": 16}}, "the default 16 is not the class value 15"),
    ({"range_bars": {"low": 5, "high": 30, "default": 15}}, "not an upper-case tunable name"),
    ({}, "at least one tunable"),
])
def test_bad_tunables_are_refused_with_the_reason(tmp_path, tunables, hint):
    w = authored_world(tmp_path)
    reply = w.store.submit(submit_request(tunables=tunables))
    assert reply["code"] == "AUTHORED_BAD_TUNABLES"
    assert hint in reply["detail"]
    assert stored_files(w) == []


@pytest.mark.parametrize("rebinding", [
    "RANGE_BARS = 15\n    RANGE_BARS = 20",
    "RANGE_BARS = 15\n    RANGE_BARS += 5",
    "RANGE_BARS = 15\n    RANGE_BARS, WIDTH = 20, 3",
    "RANGE_BARS = 15\n    if LOOKBACK_BARS > 10:\n        RANGE_BARS = 20",
])
def test_a_constant_the_class_binds_twice_is_not_a_tunable(tmp_path, rebinding):
    """The class value would not be the first number, so the default could not be checked against it."""
    w = authored_world(tmp_path)
    reply = w.store.submit(submit_request(VALID_SOURCE.replace("RANGE_BARS = 15", rebinding)))
    assert reply["code"] == "AUTHORED_BAD_TUNABLES"
    assert "RANGE_BARS is not a numeric class constant" in reply["detail"]


@pytest.mark.parametrize("tunables, hint", [
    ({"RANGE_BARS": {"low": 10**400, "high": 30, "default": 15}}, "not a finite, ordered range"),
    ({"RANGE_BARS": {"low": 5, "high": 10**5000, "default": 15}}, "not a finite, ordered range"),
    ({"RANGE_BARS": {"low": -1e16, "high": 30, "default": 15}}, "not a finite, ordered range"),
    ({"RANGE_BARS": {"low": 5, "high": 30, "default": 10**400}}, "the default is not a finite number"),
])
def test_a_tunable_number_beyond_the_bound_is_refused_not_raised(tmp_path, tunables, hint):
    w = authored_world(tmp_path)
    reply = w.store.submit(submit_request(tunables=tunables))
    assert (reply["status"], reply["code"]) == ("REFUSED", "AUTHORED_BAD_TUNABLES")
    assert hint in reply["detail"]


@pytest.mark.parametrize("value", ["10**400", "1" + "0" * 400, "1e999", "-1e16"],
                         ids=["power", "long_literal", "infinite_literal", "beyond_the_bound"])
def test_a_class_value_beyond_the_bound_is_refused_not_raised(tmp_path, value):
    w = authored_world(tmp_path)
    text = VALID_SOURCE.replace("RANGE_BARS = 15", f"RANGE_BARS = {value}")
    reply = w.store.submit(submit_request(text, tunables={"RANGE_BARS": {"low": 5, "high": 30, "default": 15}}))
    assert reply["code"] == "AUTHORED_BAD_TUNABLES", reply


def test_tunables_with_nothing_to_vary_are_refused(tmp_path):
    w = authored_world(tmp_path)
    reply = w.store.submit(submit_request(tunables={"RANGE_BARS": {"low": 15, "high": 15, "default": 15}}))
    assert reply["code"] == "AUTHORED_BAD_TUNABLES"
    assert "nothing to vary" in reply["detail"]


def test_one_fixed_tunable_beside_one_that_varies_is_stored(tmp_path):
    w = authored_world(tmp_path)
    tunables = {"RANGE_BARS": {"low": 15, "high": 15, "default": 15},
                "MIN_PROBABILITY": {"low": 0.5, "high": 0.9, "default": 0.6}}
    assert w.store.submit(submit_request(tunables=tunables))["status"] == "STORED"


def test_constants_are_read_the_same_under_warnings_as_errors():
    text = VALID_SOURCE.replace("    def on_bar", '    PATTERN = "\\d"\n\n    def on_bar')
    assert '"\\d"' in text
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert class_constants(text, "RangeBreak") == {"LOOKBACK_BARS": 40, "RANGE_BARS": 15,
                                                       "MIN_PROBABILITY": 0.6}


def test_a_bool_bound_or_too_many_tunables_never_pass_the_wire():
    with pytest.raises(ValidationError):
        submit_request(tunables={"RANGE_BARS": {"low": True, "high": 30, "default": 15}})
    with pytest.raises(ValidationError):
        submit_request(tunables={"RANGE_BARS": {"low": 5, "high": 30}})            # a default is required (D5)
    with pytest.raises(ValidationError):
        submit_request(tunables={f"T{i}": {"low": 0, "high": 1, "default": 0} for i in range(33)})
    with pytest.raises(ValidationError):
        submit_request(note="x" * 601)


def test_text_that_is_not_utf_8_never_passes_the_wire():
    """JSON can carry a lone surrogate; the digest and the .json need UTF-8 bytes."""
    with pytest.raises(ValidationError):
        submit_request(VALID_SOURCE + "# \ud800\n")
    with pytest.raises(ValidationError):
        submit_request(note="half a pair \udc00")


def test_a_crash_after_the_file_leaves_an_orphan_that_the_next_submit_records(tmp_path, monkeypatch):  # focus 3
    w = authored_world(tmp_path)

    def trader_dies(self, conn, record):
        raise RuntimeError("the trader died before the record")
    monkeypatch.setattr(AuthoredStrategyStore, "_insert_in_tx", trader_dies)
    with pytest.raises(RuntimeError):
        w.store.submit(submit_request())
    assert len(stored_files(w)) == 1 and w.store.get(digest_of(VALID_SOURCE)) is None
    monkeypatch.undo()
    assert w.store.submit(submit_request())["status"] == "STORED"


def test_the_overview_counts_the_cap_and_lists_oldest_first(tmp_path):
    w = authored_world(tmp_path, max_active=3)
    first = stored(w, 10)
    w.clock.now = NOW + dt.timedelta(minutes=1)
    second = stored(w, 11)
    view = w.store.overview()
    assert (view["enabled"], view["max_active"], view["counted"], view["room"], view["judged_count"]) == (
        True, 3, 2, 1, 0)
    assert [record["digest"] for record in view["records"]] == [first, second]
    assert view["bar_sizes"] == ["1 min", "5 mins", "15 mins"]

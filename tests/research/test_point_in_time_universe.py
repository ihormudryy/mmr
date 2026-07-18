"""P2 Task 3A — point-in-time universe membership.

Survivorship bias is the single most common way a backtest lies: if a universe
is reconstructed from *today's* members, delisted losers silently vanish and
every metric is inflated. This module pins membership to a date so an experiment
can ask "who was in the S&P 500 on 2019-03-01?" and freeze that answer into a
digest the evidence chain signs over.

Boundary semantics are load-bearing and pinned here explicitly:
- ``effective_from`` is INCLUSIVE, ``effective_to`` is EXCLUSIVE;
- ``delisted_at <= as_of`` means NOT a member (the delisting date itself is out);
- overlapping intervals for the SAME conid are a data bug and fail loudly;
- missing provenance (blank ``source``) is refused.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from types import SimpleNamespace

import pytest

from trader.research.canonical import sha256_digest
from trader.research.universe_membership import (
    MEMBERSHIP_DIGEST_PREFIX,
    PointInTimeMembership,
    members_as_of,
    membership_digest,
    read_current_universe_membership,
    validate_memberships,
)

D = dt.date


def _m(conid=265598, effective_from=D(2020, 1, 1), effective_to=None,
       symbol="AAPL", delisted_at=None, source="index_provider"):
    return PointInTimeMembership(
        conid=conid, effective_from=effective_from, effective_to=effective_to,
        symbol=symbol, delisted_at=delisted_at, source=source)


# ---------------------------------------------------------------------------
# Fake accessor for the read adapter (no DB, no operational deps)
# ---------------------------------------------------------------------------

class _FakeUniverse:
    def __init__(self, defs):
        self.security_definitions = defs


class _FakeAccessor:
    """Records reads so a test can prove the adapter never mutated it."""

    def __init__(self, universe):
        self._universe = universe
        self.get_calls = []

    def get(self, name):
        self.get_calls.append(name)
        return self._universe


class TestConstruction:
    def test_is_frozen(self):
        m = _m()
        with pytest.raises(dataclasses.FrozenInstanceError):
            m.symbol = "MSFT"  # type: ignore[misc]

    def test_field_order_matches_interface(self):
        names = [f.name for f in dataclasses.fields(PointInTimeMembership)]
        assert names == ["conid", "effective_from", "effective_to",
                         "symbol", "delisted_at", "source"]

    def test_blank_source_is_rejected(self):
        for bad in ("", "   ", "\t"):
            with pytest.raises(ValueError, match="source"):
                _m(source=bad)

    def test_blank_symbol_is_rejected(self):
        with pytest.raises(ValueError, match="symbol"):
            _m(symbol="")

    def test_missing_effective_from_is_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            _m(effective_from=None)

    def test_effective_to_not_after_effective_from_is_rejected(self):
        with pytest.raises(ValueError, match="effective_to"):
            _m(effective_from=D(2020, 6, 1), effective_to=D(2020, 6, 1))
        with pytest.raises(ValueError, match="effective_to"):
            _m(effective_from=D(2020, 6, 1), effective_to=D(2020, 1, 1))

    def test_datetime_is_rejected_for_date_fields(self):
        # A digest that mixes date and datetime silently forks — refuse datetime.
        with pytest.raises(TypeError):
            _m(effective_from=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))
        with pytest.raises(TypeError):
            _m(delisted_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))

    def test_valid_membership_constructs(self):
        m = _m(effective_to=D(2021, 1, 1), delisted_at=D(2022, 1, 1))
        assert m.conid == 265598 and m.effective_to == D(2021, 1, 1)


class TestMembersAsOf:
    def test_effective_from_is_inclusive(self):
        m = _m(effective_from=D(2020, 1, 1))
        assert members_as_of((m,), D(2020, 1, 1)) == (m,)

    def test_before_effective_from_is_not_a_member(self):
        m = _m(effective_from=D(2020, 1, 1))
        assert members_as_of((m,), D(2019, 12, 31)) == ()

    def test_effective_to_is_exclusive(self):
        m = _m(effective_from=D(2020, 1, 1), effective_to=D(2021, 1, 1))
        assert members_as_of((m,), D(2020, 12, 31)) == (m,)   # day before end: member
        assert members_as_of((m,), D(2021, 1, 1)) == ()        # end date itself: out

    def test_open_ended_membership_is_member_far_future(self):
        m = _m(effective_from=D(2020, 1, 1), effective_to=None)
        assert members_as_of((m,), D(2999, 1, 1)) == (m,)

    def test_returns_tuple_sorted_by_conid(self):
        a = _m(conid=300, symbol="C")
        b = _m(conid=100, symbol="A")
        c = _m(conid=200, symbol="B")
        got = members_as_of((a, b, c), D(2020, 6, 1))
        assert isinstance(got, tuple)
        assert [m.conid for m in got] == [100, 200, 300]


class TestTickerChanges:
    def _rows(self):
        # Same conid, two contiguous non-overlapping rows with different symbols.
        old = _m(conid=265598, effective_from=D(2018, 1, 1),
                 effective_to=D(2020, 6, 1), symbol="OLD")
        new = _m(conid=265598, effective_from=D(2020, 6, 1),
                 effective_to=None, symbol="AAPL")
        return old, new

    def test_returns_row_active_at_date(self):
        old, new = self._rows()
        assert members_as_of((old, new), D(2019, 1, 1)) == (old,)
        assert members_as_of((old, new), D(2020, 5, 31)) == (old,)

    def test_boundary_belongs_to_later_row(self):
        old, new = self._rows()
        # The changeover date == old.effective_to == new.effective_from.
        got = members_as_of((old, new), D(2020, 6, 1))
        assert got == (new,)
        assert got[0].symbol == "AAPL"

    def test_only_one_row_per_conid_active_at_a_time(self):
        old, new = self._rows()
        got = members_as_of((old, new), D(2021, 1, 1))
        assert len(got) == 1 and got[0].symbol == "AAPL"


class TestDelisting:
    def test_member_up_to_delisting_excluded_after(self):
        m = _m(conid=999, effective_from=D(2015, 1, 1), effective_to=None,
               symbol="DEAD", delisted_at=D(2022, 3, 15))
        assert members_as_of((m,), D(2022, 3, 14)) == (m,)      # day before: member
        assert members_as_of((m,), D(2022, 3, 15)) == ()        # delisting day: out
        assert members_as_of((m,), D(2022, 3, 16)) == ()        # after: out

    def test_delisting_before_effective_to_still_cuts_off(self):
        m = _m(conid=999, effective_from=D(2015, 1, 1), effective_to=D(2030, 1, 1),
               symbol="DEAD", delisted_at=D(2022, 3, 15))
        assert members_as_of((m,), D(2025, 1, 1)) == ()


class TestOverlapValidation:
    def test_overlapping_same_conid_is_rejected(self):
        a = _m(conid=5, effective_from=D(2020, 1, 1), effective_to=D(2021, 1, 1))
        b = _m(conid=5, effective_from=D(2020, 6, 1), effective_to=D(2022, 1, 1))
        with pytest.raises(ValueError, match="overlap"):
            validate_memberships((a, b))

    def test_open_ended_rows_for_same_conid_overlap(self):
        a = _m(conid=5, effective_from=D(2020, 1, 1), effective_to=None)
        b = _m(conid=5, effective_from=D(2021, 1, 1), effective_to=None)
        with pytest.raises(ValueError, match="overlap"):
            validate_memberships((a, b))

    def test_contiguous_same_conid_is_allowed(self):
        a = _m(conid=5, effective_from=D(2020, 1, 1), effective_to=D(2021, 1, 1))
        b = _m(conid=5, effective_from=D(2021, 1, 1), effective_to=None)
        assert validate_memberships((a, b))  # no raise

    def test_overlap_across_different_conids_is_allowed(self):
        a = _m(conid=1, effective_from=D(2020, 1, 1), effective_to=D(2021, 1, 1))
        b = _m(conid=2, effective_from=D(2020, 1, 1), effective_to=D(2021, 1, 1))
        assert validate_memberships((a, b))  # no raise

    def test_members_as_of_fails_loudly_on_overlap(self):
        a = _m(conid=5, effective_from=D(2020, 1, 1), effective_to=D(2021, 1, 1))
        b = _m(conid=5, effective_from=D(2020, 6, 1), effective_to=None)
        with pytest.raises(ValueError, match="overlap"):
            members_as_of((a, b), D(2020, 7, 1))

    def test_membership_digest_fails_loudly_on_overlap(self):
        a = _m(conid=5, effective_from=D(2020, 1, 1), effective_to=D(2021, 1, 1))
        b = _m(conid=5, effective_from=D(2020, 6, 1), effective_to=None)
        with pytest.raises(ValueError, match="overlap"):
            membership_digest((a, b))


class TestMembershipDigest:
    def test_digest_is_prefixed_sha256_of_canonical_body(self):
        body = [{"conid": 265598, "effective_from": D(2020, 1, 1),
                 "effective_to": None, "symbol": "AAPL",
                 "delisted_at": None, "source": "index_provider"}]
        assert membership_digest((_m(),)) == sha256_digest(MEMBERSHIP_DIGEST_PREFIX, body)

    def test_digest_is_order_independent(self):
        a = _m(conid=1, symbol="A")
        b = _m(conid=2, symbol="B")
        c = _m(conid=3, symbol="C")
        assert membership_digest((a, b, c)) == membership_digest((c, a, b))

    def test_digest_is_deterministic(self):
        assert membership_digest((_m(),)) == membership_digest((_m(),))

    def test_changing_any_field_changes_the_digest(self):
        base = membership_digest((_m(),))
        assert membership_digest((_m(conid=111111),)) != base
        assert membership_digest((_m(symbol="MSFT"),)) != base
        assert membership_digest((_m(effective_from=D(2019, 1, 1)),)) != base
        assert membership_digest((_m(effective_to=D(2030, 1, 1)),)) != base
        assert membership_digest((_m(delisted_at=D(2030, 1, 1)),)) != base
        assert membership_digest((_m(source="other_provider"),)) != base

    def test_digest_distinguishes_added_member(self):
        one = membership_digest((_m(conid=1),))
        two = membership_digest((_m(conid=1), _m(conid=2)))
        assert one != two


class TestReadCurrentUniverseAdapter:
    def _defs(self):
        return [SimpleNamespace(conId=265598, symbol="AAPL"),
                SimpleNamespace(conId=272093, symbol="MSFT")]

    def test_reads_current_universe_as_open_ended_membership(self):
        defs = self._defs()
        acc = _FakeAccessor(_FakeUniverse(defs))
        ms = read_current_universe_membership(
            acc, "sp500", as_of=D(2026, 7, 18), source="index_provider")
        assert acc.get_calls == ["sp500"]
        assert {(m.conid, m.symbol) for m in ms} == {(265598, "AAPL"), (272093, "MSFT")}
        assert all(m.effective_from == D(2026, 7, 18) for m in ms)
        assert all(m.effective_to is None for m in ms)
        assert all(m.delisted_at is None for m in ms)
        assert all(m.source == "index_provider" for m in ms)

    def test_adapter_does_not_mutate_accessor(self):
        defs = self._defs()
        uni = _FakeUniverse(defs)
        acc = _FakeAccessor(uni)
        before = list(defs)
        read_current_universe_membership(
            acc, "sp500", as_of=D(2026, 7, 18), source="index_provider")
        assert uni.security_definitions is defs      # not replaced
        assert uni.security_definitions == before     # not appended to / edited

    def test_blank_source_is_refused(self):
        acc = _FakeAccessor(_FakeUniverse([SimpleNamespace(conId=1, symbol="A")]))
        with pytest.raises(ValueError, match="source"):
            read_current_universe_membership(acc, "x", as_of=D(2026, 1, 1), source="   ")

    def test_duplicate_conid_in_universe_fails_loudly(self):
        defs = [SimpleNamespace(conId=1, symbol="A"),
                SimpleNamespace(conId=1, symbol="A_DUP")]
        acc = _FakeAccessor(_FakeUniverse(defs))
        with pytest.raises(ValueError, match="overlap"):
            read_current_universe_membership(acc, "x", as_of=D(2026, 1, 1), source="idx")

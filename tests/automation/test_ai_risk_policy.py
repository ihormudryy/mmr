"""Plan 3 Task 4: AI risk policy store, per-session timing and the breach latch."""
from __future__ import annotations

import datetime as dt
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from trader.automation.ai_risk_policy import (
    AiRiskPolicyService, PolicyRefused, apply_ai_risk_policy_migration,
)
from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.risk_limits import PAPER_LIMITS
from trader.data.broker_state import BrokerRiskSnapshot
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=UTC)          # Friday, mid-session
SATURDAY = dt.datetime(2026, 7, 18, 15, 0, tzinfo=UTC)
ACCOUNT = "DU111111"

CEILING = replace(PAPER_LIMITS, gross_fraction=0.10)
LOOSE = replace(PAPER_LIMITS, gross_fraction=0.08)
TIGHT = replace(PAPER_LIMITS, gross_fraction=0.04, position_fraction=0.04)  # position <= gross


@pytest.fixture
def db(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    apply_ai_risk_policy_migration(SchemaMigrator(db))
    return db


def broker(nl, pnl, *, account=ACCOUNT, generation_id=7, mode="paper"):
    return BrokerRiskSnapshot(
        generation_id=generation_id, source_cursor=3, promoted_at=NOW, account_id=account,
        account_mode=mode, net_liquidation=nl, daily_pnl=pnl, positions=(), working_orders=())


def svc(db, now=lambda: NOW, ceiling=CEILING):
    return AiRiskPolicyService(db=db, account_id=ACCOUNT, ceiling=ceiling,
                               calendar=XNYSCalendarPolicy(), now=now)


def publish(s, limits, cid):
    return s.publish(limits, reason="test", principal="ai_supervisor", command_id=cid,
                     broker=broker(1_000_000, 0))


def count_sessions(db):
    return db.execute("SELECT count(*) FROM ai_paper_sessions", fetch="one")[0]


def test_policy_above_the_owner_ceiling_is_refused_not_clamped(db):
    with pytest.raises(PolicyRefused) as exc:
        publish(svc(db), replace(PAPER_LIMITS, gross_fraction=0.11), "c1")
    assert (exc.value.code, exc.value.fields) == ("POLICY_ABOVE_CEILING", ("gross_fraction",))
    assert svc(db).latest_published_revision() is None


def test_structural_problem_is_refused(db):
    with pytest.raises(PolicyRefused, match="POLICY_INVALID"):
        publish(svc(db), replace(PAPER_LIMITS, gross_fraction=0.03, position_fraction=0.04), "c1")


def test_non_limits_object_is_refused(db):
    with pytest.raises(PolicyRefused, match="POLICY_INVALID"):
        publish(svc(db), PAPER_LIMITS.to_json(), "c1")


def test_no_policy_means_no_effective_limits(db):
    view = svc(db).ensure_session(broker(1_000_000, 0))
    assert view.effective is None
    with pytest.raises(PolicyRefused, match="NO_EFFECTIVE_LIMITS"):
        svc(db).effective_limits()


def test_first_policy_of_a_session_applies_at_once(db):            # R8
    s = svc(db)
    s.ensure_session(broker(1_000_000, 0))
    result = publish(s, LOOSE, "c1")
    assert s.current().effective == LOOSE
    assert result.applied_now == PAPER_LIMITS.FIELDS and s.effective_limits() == LOOSE


def test_looser_field_is_queued_not_applied_not_refused(db):
    s = svc(db)
    s.ensure_session(broker(1_000_000, 0))
    publish(s, TIGHT, "c1")
    result = publish(s, LOOSE, "c2")
    looser = ("position_fraction", "gross_fraction")
    assert (result.revision, result.applied_now, result.queued) == (2, (), looser)
    assert s.current().effective.gross_fraction == 0.04
    assert s.current().queued == looser
    assert (s.current().published_revision, s.current().effective_revision) == (1, 1)


def test_mixed_revision_applies_tighter_now_and_looser_next_session(db):
    clock = {"t": NOW}
    s = svc(db, now=lambda: clock["t"])
    s.ensure_session(broker(1_000_000, 0))
    publish(s, TIGHT, "c1")
    mixed = replace(PAPER_LIMITS, gross_fraction=0.08, max_positions=2)
    assert publish(s, mixed, "c2").applied_now == ("max_positions",)
    assert s.current().effective == replace(TIGHT, max_positions=2)
    clock["t"] = NOW + dt.timedelta(days=3)                          # next XNYS session (Monday)
    assert s.current() is None                                       # no session until the first decision
    assert s.ensure_session(broker(1_000_000, 0)).effective == mixed


def test_next_session_takes_latest_published_capped_by_the_ceiling(db):
    clock = {"t": NOW}
    s = svc(db, now=lambda: clock["t"])
    s.ensure_session(broker(1_000_000, 0))
    publish(s, LOOSE, "c1")
    clock["t"] = NOW + dt.timedelta(days=3)
    lowered = svc(db, now=lambda: clock["t"], ceiling=replace(PAPER_LIMITS, gross_fraction=0.07))
    view = lowered.ensure_session(broker(1_000_000, 0))
    assert view.effective.gross_fraction == 0.07 and view.ceiling.gross_fraction == 0.07


def test_restart_is_not_a_session_start(db):
    s = svc(db)
    s.ensure_session(broker(1_000_000, 0))
    publish(s, TIGHT, "c1")
    publish(s, LOOSE, "c2")
    restarted = svc(db)
    assert restarted.ensure_session(broker(900_000, -20_000)).effective == TIGHT   # queued loosening not applied
    assert restarted.current().anchor == 1_000_000.0                               # anchor not re-frozen


def test_restart_with_a_lower_ceiling_tightens_at_once(db):                       # R9
    s = svc(db)
    s.ensure_session(broker(1_000_000, 0))
    publish(s, LOOSE, "c1")
    view = svc(db, ceiling=replace(PAPER_LIMITS, gross_fraction=0.05)).ensure_session(broker(1_000_000, 0))
    assert view.effective.gross_fraction == 0.05


def test_restart_with_a_higher_ceiling_waits_for_the_next_session(db):
    clock = {"t": NOW}
    s = svc(db, now=lambda: clock["t"])
    s.ensure_session(broker(1_000_000, 0))
    publish(s, replace(PAPER_LIMITS, gross_fraction=0.10), "c1")
    raised = svc(db, now=lambda: clock["t"], ceiling=replace(PAPER_LIMITS, gross_fraction=0.15))
    publish(raised, replace(PAPER_LIMITS, gross_fraction=0.12), "c2")
    view = raised.ensure_session(broker(1_000_000, 0))
    assert (view.ceiling.gross_fraction, view.effective.gross_fraction) == (0.10, 0.10)
    clock["t"] = NOW + dt.timedelta(days=3)
    assert raised.ensure_session(broker(1_000_000, 0)).effective.gross_fraction == 0.12


def test_anchor_is_start_of_day_net_liquidation(db):                              # R7
    view = svc(db).ensure_session(broker(995_000, -5_000))
    assert view.anchor == 1_000_000.0


def test_tighter_daily_loss_lowers_the_budget_mid_session_on_the_frozen_anchor(db):
    s = svc(db)
    s.ensure_session(broker(1_000_000, 0))
    publish(s, PAPER_LIMITS, "c1")
    publish(s, replace(PAPER_LIMITS, daily_loss_fraction=0.002), "c2")
    s.ensure_session(broker(990_000, -10_000))
    assert s.current().daily_loss_budget == pytest.approx(2_000.0)


def test_breach_latch_survives_a_looser_revision_and_a_restart(db):
    s = svc(db)
    s.ensure_session(broker(1_000_000, 0))
    publish(s, TIGHT, "c1")
    s.latch("DAILY_LOSS", "pnl=-5000")
    publish(s, LOOSE, "c2")
    s.latch("DRAWDOWN", "later")                                                  # first breach wins
    assert svc(db).current().latch_code == "DAILY_LOSS"


def test_latch_without_a_session_is_refused(db):
    with pytest.raises(PolicyRefused, match="NO_SESSION"):
        svc(db).latch("DAILY_LOSS", "x")


def test_off_session_returns_none_and_publish_still_stores(db):
    s = svc(db, now=lambda: SATURDAY)
    assert s.ensure_session(broker(1_000_000, 0)) is None
    assert s.current() is None
    assert publish(s, TIGHT, "c1").revision == 1


@pytest.mark.parametrize("bad", [dict(nl=math.nan), dict(account="DU999"), dict(generation_id=0),
                                 dict(nl=-1.0), dict(mode="live"), dict(generation_id=True)])
def test_session_start_needs_valid_broker_evidence(db, bad):
    nl = bad.pop("nl", 1_000_000)
    with pytest.raises(PolicyRefused, match="SESSION_EVIDENCE_INVALID"):
        svc(db).ensure_session(broker(nl, 0, **bad))
    assert count_sessions(db) == 0


def test_concurrent_session_start_creates_one_row(db):
    with ThreadPoolExecutor(8) as pool:
        views = list(pool.map(lambda nl: svc(db).ensure_session(broker(nl, 0)), range(1_000_000, 1_000_008)))
    assert len({v.anchor for v in views}) == 1
    assert count_sessions(db) == 1


@pytest.mark.parametrize("reason", ["", " ", "x" * 501, 7])
def test_reason_is_required_text(db, reason):
    with pytest.raises(PolicyRefused, match="REASON_INVALID"):
        svc(db).publish(TIGHT, reason=reason, principal="ai_supervisor", command_id="c1",
                        broker=broker(1_000_000, 0))


def test_publish_never_starts_a_session(db):                                     # R7, owner answer 1
    publish(svc(db), TIGHT, "c1")
    assert count_sessions(db) == 0
    assert svc(db).ensure_session(broker(1_000_000, 0)).effective == TIGHT         # first decision: applies at once (R8)


def test_restart_never_starts_a_second_session_for_the_same_date(db):           # owner answer 1
    first = svc(db).ensure_session(broker(1_000_000, 0))
    for nl in (900_000, 1_100_000):                                              # restarts with other broker values
        again = svc(db).ensure_session(broker(nl, 0))
        assert (again.session_date, again.anchor) == (first.session_date, first.anchor)
    assert count_sessions(db) == 1


def test_session_row_is_committed_before_ensure_session_returns(db):
    svc(db).ensure_session(broker(1_000_000, 0))
    other = DuckDBConnection.get_instance(db.db_path)
    assert other.execute("SELECT anchor_net_liquidation, latch_code FROM ai_paper_sessions",
                         fetch="one") == (1_000_000.0, None)


def test_revisions_are_append_only(db):
    s = svc(db)
    publish(s, TIGHT, "c1")
    publish(s, LOOSE, "c2")
    assert db.execute("SELECT revision, command_id FROM ai_risk_policy_revisions ORDER BY revision",
                      fetch="all") == [(1, "c1"), (2, "c2")]
    assert not [m for m in dir(AiRiskPolicyService) if m.startswith(("delete", "update", "edit"))]


def test_cutoff_cancel_state_only_moves_forward(db):                             # R26 column
    s = svc(db)
    view = s.ensure_session(broker(1_000_000, 0))
    s.set_cutoff_cancel_state(view.session_date, "ISSUED")
    s.set_cutoff_cancel_state(view.session_date, "DONE")
    with pytest.raises(ValueError):
        s.set_cutoff_cancel_state(view.session_date, "ISSUED")
    assert s.current().cutoff_cancel_state == "DONE"

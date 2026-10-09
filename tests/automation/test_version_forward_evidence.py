"""SP2c Plan 5 Task 2: get_deployment_forward_evidence over the real version, shadow rows and trips."""
from __future__ import annotations

import datetime as dt

import pytest

from tests.automation.backtest_judge_fixtures import KEY, insert_reject
from tests.automation.renewal_world import (
    AFTER_EXPIRY, BUNDLE, SESSIONS, UTC, FakeBundles, renewal_world,
)
from trader.automation.forward_evidence import ForwardEvidenceRefused
from trader.research.forward_evidence_view import ForwardEvidenceView
from trader.scoreboard.seal import row_digest


def test_the_forward_evidence_lists_every_session_of_the_version(tmp_path):
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    rw.clock.now = AFTER_EXPIRY                          # ShadowIngest takes rows of closed sessions only
    assert rw.shadow_row(SESSIONS[0]) == "INSERTED"
    assert rw.shadow_row(SESSIONS[1], status="INCOMPLETE") == "INSERTED"
    view = ForwardEvidenceView.model_validate(rw.forward.read(v1))
    assert [(s.session_date, s.state) for s in view.sessions] == [
        ("2026-10-09", "COMPLETE"), ("2026-10-12", "INCOMPLETE"), ("2026-10-13", "MISSING")]
    assert view.sessions[0].pnl_usd == 4.0 and view.sessions[1].reason == "BARS_MISSING: fixture"
    assert (view.status, view.kind, view.prior_version_digest) == ("EXPIRED", "INITIAL", None)
    assert (view.binding.bundle_digest, view.binding.strategy_key, view.binding.conids) == (
        BUNDLE, KEY, [265598, 272093])
    assert (view.line.initial_judgment_id, view.line.artifact_id, view.line.family_id) == (
        "jdg-00000001", "art-1", "fam-1")
    assert (view.renewable.ok, view.trips) == (True, [])


def test_a_session_outside_the_judgments_shadow_window_is_not_replayed(tmp_path):          # review focus 4
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial(first=dt.date(2026, 10, 12), expiry=dt.date(2026, 10, 14))       # registered a day late
    rw.clock.now = dt.datetime(2026, 10, 15, 22, 0, tzinfo=UTC)
    assert [s["state"] for s in rw.forward.read(v1)["sessions"]] == ["MISSING", "MISSING", "NOT_REPLAYED"]


def test_paper_trips_of_the_version_and_only_of_it(tmp_path):
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    rw.paper_trip(v1, "rt-1", net_pnl=12.5)
    rw.paper_trip("sha256:" + "9" * 64, "rt-2", net_pnl=-3.0)
    trips = rw.forward.read(v1)["trips"]
    assert [(t["round_trip_id"], t["net_pnl_usd"], t["status"], t["fees_complete"]) for t in trips] == [
        ("rt-1", 12.5, "CLOSED", True)]


@pytest.mark.parametrize("setup,code", [
    ("active", "RENEWAL_NOT_DUE"), ("withdrawn", "RENEWAL_PRIOR_INVALID"), ("bundle_expired", "BUNDLE_EXPIRED"),
    ("bundle_ends_before_the_next_session", "BUNDLE_EXPIRED"), ("cooling", "FAMILY_COOLING_DOWN")])
def test_the_forward_evidence_says_why_a_version_cannot_be_renewed(tmp_path, setup, code):
    expires = {"bundle_expired": dt.datetime(2026, 10, 14, 12, tzinfo=UTC),
               "bundle_ends_before_the_next_session": dt.datetime(2026, 10, 15, 12, tzinfo=UTC)}
    rw = renewal_world(tmp_path, bundles=FakeBundles(expires[setup]) if setup in expires else None)
    v1 = rw.deploy_initial()
    rw.clock.now = dt.datetime(2026, 10, 12, 15, 0, tzinfo=UTC) if setup == "active" else AFTER_EXPIRY
    if setup == "withdrawn":
        rw.versions.withdraw(v1, reason="operator", principal="cli", command_id="w1")
    if setup == "cooling":
        insert_reject(rw.base.db, KEY, dt.date(2026, 10, 30))
    renewable = rw.forward.read(v1)["renewable"]
    assert (renewable["ok"], renewable["code"]) == (False, code)


def test_tampered_rows_and_unknown_versions_are_refused_by_name(tmp_path):
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    rw.clock.now = AFTER_EXPIRY
    assert rw.shadow_row(SESSIONS[0]) == "INSERTED"
    rw.base.db.execute("UPDATE shadow_results SET pnl_usd = 400.0")
    with pytest.raises(ForwardEvidenceRefused) as exc:
        rw.forward.read(v1)
    assert exc.value.code == "FORWARD_EVIDENCE_TAMPERED"
    with pytest.raises(ForwardEvidenceRefused) as exc:
        rw.forward.read("sha256:" + "f" * 64)
    assert exc.value.code == "DEPLOYMENT_VERSION_UNKNOWN"


def test_a_row_resealed_after_an_edit_is_still_tampered(tmp_path):                         # body digest check
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    rw.clock.now = AFTER_EXPIRY
    assert rw.shadow_row(SESSIONS[0]) == "INSERTED"
    rw.base.db.execute("UPDATE shadow_results SET pnl_usd = 400.0")
    (row,) = rw.scoreboard.fetch("shadow_results", {})
    rw.base.db.execute("UPDATE scoreboard_seals SET row_digest = ? WHERE table_name = 'shadow_results'",
                       [row_digest(row)])
    with pytest.raises(ForwardEvidenceRefused) as exc:
        rw.forward.read(v1)
    assert exc.value.code == "FORWARD_EVIDENCE_TAMPERED"


@pytest.mark.parametrize("edit", ["DELETE FROM shadow_results",
                                  "UPDATE shadow_results SET judgment_id = 'jdg-other'"])
def test_a_deleted_or_rekeyed_sealed_row_is_tampered_not_missing(tmp_path, edit):
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    rw.clock.now = AFTER_EXPIRY
    assert rw.shadow_row(SESSIONS[0]) == "INSERTED"
    rw.base.db.execute(edit)
    with pytest.raises(ForwardEvidenceRefused) as exc:
        rw.forward.read(v1)
    assert exc.value.code == "FORWARD_EVIDENCE_TAMPERED"


def test_a_session_that_has_not_closed_is_missing_with_its_reason(tmp_path):
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    rw.clock.now = dt.datetime(2026, 10, 12, 15, 0, tzinfo=UTC)          # Monday 11:00 New York
    view = rw.forward.read(v1)
    assert view["status"] == "ACTIVE"
    assert [(s["session_date"], s["state"], s["reason"]) for s in view["sessions"]] == [
        ("2026-10-09", "MISSING", None), ("2026-10-12", "MISSING", "SESSION_NOT_CLOSED"),
        ("2026-10-13", "MISSING", "SESSION_NOT_CLOSED")]


def test_a_tampered_judgment_of_the_version_is_refused_by_name(tmp_path):
    rw = renewal_world(tmp_path)
    v1 = rw.deploy_initial()
    rw.base.db.execute("UPDATE backtest_judgments SET body_json = replace(body_json, 'jev-1', 'jev-2') "
                       "WHERE judgment_id = 'jdg-00000001'")
    with pytest.raises(ForwardEvidenceRefused) as exc:
        rw.forward.read(v1)
    assert exc.value.code == "JUDGMENT_TAMPERED"

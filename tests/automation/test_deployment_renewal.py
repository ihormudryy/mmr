"""SP2c Plan 5 Task 3: a RENEWAL judgment at the trader (spec 5.2 item 2, 6.2) and its registration (5.2 item 5)."""
from __future__ import annotations

import datetime as dt
import threading

import pytest

from tests.automation.backtest_judge_fixtures import KEY, insert_reject
from tests.automation.renewal_world import AFTER_EXPIRY, RECORD, SESSIONS, UTC, FakeBundles, renewal_world
from trader.automation.ai_deployment_registration import registration_command_id
from trader.automation.ai_deployments import AiDeployment, DeploymentRefused
from trader.automation.ai_judgment_port import cooldown_reader_for
from trader.automation.backtest_judgments import JudgmentRefused
from trader.automation.forward_evidence import ForwardEvidenceRefused
from trader.research.evaluation_case import NO_DEPLOY_MENU
from trader.research.forward_evidence_view import ForwardEvidenceView

RENEWED_SESSIONS = ("2026-10-15", "2026-10-16", "2026-10-19")
AFTER_RENEWED_EXPIRY = dt.datetime(2026, 10, 20, 22, 0, tzinfo=UTC)


def expired_line(tmp_path, *, rows=SESSIONS, **options):
    rw = renewal_world(tmp_path, **options)
    v1 = rw.deploy_initial()
    rw.clock.now = AFTER_EXPIRY                        # ShadowIngest takes rows of closed sessions only
    for session in rows:
        assert rw.shadow_row(session) == "INSERTED"
    return rw, v1


def renewal_body(judgment_id: str = "jdg-renewal-1") -> dict:
    return {"judgment_id": judgment_id, "bundle_digest": RECORD["evidence_ref"],
            "deployment": AiDeployment.from_json(RECORD).to_json()}


def register_renewal(rw, judgment_id: str = "jdg-renewal-1") -> dict:
    body = renewal_body(judgment_id)
    return rw.registrar().register(body, principal="ai_research",
                                   command_id=registration_command_id(body, rw.clock.now.date()))


def test_a_renewal_deploy_needs_an_expired_version_with_complete_forward_rows(tmp_path):  # review focus 5
    rw, v1 = expired_line(tmp_path)
    rw.clock.now = dt.datetime(2026, 10, 13, 22, 0, tzinfo=UTC)                 # evening of the last session
    early = rw.renew(rw.renewal_case(v1), v1)
    assert (early["status"], early["code"]) == ("REFUSED", "RENEWAL_NOT_DUE")
    assert rw.judgments.renewals_of(v1) == ()
    rw.clock.now = AFTER_EXPIRY
    assert rw.renew(rw.renewal_case(v1), v1)["status"] == "RECORDED"
    (renewal,) = rw.judgments.renewals_of(v1)
    assert (renewal.kind, renewal.verdict, renewal.binding["prior_deployment_version"]) == ("RENEWAL", "DEPLOY", v1)
    assert rw.activity.status(v1) == "EXPIRED"                                  # until a renewal is registered


def test_a_missing_or_incomplete_forward_session_blocks_deploy_not_shadow(tmp_path):   # review focus 4
    rw, v1 = expired_line(tmp_path, rows=SESSIONS[:1])
    rw.shadow_row(SESSIONS[1], status="INCOMPLETE")                             # SESSIONS[2] has no row at all
    case = rw.renewal_case(v1)                  # the case claims FORWARD_COMPLETE; the trader reads its own rows
    refused = rw.renew(case, v1)
    assert (refused["status"], refused["code"]) == ("REFUSED", "FORWARD_INCOMPLETE")
    assert rw.renew(case, v1, "SHADOW")["status"] == "RECORDED"
    assert rw.activity.status(v1) == "JUDGMENT_ENDED"


@pytest.mark.parametrize("expires_at", [dt.datetime(2026, 10, 14, 12, tzinfo=UTC),       # already expired
                                        dt.datetime(2026, 10, 15, 12, tzinfo=UTC)])      # no session left
def test_a_renewal_after_the_bundle_expired_cannot_deploy(tmp_path, expires_at):           # review focus 3
    rw, v1 = expired_line(tmp_path, bundles=FakeBundles(expires_at))
    case = rw.renewal_case(v1)
    assert rw.renew(case, v1)["code"] == "BUNDLE_EXPIRED"
    assert rw.renew(case, v1, "SHADOW")["status"] == "RECORDED"                 # Jev may still end the line


def test_a_cooling_key_cannot_renew_with_deploy(tmp_path):
    rw, v1 = expired_line(tmp_path)
    insert_reject(rw.base.db, KEY, dt.date(2026, 10, 30))
    assert rw.renew(rw.renewal_case(v1), v1)["code"] == "FAMILY_COOLING_DOWN"


@pytest.mark.parametrize("changes,detail", [
    ({"selected_params": {"RANGE_MINUTES": 30}, "cohort": [{"RANGE_MINUTES": 30}]}, "params differ"),
    ({"bar_size": "1 min"}, "bar size differs"),
    ({"strategy_file_hash": "sha256:" + "c" * 64}, "file hash differs"),
    ({"artifact_id": "art-9"}, "artifact_id differs"),
    ({"family_id": "fam-9"}, "family_id differs"),
    ({"strategy_key": "strategies/other_breakout.py:OpeningRangeBreakout"}, "strategy key differs"),
    ({"conids": [265598, 999999]}, "conids differ"),
    ({"forward_sessions": 20}, "forward_sessions"),
])
def test_a_case_bound_to_other_facts_is_refused_whole(tmp_path, changes, detail):
    rw, v1 = expired_line(tmp_path)
    reply = rw.renew(rw.renewal_case(v1, **changes), v1, "SHADOW")
    assert reply["code"] == "RENEWAL_CASE_MISMATCH" and detail in reply["detail"]
    assert rw.judgments.renewals_of(v1) == ()


def test_a_withdrawn_or_unknown_prior_is_refused(tmp_path):
    rw, v1 = expired_line(tmp_path)
    unknown = "sha256:" + "f" * 64
    assert rw.renew(rw.renewal_case(unknown), unknown, "SHADOW")["code"] == "RENEWAL_PRIOR_INVALID"
    rw.versions.withdraw(v1, reason="operator", principal="cli", command_id="w1")
    assert rw.renew(rw.renewal_case(v1), v1, "SHADOW")["code"] == "RENEWAL_PRIOR_INVALID"


def test_one_renewal_judgment_per_version(tmp_path):                                   # review focus 2
    rw, v1 = expired_line(tmp_path)
    first = rw.renewal_case(v1)
    assert rw.renew(first, v1)["status"] == "RECORDED"
    assert rw.renew(first, v1)["status"] == "EXISTING"
    other = rw.renewal_case(v1, created_at="2026-10-14T22:01:00+00:00")
    assert rw.renew(other, v1, "REJECT", judgment_id="jdg-renewal-2")["code"] == "RENEWAL_ALREADY_JUDGED"
    assert len(rw.judgments.renewals_of(v1)) == 1


def test_a_renewal_reject_ends_the_line_and_cools_the_key_down(tmp_path):              # review focus 5
    rw, v1 = expired_line(tmp_path)
    reply = rw.renew(rw.renewal_case(v1), v1, "REJECT")
    assert reply["status"] == "RECORDED" and reply["cooldown_until_session"] is not None
    assert rw.activity.status(v1) == "JUDGMENT_ENDED"
    assert cooldown_reader_for(rw.base.db).cooling_down(KEY, rw.clock.now) is True
    late = rw.renew(rw.renewal_case(v1, created_at="2026-10-14T22:05:00+00:00"), v1, judgment_id="jdg-renewal-2")
    assert late["code"] == "RENEWAL_LINE_ENDED"


def test_an_incomplete_case_offers_no_deploy(tmp_path):
    rw, v1 = expired_line(tmp_path)
    case = rw.renewal_case(v1, stage="FORWARD_INCOMPLETE", incomplete_sessions=1)
    assert rw.renew(case, v1)["code"] == "JUDGMENT_MENU_MISMATCH"                 # Plan 1: menu is SHADOW/REJECT
    assert rw.renew(case, v1, "REJECT", menu=NO_DEPLOY_MENU)["status"] == "RECORDED"


def test_a_recorded_renewal_deploy_registers_a_fresh_version_on_the_same_base(tmp_path):   # review focus 1, 2
    rw, v1 = expired_line(tmp_path)
    assert rw.renew(rw.renewal_case(v1), v1)["status"] == "RECORDED"
    registrar = rw.registrar()
    body = {"judgment_id": "jdg-renewal-1", "bundle_digest": RECORD["evidence_ref"],
            "deployment": AiDeployment.from_json(RECORD).to_json()}
    command_id = registration_command_id(body, AFTER_EXPIRY.date())
    first = registrar.register(body, principal="ai_research", command_id=command_id)
    again = registrar.register(body, principal="ai_research", command_id=command_id)
    assert first["version_digest"] != v1 and first["digest"] == rw.versions.get(v1).base_digest
    assert (first["kind"], first["first_session"], first["created"]) == ("RENEWAL", "2026-10-15", True)
    assert (again["version_digest"], again["created"]) == (first["version_digest"], False)
    assert rw.activity.status(v1) == "SUPERSEDED"                               # never active, never renewed again
    assert rw.activity.status(first["version_digest"]) == "NOT_STARTED"


# Controller carries for Task 3 (fail loudly, the index cross-check, the journal lock, a renewed version's evidence)

@pytest.mark.parametrize("tamper,code", [
    ("UPDATE shadow_results SET pnl_usd = 400.0", "FORWARD_EVIDENCE_TAMPERED"),
    ("UPDATE ai_deployment_versions SET record_json = replace(record_json, '2026-10-13', '2026-10-20')",
     "DEPLOYMENT_VERSION_TAMPERED"),
])
def test_tampered_forward_evidence_fails_the_judgment_loudly(tmp_path, tamper, code):
    rw, v1 = expired_line(tmp_path)
    case = rw.renewal_case(v1)
    rw.base.db.execute(tamper)
    for verdict in ("DEPLOY", "SHADOW"):               # never a business refusal that could end the line
        with pytest.raises(JudgmentRefused) as exc:
            rw.renew(case, v1, verdict)
        assert exc.value.code == code
    assert rw.base.db.execute("SELECT COUNT(*) FROM backtest_judgments WHERE kind = 'RENEWAL'", fetch="one")[0] == 0


def test_a_sealed_renewal_judgment_missing_from_the_index_is_tampered(tmp_path):
    rw, v1 = expired_line(tmp_path)
    assert rw.renew(rw.renewal_case(v1), v1, "SHADOW")["status"] == "RECORDED"
    rw.base.db.execute("DELETE FROM renewal_judgments")
    with pytest.raises(JudgmentRefused) as exc:
        rw.judgments.renewals_of(v1)
    assert exc.value.code == "JUDGMENT_TAMPERED"
    other = rw.renewal_case(v1, created_at="2026-10-14T22:01:00+00:00")
    with pytest.raises(JudgmentRefused) as exc:         # a second renewal does not slip in
        rw.renew(other, v1, "REJECT", judgment_id="jdg-renewal-2")
    assert exc.value.code == "JUDGMENT_TAMPERED"


def test_a_renewal_seal_and_a_withdrawal_of_its_prior_are_ordered_by_the_journal_lock(tmp_path, monkeypatch):
    rw, v1 = expired_line(tmp_path)
    assert rw.renew(rw.renewal_case(v1), v1)["status"] == "RECORDED"
    inside, release, outcome = threading.Event(), threading.Event(), {}
    seal_in_tx = rw.versions.seal_in_tx

    def paused_seal(conn, version, **kwargs):
        inside.set()                                   # the registration's write transaction is open
        assert release.wait(10)
        return seal_in_tx(conn, version, **kwargs)
    monkeypatch.setattr(rw.versions, "seal_in_tx", paused_seal)

    def run(name, call):
        try:
            outcome[name] = call()
        except Exception as exc:                       # surfaced by the asserts below
            outcome[name] = exc
    registering = threading.Thread(target=run, args=("register", lambda: register_renewal(rw)))
    registering.start()
    assert inside.wait(10)
    withdrawing = threading.Thread(target=run, args=("withdraw", lambda: rw.versions.withdraw(
        v1, reason="operator", principal="cli", command_id="w1")))
    withdrawing.start()
    withdrawing.join(0.3)
    blocked = withdrawing.is_alive()                   # the withdrawal waits for the seal's journal lock
    release.set()
    registering.join(10)
    withdrawing.join(10)
    assert blocked
    assert outcome["register"]["created"] is True
    refused = outcome["withdraw"]                      # the seal won: v1 is superseded, its successor is named
    assert isinstance(refused, DeploymentRefused) and refused.code == "VERSION_SUPERSEDED"
    assert outcome["register"]["version_digest"] in refused.message
    assert v1 not in rw.versions.withdrawn()


def test_a_withdrawal_committed_before_the_renewal_seal_refuses_it(tmp_path, monkeypatch):
    rw, v1 = expired_line(tmp_path)
    assert rw.renew(rw.renewal_case(v1), v1)["status"] == "RECORDED"
    stands_for = rw.activity.stands_for

    def withdraw_then_read(*args, **kwargs):           # the registration's reads are done, its write not yet
        rw.versions.withdraw(v1, reason="operator", principal="cli", command_id="w1")
        return stands_for(*args, **kwargs)
    monkeypatch.setattr(rw.activity, "stands_for", withdraw_then_read)
    with pytest.raises(DeploymentRefused) as exc:
        register_renewal(rw)
    assert exc.value.code == "RENEWAL_PRIOR_INVALID"
    assert rw.base.db.execute("SELECT COUNT(*) FROM ai_deployment_versions", fetch="one")[0] == 1


def renewed_line(tmp_path):
    """v1 renewed by DEPLOY into v2, and v2 expired with a COMPLETE row for each of its sessions."""
    rw, v1 = expired_line(tmp_path)
    assert rw.renew(rw.renewal_case(v1), v1)["status"] == "RECORDED"
    v2 = register_renewal(rw)["version_digest"]
    rw.clock.now = AFTER_RENEWED_EXPIRY
    for session in RENEWED_SESSIONS:
        assert rw.shadow_row(session, judgment_id="jdg-renewal-1") == "INSERTED"
    return rw, v1, v2


def test_a_renewed_version_reads_its_own_rows_and_the_initial_line_and_can_renew_again(tmp_path):
    rw, v1, v2 = renewed_line(tmp_path)
    view = ForwardEvidenceView.model_validate(rw.forward.read(v2))
    assert (view.kind, view.prior_version_digest, view.judgment_id, view.status) == (
        "RENEWAL", v1, "jdg-renewal-1", "EXPIRED")
    assert [(s.session_date, s.state) for s in view.sessions] == [(d, "COMPLETE") for d in RENEWED_SESSIONS]
    assert (view.line.initial_judgment_id, view.line.artifact_id, view.line.family_id) == (
        "jdg-00000001", "art-1", "fam-1")
    assert view.renewable.ok is True
    other_artifact = rw.renewal_case(v2, artifact_id="art-9")         # the line's artifact comes from INITIAL
    assert rw.renew(other_artifact, v2, "SHADOW", judgment_id="jdg-renewal-2")["code"] == "RENEWAL_CASE_MISMATCH"
    second = rw.renew(rw.renewal_case(v2, created_at=AFTER_RENEWED_EXPIRY.isoformat()), v2,
                      judgment_id="jdg-renewal-2")
    assert second["status"] == "RECORDED"
    assert [j.judgment_id for j in rw.judgments.renewals_of(v2)] == ["jdg-renewal-2"]


# Fix round 1: a miss behind a sealed version is tampering, never a business refusal

@pytest.mark.parametrize("delete,code", [
    ("DELETE FROM backtest_judgments WHERE judgment_id = 'jdg-00000001'", "JUDGMENT_TAMPERED"),
    ("DELETE FROM ai_deployments", "DEPLOYMENT_VERSION_TAMPERED"),
])
def test_a_deleted_record_behind_the_prior_version_fails_the_judgment_loudly(tmp_path, delete, code):
    rw, v1 = expired_line(tmp_path)
    case = rw.renewal_case(v1)
    rw.base.db.execute(delete)
    with pytest.raises(JudgmentRefused) as exc:
        rw.renew(case, v1, "SHADOW")
    assert exc.value.code == code
    assert rw.base.db.execute("SELECT COUNT(*) FROM backtest_judgments WHERE kind = 'RENEWAL'", fetch="one")[0] == 0


def test_a_deleted_chain_prior_is_tampered_for_the_evidence_and_the_registration(tmp_path):
    rw, v1, v2 = renewed_line(tmp_path)
    assert rw.renew(rw.renewal_case(v2, created_at=AFTER_RENEWED_EXPIRY.isoformat()), v2,
                    judgment_id="jdg-renewal-2")["status"] == "RECORDED"
    rw.base.db.execute("DELETE FROM ai_deployment_versions WHERE digest = ?", [v1])
    with pytest.raises(ForwardEvidenceRefused) as read:
        rw.forward.read(v2)
    assert read.value.code == "DEPLOYMENT_VERSION_TAMPERED"
    with pytest.raises(DeploymentRefused) as registered:
        register_renewal(rw, "jdg-renewal-2")
    assert registered.value.code == "DEPLOYMENT_VERSION_TAMPERED"


def test_a_deleted_initial_judgment_is_tampered_for_the_registration(tmp_path):
    rw, v1 = expired_line(tmp_path)
    assert rw.renew(rw.renewal_case(v1), v1)["status"] == "RECORDED"
    rw.base.db.execute("DELETE FROM backtest_judgments WHERE judgment_id = 'jdg-00000001'")
    with pytest.raises(DeploymentRefused) as exc:
        register_renewal(rw)
    assert exc.value.code == "JUDGMENT_TAMPERED"
    assert rw.base.db.execute("SELECT COUNT(*) FROM ai_deployment_versions", fetch="one")[0] == 1


# Fix round 2 (PR #100 blocker 2): the judgment is recorded on the forward evidence the case was signed on

def test_a_deploy_on_forward_evidence_that_changed_after_signing_is_refused(tmp_path):
    """Signed with a CLOSED trip of +12.50; a broker correction makes it -12000 before Jev's DEPLOY."""
    rw, v1 = expired_line(tmp_path)
    rw.paper_trip(v1, "rt-1", net_pnl=12.5)
    case = rw.renewal_case(v1)
    rw.base.db.execute("UPDATE broker_fills SET quantity = 200 WHERE exec_id IN ('x-rt-1-in', 'x-rt-1-out')")
    rw.base.db.execute("UPDATE broker_fills SET price = 40.005 WHERE exec_id = 'x-rt-1-out'")
    rw.refresh_trips()
    assert [t["net_pnl_usd"] for t in rw.forward.read(v1)["trips"]] == [-12000.0]
    refused = rw.renew(case, v1)
    assert (refused["status"], refused["code"]) == ("REFUSED", "FORWARD_EVIDENCE_CHANGED")
    assert rw.judgments.renewals_of(v1) == ()
    with pytest.raises(DeploymentRefused):
        register_renewal(rw)
    assert rw.base.db.execute("SELECT COUNT(*) FROM ai_deployment_versions", fetch="one")[0] == 1
    assert rw.renew(case, v1, "SHADOW")["status"] == "RECORDED"                 # Jev may still end the line
    assert rw.activity.status(v1) == "JUDGMENT_ENDED"


def test_a_deploy_on_an_edited_trip_projection_fails_loudly(tmp_path):
    """The reviewer's exact trace: only the stored projection changes. That is tampering (blocker 1), loud."""
    rw, v1 = expired_line(tmp_path)
    rw.paper_trip(v1, "rt-1", net_pnl=12.5)
    case = rw.renewal_case(v1)
    rw.base.db.execute("UPDATE round_trips SET net_pnl_usd = -12000.0")
    with pytest.raises(JudgmentRefused) as exc:
        rw.renew(case, v1)
    assert exc.value.code == "FORWARD_EVIDENCE_TAMPERED"
    assert rw.base.db.execute("SELECT COUNT(*) FROM ai_deployment_versions", fetch="one")[0] == 1


def test_a_case_without_a_forward_evidence_digest_cannot_deploy(tmp_path):
    rw, v1 = expired_line(tmp_path)
    case = rw.renewal_case(v1, evidence={"note": "unbound"})
    assert rw.renew(case, v1)["code"] == "FORWARD_EVIDENCE_CHANGED"

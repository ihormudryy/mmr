"""SP2c Plan 5 Task 4: the trader serves the real renewal ports (no default refuses any more)."""
from __future__ import annotations

import pytest

from tests.automation.judged_deployment import install_seeded_judgments, seed_judged_deployment
from tests.sp1_acceptance.conftest import loop_thread  # noqa: F401
from tests.sp1_fixtures import served_stack
from trader.acceptance.scenario import AcceptanceSettings, deployment_record
from trader.automation.deployment_renewal import TraderRenewalChecks, VersionForwardEvidence
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.typed_rpc import TypedRpcRemoteError

ZERO = "sha256:" + "0" * 64


@pytest.fixture
def served(tmp_path, loop_thread, monkeypatch):
    stack = served_stack(tmp_path, loop_thread, monkeypatch)
    yield stack
    stack.close()


@pytest.fixture
def seeded_served(tmp_path, loop_thread, monkeypatch):
    seeded = install_seeded_judgments(monkeypatch)
    stack = served_stack(tmp_path, loop_thread, monkeypatch)
    stack.seeded = seeded
    yield stack
    stack.close()


def test_the_trader_wires_the_real_renewal_ports(served):
    ai_paper = served.composed.stack.ai_paper
    assert isinstance(ai_paper.forward_evidence, VersionForwardEvidence)
    assert isinstance(ai_paper.judgments._renewals, TraderRenewalChecks)
    assert ai_paper.forward_evidence._versions is ai_paper.versions is ai_paper.registrar._versions
    assert 125 in SchemaMigrator(served.trader.journal_db).applied_versions()


def test_forward_evidence_is_served_to_research_only(served):
    reply = served.call("research", "get_deployment_forward_evidence", {"deployment_version": ZERO})
    assert (reply["status"], reply["code"], reply["evidence"]) == ("REFUSED", "DEPLOYMENT_VERSION_UNKNOWN", None)
    for principal in ("ai_research", "cli"):
        with pytest.raises(TypedRpcRemoteError) as exc:
            served.call(principal, "get_deployment_forward_evidence", {"deployment_version": ZERO})
        assert exc.value.code == "PERMISSION_DENIED"


def test_the_real_source_reads_the_judgment_store(seeded_served):
    """A seeded version has no recorded judgment. The real source looks in the store and names it loudly:
    a sealed version without its judgment is tampering (Task 3), an RPC error, never a REFUSED body."""
    record = deployment_record(AcceptanceSettings(run_id="r", account_id="DU1", strategy_bytes=b"x"))
    _, version = seed_judged_deployment(seeded_served.composed.stack.ai_paper, seeded_served.seeded, record,
                                        today=seeded_served.now().date())
    with pytest.raises(TypedRpcRemoteError) as exc:
        seeded_served.call("research", "get_deployment_forward_evidence", {"deployment_version": version})
    assert exc.value.code == "JUDGMENT_TAMPERED"

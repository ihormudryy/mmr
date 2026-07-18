"""P1 Task 5: asymmetric pause/resume command authority."""
import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.proposal_repository import (
    ProposalRepository,
    apply_proposal_authority_migration,
)
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import register_command_authority
from trader.messaging.typed_rpc import TypedRpcRegistry
from trader.trading.command_coordinator import (
    CommandAudit,
    CommandLedger,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
    canonical_request_hash,
)
from trader.trading.trading_control import (
    TradingControlStore,
    apply_trading_control_migration,
)


NOW = dt.datetime(2026, 7, 18, 14, 0, tzinfo=dt.timezone.utc)


class RecordingNonces:
    def __init__(self):
        self.calls = []
        self.issued = {}

    def issue_with_expiry(self, **binding):
        nonce = f"nonce-{len(self.issued) + 1}"
        self.issued[nonce] = binding
        return nonce, NOW + dt.timedelta(seconds=120)

    def consume_in_tx(self, conn, nonce, request):
        self.calls.append((nonce, request.action))
        binding = self.issued.get(nonce)
        return bool(binding) and all((
            binding["command_id"] == request.command_id,
            binding["account_id"] == request.account_id,
            binding["session_fingerprint"] == request.session_fingerprint,
            binding["request_hash"] == canonical_request_hash(request),
        ))


@pytest.fixture()
def authority(tmp_path):
    db = DuckDBConnection.get_instance(str(tmp_path / "journal.duckdb"))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_proposal_authority_migration(migrator)
    apply_command_ledger_migration(migrator)
    apply_trading_control_migration(migrator)
    repository = ProposalRepository(journal)

    def build(*, account_id="DU111111", mode="paper", ready=True, reconciled=True):
        controls = TradingControlStore(journal)
        db.transaction(lambda conn: controls.seed_in_tx(conn, [(account_id, mode)], NOW))
        nonces = RecordingNonces()
        coordinator = TradingCommandCoordinator(
            journal=journal,
            ledger=CommandLedger(journal),
            audit=CommandAudit(journal),
            nonces=nonces,
            now=lambda: NOW,
        )
        registry = TypedRpcRegistry()
        register_command_authority(
            registry,
            coordinator,
            SimpleNamespace(),
            repository,
            account_id=account_id,
            account_mode=mode,
            controls=controls,
            preflight_nonces=nonces,
            resume_ready=lambda: ready,
            reconciliation_complete=lambda command_id: reconciled,
        )
        return SimpleNamespace(
            registry=registry, controls=controls, coordinator=coordinator, nonces=nonces,
        )

    return build


def _invoke(registry, method, body):
    registration = registry.resolve("command", method)
    assert registration is not None
    parsed = registration.request_model.model_validate(body)
    return registration.handler(parsed)


def test_split_methods_replace_legacy_and_pause_is_idempotent(authority):
    a = authority()
    assert a.registry.contains("command", "pause_trading")
    assert a.registry.contains("command", "resume_trading")
    assert not a.registry.contains("command", "set_trading_pause")

    first = _invoke(a.registry, "pause_trading", {
        "command_id": "pause-1", "reason": "operator halt",
    })
    second = _invoke(a.registry, "pause_trading", {
        "command_id": "pause-2", "reason": "operator halt again",
    })

    assert first["state"] == second["state"] == "RESOLVED"
    assert a.controls.get("DU111111").revision == 2
    assert a.nonces.calls == []


@pytest.mark.parametrize("extra", [
    {"account_id": "U-ATTACKER"},
    {"is_live": False},
    {"paused": False},
])
def test_pause_rejects_caller_owned_authority_fields(authority, extra):
    a = authority()
    registration = a.registry.resolve("command", "pause_trading")
    with pytest.raises(ValidationError):
        registration.request_model.model_validate({
            "command_id": "pause-bad", "reason": "halt", **extra,
        })


def test_pause_requires_nonblank_reason(authority):
    a = authority()
    registration = a.registry.resolve("command", "pause_trading")
    with pytest.raises(ValidationError):
        registration.request_model.model_validate({"command_id": "pause-bad", "reason": "  "})


def test_paper_resume_uses_exact_revision_without_preflight(authority):
    a = authority()
    _invoke(a.registry, "pause_trading", {"command_id": "pause-1", "reason": "halt"})
    revision = a.controls.get("DU111111").revision
    result = _invoke(a.registry, "resume_trading", {
        "command_id": "resume-1",
        "expected_control_revision": revision,
        "reason": "paper recovery complete",
    })
    assert result["state"] == "RESOLVED"
    assert a.controls.get("DU111111").new_exposure_paused is False
    assert a.nonces.calls == []


def test_live_resume_derives_mode_and_requires_preflight(authority):
    a = authority(account_id="U111111", mode="live")
    revision = a.controls.get("U111111").revision

    missing = _invoke(a.registry, "resume_trading", {
        "command_id": "resume-live-missing",
        "expected_control_revision": revision,
        "reason": "checks complete",
    })
    assert missing["state"] == "REJECTED"
    assert missing["error_code"] == "PREFLIGHT_REQUIRED"

    accepted = _invoke(a.registry, "resume_trading", {
        "command_id": "resume-live-ok",
        "expected_control_revision": revision,
        "reason": "checks complete",
        "preflight_nonce": _invoke(a.registry, "preflight_command", {
            "command_id": "resume-live-ok",
            "action": "resume_trading",
            "params": {"reason": "checks complete"},
            "expected_version": revision,
            "session_fingerprint": "session-fingerprint-one",
        })["nonce"],
        "session_fingerprint": "session-fingerprint-one",
    })
    assert accepted["state"] == "RESOLVED"
    assert a.controls.get("U111111").new_exposure_paused is False


@pytest.mark.parametrize("ready,reconciled,code", [
    (False, True, "TRADER_NOT_READY"),
    (True, False, "RECONCILIATION_INCOMPLETE"),
])
def test_resume_fails_closed_until_ready_and_reconciled(authority, ready, reconciled, code):
    a = authority(ready=ready, reconciled=reconciled)
    _invoke(a.registry, "pause_trading", {"command_id": "pause-1", "reason": "halt"})
    revision = a.controls.get("DU111111").revision
    result = _invoke(a.registry, "resume_trading", {
        "command_id": f"resume-{code.lower()}",
        "expected_control_revision": revision,
        "reason": "recovery attempt",
    })
    assert result["state"] == "REJECTED"
    assert result["error_code"] == code
    assert a.controls.get("DU111111").new_exposure_paused is True


def test_resume_rejects_account_and_mode_overrides(authority):
    a = authority(account_id="U111111", mode="live")
    registration = a.registry.resolve("command", "resume_trading")
    for extra in ({"account_id": "DU111111"}, {"is_live": False}, {"paused": False}):
        with pytest.raises(ValidationError):
            registration.request_model.model_validate({
                "command_id": "resume-bad",
                "expected_control_revision": 1,
                "reason": "attempted downgrade",
                "preflight_nonce": "some-nonce",
                "session_fingerprint": "session-fingerprint-one",
                **extra,
            })


def test_live_resume_nonce_is_bound_to_reason_revision_and_session(authority):
    a = authority(account_id="U111111", mode="live")
    revision = a.controls.get("U111111").revision
    ticket = _invoke(a.registry, "preflight_command", {
        "command_id": "resume-bound",
        "action": "resume_trading",
        "params": {"reason": "reviewed recovery"},
        "expected_version": revision,
        "session_fingerprint": "session-fingerprint-one",
    })
    result = _invoke(a.registry, "resume_trading", {
        "command_id": "resume-bound",
        "expected_control_revision": revision,
        "reason": "different reason",
        "preflight_nonce": ticket["nonce"],
        "session_fingerprint": "session-fingerprint-two",
    })
    assert result["state"] == "REJECTED"
    assert result["error_code"] == "PREFLIGHT_REQUIRED"
    assert a.controls.get("U111111").new_exposure_paused is True


def test_browser_uses_split_routes_and_waits_for_authoritative_command_state():
    source = Path("web/static/command_center.js").read_text()
    assert "ccSubmitCommand('pause_trading'" in source
    assert "ccSubmitCommand('resume_trading'" in source
    assert "'/api/commands/pause'" in source
    assert "'/api/commands/resume'" in source
    assert "set_trading_pause" not in source
    assert "command.updated SSE reducer" in source
    assert "202 accepted" in source

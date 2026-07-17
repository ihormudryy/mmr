"""[M1-F3] Task 7 -- strategy revisions, receipt ledger, and coordinator
forwarding.

Covers, per the task brief:
  - ``StrategyRevisionStore``: control/state revision bookkeeping, the
    idempotent command-receipt ledger, the transactional acknowledgement
    outbox (state_revision bump + outbox row commit/rollback together), the
    staged-config-swap ledger, and startup recovery.
  - ``StrategyRuntime.apply_control_command``: CAS rejection, idempotent
    retries, a successful staged YAML swap, and rollback-on-failure with the
    prior configuration restored.
  - The trader-side forwarding saga (``StrategyControlCommandService``,
    ``command_coordinator.py``): journals ``strategy.updated`` only after
    strategy_service acknowledges, degrades a lost ``forward()`` reply to
    ``OUTCOME_UNKNOWN``, rejects an unknown strategy, and (when an ownership
    port is supplied) rejects a disable that would orphan exposure.
  - Production wiring on both sides: the typed method names are registered
    on the frozen roles/sockets described in the brief.
"""
from __future__ import annotations

import datetime as dt
import os
import types
from pathlib import Path

import pytest
import yaml

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator
from trader.messaging.production_api import (
    DisableStrategyRequest,
    EnableStrategyRequest,
    RecordStateAcknowledgedRequest,
    UpdateStrategyParamsRequest,
    build_production_registry,
)
from trader.messaging.typed_rpc import TypedRpcRegistry
from trader.strategy.strategy_revisions import (
    OutboxRow,
    StrategyCommandReceipt,
    StrategyRevisionStore,
)
from trader.strategy.strategy_runtime import (
    ControlRevisionConflict,
    StartupConfigRecoveryError,
    StrategyRuntime,
    register_strategy_control_authority,
)
from trader.trading.command_coordinator import (
    CommandAudit,
    CommandLedger,
    CommandRequest,
    StrategyControlCommandService,
    TradingCommandCoordinator,
    apply_command_ledger_migration,
)
from trader.trading.strategy import StrategyState

ACCOUNT_ID = "DU111111"


# ---------------------------------------------------------------------------
# Store-level fixtures (StrategyRevisionStore against its own DuckDB file).
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path) -> DuckDBConnection:
    return DuckDBConnection(str(tmp_path / "strategy_revisions.duckdb"))


@pytest.fixture
def revisions(db) -> StrategyRevisionStore:
    store = StrategyRevisionStore(db)
    store.migrate()
    return store


# ---------------------------------------------------------------------------
# StrategyRuntime fixtures -- a minimally-wired runtime (no ZMQ/IB) with one
# real loaded strategy ("smi_crossover") backed by a test-double module that
# can be told to fail its NEXT reload exactly once (self-deleting sentinel
# file), so update_strategy_params's staged-swap rollback path is exercised
# deterministically.
# ---------------------------------------------------------------------------

_STRATEGY_BODY = '''
import os
from trader.trading.strategy import Strategy


class SMICrossOverDouble(Strategy):
    EMA_PERIOD = 20

    def install(self, context):
        sentinel = (context.params or {}).get("_fail_sentinel")
        if sentinel and os.path.exists(sentinel):
            os.remove(sentinel)  # fail exactly once -- a restore retry must succeed
            raise RuntimeError("synthetic reload failure")
        return super().install(context)

    def on_prices(self, prices):
        return None
'''


@pytest.fixture
def sentinel_path(tmp_path) -> Path:
    return tmp_path / "FAIL_NEXT_RELOAD"


@pytest.fixture
def config_path(tmp_path, sentinel_path) -> Path:
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "smi_crossover.py").write_text(_STRATEGY_BODY)

    cfg_path = tmp_path / "strategy_runtime.yaml"
    cfg = {
        "strategies": [
            {
                "name": "smi_crossover",
                "module": str(strategies_dir / "smi_crossover.py"),
                "class_name": "SMICrossOverDouble",
                "bar_size": "1 min",
                "conids": [265598],
                "historical_days_prior": 0,
                "params": {"_fail_sentinel": str(sentinel_path)},
            }
        ]
    }
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return cfg_path


@pytest.fixture
def runtime(tmp_path, config_path, sentinel_path) -> StrategyRuntime:
    rt = StrategyRuntime.__new__(StrategyRuntime)  # skip __init__
    rt.strategies_directory = str(tmp_path / "strategies")
    rt.strategy_config_file = str(config_path)
    rt.strategy_implementations = []
    rt.strategies = {}
    rt.streams = {}
    rt.storage = None  # type: ignore
    rt.universe_accessor = None  # type: ignore
    rt._config_mtime = 0.0
    rt._last_dispatched_bar = {}
    rt.trader_client = None  # type: ignore
    rt.paper_trading = True
    rt.duckdb_path = str(tmp_path / "strategy.duckdb")
    rt._revisions = StrategyRevisionStore(DuckDBConnection.get_instance(rt.duckdb_path))
    rt._revisions.migrate()

    rt.config_loader(rt.strategy_config_file)
    assert rt.get_strategy("smi_crossover") is not None, "fixture strategy failed to load"

    rt.fail_next_reload = types.MethodType(lambda self: sentinel_path.touch(), rt)
    return rt


# ---------------------------------------------------------------------------
# Step 1 (brief, verbatim).
# ---------------------------------------------------------------------------

def test_control_revision_cas_rejects_stale_expected_version(runtime):
    current = runtime._revisions.control_revision("smi_crossover")
    with pytest.raises(ControlRevisionConflict):
        runtime.apply_control_command(
            "cmd-1", "smi_crossover", "disable_strategy",
            expected_control_revision=current - 1, params=None)
    assert runtime.get_strategy("smi_crossover").state != StrategyState.DISABLED


def test_receipt_ledger_makes_forwarded_retries_idempotent(runtime):
    current = runtime._revisions.control_revision("smi_crossover")
    first = runtime.apply_control_command(
        "cmd-1", "smi_crossover", "disable_strategy",
        expected_control_revision=current, params=None)
    assert first.state == "COMMITTED"
    retry = runtime.apply_control_command(
        "cmd-1", "smi_crossover", "disable_strategy",
        expected_control_revision=current, params=None)   # stale revision on purpose
    assert retry == first                                 # recorded receipt, mutation NOT repeated
    assert runtime._revisions.control_revision("smi_crossover") == current + 1


def test_state_revision_and_outbox_commit_together(revisions, db):
    def _bump(conn):
        rev = revisions.bump_state_revision_in_tx(conn, "smi_crossover", {"state": "RUNNING"})
        raise RuntimeError("crash before commit")
    with pytest.raises(RuntimeError):
        db.transaction(_bump)
    assert revisions.unacknowledged_outbox(10) == []      # neither row survived


def test_param_update_stages_prepared_then_committed(runtime, config_path):
    current = runtime._revisions.control_revision("smi_crossover")
    receipt = runtime.apply_control_command(
        "cmd-1", "smi_crossover", "update_strategy_params",
        expected_control_revision=current, params={"EMA_PERIOD": 15})
    assert receipt.state == "COMMITTED"
    states = runtime._revisions.config_revision_states("smi_crossover")
    assert states[-1] == "COMMITTED"
    assert yaml.safe_load(config_path.read_text())["strategies"][0]["params"]["EMA_PERIOD"] == 15


def test_failed_swap_restores_prior_config_and_marks_rolled_back(runtime, config_path):
    prior = config_path.read_text()
    runtime.fail_next_reload()                            # instantiation of the replacement fails
    current = runtime._revisions.control_revision("smi_crossover")
    receipt = runtime.apply_control_command(
        "cmd-1", "smi_crossover", "update_strategy_params",
        expected_control_revision=current, params={"EMA_PERIOD": 15})
    assert receipt.state == "ROLLED_BACK" and receipt.error
    assert config_path.read_text() == prior               # prior configuration restored
    assert runtime._revisions.control_revision("smi_crossover") == current   # no revision minted


def test_double_failure_swap_and_restore_reports_rollback_failed(runtime, config_path):
    """[Fix 3] If the staged swap fails AND ``_restore_runtime`` ALSO fails,
    the strategy is left UNLOADED with nothing replacing it. That must be a
    DISTINCT terminal receipt (ROLLBACK_FAILED) carrying the restore error --
    NOT a plain ROLLED_BACK that falsely implies the prior state was
    restored."""
    runtime.fail_next_reload()                            # the staged swap fails -> strategy unloaded

    def _failing_restore(strategy_name, prior_entry):     # the restore ALSO fails
        return "synthetic restore failure"                # never reloads -> strategy stays None
    runtime._restore_runtime = _failing_restore

    current = runtime._revisions.control_revision("smi_crossover")
    receipt = runtime.apply_control_command(
        "cmd-double", "smi_crossover", "update_strategy_params",
        expected_control_revision=current, params={"EMA_PERIOD": 15})

    assert receipt.state == "ROLLBACK_FAILED"
    assert receipt.error and "restore" in receipt.error.lower()
    assert runtime.get_strategy("smi_crossover") is None  # genuinely unloaded
    assert runtime._revisions.control_revision("smi_crossover") == current  # no revision minted


def test_restart_recovers_prepared_to_last_committed(revisions, config_path):
    rid = revisions.prepare_config_revision(
        "smi_crossover", expected_control_revision=3,
        prior={"params": {"EMA_PERIOD": 20}}, proposed={"params": {"EMA_PERIOD": 15}},
        command_id="cmd-crash")
    recovered = revisions.recover_on_startup()
    assert recovered == [rid]
    assert revisions.config_revision_state(rid) == "ROLLED_BACK"


def test_startup_recovery_rewrites_live_yaml_back_to_prior_config(runtime, config_path):
    """[Fix 1a] A staged config revision left PREPARED by a crash AFTER
    ``os.replace`` (the live YAML already holds the PROPOSED params) must be
    REWRITTEN back to ``prior_config`` on startup recovery -- not merely
    flipped to ROLLED_BACK in the DB while the live YAML keeps the proposed
    values. The JSON columns come back from DuckDB as ``str``; without a
    ``json.loads`` on read, ``_stage_yaml`` gets a str and blows up, the
    swallow path leaves the YAML at proposed, yet the DB reports 'recovered'."""
    cfg = yaml.safe_load(config_path.read_text())
    prior_entry = dict(cfg["strategies"][0])
    proposed_entry = dict(prior_entry)
    proposed_entry["params"] = dict(prior_entry.get("params") or {}, EMA_PERIOD=15)

    # Post-os.replace crash state: the live YAML already holds PROPOSED.
    crashed = dict(cfg, strategies=[proposed_entry])
    config_path.write_text(yaml.safe_dump(crashed, sort_keys=False))
    assert yaml.safe_load(config_path.read_text())["strategies"][0]["params"]["EMA_PERIOD"] == 15

    rid = runtime._revisions.prepare_config_revision(
        "smi_crossover",
        expected_control_revision=runtime._revisions.control_revision("smi_crossover"),
        prior=prior_entry, proposed=proposed_entry, command_id="cmd-crash")

    recovered = runtime.recover_startup_config()

    assert recovered == [rid]
    assert runtime._revisions.config_revision_state(rid) == "ROLLED_BACK"
    live_params = yaml.safe_load(config_path.read_text())["strategies"][0].get("params") or {}
    assert "EMA_PERIOD" not in live_params      # live YAML rewritten back to prior_config


def test_startup_recovery_fails_loud_and_retains_prepared_when_restore_raises(runtime, config_path):
    """[Fix 1] A genuinely failed restore must NOT be downgraded to a log
    line while the revision is reported as recovered. It must fail loud
    (raise) AND leave the revision PREPARED -- flipping it to ROLLED_BACK
    before a confirmed restore would both falsify the audit trail and lose
    the retry, so the NEXT startup would find nothing to recover and silently
    come up on the un-restored (proposed) config."""
    rid = runtime._revisions.prepare_config_revision(
        "smi_crossover",
        expected_control_revision=runtime._revisions.control_revision("smi_crossover"),
        prior={"name": "smi_crossover", "params": {"EMA_PERIOD": 20}},
        proposed={"name": "smi_crossover", "params": {"EMA_PERIOD": 15}},
        command_id="cmd-crash")

    def _boom(entry):
        raise RuntimeError("disk full")
    runtime._stage_yaml = _boom     # the YAML restore genuinely fails

    with pytest.raises(StartupConfigRecoveryError):
        runtime.recover_startup_config()

    # Left PREPARED for retry on the next startup -- NOT prematurely flipped.
    assert runtime._revisions.config_revision_state(rid) == "PREPARED"


# ---------------------------------------------------------------------------
# Trader-side forwarding saga fixtures + fakes.
# ---------------------------------------------------------------------------

class FakeStrategySnapshot:
    def __init__(self, names):
        self._names = set(names)

    def exists(self, name):
        return name in self._names


class FakeOwnership:
    def __init__(self, resolved: bool):
        self._resolved = resolved

    def remaining_owner_resolved(self, strategy_name):
        return self._resolved


class FakeReconciler:
    def __init__(self):
        self.scheduled = []

    def schedule(self, command_id, now):
        self.scheduled.append(command_id)


class FakeNonceGate:
    def consume_in_tx(self, conn, nonce, request):
        return True


class FakeStrategyControlPort:
    """Test double for ``StrategyControlPort``. By default returns a canned
    COMMITTED receipt (control_revision bumped by 1, state_revision 1 --
    mirroring a fresh strategy_service-side row); tests can override via
    ``canned``/``raise_on_forward``."""

    def __init__(self):
        self.calls: list[CommandRequest] = []
        self._raise = None
        self._receipts: dict[str, StrategyCommandReceipt] = {}

    def raise_on_forward(self, exc: Exception) -> None:
        self._raise = exc

    def canned(self, command_id: str, receipt: StrategyCommandReceipt) -> None:
        self._receipts[command_id] = receipt

    def forward(self, request: CommandRequest) -> StrategyCommandReceipt:
        self.calls.append(request)
        if self._raise is not None:
            exc, self._raise = self._raise, None
            raise exc
        receipt = self._receipts.get(request.command_id)
        if receipt is None:
            receipt = StrategyCommandReceipt(
                command_id=request.command_id,
                strategy_name=request.body["strategy_name"],
                action=request.action,
                state="COMMITTED",
                control_revision=(request.expected_version or 0) + 1,
                state_revision=1,
                error=None,
            )
        return receipt

    def get_receipt(self, command_id: str):
        return self._receipts.get(command_id)


def _build_journal(tmp_path):
    db_ = DuckDBConnection(str(tmp_path / "journal.duckdb"))
    journal = DomainJournal(db_)
    migrator = SchemaMigrator(db_)
    journal.migrate(migrator)
    apply_command_ledger_migration(migrator)
    return db_, journal


class Forwarding:
    def __init__(self, coordinator, journal, port, ledger, reconciler):
        self.coordinator = coordinator
        self.journal = journal
        self.port = port
        self.ledger = ledger
        self.reconciler = reconciler


def _make_forwarding(tmp_path, *, ownership=None, strategy_names=("smi_crossover",)):
    _, journal = _build_journal(tmp_path)
    ledger = CommandLedger(journal)
    audit = CommandAudit(journal)
    coordinator = TradingCommandCoordinator(
        journal=journal, ledger=ledger, audit=audit, nonces=FakeNonceGate())
    port = FakeStrategyControlPort()
    reconciler = FakeReconciler()
    service = StrategyControlCommandService(
        journal=journal, ledger=ledger, port=port,
        snapshot=FakeStrategySnapshot(strategy_names), reconciler=reconciler,
        ownership=ownership,
    )
    coordinator.register_action("enable_strategy", service.enable_strategy, requires_preflight=False, saga=True)
    coordinator.register_action("disable_strategy", service.disable_strategy, requires_preflight=False, saga=True)
    coordinator.register_action(
        "update_strategy_params", service.update_strategy_params, requires_preflight=False, saga=True)
    return Forwarding(coordinator, journal, port, ledger, reconciler)


@pytest.fixture
def forwarding(tmp_path):
    return _make_forwarding(tmp_path)


def _strategy_request(command_id, action, *, expected_version, strategy_name="smi_crossover", params=None):
    body = {"strategy_name": strategy_name}
    if params is not None:
        body["params"] = params
    return CommandRequest(
        command_id=command_id, action=action, account_id=ACCOUNT_ID,
        target_type="strategy", target_id=strategy_name, expected_version=expected_version,
        body=body, source="test",
    )


def test_trader_journals_strategy_updated_only_after_acknowledgement(forwarding):
    receipt = forwarding.coordinator.execute(_strategy_request(
        "cmd-1", "enable_strategy", expected_version=4))
    assert receipt.state == "RESOLVED"
    kinds = [e.event_type for e in forwarding.journal.read_after(0, 100)]
    assert "strategy.updated" in kinds
    strategy_events = [e for e in forwarding.journal.read_after(0, 100)
                       if e.event_type == "strategy.updated"]
    assert strategy_events[0].entity_revision == receipt.outcome["state_revision"]


class TestForwardingSagaEdgeCases:
    def test_forward_timeout_degrades_to_outcome_unknown_and_schedules_reconcile(self, forwarding):
        forwarding.port.raise_on_forward(TimeoutError("no reply"))
        receipt = forwarding.coordinator.execute(_strategy_request(
            "cmd-2", "disable_strategy", expected_version=0))
        assert receipt.state == "OUTCOME_UNKNOWN"
        assert receipt.error_code == "DISPATCH_AMBIGUOUS"
        assert "cmd-2" in forwarding.reconciler.scheduled
        # No strategy.updated is journaled on an ambiguous outcome.
        kinds = [e.event_type for e in forwarding.journal.read_after(0, 100)]
        assert "strategy.updated" not in kinds

    def test_forward_deterministic_conflict_rejects_not_ambiguous(self, forwarding):
        """[Fix 2] A DETERMINISTIC strategy-side rejection surfaced as a
        declared-code ``TypedRpcRemoteError`` (e.g. a stale-CAS
        CONTROL_REVISION_CONFLICT raised BEFORE any strategy-side mutation) is
        a clean REJECT, not an ambiguous dispatch: REJECTED with that code,
        retryable=True (operator re-issues with a fresh control_revision), and
        the reconciler is NOT scheduled (ambiguity is reserved for
        Timeout/Connection errors that leave the true state unknowable)."""
        from trader.messaging.typed_rpc import TypedRpcRemoteError

        forwarding.port.raise_on_forward(
            TypedRpcRemoteError("CONTROL_REVISION_CONFLICT", "stale control_revision"))
        receipt = forwarding.coordinator.execute(_strategy_request(
            "cmd-conflict", "update_strategy_params", expected_version=0,
            params={"EMA_PERIOD": 15}))

        assert receipt.state == "REJECTED"
        assert receipt.error_code == "CONTROL_REVISION_CONFLICT"
        assert receipt.retryable is True
        assert forwarding.reconciler.scheduled == []      # NOT reconciled
        # No strategy.updated on a rejected dispatch.
        kinds = [e.event_type for e in forwarding.journal.read_after(0, 100)]
        assert "strategy.updated" not in kinds

    def test_forward_rejects_unknown_strategy(self, forwarding):
        receipt = forwarding.coordinator.execute(_strategy_request(
            "cmd-3", "enable_strategy", expected_version=0, strategy_name="ghost"))
        assert receipt.state == "REJECTED"
        assert receipt.error_code == "STRATEGY_NOT_FOUND"
        assert forwarding.port.calls == []      # never forwarded

    def test_rolled_back_strategy_outcome_resolves_not_rejected(self, tmp_path):
        forwarding = _make_forwarding(tmp_path)
        forwarding.port.canned("cmd-4", StrategyCommandReceipt(
            command_id="cmd-4", strategy_name="smi_crossover", action="update_strategy_params",
            state="ROLLED_BACK", control_revision=0, state_revision=0,
            error="synthetic reload failure",
        ))
        receipt = forwarding.coordinator.execute(_strategy_request(
            "cmd-4", "update_strategy_params", expected_version=0, params={"EMA_PERIOD": 15}))
        assert receipt.state == "RESOLVED"
        assert receipt.outcome["error"] == "synthetic reload failure"

    def test_disable_rejected_when_exposure_ownership_unresolved(self, tmp_path):
        forwarding = _make_forwarding(tmp_path, ownership=FakeOwnership(resolved=False))
        receipt = forwarding.coordinator.execute(_strategy_request(
            "cmd-5", "disable_strategy", expected_version=0))
        assert receipt.state == "REJECTED"
        assert receipt.error_code == "EXPOSURE_OWNERSHIP_UNRESOLVED"
        assert forwarding.port.calls == []

    def test_disable_allowed_when_exposure_ownership_resolved(self, tmp_path):
        forwarding = _make_forwarding(tmp_path, ownership=FakeOwnership(resolved=True))
        receipt = forwarding.coordinator.execute(_strategy_request(
            "cmd-6", "disable_strategy", expected_version=0))
        assert receipt.state == "RESOLVED"
        assert len(forwarding.port.calls) == 1

    def test_acknowledge_state_idempotent_on_replayed_state_revision(self, tmp_path):
        """A later re-delivery of the SAME state_revision (e.g. strategy_service's
        outbox-drain backstop re-acknowledging a revision the happy path already
        journaled) must not raise -- it should recognize the revision is already
        durably recorded and return the same entity_revision."""
        _, journal = _build_journal(tmp_path)
        ledger = CommandLedger(journal)
        service = StrategyControlCommandService(
            journal=journal, ledger=ledger, port=FakeStrategyControlPort(),
            snapshot=FakeStrategySnapshot(["smi_crossover"]), reconciler=FakeReconciler(),
        )
        payload = {"strategy_name": "smi_crossover", "state": "RUNNING"}
        first = service.acknowledge_state(
            "smi_crossover", 1, 1, payload, correlation_id="corr-1")
        second = service.acknowledge_state(
            "smi_crossover", 1, 1, payload, correlation_id="corr-1")
        assert first == second


# ---------------------------------------------------------------------------
# Production wiring -- both sides register the frozen typed method names.
# ---------------------------------------------------------------------------

class _FakeTrader:
    def __init__(self):
        self.ib_account = ACCOUNT_ID

    def status(self):
        return {}


class TestTraderSideRegistration:
    def test_enable_disable_update_registered_on_command_role_only(self, tmp_path):
        from trader.trading.proposal_command_service import ProposalCommandService

        db_, journal = _build_journal(tmp_path)
        ledger = CommandLedger(journal)
        audit = CommandAudit(journal)
        coordinator = TradingCommandCoordinator(
            journal=journal, ledger=ledger, audit=audit, nonces=FakeNonceGate())
        from trader.data.proposal_repository import ProposalRepository
        repo = ProposalRepository(journal)

        class _StubProposalService:
            def create_proposal(self, *a, **k):
                raise NotImplementedError

            def reject_proposal(self, *a, **k):
                raise NotImplementedError

        strategy_service = StrategyControlCommandService(
            journal=journal, ledger=ledger, port=FakeStrategyControlPort(),
            snapshot=FakeStrategySnapshot(["smi_crossover"]), reconciler=FakeReconciler(),
        )
        registry = build_production_registry(
            _FakeTrader(), _hmac_authenticator(),
            command_coordinator=coordinator, proposal_service=_StubProposalService(),
            proposal_repository=repo, strategy_control_service=strategy_service,
        )
        for method in ("enable_strategy", "disable_strategy", "update_strategy_params",
                       "record_state_acknowledged"):
            assert registry.contains("command", method), method
            assert not registry.contains("query", method), method

    def test_colon_bearing_command_id_rejected_at_request_model(self):
        with pytest.raises(Exception):
            EnableStrategyRequest(command_id="bad:id", strategy_name="x", expected_control_revision=0)
        with pytest.raises(Exception):
            DisableStrategyRequest(command_id="bad:id", strategy_name="x", expected_control_revision=0)
        with pytest.raises(Exception):
            UpdateStrategyParamsRequest(command_id="bad:id", strategy_name="x", expected_control_revision=0)

    def test_record_state_acknowledged_has_no_command_id_field(self):
        """Internal ack, not routed through the coordinator ledger -- no
        command_id/preflight_nonce field at all."""
        fields = RecordStateAcknowledgedRequest.model_fields
        assert "command_id" not in fields
        assert "preflight_nonce" not in fields


def _hmac_authenticator():
    from trader.messaging.typed_rpc import HmacServiceAuthenticator
    return HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)


class TestStrategyServiceSideRegistration:
    def test_registers_frozen_method_names(self, runtime):
        command_registry = TypedRpcRegistry()
        query_registry = TypedRpcRegistry()
        register_strategy_control_authority(command_registry, query_registry, runtime)

        for method in ("enable_strategy", "disable_strategy", "update_strategy_params"):
            assert command_registry.contains("command", method)
        assert query_registry.contains("query", "get_strategy_receipt")

    def test_enable_strategy_handler_round_trips_through_apply_control_command(self, runtime):
        command_registry = TypedRpcRegistry()
        query_registry = TypedRpcRegistry()
        register_strategy_control_authority(command_registry, query_registry, runtime)

        current = runtime._revisions.control_revision("smi_crossover")
        registration = command_registry.resolve("command", "enable_strategy")
        parsed = registration.request_model(
            command_id="wire-1", strategy_name="smi_crossover",
            expected_control_revision=current,
        )
        result = registration.handler(parsed)
        assert result["state"] == "COMMITTED"

        receipt_registration = query_registry.resolve("query", "get_strategy_receipt")
        receipt_parsed = receipt_registration.request_model(command_id="wire-1")
        receipt = receipt_registration.handler(receipt_parsed)
        assert receipt["command_id"] == "wire-1"

    def test_stale_control_revision_surfaces_as_dispatch_problem(self, runtime):
        from trader.messaging.typed_rpc import _DispatchProblem

        command_registry = TypedRpcRegistry()
        query_registry = TypedRpcRegistry()
        register_strategy_control_authority(command_registry, query_registry, runtime)

        registration = command_registry.resolve("command", "disable_strategy")
        parsed = registration.request_model(
            command_id="wire-2", strategy_name="smi_crossover",
            expected_control_revision=999,
        )
        with pytest.raises(_DispatchProblem) as exc:
            registration.handler(parsed)
        assert exc.value.code == "CONTROL_REVISION_CONFLICT"

    def test_get_strategy_receipt_unknown_command_raises_command_not_found(self, runtime):
        from trader.messaging.typed_rpc import _DispatchProblem

        command_registry = TypedRpcRegistry()
        query_registry = TypedRpcRegistry()
        register_strategy_control_authority(command_registry, query_registry, runtime)

        registration = query_registry.resolve("query", "get_strategy_receipt")
        parsed = registration.request_model(command_id="never-happened")
        with pytest.raises(_DispatchProblem) as exc:
            registration.handler(parsed)
        assert exc.value.code == "COMMAND_NOT_FOUND"


# ---------------------------------------------------------------------------
# Misc store-level coverage.
# ---------------------------------------------------------------------------

class TestStoreMisc:
    def test_get_receipt_returns_none_when_missing(self, revisions):
        assert revisions.get_receipt("never-happened") is None

    def test_unacknowledged_outbox_and_mark_acknowledged(self, revisions, db):
        def _bump(conn):
            revisions.bump_state_revision_in_tx(conn, "smi_crossover", {"state": "RUNNING"})
        db.transaction(_bump)
        rows = revisions.unacknowledged_outbox(10)
        assert len(rows) == 1
        assert isinstance(rows[0], OutboxRow)
        assert rows[0].strategy_name == "smi_crossover"
        revisions.mark_acknowledged(rows[0].ack_id)
        assert revisions.unacknowledged_outbox(10) == []

    def test_unacknowledged_outbox_payload_is_a_dict_not_str(self, revisions, db):
        """[Fix 1b] The ``payload`` JSON column comes back from DuckDB as a
        ``str``; the ack-drain feeds it straight into
        ``RecordStateAcknowledgedRequest.payload: Dict[str, Any]``, so it MUST
        be re-parsed to a dict on read or the whole ack backstop is broken."""
        payload = {"strategy_name": "smi_crossover", "state": "RUNNING", "control_revision": 1}

        def _bump(conn):
            revisions.bump_state_revision_in_tx(conn, "smi_crossover", payload)
        db.transaction(_bump)

        rows = revisions.unacknowledged_outbox(10)
        assert len(rows) == 1
        assert isinstance(rows[0].payload, dict)
        assert rows[0].payload == payload

    def test_apply_control_command_rejects_unknown_action(self, runtime):
        current = runtime._revisions.control_revision("smi_crossover")
        with pytest.raises(ValueError):
            runtime.apply_control_command(
                "cmd-x", "smi_crossover", "reticulate_splines",
                expected_control_revision=current, params=None)

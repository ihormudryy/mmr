"""Experiment start, pause, resume and stop (SP1 Plan 4 Task 3, spec 5.5).

Four single-step coordinator actions. Each returns the experiment view or
raises ``CommandValidationError(code, message)``. Every arming check that
cannot be proven refuses with its own code (K12). ``KILLED`` is never resumed
(K10): after a kill the operator stops a flat account and starts a new
experiment. Only operators (``cli``, ``dashboard``) start, resume and stop;
``ai_supervisor`` may also pause (K14).
"""
from __future__ import annotations

import datetime as dt
import logging
import math
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping, Optional, Protocol, Union

from trader.automation.experiments import (
    ExperimentRecord, ExperimentRefused, ExperimentStore, experiment_id_for, is_experiment_id,
)
from trader.automation.kill_line import effective_kill_line
from trader.research.canonical import sha256_digest
from trader.trading.command_coordinator import CommandValidationError

logger = logging.getLogger(__name__)

HUMAN_PRINCIPALS = frozenset({"cli", "dashboard"})
PAUSE_PRINCIPALS = HUMAN_PRINCIPALS | {"ai_supervisor"}
MAX_REASON_LENGTH = 200
CONFIG_DIGEST_PREFIX = "mmr.ai-paper-config.v1"
OUTAGE_PAUSE = "BROKER_DATA_OUTAGE"
_IDENTITY_CODES = ("AI_SUPERVISOR_KEY_MISSING", "AI_ALLOW_LIST_NOT_LOADED")


class ArmingLock:
    """One per trader: experiment start/resume/stop and one-strategy arming never interleave."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def hold(self, timeout: float = 10.0) -> Iterator[None]:
        if not self._lock.acquire(timeout=timeout):
            raise ExperimentRefused("ARMING_BUSY", "another arming change is in progress")
        try:
            yield
        finally:
            self._lock.release()


@dataclass(frozen=True)
class StartFx:
    base_currency: str
    usd_per_base: float
    source: str


def start_fx_from_cash(cash: Mapping[str, Any]) -> StartFx:
    """K13: USD evidence for the start value, from the trader's own account values."""
    def refuse(detail: str) -> ExperimentRefused:
        return ExperimentRefused("START_FX_UNAVAILABLE", detail)

    if not isinstance(cash, Mapping):
        raise refuse("account cash is not readable")
    base = cash.get("base_currency")
    if type(base) is not str or len(base) != 3 or not base.isalpha() or not base.isupper():
        raise refuse(f"base currency {base!r} is not a currency code")
    if base == "USD":
        return StartFx("USD", 1.0, "base_is_usd")
    currencies = cash.get("currencies")
    usd = currencies.get("USD") if isinstance(currencies, Mapping) else None
    rate = usd.get("exchange_rate") if isinstance(usd, Mapping) else None
    if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
        raise refuse(f"no finite positive USD exchange rate for base {base}")
    return StartFx(base, 1.0 / float(rate), "ib_account_values")


@dataclass
class ArmingPorts:
    """What start / resume / stop read. Mutable so a test can swap one port."""
    broker: Any                                     # .capture(account_id) -> BrokerRiskSnapshot
    account_cash: Callable[[], Mapping]
    resume_ready: Callable[[], bool]
    # No unresolved command other than the one being executed (its own ledger row is RECEIVED).
    reconciliation_safe: Callable[[Optional[str]], bool]
    breaker_clear: Callable[[], bool]
    exit_owners: Any                                # .account_owner(account_id)
    liquidation_roots: Callable[[], list]           # non-terminal roots
    old_path_armed: Callable[[], Optional[str]]
    ai_paper_built: Callable[[], bool]


class ExperimentLockPort(Protocol):
    def blocking_state(self) -> Optional[str]: ...

    def hold(self, timeout: float = 10.0) -> Any: ...


class ExperimentLock:
    """The one-strategy side of the lock (spec 5.5 "Lock in both directions")."""

    def __init__(self, store: ExperimentStore, lock: ArmingLock):
        self._store = store
        self._lock = lock

    def blocking_state(self) -> Optional[str]:
        record = self._store.active()
        return None if record is None else record.state

    def hold(self, timeout: float = 10.0):
        return self._lock.hold(timeout)


def ai_supervisor_identity_problem(identity: Any, registry: Any) -> Optional[str]:
    """K22: the ai_supervisor key is in the keyring and the allow-list is loaded."""
    from trader.messaging.typed_rpc import ServiceIdentity
    if not isinstance(identity, ServiceIdentity) or not identity.accepts("ai_supervisor"):
        return "AI_SUPERVISOR_KEY_MISSING"
    registration = None if registry is None else registry.resolve("command", "submit_ai_paper_decision")
    if registration is None or frozenset(registration.allowed_principals) != frozenset({"ai_supervisor"}):
        return "AI_ALLOW_LIST_NOT_LOADED"
    return None


def _iso(value: Optional[dt.datetime]) -> Optional[str]:
    return None if value is None else value.isoformat()


def record_view(record: ExperimentRecord, config: Any) -> dict:
    line = effective_kill_line(record, config)
    return {
        "experiment_id": record.experiment_id, "state": record.state, "started_at": _iso(record.started_at),
        "start_net_liquidation": record.start_net_liquidation, "base_currency": record.base_currency,
        "start_usd_per_base": record.start_usd_per_base, "kill_drawdown_pct": record.kill_drawdown_pct,
        "kill_basis": record.kill_basis, "effective_kill_pct": None if line is None else line.pct,
        "peak_net_liquidation": record.peak_net_liquidation, "killed_at": _iso(record.killed_at),
        "kill_flat_state": record.kill_flat_state, "pause_cause": record.pause_cause,
        "revision": record.revision,
    }


def _body(body: object, keys: frozenset[str]) -> dict:
    if not isinstance(body, dict) or set(body) != keys:
        raise ExperimentRefused("EXPERIMENT_REQUEST_INVALID", f"body must have exactly {sorted(keys)}")
    reason = body["reason"]
    if type(reason) is not str or not 1 <= len(reason) <= MAX_REASON_LENGTH:
        raise ExperimentRefused("EXPERIMENT_REQUEST_INVALID",
                                f"reason must be 1-{MAX_REASON_LENGTH} characters")
    if "experiment_id" in keys and not is_experiment_id(body["experiment_id"]):
        raise ExperimentRefused("EXPERIMENT_REQUEST_INVALID", "experiment_id must match exp-<20 hex>")
    return body


def _refusals(fn):
    """Store and check refusals become coordinator validation errors with the same code."""
    def wrapped(self, cmd):
        try:
            return fn(self, cmd)
        except ExperimentRefused as exc:
            raise CommandValidationError(exc.code, exc.message) from exc
    wrapped.__name__ = fn.__name__
    return wrapped


class ExperimentService:
    def __init__(self, *, store: ExperimentStore, ports: ArmingPorts, lock: ArmingLock, config: Any,
                 account_id: str, account_mode: Union[str, Callable[[], str]],
                 now: Callable[[], dt.datetime]):
        self.store = store
        self.ports = ports
        self._lock = lock
        self._config = config
        self._account_id = account_id
        self._account_mode = account_mode
        self._now = now
        self._identity_check: Optional[Callable[[], Optional[str]]] = None

    @property
    def config(self) -> Any:
        return self._config

    def attach_identity_check(self, check: Callable[[], Optional[str]]) -> None:
        self._identity_check = check

    def identity_problem(self) -> Optional[str]:
        if self._identity_check is None:
            return "AI_ALLOW_LIST_NOT_LOADED"
        return self._identity_check()

    def _view(self, record: ExperimentRecord) -> dict:
        return record_view(record, self._config)

    # -- actions -------------------------------------------------------------

    @_refusals
    def start(self, cmd) -> dict:
        self._require_principal(cmd, HUMAN_PRINCIPALS)
        reason = _body(cmd.body, frozenset({"reason"}))["reason"]
        with self._lock.hold():
            self._require_enabled()
            self._require_paper()
            self._require_identity()
            self._require_old_path_disarmed()
            self._require_no_active()
            self._require_ready(cmd.command_id)
            snapshot = self._capture()
            self._require_flat(snapshot)
            fx = start_fx_from_cash(self._read_cash())
            record = self._armed_record(cmd.command_id, snapshot, fx)
            inserted = self.store.insert_armed(record, principal=cmd.principal, reason=reason)
            if inserted.experiment_id != record.experiment_id:
                raise ExperimentRefused("EXPERIMENT_ACTIVE", f"experiment {inserted.experiment_id} is active")
            return self._view(inserted)

    @_refusals
    def pause(self, cmd) -> dict:
        self._require_principal(cmd, PAUSE_PRINCIPALS)
        body = _body(cmd.body, frozenset({"experiment_id", "reason"}))
        record = self._target(body["experiment_id"])
        if record.state == "KILLED":
            raise ExperimentRefused("EXPERIMENT_KILLED", "a killed experiment is flattening; stop it once flat")
        if record.state == "PAUSED":
            return self._view(record)
        paused = self.store.transition(record.experiment_id, expected=frozenset({"ARMED"}), to="PAUSED",
                                       principal=cmd.principal, command_id=cmd.command_id, reason=body["reason"])
        return self._view(paused)

    @_refusals
    def resume(self, cmd) -> dict:
        self._require_principal(cmd, HUMAN_PRINCIPALS)
        body = _body(cmd.body, frozenset({"experiment_id", "reason"}))
        with self._lock.hold():
            record = self._target(body["experiment_id"])
            if record.state == "KILLED":
                raise ExperimentRefused("EXPERIMENT_KILLED",
                                        "a killed experiment is never resumed; stop it once flat and start a new one")
            if record.state == "ARMED":
                return self._view(record)
            self._require_enabled()
            if record.pause_cause == OUTAGE_PAUSE:
                self._require_fresh_evidence(record, cmd.command_id)
            resumed = self.store.transition(
                record.experiment_id, expected=frozenset({"PAUSED"}), to="ARMED", principal=cmd.principal,
                command_id=cmd.command_id, reason=body["reason"],
                changes={"pause_cause": None, "pause_generation_id": None})
            return self._view(resumed)

    @_refusals
    def stop(self, cmd) -> dict:
        self._require_principal(cmd, HUMAN_PRINCIPALS)
        body = _body(cmd.body, frozenset({"experiment_id", "reason"}))
        with self._lock.hold():
            record = self._target(body["experiment_id"])
            self._require_flat(self._capture())
            stopped = self.store.transition(
                record.experiment_id, expected=frozenset({"ARMED", "PAUSED", "KILLED"}), to="STOPPED",
                principal=cmd.principal, command_id=cmd.command_id, reason=body["reason"])
            return self._view(stopped)

    # -- checks (each refuses with its own code) -----------------------------

    @staticmethod
    def _require_principal(cmd, allowed: frozenset[str]) -> None:
        if getattr(cmd, "principal", None) not in allowed:
            raise ExperimentRefused("PRINCIPAL_FORBIDDEN", f"{getattr(cmd, 'principal', None)!r} may not do this")

    def _read_bool(self, code: str, port: Callable[[], Any]) -> bool:
        try:
            value = port()
        except Exception as exc:
            raise ExperimentRefused(code, f"evidence unreadable: {exc}") from exc
        if type(value) is not bool:
            raise ExperimentRefused(code, f"evidence is not a bool: {value!r}")
        return value

    def _require_enabled(self) -> None:
        built = self._read_bool("AI_PAPER_CONFIG_UNREADABLE", self.ports.ai_paper_built)
        if not (getattr(self._config, "enabled", False) is True and built):
            raise ExperimentRefused("AI_PAPER_DISABLED", "ai_paper.enabled is false or its services are not built")

    def _mode(self) -> str:
        try:
            mode = self._account_mode() if callable(self._account_mode) else self._account_mode
        except Exception as exc:
            raise ExperimentRefused("ACCOUNT_MODE_UNREADABLE", str(exc)) from exc
        if mode not in ("paper", "live"):
            raise ExperimentRefused("ACCOUNT_MODE_UNREADABLE", f"account mode {mode!r}")
        return mode

    def _require_paper(self) -> None:
        if self._mode() != "paper":
            raise ExperimentRefused("ACCOUNT_NOT_PAPER", "experiments run on a paper account only")

    def _require_identity(self) -> None:
        try:
            problem = self.identity_problem()
        except Exception as exc:
            raise ExperimentRefused("IDENTITY_CHECK_UNREADABLE", str(exc)) from exc
        if problem is None:
            return
        if problem not in _IDENTITY_CODES:
            raise ExperimentRefused("IDENTITY_CHECK_UNREADABLE", f"identity check returned {problem!r}")
        raise ExperimentRefused(problem, "the ai_supervisor identity or its allow-list entry is not ready")

    def _require_old_path_disarmed(self) -> None:
        try:
            armed = self.ports.old_path_armed()
        except Exception as exc:
            raise ExperimentRefused("ONE_STRATEGY_STATE_UNREADABLE", str(exc)) from exc
        if armed is None:
            return
        if type(armed) is not str:
            raise ExperimentRefused("ONE_STRATEGY_STATE_UNREADABLE", f"one-strategy state {armed!r}")
        raise ExperimentRefused("ONE_STRATEGY_ARMED", "deactivate the one-strategy automation first")

    def _require_no_active(self) -> None:
        active = self.store.active()
        if active is not None:
            raise ExperimentRefused("EXPERIMENT_ACTIVE", f"experiment {active.experiment_id} is {active.state}")

    def _require_ready(self, command_id: Optional[str]) -> None:
        if not self._read_bool("TRADER_NOT_READY", self.ports.resume_ready):
            raise ExperimentRefused("TRADER_NOT_READY", "no current, fenced broker evidence")
        if not self._read_bool("BREAKER_TRIPPED", self.ports.breaker_clear):
            raise ExperimentRefused("BREAKER_TRIPPED", "the circuit breaker is not clear")
        self._require_reconciled(command_id)

    def _require_reconciled(self, command_id: Optional[str]) -> None:
        if not self._read_bool("RECONCILIATION_INCOMPLETE", lambda: self.ports.reconciliation_safe(command_id)):
            raise ExperimentRefused("RECONCILIATION_INCOMPLETE", "a command is still unresolved")

    def _capture(self) -> Any:
        try:
            snapshot = self.ports.broker.capture(self._account_id)
        except Exception as exc:
            raise ExperimentRefused("BROKER_SNAPSHOT_UNAVAILABLE", str(exc)) from exc
        nlv = getattr(snapshot, "net_liquidation", None)
        generation = getattr(snapshot, "generation_id", None)
        if (type(nlv) not in (int, float) or not math.isfinite(nlv) or nlv <= 0
                or type(generation) is not int or not isinstance(getattr(snapshot, "positions", None), tuple)
                or not isinstance(getattr(snapshot, "working_orders", None), tuple)):
            raise ExperimentRefused("BROKER_SNAPSHOT_UNAVAILABLE", "the broker capture is not usable")
        if snapshot.account_id != self._account_id:
            raise ExperimentRefused("BROKER_SNAPSHOT_UNAVAILABLE", "the broker capture names another account")
        if snapshot.account_mode != "paper":
            raise ExperimentRefused("ACCOUNT_NOT_PAPER", "the broker reports a live account")
        return snapshot

    def _require_flat(self, snapshot: Any) -> None:
        open_items = [f"position {p.conid} {p.quantity:g}" for p in snapshot.positions if p.quantity != 0]
        open_items += [f"working order {o.order_entity_id}" for o in snapshot.working_orders]
        try:
            owner = self.ports.exit_owners.account_owner(self._account_id)
            roots = list(self.ports.liquidation_roots())
        except Exception as exc:
            raise ExperimentRefused("BROKER_SNAPSHOT_UNAVAILABLE", f"close state unreadable: {exc}") from exc
        if owner is not None:
            open_items.append(f"account exit owner {getattr(owner, 'root_id', owner)}")
        open_items += [f"liquidation root {root}" for root in roots]
        if open_items:
            raise ExperimentRefused("NOT_FLAT", "open: " + ", ".join(open_items))

    def _read_cash(self) -> Mapping:
        try:
            return self.ports.account_cash()
        except Exception as exc:
            raise ExperimentRefused("START_FX_UNAVAILABLE", f"account cash unreadable: {exc}") from exc

    def _require_fresh_evidence(self, record: ExperimentRecord, command_id: Optional[str]) -> None:
        """K11: an outage pause resumes only on a capture newer than the pause."""
        if not self._read_bool("TRADER_NOT_READY", self.ports.resume_ready):
            raise ExperimentRefused("TRADER_NOT_READY", "no current, fenced broker evidence")
        snapshot = self._capture()
        floor = (record.pause_generation_id if record.pause_generation_id is not None
                 else record.start_generation_id)
        if snapshot.generation_id <= floor:
            raise ExperimentRefused("RESUME_EVIDENCE_STALE",
                                    f"broker generation {snapshot.generation_id} is not newer than {floor}")
        self._require_reconciled(command_id)

    def _target(self, experiment_id: str) -> ExperimentRecord:
        active = self.store.active()
        if active is None:
            latest = self.store.latest()
            if latest is None:
                raise ExperimentRefused("NO_EXPERIMENT", "there is no experiment")
            raise ExperimentRefused("EXPERIMENT_STOPPED", f"experiment {latest.experiment_id} is stopped")
        if active.experiment_id != experiment_id:
            raise ExperimentRefused("EXPERIMENT_MISMATCH", f"the active experiment is {active.experiment_id}")
        return active

    def _armed_record(self, command_id: str, snapshot: Any, fx: StartFx) -> ExperimentRecord:
        nlv = float(snapshot.net_liquidation)
        return ExperimentRecord(
            experiment_id=experiment_id_for(self._account_id, command_id), account_id=self._account_id,
            started_at=self._now(), start_net_liquidation=nlv, base_currency=fx.base_currency,
            start_usd_per_base=fx.usd_per_base, state="ARMED", killed_at=None, start_command_id=command_id,
            start_generation_id=snapshot.generation_id, start_fx_source=fx.source,
            config_digest=sha256_digest(CONFIG_DIGEST_PREFIX, dict(self._config.raw_section)),
            styles=tuple(self._config.styles), kill_drawdown_pct=self._config.experiment_kill_drawdown_pct,
            kill_basis=self._config.experiment_kill_basis, revision=1, peak_net_liquidation=nlv,
            kill_anchor_net_liquidation=nlv)


# -- the get_experiment read (K1, K9) -----------------------------------------

def _line_json(line: Any) -> Optional[dict]:
    return None if line is None else line.to_json()


def configured_kill_line(record: Optional[ExperimentRecord], config_path: Any, account_mode: str) -> Any:
    """K9: the ai_paper block re-read from the trader's own config file now. Never applied."""
    import yaml
    from pathlib import Path

    from trader.automation.ai_paper_config import load_ai_paper_config
    try:
        path = Path(config_path)
        raw = yaml.safe_load(path.read_text()) if path.exists() else {}
        if raw is not None and not isinstance(raw, dict):
            raise ValueError("trader.yaml is not a mapping")
        configured = load_ai_paper_config((raw or {}).get("ai_paper"), trading_mode=account_mode)
    except Exception:
        logger.warning("the configured kill line could not be read from %s", config_path, exc_info=True)
        return "UNREADABLE"
    return _line_json(_line_for(record, configured))


def _line_for(record: Optional[ExperimentRecord], config: Any) -> Any:
    from trader.automation.kill_line import KillLine
    if record is not None:
        return effective_kill_line(record, config)
    pct = getattr(config, "experiment_kill_drawdown_pct", None)
    return None if pct is None else KillLine(float(pct), config.experiment_kill_basis)


def experiment_status(experiments: Any, *, account_mode: str, mode_conflict: Optional[str],
                      config_path: Any) -> dict:
    """The newest experiment of the account, its entry block, the last evaluation and both kill lines."""
    from trader.automation.kill_monitor import DETECTION_NOTE

    service, monitor, store = experiments.service, experiments.monitor, experiments.store
    record = store.latest()
    active = _line_json(_line_for(record, service.config))
    configured = configured_kill_line(record, config_path, account_mode)
    block = None
    if record is not None:
        block = mode_conflict or monitor.entry_block(record)
    view = None
    transitions: list = []
    if record is not None:
        view = record_view(record, service.config)
        view.update({"kill_round": record.kill_round, "kill_flatten_root": record.kill_flatten_root,
                     "kill_alert_state": record.kill_alert_state,
                     "kill_observed_drawdown_pct": record.kill_observed_drawdown_pct,
                     "stopped_at": _iso(record.stopped_at)})
        transitions = [{**t, "at": _iso(t["at"])} for t in store.transitions(record.experiment_id)]
    return {
        "experiment": view,
        "entry_block": block,
        "last_evaluation": monitor.last_evaluation_view(),
        "kill_line": {"active": active, "configured": configured,
                      "pending_restart": configured != "UNREADABLE" and configured != active,
                      "detection": DETECTION_NOTE},
        "mode_conflict": mode_conflict,
        "transitions": transitions,
    }


def attach_production_identity(experiments: Any, identity: Any, registry: Any) -> None:
    """K22: trading_runtime calls this once the served registry exists."""
    if experiments is not None:
        experiments.service.attach_identity_check(lambda: ai_supervisor_identity_problem(identity, registry))

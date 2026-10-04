"""P4 Task 4 — fault-injection registry and primitives for recovery drills.

This module is deliberately production-free: nothing in ``trader_service``,
``strategy_service``, ``data_service``, or ``web.app`` imports it, and it
never reaches into a running process to inject anything. Instead it gives
drill scripts / integration tests (``scripts/automation_fault_drill.py``,
``tests/integration/test_automation_faults.py``) a shared, explicitly-named
vocabulary for:

1. **Injection points** — the exact named boundaries in the automated-trading
   pipeline a fault drill certifies (command claim, initial approval,
   dispatch revalidation, IB send, broker acknowledgement, partial fill,
   protection submit, journal event, attribution append, replay seal).
   ``FaultInjectionRegistry`` records which points a scenario battery
   actually exercised so a drill report can never silently claim coverage it
   didn't earn (mirrors ``scripts/command_plane_drill.py``'s
   pending-vs-covered distinction).
2. **Reusable fault primitives** — small, generic building blocks
   (``SimulatedCrash``, event duplication/reordering, a disk-error
   injector, an out-of-band broker mutation marker) that scenario code
   composes with the existing fake ports (``FakeOrders``, ``FakeBroker``,
   ``FakeBracketDispatch``, ...) from ``scripts/automation_paper_drill.py``
   and ``scripts/command_plane_drill.py`` — this module does not reimplement
   those fakes.
3. **A production-data safety guard** — ``assert_disposable_path`` refuses
   any destructive operation (backup/restore, simulated power loss) outside
   the process temp root, so a fault drill can never touch real trading
   data even if misconfigured.
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Sequence, TypeVar

T = TypeVar("T")

__all__ = [
    "InjectionPoint",
    "ALL_INJECTION_POINTS",
    "FaultRecord",
    "FaultInjectionRegistry",
    "SimulatedCrash",
    "duplicate_last",
    "reorder_swap_last_two",
    "DiskErrorInjector",
    "ExternalBrokerMutation",
    "ProductionDataGuardError",
    "assert_disposable_path",
    "backup_copy",
    "simulate_power_loss",
]


# ---------------------------------------------------------------------------
# Injection points — the explicit, named vocabulary the P4 Task 4 brief
# requires ("explicit injection-point names"). Order matches the chronology
# of one automated trade: a command is claimed, approved, revalidated right
# before the irreversible IB send, acknowledged/partially filled by the
# broker, protected, journalled, attributed, and finally sealed into a
# forensic replay bundle.
# ---------------------------------------------------------------------------
class InjectionPoint(str, Enum):
    COMMAND_CLAIMED = "command_claimed"
    INITIAL_APPROVAL = "initial_approval"
    DISPATCH_REVALIDATION = "dispatch_revalidation"
    IB_SEND = "ib_send"
    BROKER_ACKNOWLEDGEMENT = "broker_acknowledgement"
    PARTIAL_FILL = "partial_fill"
    PROTECTION_SUBMIT = "protection_submit"
    JOURNAL_EVENT = "journal_event"
    ATTRIBUTION_APPEND = "attribution_append"
    REPLAY_SEAL = "replay_seal"


ALL_INJECTION_POINTS: tuple[InjectionPoint, ...] = tuple(InjectionPoint)


@dataclass(frozen=True)
class FaultRecord:
    """One exercised injection point: which scenario hit it, and how."""

    point: InjectionPoint
    scenario: str
    detail: str = ""


class FaultInjectionRegistry:
    """Coverage ledger for a fault-drill battery.

    Scenario code calls :meth:`mark` at the moment it simulates a fault
    corresponding to one of the ten named ``InjectionPoint`` values. The
    registry never fabricates coverage: :meth:`assert_full_coverage` (and the
    report's ``injection_point_coverage`` block) is fail-closed — a point
    that no scenario ever marked is reported as NOT exercised, never as
    silently passed.
    """

    def __init__(self) -> None:
        self._fired: list[FaultRecord] = []

    def mark(
        self, point: InjectionPoint, *, scenario: str, detail: str = "",
    ) -> FaultRecord:
        point = InjectionPoint(point)  # raises ValueError on an unknown point
        if not scenario:
            raise ValueError("scenario name is required to mark an injection point")
        record = FaultRecord(point=point, scenario=scenario, detail=detail)
        self._fired.append(record)
        return record

    @property
    def fired(self) -> tuple[FaultRecord, ...]:
        return tuple(self._fired)

    def exercised_points(self) -> frozenset[InjectionPoint]:
        return frozenset(record.point for record in self._fired)

    def coverage_report(
        self, required: Sequence[InjectionPoint] = ALL_INJECTION_POINTS,
    ) -> dict[str, bool]:
        exercised = self.exercised_points()
        return {point.value: (point in exercised) for point in required}

    def assert_full_coverage(
        self, required: Sequence[InjectionPoint] = ALL_INJECTION_POINTS,
    ) -> None:
        exercised = self.exercised_points()
        missing = [point.value for point in required if point not in exercised]
        if missing:
            raise AssertionError(
                f"fault drill never exercised injection point(s): {missing}"
            )

    def reset(self) -> None:
        self._fired.clear()


# ---------------------------------------------------------------------------
# Generic fault primitives — composed with existing fake ports by scenario
# code; this module holds none of its own broker/journal test doubles.
# ---------------------------------------------------------------------------
class SimulatedCrash(BaseException):
    """A hard process death (SIGKILL/OOM) mid-operation.

    Subclasses ``BaseException`` (not ``Exception``) so it escapes a
    handler's ``except Exception`` the same way a real crash would —
    the only way to recover from it is a fresh process reading durable
    state, never an in-process retry. Mirrors the identically-named
    exception already used ad hoc in ``scripts/command_plane_drill.py``
    and ``scripts/automation_paper_drill.py``; this is the shared,
    canonical definition scenario code should import going forward.
    """


def duplicate_last(events: Sequence[T]) -> list[T]:
    """Model message duplication: re-deliver the last event a second time."""
    events = list(events)
    if not events:
        raise ValueError("cannot duplicate from an empty event sequence")
    return events + [events[-1]]


def reorder_swap_last_two(events: Sequence[T]) -> list[T]:
    """Model message reordering: swap the last two events out of order."""
    events = list(events)
    if len(events) < 2:
        raise ValueError("need at least two events to model reordering")
    events[-1], events[-2] = events[-2], events[-1]
    return events


class DiskErrorInjector:
    """Wraps a callable so its first ``fail_calls`` invocations raise
    ``OSError`` (simulating e.g. ``ENOSPC``/a failed fsync) before falling
    through to the real implementation.

    Used to wrap a durable-write method (``journal.mutate``,
    ``AttributionLedger.append``'s underlying write, ...) on a *test
    fixture instance* — never monkeypatches a class, so only the one
    fixture under test is affected.
    """

    def __init__(
        self, *, fail_calls: int = 1, message: str = "simulated disk error (ENOSPC)",
    ) -> None:
        if fail_calls < 1:
            raise ValueError("fail_calls must be >= 1")
        self._remaining = fail_calls
        self.message = message
        self.attempts = 0

    def wrap(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            self.attempts += 1
            if self._remaining > 0:
                self._remaining -= 1
                raise OSError(self.message)
            return fn(*args, **kwargs)

        return _wrapped

    @property
    def exhausted(self) -> bool:
        return self._remaining <= 0


@dataclass(frozen=True)
class ExternalBrokerMutation:
    """An out-of-band broker-side position/order our command authority never
    created (e.g. a manual trade placed directly in TWS, or a corporate
    action). Carries no ``command_id``/``order_group_id`` by construction —
    any code path handed one of these must fail loudly (refuse to invent an
    identity for it) rather than silently absorbing it as one of our own
    commands.
    """

    kind: Literal["position", "order"]
    conid: int
    quantity: float
    description: str = ""


# ---------------------------------------------------------------------------
# Production-data safety guard + disposable-volume helpers for backup/
# restore and power-loss drills.
# ---------------------------------------------------------------------------
class ProductionDataGuardError(RuntimeError):
    """Raised when a destructive drill helper is pointed at a path outside
    the disposable temp root. Backup/restore and power-loss drills must
    NEVER run against real trading data — this is the one place that
    guarantee is enforced, so every destructive helper below routes through
    it rather than each re-implementing its own check.
    """


def assert_disposable_path(path: Any) -> Path:
    """Resolve ``path`` and raise ``ProductionDataGuardError`` unless it sits
    under the OS temp root (i.e. it can only be a ``tempfile``-created
    scratch file/directory, never a real config/data path such as
    ``~/.local/share/mmr``).
    """
    resolved = Path(path).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved.relative_to(temp_root)
    except ValueError:
        raise ProductionDataGuardError(
            f"refusing to run a destructive drill against non-disposable path "
            f"{resolved} (must be under the temp root {temp_root})"
        ) from None
    return resolved


def backup_copy(source: Any, dest_dir: Any) -> Path:
    """Copy a DuckDB file (plus its ``.wal`` companion, if present) into a
    disposable destination directory. ``dest_dir`` is guarded by
    :func:`assert_disposable_path`; ``source`` is copied read-only and never
    mutated, so it may legitimately be the drill's own scratch DB.
    """
    source_path = Path(source)
    if not source_path.is_file():
        raise FileNotFoundError(f"backup source does not exist: {source_path}")
    dest_root = assert_disposable_path(dest_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / source_path.name
    shutil.copy2(source_path, dest)
    wal = source_path.with_name(source_path.name + ".wal")
    if wal.exists():
        shutil.copy2(wal, dest_root / wal.name)
    return dest


def simulate_power_loss(path: Any, *, truncate_bytes: int = 4096) -> Path:
    """Truncate the last ``truncate_bytes`` bytes off a DISPOSABLE copy of a
    DuckDB file to model an unexpected power loss mid-write (a torn write).

    ``path`` is guarded by :func:`assert_disposable_path` — callers must
    pass a copy produced by :func:`backup_copy`, never a live/original
    database file.
    """
    guarded = assert_disposable_path(path)
    size = guarded.stat().st_size
    if size == 0:
        raise ValueError(f"cannot simulate power loss on an empty file: {guarded}")
    new_size = max(0, size - truncate_bytes)
    with open(guarded, "r+b") as fh:
        fh.truncate(new_size)
    return guarded

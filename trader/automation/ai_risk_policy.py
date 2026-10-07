"""AI risk policies, their per-session timing and the breach latch (spec 5.4, Plan 3 Task 4).

``ai_supervisor`` publishes risk policies as append-only revisions under the
owner ceiling. What is in force is the effective limits of the current XNYS
session: the first policy of a session applies at once (R8); after that a
tighter field applies at once and a looser one waits for the next session.
A session starts on the first decision of its session date (R7): its row
freezes the owner ceiling and the start-of-day net liquidation. A breach
latch on the row refuses every later entry of that session (R10).
"""
from __future__ import annotations

import datetime as dt
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional

from trader.automation.calendar_policy import XNYSCalendarPolicy
from trader.automation.risk_limits import RiskLimits
from trader.data.schema_migrations import SchemaMigrator

AI_RISK_POLICY_MIGRATION_VERSION = 54
MAX_REASON_LENGTH = 500
CUTOFF_CANCEL_STATES = ("ISSUED", "AMBIGUOUS", "DONE")


def apply_ai_risk_policy_migration(migrator: SchemaMigrator) -> bool:
    return migrator.apply(AI_RISK_POLICY_MIGRATION_VERSION, "sp1_ai_risk_policy", (
        """CREATE TABLE IF NOT EXISTS ai_risk_policy_revisions (
            account_id VARCHAR NOT NULL, revision INTEGER NOT NULL, limits_json VARCHAR NOT NULL,
            reason VARCHAR NOT NULL, principal VARCHAR NOT NULL, command_id VARCHAR NOT NULL UNIQUE,
            published_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (account_id, revision))""",
        """CREATE TABLE IF NOT EXISTS ai_paper_sessions (
            account_id VARCHAR NOT NULL, session_date DATE NOT NULL,
            anchor_net_liquidation DOUBLE NOT NULL, anchor_generation_id BIGINT NOT NULL,
            ceiling_json VARCHAR NOT NULL, started_at TIMESTAMPTZ NOT NULL,
            latch_code VARCHAR, latch_detail VARCHAR, latched_at TIMESTAMPTZ,
            cutoff_cancel_state VARCHAR, cutoff_cancel_generation BIGINT,
            PRIMARY KEY (account_id, session_date))""",
        """CREATE TABLE IF NOT EXISTS ai_effective_limits (
            account_id VARCHAR NOT NULL, session_date DATE NOT NULL, revision INTEGER NOT NULL,
            limits_json VARCHAR NOT NULL, published_revision INTEGER NOT NULL,
            cause VARCHAR NOT NULL, created_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (account_id, session_date, revision))""",
    ))


class PolicyRefused(Exception):
    def __init__(self, code: str, message: str, fields: tuple[str, ...] = ()):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.fields = fields


@dataclass(frozen=True)
class PublishResult:
    revision: int
    applied_now: tuple[str, ...]
    queued: tuple[str, ...]


@dataclass(frozen=True)
class SessionView:
    session_date: dt.date
    anchor: float
    ceiling: RiskLimits
    effective: Optional[RiskLimits]
    effective_revision: Optional[int]
    published_revision: Optional[int]
    queued: tuple[str, ...]
    latch_code: Optional[str]
    cutoff_cancel_state: Optional[str] = None
    cutoff_cancel_generation: Optional[int] = None

    @property
    def daily_loss_budget(self) -> Optional[float]:
        if self.effective is None:
            return None
        return self.anchor * self.effective.daily_loss_fraction


@dataclass(frozen=True)
class _SessionRow:
    session_date: dt.date
    anchor: float
    ceiling: RiskLimits
    latch_code: Optional[str]
    cutoff_cancel_state: Optional[str]
    cutoff_cancel_generation: Optional[int]


@dataclass(frozen=True)
class _Effective:
    revision: int
    limits: RiskLimits
    published_revision: int


def _limits(text: str) -> RiskLimits:
    return RiskLimits.from_json(json.loads(text))


def _dump(limits: RiskLimits) -> str:
    return json.dumps(limits.to_json(), sort_keys=True)


class AiRiskPolicyService:
    def __init__(self, *, db: Any, account_id: str, ceiling: RiskLimits,
                 calendar: XNYSCalendarPolicy, now: Callable[[], dt.datetime]):
        if not isinstance(ceiling, RiskLimits):
            raise TypeError("ceiling must be RiskLimits")
        self._db = db
        self._account_id = account_id
        self._ceiling = ceiling
        self._calendar = calendar
        self._now = now

    # -- writes --------------------------------------------------------------

    def publish(self, limits: RiskLimits, *, reason: str, principal: str, command_id: str,
                broker: Any) -> PublishResult:
        self._check_policy(limits)
        self._check_reason(reason)
        self._check_broker(broker)
        now = self._now()
        session_date = self._session_date(now)
        return self._db.transaction(
            lambda conn: self._publish_in_tx(conn, limits, reason, principal, command_id, now, session_date))

    def ensure_session(self, broker: Any) -> Optional[SessionView]:
        """Start the session of today's XNYS date if absent; commits before it returns (R7).

        Called only by decision admission. Returns ``None`` off-session.
        """
        anchor = self._check_broker(broker)
        now = self._now()
        session_date = self._session_date(now)
        if session_date is None:
            return None
        return self._db.transaction(
            lambda conn: self._ensure_in_tx(conn, session_date, anchor, broker.generation_id, now))

    def latch(self, code: str, detail: str) -> None:
        """Latch today's session on a loss breach. The first breach wins."""
        now = self._now()
        session_date = self._session_date(now)

        def write(conn) -> None:
            if session_date is None or self._session_in_tx(conn, session_date) is None:
                raise PolicyRefused("NO_SESSION", "no ai_paper session today to latch")
            conn.execute(
                "UPDATE ai_paper_sessions SET latch_code = ?, latch_detail = ?, latched_at = ? "
                "WHERE account_id = ? AND session_date = ? AND latch_code IS NULL",
                [code, detail, now, self._account_id, session_date])
        self._db.transaction(write)

    def set_cutoff_cancel_state(self, session_date: dt.date, state: str, *, generation: int) -> None:
        """Forward-only: NULL -> ISSUED -> AMBIGUOUS -> DONE, never backwards (R26).

        ``generation`` is the broker generation the decision was read from; the next check
        waits for a newer one.
        """
        if state not in CUTOFF_CANCEL_STATES:
            raise ValueError(f"unknown cutoff cancel state {state!r}")

        def write(conn) -> None:
            row = self._session_in_tx(conn, session_date)
            if row is None:
                raise PolicyRefused("NO_SESSION", "no ai_paper session to record the cutoff cancel on")
            current = row.cutoff_cancel_state
            rank = {None: -1, **{name: i for i, name in enumerate(CUTOFF_CANCEL_STATES)}}
            if rank[state] < rank[current]:
                raise ValueError(f"cutoff cancel state cannot move from {current} to {state}")
            conn.execute(
                "UPDATE ai_paper_sessions SET cutoff_cancel_state = ?, cutoff_cancel_generation = ? "
                "WHERE account_id = ? AND session_date = ?",
                [state, int(generation), self._account_id, session_date])
        self._db.transaction(write)

    # -- reads ---------------------------------------------------------------

    def current(self) -> Optional[SessionView]:
        session_date = self._session_date(self._now())
        if session_date is None:
            return None
        return self._db.transaction(lambda conn: self._view_in_tx(conn, session_date))

    def latest_published_revision(self) -> Optional[int]:
        latest = self.latest_published()
        return None if latest is None else latest[0]

    def latest_published(self) -> Optional[tuple[int, RiskLimits]]:
        return self._db.transaction(self._latest_published_in_tx)

    def effective_limits(self) -> RiskLimits:
        view = self.current()
        if view is None or view.effective is None:
            raise PolicyRefused("NO_EFFECTIVE_LIMITS", "no effective ai_paper limits in force")
        return view.effective

    @property
    def ceiling(self) -> RiskLimits:
        return self._ceiling

    # -- validation ----------------------------------------------------------

    def _check_policy(self, limits: object) -> None:
        if not isinstance(limits, RiskLimits):
            raise PolicyRefused("POLICY_INVALID", "limits must be RiskLimits")
        problems = limits.structural_problems()
        if problems:
            raise PolicyRefused("POLICY_INVALID", ", ".join(problems))
        above = limits.fields_above(self._ceiling)
        if above:
            raise PolicyRefused("POLICY_ABOVE_CEILING", "a field is above the owner ceiling", above)

    @staticmethod
    def _check_reason(reason: object) -> None:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON_LENGTH:
            raise PolicyRefused("REASON_INVALID", f"reason must be 1-{MAX_REASON_LENGTH} characters of text")

    def _check_broker(self, broker: Any) -> float:
        """The fenced paper snapshot of this account; returns the start-of-day anchor."""
        try:
            fenced = (broker.account_id == self._account_id and broker.account_mode == "paper"
                      and type(broker.generation_id) is int and broker.generation_id > 0)
            net_liquidation = float(broker.net_liquidation)
            daily_pnl = float(broker.daily_pnl)
        except Exception:
            raise PolicyRefused("SESSION_EVIDENCE_INVALID", "broker snapshot is unreadable") from None
        anchor = net_liquidation - daily_pnl
        if not fenced or not all(math.isfinite(v) for v in (net_liquidation, daily_pnl)) or not anchor > 0:
            raise PolicyRefused("SESSION_EVIDENCE_INVALID", "a fenced paper snapshot of this account is required")
        return anchor

    def _session_date(self, now: dt.datetime) -> Optional[dt.date]:
        schedule = self._calendar.resolve(now)
        return None if schedule is None else schedule.session_date

    # -- in-transaction helpers ---------------------------------------------

    def _publish_in_tx(self, conn, limits, reason, principal, command_id, now, session_date) -> PublishResult:
        latest = self._latest_published_in_tx(conn)
        revision = 1 if latest is None else latest[0] + 1
        conn.execute("INSERT INTO ai_risk_policy_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [self._account_id, revision, _dump(limits), reason, principal, command_id, now])
        session = None if session_date is None else self._session_in_tx(conn, session_date)
        if session is None:
            return PublishResult(revision, (), ())  # no session in force; its first decision reads it
        cap = session.ceiling.tighter(self._ceiling)
        current = self._effective_in_tx(conn, session_date)
        if current is None:  # R8: the first policy of the session applies at once
            new, cause, applied = limits.tighter(cap), "first_policy", RiskLimits.FIELDS
        else:
            new, cause = current.limits.tighter(limits), "published_tighter"
            applied = current.limits.fields_above(new)
        if applied:
            self._append_effective_in_tx(conn, session_date, new, revision, cause, now)
        return PublishResult(revision, applied, limits.tighter(cap).fields_above(new))

    def _ensure_in_tx(self, conn, session_date, anchor, generation_id, now) -> SessionView:
        if self._session_in_tx(conn, session_date) is None:
            conn.execute(
                "INSERT INTO ai_paper_sessions (account_id, session_date, anchor_net_liquidation, "
                "anchor_generation_id, ceiling_json, started_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                [self._account_id, session_date, anchor, generation_id, _dump(self._ceiling), now])
        session = self._session_in_tx(conn, session_date)
        current = self._effective_in_tx(conn, session_date)
        latest = self._latest_published_in_tx(conn)
        if current is None and latest is not None:
            cap = session.ceiling.tighter(self._ceiling)
            self._append_effective_in_tx(conn, session_date, latest[1].tighter(cap), latest[0], "session_start", now)
        elif current is not None and current.limits.fields_above(self._ceiling):
            # R9: a ceiling lowered in trader.yaml applies at the next decision.
            self._append_effective_in_tx(conn, session_date, current.limits.tighter(self._ceiling),
                                         current.published_revision, "ceiling_tighter", now)
        return self._view_in_tx(conn, session_date)

    def _view_in_tx(self, conn, session_date) -> Optional[SessionView]:
        session = self._session_in_tx(conn, session_date)
        if session is None:
            return None
        current = self._effective_in_tx(conn, session_date)
        latest = self._latest_published_in_tx(conn)
        queued: tuple[str, ...] = ()
        if current is not None and latest is not None:
            queued = latest[1].tighter(session.ceiling).fields_above(current.limits)
        return SessionView(
            session_date=session.session_date, anchor=session.anchor, ceiling=session.ceiling,
            effective=None if current is None else current.limits,
            effective_revision=None if current is None else current.revision,
            published_revision=None if current is None else current.published_revision,
            queued=queued, latch_code=session.latch_code, cutoff_cancel_state=session.cutoff_cancel_state,
            cutoff_cancel_generation=session.cutoff_cancel_generation)

    def _session_in_tx(self, conn, session_date) -> Optional[_SessionRow]:
        row = conn.execute(
            "SELECT session_date, anchor_net_liquidation, ceiling_json, latch_code, cutoff_cancel_state, "
            "cutoff_cancel_generation FROM ai_paper_sessions WHERE account_id = ? AND session_date = ?",
            [self._account_id, session_date]).fetchone()
        if row is None:
            return None
        return _SessionRow(row[0], float(row[1]), _limits(row[2]), row[3], row[4],
                           None if row[5] is None else int(row[5]))

    def _effective_in_tx(self, conn, session_date) -> Optional[_Effective]:
        row = conn.execute(
            "SELECT revision, limits_json, published_revision FROM ai_effective_limits "
            "WHERE account_id = ? AND session_date = ? ORDER BY revision DESC LIMIT 1",
            [self._account_id, session_date]).fetchone()
        return None if row is None else _Effective(int(row[0]), _limits(row[1]), int(row[2]))

    def _latest_published_in_tx(self, conn) -> Optional[tuple[int, RiskLimits]]:
        row = conn.execute(
            "SELECT revision, limits_json FROM ai_risk_policy_revisions WHERE account_id = ? "
            "ORDER BY revision DESC LIMIT 1", [self._account_id]).fetchone()
        return None if row is None else (int(row[0]), _limits(row[1]))

    def _append_effective_in_tx(self, conn, session_date, limits, published_revision, cause, now) -> None:
        current = self._effective_in_tx(conn, session_date)
        revision = 1 if current is None else current.revision + 1
        conn.execute("INSERT INTO ai_effective_limits VALUES (?, ?, ?, ?, ?, ?, ?)",
                     [self._account_id, session_date, revision, _dump(limits), published_revision, cause, now])

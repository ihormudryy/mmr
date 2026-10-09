"""Research DB migrations 20-22: the service's requests, its signed cases, the shadow cohort (SP2c Plan 3)."""
from __future__ import annotations

import datetime as dt
import json
from typing import Any, Optional

RESEARCH_MIGRATION_REQUESTS = 20
RESEARCH_MIGRATION_CASES = 21
RESEARCH_MIGRATION_SHADOW = 22
REQUEST_STATES = ("CLAIMING", "REFUSED", "QUEUED", "RUNNING", "DONE", "FAILED", "PARKED")
OPEN_STATES = ("QUEUED", "RUNNING")                  # PARKED is terminal and takes no queue slot
UNREAD = ""                                          # a NOT NULL column whose value was never read

_REQUESTS = ("""CREATE TABLE IF NOT EXISTS research_requests (
    request_id VARCHAR PRIMARY KEY, body_json VARCHAR NOT NULL, strategy_key VARCHAR NOT NULL,
    file_hash VARCHAR NOT NULL, state VARCHAR NOT NULL
        CHECK (state IN ('CLAIMING','REFUSED','QUEUED','RUNNING','DONE','FAILED','PARKED')),
    ny_day DATE, code VARCHAR, detail VARCHAR, parked_reason VARCHAR, case_digest VARCHAR, summary_json VARCHAR,
    pending_report VARCHAR CHECK (pending_report IS NULL OR pending_report IN ('DONE','FAILED')),
    created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL)""",)
_CASES = ("""CREATE TABLE IF NOT EXISTS research_cases (
    case_digest VARCHAR PRIMARY KEY, request_id VARCHAR NOT NULL UNIQUE, stage VARCHAR NOT NULL,
    signed_at TIMESTAMPTZ NOT NULL)""",)
_SHADOW = (
    """CREATE TABLE IF NOT EXISTS shadow_members (
        judgment_id VARCHAR PRIMARY KEY, case_digest VARCHAR NOT NULL UNIQUE, verdict VARCHAR NOT NULL
            CHECK (verdict IN ('DEPLOY','SHADOW','REJECT')),
        recorded_at TIMESTAMPTZ NOT NULL, first_session DATE NOT NULL, last_session DATE NOT NULL,
        joined_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS shadow_outbox (
        judgment_id VARCHAR NOT NULL, session_date DATE NOT NULL, body_json VARCHAR NOT NULL,
        queued_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (judgment_id, session_date))""",
    """CREATE TABLE IF NOT EXISTS shadow_sent (
        judgment_id VARCHAR NOT NULL, session_date DATE NOT NULL, status VARCHAR NOT NULL,
        reply_status VARCHAR NOT NULL CHECK (reply_status IN ('INSERTED','DUPLICATE')),
        sent_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (judgment_id, session_date))""",
    """CREATE TABLE IF NOT EXISTS shadow_failures (
        judgment_id VARCHAR NOT NULL, session_date DATE NOT NULL, code VARCHAR NOT NULL, detail VARCHAR,
        failed_at TIMESTAMPTZ NOT NULL, PRIMARY KEY (judgment_id, session_date))""",
)


def apply_service_migrations(migrator: Any) -> None:
    migrator.apply(version=RESEARCH_MIGRATION_REQUESTS, name="research_service_requests", statements=list(_REQUESTS))
    migrator.apply(version=RESEARCH_MIGRATION_CASES, name="research_service_cases", statements=list(_CASES))
    migrator.apply(version=RESEARCH_MIGRATION_SHADOW, name="research_service_shadow", statements=list(_SHADOW))


_COLUMNS = ("request_id", "body_json", "strategy_key", "file_hash", "state", "ny_day", "code", "detail",
            "parked_reason", "case_digest", "summary_json", "pending_report", "created_at", "updated_at")


class ResearchStore:
    def __init__(self, db: Any):
        self._db = db

    @staticmethod
    def _row(values) -> dict:
        row = dict(zip(_COLUMNS, values))
        row["body"] = json.loads(row.pop("body_json"))
        summary = row.pop("summary_json")
        row["summary"] = None if summary is None else json.loads(summary)
        return row

    def get(self, request_id: str) -> Optional[dict]:
        found = self._db.execute(f"SELECT {', '.join(_COLUMNS)} FROM research_requests WHERE request_id = ?",
                                 [request_id], fetch="one")
        return None if found is None else self._row(found)

    def begin(self, request_id: str, body: dict, strategy_key: str, file_hash: str, now: dt.datetime) -> None:
        """Insert a CLAIMING row, or move a REFUSED/CLAIMING row back to CLAIMING for another claim."""
        def tx(conn):
            if conn.execute("SELECT 1 FROM research_requests WHERE request_id = ?", [request_id]).fetchone():
                conn.execute("UPDATE research_requests SET state = 'CLAIMING', code = NULL, detail = NULL, "
                             "file_hash = ?, updated_at = ? WHERE request_id = ? AND state IN ('CLAIMING','REFUSED')",
                             [file_hash, now, request_id])
                return
            conn.execute("INSERT INTO research_requests (request_id, body_json, strategy_key, file_hash, state, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, 'CLAIMING', ?, ?)",
                         [request_id, json.dumps(body, sort_keys=True), strategy_key, file_hash, now, now])
        self._db.transaction(tx)

    def set_state(self, request_id: str, state: str, *, now: dt.datetime, ny_day=None, code=None, detail=None,
                  case_digest=None, summary=None) -> None:
        if state not in REQUEST_STATES:
            raise ValueError(f"unknown request state {state!r}")
        self._db.execute(
            "UPDATE research_requests SET state = ?, ny_day = COALESCE(?, ny_day), code = ?, detail = ?, "
            "case_digest = COALESCE(?, case_digest), summary_json = COALESCE(?, summary_json), updated_at = ? "
            "WHERE request_id = ?",
            [state, ny_day, code, detail, case_digest, None if summary is None else json.dumps(summary), now,
             request_id])

    def park(self, request_id: str, reason: str, *, now: dt.datetime) -> None:
        """Set a request aside for good: it can never finish, so it holds no queue slot and owes no report."""
        self._db.execute("UPDATE research_requests SET state = 'PARKED', parked_reason = ?, pending_report = NULL, "
                         "updated_at = ? WHERE request_id = ?", [reason, now, request_id])

    def count(self, states) -> int:
        marks = ", ".join("?" for _ in states)
        return int(self._db.execute(f"SELECT COUNT(*) FROM research_requests WHERE state IN ({marks})",
                                    list(states), fetch="one")[0])

    def _select(self, where: str) -> list[dict]:
        rows = self._db.execute(f"SELECT {', '.join(_COLUMNS)} FROM research_requests WHERE {where} "
                                "ORDER BY created_at, request_id", fetch="all")
        return [self._row(r) for r in rows]

    def pending(self) -> list[dict]:
        return self._select("state IN ('CLAIMING','QUEUED','RUNNING')")

    def finished(self) -> list[dict]:
        return self._select("state IN ('DONE','FAILED')")

    def hold_report(self, request_id: str, final: str, *, now: dt.datetime, case_digest: str, summary: dict) -> None:
        """Ruling 24: the case is signed, but the row stays RUNNING until the trader confirms ``final``."""
        if final not in ("DONE", "FAILED"):
            raise ValueError(f"a report owes DONE or FAILED, not {final!r}")
        self._db.execute("UPDATE research_requests SET pending_report = ?, case_digest = ?, summary_json = ?, "
                         "updated_at = ? WHERE request_id = ? AND state = 'RUNNING'",
                         [final, case_digest, json.dumps(summary), now, request_id])

    def confirm_report(self, request_id: str, *, now: dt.datetime) -> None:
        self._db.execute("UPDATE research_requests SET state = pending_report, pending_report = NULL, updated_at = ? "
                         "WHERE request_id = ? AND pending_report IS NOT NULL", [now, request_id])

    def pending_reports(self) -> list[dict]:
        return self._select("pending_report IS NOT NULL")

    def record_case(self, case_digest: str, request_id: str, stage: str, signed_at: dt.datetime) -> None:
        def tx(conn):
            if conn.execute("SELECT 1 FROM research_cases WHERE case_digest = ?", [case_digest]).fetchone() is None:
                conn.execute("INSERT INTO research_cases VALUES (?, ?, ?, ?)",
                             [case_digest, request_id, stage, signed_at])
        self._db.transaction(tx)

    def record_renewal(self, request_id: str, body: dict, *, strategy_key: str, file_hash: str, case_digest: str,
                       stage: str, summary: dict, now: dt.datetime) -> None:
        """A renewal request is DONE at once (Plan 5 rulings 2-3): the request and its case in one transaction."""
        def tx(conn):
            if conn.execute("SELECT 1 FROM research_requests WHERE request_id = ?", [request_id]).fetchone():
                return
            conn.execute("INSERT INTO research_requests (request_id, body_json, strategy_key, file_hash, state, "
                         "case_digest, summary_json, created_at, updated_at) VALUES (?, ?, ?, ?, 'DONE', ?, ?, ?, ?)",
                         [request_id, json.dumps(body, sort_keys=True), strategy_key, file_hash, case_digest,
                          json.dumps(summary), now, now])
            conn.execute("INSERT INTO research_cases VALUES (?, ?, ?, ?)", [case_digest, request_id, stage, now])
        self._db.transaction(tx)

    def record_parked_renewal(self, request_id: str, body: dict, reason: str, *, now: dt.datetime) -> None:
        """A renewal whose forward evidence the trader called tampered: parked at once, with no case.

        No binding was read, so the strategy key and file hash are stored as UNREAD."""
        def tx(conn):
            if conn.execute("SELECT 1 FROM research_requests WHERE request_id = ?", [request_id]).fetchone():
                return
            conn.execute("INSERT INTO research_requests (request_id, body_json, strategy_key, file_hash, state, "
                         "parked_reason, created_at, updated_at) VALUES (?, ?, ?, ?, 'PARKED', ?, ?, ?)",
                         [request_id, json.dumps(body, sort_keys=True), UNREAD, UNREAD, reason, now, now])
        self._db.transaction(tx)

    def case(self, *, case_digest: Optional[str] = None, request_id: Optional[str] = None) -> Optional[dict]:
        column, value = ("case_digest", case_digest) if case_digest else ("request_id", request_id)
        found = self._db.execute(f"SELECT case_digest, request_id, stage, signed_at FROM research_cases "
                                 f"WHERE {column} = ?", [value], fetch="one")
        return None if found is None else dict(zip(("case_digest", "request_id", "stage", "signed_at"), found))

    # -- shadow replay (spec 7) ----------------------------------------------------------

    def cases_without_member(self, since: dt.datetime) -> list[dict]:
        rows = self._db.execute(
            "SELECT c.case_digest, c.request_id FROM research_cases c LEFT JOIN shadow_members m "
            "ON m.case_digest = c.case_digest WHERE m.judgment_id IS NULL AND c.signed_at >= ? "
            "ORDER BY c.signed_at, c.case_digest", [since], fetch="all")
        return [{"case_digest": r[0], "request_id": r[1]} for r in rows]

    def add_member(self, judgment_id: str, case_digest: str, verdict: str, recorded_at: dt.datetime,
                   first_session: dt.date, last_session: dt.date, now: dt.datetime) -> None:
        """Spec 7: a judgment joins once, with the window of its sealed ``recorded_at``; a repeat changes nothing."""
        def tx(conn):
            if conn.execute("SELECT 1 FROM shadow_members WHERE judgment_id = ?", [judgment_id]).fetchone() is None:
                conn.execute("INSERT INTO shadow_members VALUES (?, ?, ?, ?, ?, ?, ?)",
                             [judgment_id, case_digest, verdict, recorded_at, first_session, last_session, now])
        self._db.transaction(tx)

    def shadow_members(self) -> list[dict]:
        names = ("judgment_id", "case_digest", "verdict", "recorded_at", "first_session", "last_session")
        rows = self._db.execute(f"SELECT {', '.join(names)} FROM shadow_members ORDER BY joined_at, judgment_id",
                                fetch="all")
        return [dict(zip(names, r)) for r in rows]

    def sent_sessions(self, judgment_id: str) -> set:
        rows = self._db.execute("SELECT session_date FROM shadow_sent WHERE judgment_id = ?", [judgment_id],
                                fetch="all")
        return {r[0] for r in rows}

    def queue_row(self, judgment_id: str, session: dt.date, body: dict, now: dt.datetime) -> None:
        """Keep the exact body before it is sent: a retry after a lost reply sends the same body (a DUPLICATE),
        never a recomputed one the trader would refuse as a conflict."""
        self._db.execute("INSERT INTO shadow_outbox VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
                         [judgment_id, session, json.dumps(body, sort_keys=True), now])

    def queued_row(self, judgment_id: str, session: dt.date) -> Optional[dict]:
        found = self._db.execute("SELECT body_json FROM shadow_outbox WHERE judgment_id = ? AND session_date = ?",
                                 [judgment_id, session], fetch="one")
        return None if found is None else json.loads(found[0])

    def mark_sent(self, judgment_id: str, session: dt.date, status: str, reply_status: str, now: dt.datetime) -> None:
        """Ruling 25: only a row the trader stored (INSERTED, or DUPLICATE of the same body) is sent."""
        if reply_status not in ("INSERTED", "DUPLICATE"):
            raise ValueError(f"a {reply_status} reply is not a sent row")

        def tx(conn):
            conn.execute("INSERT INTO shadow_sent VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                         [judgment_id, session, status, reply_status, now])
            conn.execute("DELETE FROM shadow_outbox WHERE judgment_id = ? AND session_date = ?",
                         [judgment_id, session])
        self._db.transaction(tx)

    def mark_failed(self, judgment_id: str, session: dt.date, code: str, detail: Optional[str],
                    now: dt.datetime) -> None:
        """Ruling 25: a final refusal is kept visible and the row is never sent again."""
        def tx(conn):
            conn.execute("INSERT INTO shadow_failures VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                         [judgment_id, session, code, None if detail is None else str(detail)[:300], now])
            conn.execute("DELETE FROM shadow_outbox WHERE judgment_id = ? AND session_date = ?",
                         [judgment_id, session])
        self._db.transaction(tx)

    def failed_sessions(self, judgment_id: str) -> set:
        rows = self._db.execute("SELECT session_date FROM shadow_failures WHERE judgment_id = ?", [judgment_id],
                                fetch="all")
        return {r[0] for r in rows}

    def shadow_failures(self) -> list[dict]:
        names = ("judgment_id", "session_date", "code", "detail", "failed_at")
        rows = self._db.execute(f"SELECT {', '.join(names)} FROM shadow_failures ORDER BY failed_at, judgment_id",
                                fetch="all")
        return [dict(zip(names, r)) for r in rows]

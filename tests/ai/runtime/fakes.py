"""Shared fakes for the Plan 5 runtime tests. Later tasks append their own fakes here."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
FRIDAY = dt.date(2026, 7, 17)
AAPL, MSFT = 265598, 272093


def et(hour, minute, second=0, day=FRIDAY):
    return dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=ET).astimezone(UTC)


class FakeSocket:
    """Stands in for one TypedRpcClient: records calls, raises ``error`` or returns ``reply``."""

    def __init__(self, reply=None, error=None):
        self.calls, self.reply, self.error, self.closed = [], reply, error, False

    def call(self, method, body, response_model, timeout=None, **options):
        self.calls.append((method, body, timeout, options))
        if self.error is not None:
            raise self.error
        return {"method": method} if self.reply is None else self.reply

    def close(self):
        self.closed = True


class EpochTrader:
    """The supervisor's grant served by Plan 1's real ControllerEpochs on a temp journal (trader time = clock)."""

    def __init__(self, directory, clock):
        from trader.automation.controller_epoch import ControllerEpochs, apply_controller_epoch_migration
        from trader.data.domain_journal import DomainJournal
        from trader.data.duckdb_store import DuckDBConnection
        from trader.data.schema_migrations import SchemaMigrator

        directory.mkdir(parents=True, exist_ok=True)
        db = DuckDBConnection.get_instance(str(directory / "journal.duckdb"))
        migrator = SchemaMigrator(db)
        journal = DomainJournal(db)
        journal.migrate(migrator)
        apply_controller_epoch_migration(migrator)
        self.epochs = ControllerEpochs(journal=journal, now=clock.now)
        self.down = False
        self.calls = []

    async def call(self, method, body, *, epoch=None):
        from trader.ai.rpc_clients import RpcNotSent, RpcRefused
        from trader.automation.controller_epoch import EpochRefused

        assert method == "grant_ai_controller_epoch"
        self.calls.append(dict(body))
        if self.down:
            raise RpcNotSent("TRADER_UNREACHABLE")
        try:
            grant = self.epochs.grant(**body)
        except EpochRefused as exc:
            raise RpcRefused(exc.code, exc.message) from None
        return {"epoch": grant.epoch, "lease_expires_at": grant.lease_expires_at.isoformat()}


class FakeLeadership:
    holder_id = "ai-fake00000000"

    def __init__(self, epoch=1):
        self.epoch, self.last_epoch, self.lost = epoch, epoch, []

    def current_epoch(self):
        return self.epoch

    async def on_stale(self, code):
        self.lost.append(code)
        self.epoch = None


def receipt(decision_id, state="SUBMITTED", error_code=None):
    return {"command_id": f"aip-{decision_id}", "correlation_id": f"aip-{decision_id}", "state": state,
            "outcome": None, "error_code": error_code, "retryable": False}


class ScriptedTrader:
    """submit_ai_paper_decision and get_ai_paper_decision. Each submit takes the next scripted step:
    "accept", "accept_lose_reply", ("receipt", state, code) or an exception to raise before anything lands."""

    def __init__(self):
        self.ledger, self.sent, self.reads, self.script = {}, [], [], []
        self.on_submit = None

    async def call(self, method, body, *, epoch=None):
        import json
        from trader.ai.rpc_clients import RpcOutcomeUnknown
        if method == "get_ai_paper_decision":
            self.reads.append((body["decision_id"], epoch))
            found = self.ledger.get(body["decision_id"])
            return {"decision_id": body["decision_id"], "command_id": f"aip-{body['decision_id']}",
                    "found": found is not None, "receipt": found, "decision_state": None,
                    "decision_error_code": None, "close_root_id": None, "controller_epoch": epoch}
        assert method == "submit_ai_paper_decision", method
        self.sent.append((json.dumps(body, sort_keys=True), epoch))
        if self.on_submit is not None:
            self.on_submit(body)
        step = self.script.pop(0) if self.script else "accept"
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            self.ledger[body["decision_id"]] = receipt(body["decision_id"], step[1], step[2])
            return self.ledger[body["decision_id"]]
        self.ledger.setdefault(body["decision_id"], receipt(body["decision_id"]))
        if step == "accept_lose_reply":
            raise RpcOutcomeUnknown("REPLY_TIMEOUT")
        return self.ledger[body["decision_id"]]


class FakeIngest:
    """Plan 2's ingestion semantics in memory. Script steps: "down", "lose_ack", ("refuse", code, retryable)."""

    def __init__(self):
        self.rows, self.script, self.calls = {}, [], []

    async def call(self, method, body, *, epoch=None):
        from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown
        self.calls.append((method, body["record_id"]))
        step = self.script.pop(0) if self.script else "ok"
        if step == "down":
            raise RpcNotSent("TRADER_UNREACHABLE")
        if isinstance(step, tuple):
            return {"status": "REFUSED", "record_id": body["record_id"], "code": step[1], "detail": None,
                    "retryable": step[2]}
        known = self.rows.get(body["record_id"])
        if known is not None and known != body:
            return {"status": "REFUSED", "record_id": body["record_id"], "code": "CONFLICTING_DUPLICATE",
                    "detail": None, "retryable": False}
        status = "DUPLICATE" if known is not None else "INSERTED"
        self.rows[body["record_id"]] = body
        if step == "lose_ack":
            raise RpcOutcomeUnknown("REPLY_TIMEOUT")
        return {"status": status, "record_id": body["record_id"], "code": None, "detail": None, "retryable": False}


def write_cost_event(store, *, request_key, kind, cost_micros, now, usage=None):
    """One Plan 4 attempt and its cost event, as the gateway writes them; returns the attempt key."""
    from trader.ai.journal import AttemptJournal
    from trader.ai.model_client import ChatMessage, ModelRequest, ModelResponse
    journal = AttemptJournal(store)

    def work(conn):
        attempt = journal.begin_in_tx(
            conn, request=ModelRequest(request_key=request_key, messages=(ChatMessage("user", "hi"),),
                                       max_output_tokens=10),
            role="jev", backend="openrouter", model="vendor/jev-1", reservation_id=f"res-{request_key}", now=now)
        if kind == "CONFIRMED":
            journal.finish_success_in_tx(conn, attempt.attempt_key,
                                         ModelResponse("ok", usage, "vendor/jev-1", "openrouter", "stop", "g-1"), now)
        else:
            journal.finish_failure_in_tx(conn, attempt.attempt_key,
                                         outcome="NOT_SENT" if kind == "NONE" else "UNKNOWN",
                                         error_code="TEST", error_detail="", now=now)
        journal.add_cost_event_in_tx(conn, attempt=attempt, kind=kind, cost_micros=cost_micros, usage=usage, now=now)
        return attempt.attempt_key
    return store.transaction(work)


def write_correction(store, attempt_key, *, usage, cost_micros, now):
    from trader.ai.journal import AttemptJournal
    journal = AttemptJournal(store)

    def work(conn):
        journal.reconcile_late_usage_in_tx(conn, attempt_key, usage, now)
        journal.add_cost_event_in_tx(conn, attempt=journal.get_in_tx(conn, attempt_key), kind="CORRECTION",
                                     cost_micros=cost_micros, usage=usage, now=now)
    store.transaction(work)


def signal(cursor, *, action="BUY", conid=AAPL, at=None, strategy="orb"):
    import hashlib
    at = at or et(11, 0)
    return {"cursor": cursor, "source_event_id": "sig-" + hashlib.sha256(f"{strategy}{cursor}".encode()).hexdigest()[:32],
            "strategy_name": strategy, "conid": conid, "action": action, "probability": 0.6,
            "signal_time": at.isoformat(), "recorded_at": at.isoformat()}


class FakeSignals:
    """Plan 1's read_ai_signals: a cursor-ordered record with a retention watermark and resets."""

    def __init__(self):
        self.record, self.watermark, self.calls = [], 0, []

    def add(self, **kwargs):
        self.record.append(signal(len(self.record) + 1, **kwargs))
        return self.record[-1]

    async def call(self, method, body, *, epoch=None):
        from trader.ai.rpc_clients import RpcRefused
        assert method == "read_ai_signals"
        self.calls.append((body["after_cursor"], epoch))
        # The position in the record is the real cursor, so a test may corrupt a signal's "cursor" field.
        last = len(self.record) if self.record else self.watermark
        if body["after_cursor"] > last:
            raise RpcRefused("SIGNAL_CURSOR_AHEAD", "the record was reset")
        start = max(body["after_cursor"], self.watermark)
        page = [(position, s) for position, s in enumerate(self.record, 1) if position > start][:body["limit"]]
        rows = [s for _, s in page]
        return {"signals": rows, "next_cursor": page[-1][0] if page else start,
                "oldest_retained_cursor": self.watermark + 1, "gap": body["after_cursor"] < self.watermark}

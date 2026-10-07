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

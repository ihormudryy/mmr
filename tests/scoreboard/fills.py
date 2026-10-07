"""Helpers that build fills: pure FillFacts and real broker_fills rows."""
import datetime as dt
from decimal import Decimal

from trader.data.broker_state import BrokerFillRow, BrokerOrderRow, BrokerStateStore
from trader.scoreboard.round_trips import FillFact

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 10, 6, 14, 0, tzinfo=UTC)
T1 = dt.datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
T2 = dt.datetime(2026, 10, 6, 16, 0, tzinfo=UTC)


def fill(exec_id, side, quantity, price, commission, when, *, conid=265598, ref=None, symbol=None,
         naive=False):
    commission = None if commission is None else Decimal(str(commission))
    if naive:
        when = when.replace(tzinfo=None)
    return FillFact(exec_id=exec_id, conid=conid, side=side, quantity=Decimal(str(quantity)),
                    price=Decimal(str(price)), commission=commission, fill_time=when,
                    order_ref=ref, symbol=symbol)


def broker_store(db, migrator):
    store = BrokerStateStore(db)
    store.migrate(migrator)
    return store


def put_fill(db, store, account, exec_id, side, quantity, price, commission, when, *, conid=265598,
             currency="USD", ref=None, symbol="AAPL", order_id=None):
    order_id = order_id or f"ord-{exec_id}"

    def tx(conn):
        store.upsert_order_in_tx(conn, BrokerOrderRow(
            order_entity_id=order_id, account_id=account, conid=conid, symbol=symbol, order_group_id=None,
            leg=None, is_external=False, action=side, order_type="LMT", total_quantity=float(quantity),
            filled_quantity=float(quantity), avg_fill_price=float(price), limit_price=float(price),
            stop_price=None, tif="DAY", status="Filled", deleted=False, revision=1, source_timestamp=when))
        if ref is not None:
            store.bind_alias_in_tx(conn, "order_ref", ref, account, "", order_id, when)
        store.upsert_fill_in_tx(conn, BrokerFillRow(
            account_id=account, exec_id=exec_id, order_entity_id=order_id, perm_id=None, client_order_id=None,
            session_epoch="", conid=conid, side=side, quantity=float(quantity), price=float(price),
            commission=None if commission is None else float(commission),
            commission_currency=None if commission is None else currency, realized_pnl=None, fill_time=when,
            revision=1, source_timestamp=when))
    db.transaction(tx)


def set_commission(db, account, exec_id, commission, currency="USD"):
    db.execute("UPDATE broker_fills SET commission = ?, commission_currency = ? WHERE account_id = ? "
               "AND exec_id = ?", [float(commission), currency, account, exec_id])

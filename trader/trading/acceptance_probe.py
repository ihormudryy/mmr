"""The live OCA shrink probe and its durable acceptance mark (SP1 Plan 6 ruling 23, Task 7).

Two operator-only commands (ACL ``{"cli"}``), both refused unless the account is
paper, ``ai_paper.acceptance_probe`` is true and the experiment is ARMED:

- ``acceptance_mark_start`` writes the one durable mark: experiment, run, conid
  and the S entry's decision id ``<run_id>-e-s``, valid 24 h, one probe.
- ``acceptance_shrink_probe`` must name exactly what the mark names. It checks
  the open position (3 shares, entered by the marked decision) and S's two
  working exit legs (one OCA group of type 2), consumes the mark in the same
  transaction that records the probe intent, then modifies only the target:
  a copy of the broker-confirmed working ``Order`` with ``lmtPrice`` at the bid
  and ``displaySize`` 1. ``ib_async`` sends the whole order on a modify, so the
  copy keeps ``orderId``, ``permId``, ``ocaGroup``, ``ocaType``,
  ``totalQuantity``, ``action``, ``orderType``, ``tif`` and ``parentId``. It
  then re-reads the broker evidence and refuses ``PROBE_OCA_LOST`` when the
  link or the stop changed. It never changes a quantity, adds an order or
  touches the stop.
"""
from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
import logging
import time
from typing import Any, Callable, Optional

from trader.trading.command_coordinator import CommandValidationError

logger = logging.getLogger(__name__)

ACCEPTANCE_MARKS_MIGRATION_VERSION = 80
MARK_ACTION = "acceptance_mark_start"
PROBE_ACTION = "acceptance_shrink_probe"
MARK_LIFETIME = dt.timedelta(hours=24)
PROBE_QUANTITY = 3.0
MODIFY_CONFIRM_SECONDS = 10.0
_WORKING = ("Submitted", "PreSubmitted")

_DDL = """CREATE TABLE IF NOT EXISTS acceptance_marks (
    experiment_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    conid BIGINT NOT NULL,
    decision_id VARCHAR NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    probe_json VARCHAR
)"""


def apply_migration_80_acceptance_marks(migrator: Any) -> bool:
    return migrator.apply(ACCEPTANCE_MARKS_MIGRATION_VERSION, "p6_acceptance_marks", [_DDL])


def _refuse(code: str, message: str) -> CommandValidationError:
    return CommandValidationError(code, message)


class AcceptanceMarkStore:
    """Trader-owned rows in the journal database; only the probe service writes them."""

    def __init__(self, db: Any):
        self._db = db

    def get(self, experiment_id: str) -> Optional[dict]:
        row = self._db.execute(
            "SELECT experiment_id, run_id, conid, decision_id, created_at, expires_at, consumed_at "
            "FROM acceptance_marks WHERE experiment_id = ?", [experiment_id], fetch="one")
        if row is None:
            return None
        keys = ("experiment_id", "run_id", "conid", "decision_id", "created_at", "expires_at", "consumed_at")
        return dict(zip(keys, row))

    def marks(self) -> list[dict]:
        rows = self._db.execute("SELECT experiment_id FROM acceptance_marks ORDER BY created_at", fetch="all")
        return [self.get(row[0]) for row in rows]

    def insert(self, *, experiment_id: str, run_id: str, conid: int, decision_id: str, now: dt.datetime) -> None:
        def write(conn):
            if conn.execute("SELECT 1 FROM acceptance_marks WHERE experiment_id = ?", [experiment_id]).fetchone():
                raise _refuse("PROBE_MARK_EXISTS", f"experiment {experiment_id} already has an acceptance mark")
            conn.execute(
                "INSERT INTO acceptance_marks (experiment_id, run_id, conid, decision_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", [experiment_id, run_id, int(conid), decision_id, now, now + MARK_LIFETIME])
        self._db.transaction(write)

    def consume(self, experiment_id: str, probe: dict, now: dt.datetime) -> None:
        """One probe per mark: the consume and the probe intent commit together, or not at all."""
        def write(conn):
            row = conn.execute("SELECT consumed_at FROM acceptance_marks WHERE experiment_id = ?",
                               [experiment_id]).fetchone()
            if row is None or row[0] is not None:
                raise _refuse("PROBE_MARK_STALE", "the mark was consumed meanwhile")
            conn.execute("UPDATE acceptance_marks SET consumed_at = ?, probe_json = ? WHERE experiment_id = ?",
                         [now, json.dumps(probe, sort_keys=True, default=str), experiment_id])
        self._db.transaction(write)


class AcceptanceProbeService:
    def __init__(self, *, trader: Any, marks: AcceptanceMarkStore, experiments: Any, config: Any,
                 account_id: str, account_mode: str, broker: Any, quotes: Any, saga: Any,
                 evidence: Callable[[Optional[int]], dict], now: Callable[[], dt.datetime],
                 confirm_seconds: float = MODIFY_CONFIRM_SECONDS, pause: Callable[[float], None] = time.sleep):
        self._trader = trader
        self.marks = marks
        self._experiments = experiments
        self._config = config
        self._account_id = account_id
        self._account_mode = account_mode
        self._broker = broker
        self._quotes = quotes
        self._saga = saga
        self._evidence = evidence
        self._now = now
        self._confirm_seconds = confirm_seconds
        self._pause = pause

    # -- shared checks -----------------------------------------------------------------------
    def _require_scope(self, cmd: Any) -> Any:
        if cmd.principal != "cli":
            raise _refuse("PRINCIPAL_FORBIDDEN", "only the operator (cli) may run the acceptance probe")
        if getattr(self._config, "acceptance_probe", False) is not True:
            raise _refuse("PROBE_NOT_ENABLED", "ai_paper.acceptance_probe is false")
        if self._account_mode != "paper" or not str(self._account_id).startswith("DU"):
            raise _refuse("PROBE_NOT_PAPER", "the acceptance probe runs on a paper account only")
        record = self._experiments.active()
        if record is None or record.state != "ARMED":
            raise _refuse("PROBE_NOT_ARMED", "no ARMED experiment")
        return record

    @staticmethod
    def _body(cmd: Any, keys: frozenset) -> dict:
        body = dict(cmd.body or {})
        if set(body) != set(keys):
            raise _refuse("PROBE_REQUEST_INVALID", f"body must have exactly {sorted(keys)}")
        return body

    # -- acceptance_mark_start -------------------------------------------------------------------
    def mark_start(self, cmd: Any) -> dict:
        body = self._body(cmd, frozenset({"experiment_id", "run_id", "conid", "decision_id"}))
        record = self._require_scope(cmd)
        if body["experiment_id"] != record.experiment_id:
            raise _refuse("PROBE_NOT_ARMED", f"experiment {body['experiment_id']} is not the ARMED one")
        if body["decision_id"] != f"{body['run_id']}-e-s":
            raise _refuse("PROBE_MARK_MISMATCH", "the mark names the run's S entry, <run_id>-e-s")
        now = self._now()
        self.marks.insert(experiment_id=body["experiment_id"], run_id=body["run_id"], conid=int(body["conid"]),
                          decision_id=body["decision_id"], now=now)
        return {"marked": True, "experiment_id": body["experiment_id"], "expires_at": (now + MARK_LIFETIME).isoformat()}

    # -- acceptance_shrink_probe -----------------------------------------------------------------
    def probe(self, cmd: Any) -> dict:
        body = self._body(cmd, frozenset({"experiment_id", "run_id", "conid", "decision_id", "display_size"}))
        if body["display_size"] != 1:
            raise _refuse("PROBE_REQUEST_INVALID", "display_size must be 1")
        record = self._require_scope(cmd)
        mark = self._current_mark(body, record)
        conid = int(mark["conid"])
        self._require_position(conid, mark["decision_id"])
        target, stop, generation = self._linked_legs(conid, mark["decision_id"])
        working = self._ib_trade(target)
        bid = self._bid(conid)
        intent = {"command_id": cmd.command_id, "target_perm_id": target["perm_id"], "lmt_price": bid,
                  "display_size": 1, "stop_before": _leg_identity(stop)}
        self.marks.consume(mark["experiment_id"], intent, self._now())     # a crash after this is UNPROVEN
        modified = self._modified_copy(working.order, bid)
        self._place(working.contract, modified)
        after = self._confirmed(conid, target, stop, generation)
        return {"modified": True, "target_order_id": modified.orderId, "lmt_price": bid, "display_size": 1,
                "oca_group": after["target"]["oca_group"], "generation_id": after["generation_id"]}

    def _current_mark(self, body: dict, record: Any) -> dict:
        """The mark of the ARMED experiment; a mark of an older experiment is stale, never reused."""
        mark = self.marks.get(record.experiment_id)
        if mark is None:
            if body["experiment_id"] != record.experiment_id and self.marks.get(body["experiment_id"]) is not None:
                raise _refuse("PROBE_MARK_STALE", "the mark belongs to an experiment that is no longer ARMED")
            raise _refuse("PROBE_MARK_MISSING", f"no acceptance mark for experiment {record.experiment_id}")
        if mark["consumed_at"] is not None or mark["expires_at"] <= self._now():
            raise _refuse("PROBE_MARK_STALE", "the mark is expired or already consumed")
        named = (body["experiment_id"], body["run_id"], int(body["conid"]), body["decision_id"])
        if named != (mark["experiment_id"], mark["run_id"], int(mark["conid"]), mark["decision_id"]):
            raise _refuse("PROBE_MARK_MISMATCH", "the probe body does not match the acceptance mark")
        return mark

    def _require_position(self, conid: int, decision_id: str) -> None:
        snapshot = self._broker.capture(self._account_id)
        held = sum(float(p.quantity) for p in snapshot.positions if int(p.conid) == conid)
        if held != PROBE_QUANTITY:
            raise _refuse("PROBE_POSITION_MISMATCH", f"position {held:g} on {conid}, expected {PROBE_QUANTITY:g}")
        saga = self._saga.resume(f"aip-{decision_id}")
        if (saga is None or int(saga.conid) != conid or float(saga.filled_quantity) != PROBE_QUANTITY
                or saga.state != "PROTECTED"):
            raise _refuse("PROBE_MARK_MISMATCH", f"the open position was not entered by decision {decision_id}")

    def _linked_legs(self, conid: int, decision_id: str) -> tuple[dict, dict, Any]:
        evidence = self._evidence(conid)
        if evidence.get("capture_error"):
            raise _refuse("PROBE_OCA_NOT_FOUND", f"broker evidence unavailable: {evidence['capture_error']}")
        group = f"og-aip-{decision_id}"
        legs = [o for o in evidence.get("orders") or []
                if o.get("order_group_id") == group and o.get("status") in _WORKING]
        targets = [o for o in legs if o.get("leg") == "take_profit"]
        stops = [o for o in legs if o.get("leg") == "stop"]
        if len(targets) != 1 or len(stops) != 1 or not targets[0].get("oca_group") \
                or targets[0].get("oca_group") != stops[0].get("oca_group"):
            raise _refuse("PROBE_OCA_NOT_FOUND", "S needs exactly one working stop and one target in one OCA group")
        if targets[0].get("oca_type") != 2 or stops[0].get("oca_type") != 2:
            raise _refuse("PROBE_OCA_TYPE_NOT_2", "S's exit legs are not linked with OCA type 2")
        return targets[0], stops[0], evidence.get("generation_id")

    def _ib_trade(self, target: dict) -> Any:
        perm_id = target.get("perm_id")
        matches = [t for t in self._trader.client.ib.openTrades()
                   if perm_id and int(getattr(t.order, "permId", 0) or 0) == int(perm_id)]
        if len(matches) != 1:
            raise _refuse("PROBE_OCA_NOT_FOUND", f"no single working ib_async trade for target perm {perm_id}")
        return matches[0]

    def _bid(self, conid: int) -> float:
        quote = self._quotes.executable_quote(conid, side="SELL")
        bid = None if quote is None else getattr(quote, "bid", None)
        if bid is None or not bid > 0:
            raise _refuse("PROBE_QUOTE_UNAVAILABLE", f"no live bid for {conid}")
        return float(bid)

    @staticmethod
    def _modified_copy(order: Any, bid: float) -> Any:
        """The whole working order, with only the limit and the display size changed."""
        modified = copy.copy(order)
        modified.lmtPrice = bid
        modified.displaySize = 1
        return modified

    def _place(self, contract: Any, order: Any) -> None:
        loop = getattr(self._trader, "_main_loop", None)
        ib = self._trader.client.ib

        async def place():
            ib.placeOrder(contract, order)
        if loop is None:
            raise _refuse("PROBE_NOT_SENT", "trader event loop unavailable")
        asyncio.run_coroutine_threadsafe(place(), loop).result(timeout=10.0)

    def _confirmed(self, conid: int, target: dict, stop: dict, generation: Any) -> dict:
        """Re-read the broker after the modify: the link and the stop must be as before (else PROBE_OCA_LOST).

        The modify is confirmed by a new recorded event on the target (IB echoes the modified order) or a
        newer promoted generation. The stop may have shrunk meanwhile (a slice filled); it must not have
        been replaced, re-typed or grown.
        """
        before = {e["cursor"] for e in target.get("status_events") or []}
        deadline = time.monotonic() + self._confirm_seconds
        while True:
            evidence = self._evidence(conid)
            rows = {o.get("order_entity_id"): o for o in evidence.get("orders") or []}
            after_target, after_stop = rows.get(target["order_entity_id"]), rows.get(stop["order_entity_id"])
            fresh = evidence.get("generation_id") != generation or bool(
                after_target is not None and {e["cursor"] for e in after_target.get("status_events") or []} - before)
            if not evidence.get("capture_error") and after_target is not None and fresh:
                break
            if time.monotonic() >= deadline:
                raise _refuse("PROBE_OCA_LOST", "the modify was not confirmed by the broker in time")
            self._pause(0.2)
        group = stop.get("oca_group")
        linked = (after_stop is not None and after_target.get("oca_type") == 2 and after_stop.get("oca_type") == 2
                  and after_target.get("oca_group") == group and after_stop.get("oca_group") == group
                  and _leg_identity(after_stop) == _leg_identity(stop)
                  and float(after_stop.get("total_quantity") or 0) <= float(stop.get("total_quantity") or 0))
        if not linked:
            logger.critical("acceptance probe: the OCA link is gone after the modify (target %s, stop %s)",
                            after_target, after_stop)
            raise _refuse("PROBE_OCA_LOST", "after the modify the target or the stop is not in the OCA type 2 group")
        return {"target": after_target, "stop": after_stop, "generation_id": evidence.get("generation_id")}


def evidence_source(trader: Any) -> str:
    """``ib`` only when Trader.connect() set it for a real IB session; a composed test stack never does."""
    return "ib" if getattr(trader, "broker_evidence_source", None) == "ib" else "synthetic"


def read_broker_order_evidence(trader: Any, conid: Optional[int]) -> dict:
    from trader.data.broker_order_events import broker_order_evidence_in_tx
    store, account_id = trader.broker_state_store, trader.ib_account
    return trader.journal_db.transaction(
        lambda conn: broker_order_evidence_in_tx(conn, store, account_id, conid, source=evidence_source(trader)))


def _leg_identity(row: dict) -> dict:
    return {k: row.get(k) for k in ("order_entity_id", "perm_id", "action", "order_type")}

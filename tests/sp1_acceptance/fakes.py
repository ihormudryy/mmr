"""A scripted ``AcceptancePort`` for the scenario unit tests (Plan 6 Tasks 2 and 7).

A tiny broker model: an ENTER fills at once and leaves a working stop (and a
target when it has one, linked with OCA type 2); a PARTIAL_CLOSE re-protects
the remainder; a CLOSE flattens the conid. The clock moves only in ``sleep``.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from trader.acceptance.journal import canonical_text
from trader.acceptance.ports import OperatorChannelUnavailable, RemoteRefusal

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 17, 15, 0, tzinfo=UTC)          # 11:00 ET, a Friday session
AAPL, MSFT = 265598, 272093
BASE_DIGEST = "sha256:" + "d" * 64
WAIT_READS = frozenset({"get_positions", "get_open_orders", "get_command", "get_broker_order_evidence",
                        "get_experiment_trips", "get_scoreboard"})
WRITES = frozenset({"publish_ai_risk_policy", "submit_ai_paper_decision",
                    "acceptance_mark_start", "acceptance_shrink_probe"})


def target(filled, remaining=None, total=3.0):
    return ("take_profit", float(filled), None if remaining is None else float(remaining), float(total))


def stop(remaining, total=None):
    return ("stop", 0.0, float(remaining), float(total if total is not None else remaining))


def clean_reading(now, **changes):
    reading = {"account_id": "DU111111", "account_mode": "paper", "generation_id": 7,
               "net_liquidation": 1_000_000.0, "nlv_as_of": now.isoformat(), "daily_pnl": 0.0,
               "base_currency": "USD", "usd_per_base": 1.0, "positions": [], "working_orders": [],
               "unresolved_commands": [], "open_liquidation_roots": [], "exit_owner": None,
               "breaker_tripped": False, "experiment_state": "ARMED"}
    reading.update(changes)
    return reading


class FakePort:
    def __init__(self, *, operator=True):
        self.clock = NOW
        self.all_calls: list[tuple[str, str, dict]] = []
        self.experiment_state: Optional[str] = "ARMED"
        self.asks = {AAPL: 230.0, MSFT: 500.0}
        self.ask: Optional[float] = None                     # overrides every ask when set
        self.positions: dict[int, float] = {}
        self.orders: list[dict] = []
        self.commands: dict[str, dict] = {}
        self.never: set[str] = set()
        self.partial_override: Optional[dict] = None
        self.equity_row: Optional[dict] = None
        self.trips_rows: Optional[list] = None
        self.incidents: list = []
        self.generation = 10
        self.operator_enabled = operator
        self._script: Optional[list[dict]] = None
        self._script_after_probe: Optional[callable] = None
        self._probed = False
        self._enter_s_legs = (True, True)
        self.run_id = "acc-20260717-abcdef"
        self.version_state: Optional[str] = "ACTIVE"         # None: the trader knows no such version
        self.deployment_conids = [AAPL, MSFT]
        self.version_sessions = ("2026-07-17", "2026-08-14")   # first_session, expiry_session
        self.strategy_digest = "sha256:" + "5" * 64

    # -- scripting ----------------------------------------------------------------------------
    def never_fill(self, step):
        self.never.add(step)

    def hold(self, A=None, stop=False, extra_position=None):
        if A is not None:
            self.positions[AAPL] = float(A)
            if stop:
                self.orders.append(self._row(AAPL, "stop", A, group=f"og-aip-{self.run_id}-e-a"))
        if extra_position is not None:
            self.positions[int(extra_position)] = 1.0

    def forget_commands(self):
        self.commands.clear()

    def after_partial(self, *, liquidation_state, legs):
        self.partial_override = {"liquidation_state": liquidation_state, "legs": legs}

    def evidence_script(self, events, *, held=None):
        """One generation in which these events were recorded, in this order, after the probe."""
        self._script_after_probe = lambda: self._shrink_reading(events, held)

    def evidence_script_whole_fill_in_one_event(self):
        self.evidence_script([target(filled=3, remaining=0)])

    def evidence_script_no_fill_in_60s(self, held=3):
        self.evidence_script([], held=held)

    def evidence_script_for_enter_s(self, *, stop=True, target=True):
        self._enter_s_legs = (stop, target)

    # -- AcceptancePort ---------------------------------------------------------------------------
    @property
    def has_operator(self):
        return self.operator_enabled

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += dt.timedelta(seconds=seconds)

    def supervisor(self, method, body):
        return self._record("supervisor", method, body)

    def operator(self, method, body):
        if not self.operator_enabled:
            raise OperatorChannelUnavailable("no cli channel")
        return self._record("operator", method, body)

    def evidence(self, conid=None):
        return self.supervisor("get_broker_order_evidence", {"conid": conid})

    def trips(self, experiment_id):
        return self.supervisor("get_experiment_trips", {"experiment_id": experiment_id})

    # -- what the tests read ------------------------------------------------------------------------
    @property
    def calls(self):
        return [(p, m) for p, m, _ in self.all_calls if m not in WAIT_READS]

    def methods(self):
        return [m for _, m, _ in self.all_calls]

    def methods_used_for_assertions(self):
        return set(self.methods())

    def writes(self):
        return [m for m in self.methods() if m in WRITES]

    def body_of(self, method):
        return next(b for _, m, b in self.all_calls if m == method)

    def sent_bodies(self, method):
        return [canonical_text(b) for _, m, b in self.all_calls if m == method]

    def decision_ids(self):
        return [b["decision_id"] for _, m, b in self.all_calls if m == "submit_ai_paper_decision"]

    # -- the model --------------------------------------------------------------------------------
    def _record(self, principal, method, body):
        self.all_calls.append((principal, method, json.loads(json.dumps(body))))
        return getattr(self, f"_{method}")(body)

    def _row(self, conid, leg, quantity, *, group, oca_group=None, oca_type=None, status="Submitted", filled=0.0):
        self.generation += 1
        return {"order_entity_id": f"{group}:{leg}:{self.generation}", "conid": conid, "order_group_id": group,
                "leg": leg, "status": status, "total_quantity": float(quantity), "filled_quantity": filled,
                "remaining_quantity": float(quantity) - filled, "oca_group": oca_group, "oca_type": oca_type,
                "perm_id": 1000 + self.generation, "order_id": self.generation, "status_events": []}

    def _get_acceptance_preflight(self, _body):
        return clean_reading(self.clock, experiment_state=self.experiment_state,
                             positions=[{"conid": c, "quantity": q} for c, q in self.positions.items() if q],
                             working_orders=[o for o in self.orders if o["status"] in ("Submitted", "PreSubmitted")])

    def _get_experiment(self, _body):
        if self.experiment_state is None:
            return {"experiment": None}
        return {"experiment": {"experiment_id": "exp-" + "a" * 20, "state": self.experiment_state}}

    def _get_ai_deployment_version(self, body):
        if self.version_state is None:
            return {"found": False, "version": None}
        return {"found": True, "version": {
            "version_digest": body["version_digest"], "base_digest": BASE_DIGEST, "judgment_id": "jdg-fake-0001",
            "kind": "INITIAL", "prior_version_digest": None, "first_session": self.version_sessions[0],
            "expiry_session": self.version_sessions[1], "state": self.version_state}}

    def _get_ai_deployment(self, body):
        assert body["digest"] == BASE_DIGEST
        return {"deployment": {"conids": list(self.deployment_conids), "strategy_digest": self.strategy_digest}}

    def _publish_ai_risk_policy(self, body):
        return {"state": "RESOLVED", "outcome": {"revision": 1}, "error_code": None}

    def _get_snapshot(self, body):
        ask = self.ask if self.ask is not None else self.asks[body["instrument_id"]]
        return {"snapshot": {"bid": ask - 0.1, "ask": ask}}

    def _get_positions(self, _body):
        return {"positions": [{"instrument_id": c, "position": q} for c, q in self.positions.items() if q]}

    def _get_command(self, body):
        if body["command_id"] not in self.commands:
            raise RemoteRefusal("COMMAND_NOT_FOUND")
        return self.commands[body["command_id"]]

    def _get_broker_order_evidence(self, body):
        if self._probed and self._script is not None:
            return self._script
        conid = body.get("conid")
        orders = [o for o in self.orders if conid is None or o["conid"] == conid]
        return {"generation_id": self.generation, "promoted": True, "events_gapless": True, "orders": orders,
                "source": "synthetic"}

    def _get_scoreboard(self, body):
        sessions = [] if self.equity_row is None else [{"date": "2026-07-17", **self.equity_row}]
        return {"sessions": sessions, "incidents": self.incidents}

    def _get_experiment_trips(self, body):
        return {"trips": self.trips_rows or []}

    def _acceptance_mark_start(self, body):
        return {"state": "RESOLVED", "outcome": {"marked": True}, "error_code": None}

    def _acceptance_shrink_probe(self, body):
        self._probed = True
        if self._script_after_probe is not None:
            self._script = self._script_after_probe()
        return {"state": "RESOLVED", "outcome": {"modified": True}, "error_code": None}

    def _submit_ai_paper_decision(self, body):
        command_id = f"aip-{body['decision_id']}"
        if command_id in self.commands:
            return self.commands[command_id]
        step = body["decision_id"].rsplit("-", 2)[-2] + "-" + body["decision_id"].rsplit("-", 1)[-1]
        conid, action = body["conid"], body["action"]
        group = f"og-{command_id}"
        if action == "ENTER":
            receipt = {"command_id": command_id, "state": "SUBMITTED", "error_code": None, "outcome": {}}
            if step not in self.never:
                self.positions[conid] = self.positions.get(conid, 0.0) + body["quantity"]
                has_stop, has_target = self._enter_s_legs if step == "e-s" else (True, True)
                linked = body["target_price"] is not None
                oca = f"oca-{group}" if linked else None
                if has_stop:
                    self.orders.append(self._row(conid, "stop", body["quantity"], group=group, oca_group=oca,
                                                 oca_type=2 if linked else None))
                if linked and has_target:
                    self.orders.append(self._row(conid, "take_profit", body["quantity"], group=group,
                                                 oca_group=oca, oca_type=2))
        elif action == "PARTIAL_CLOSE":
            self.positions[conid] -= body["quantity"]
            self._cancel(conid)
            remainder = self.positions[conid]
            override = self.partial_override or {}
            state = override.get("liquidation_state", "DONE")
            for leg, quantity, oca in override.get("legs", [("stop", remainder, "g-re"), ("take_profit", remainder,
                                                                                          "g-re")]):
                self.orders.append(self._row(conid, leg, quantity, group=f"{command_id}-reprotect", oca_group=oca,
                                             oca_type=2))
            receipt = {"command_id": command_id, "state": "OUTCOME_UNKNOWN", "error_code": "CLOSE_PENDING",
                       "outcome": {"close_root_id": command_id}}
            self.commands[command_id] = {"command_id": command_id, "state": "RESOLVED", "error_code": None,
                                         "outcome": {"liquidation_state": state}}
            return receipt
        else:
            self.positions[conid] = 0.0
            self._cancel(conid)
            self._script = None
            receipt = {"command_id": command_id, "state": "OUTCOME_UNKNOWN", "error_code": "CLOSE_PENDING",
                       "outcome": {"close_root_id": command_id}}
            self.commands[command_id] = {"command_id": command_id, "state": "RESOLVED", "error_code": None,
                                         "outcome": {"liquidation_state": "CLOSED"}}
            return receipt
        self.commands[command_id] = receipt
        return receipt

    def _cancel(self, conid):
        for order in self.orders:
            if order["conid"] == conid and order["status"] in ("Submitted", "PreSubmitted"):
                order["status"] = "Cancelled"

    def _shrink_reading(self, events, held):
        legs = {o["leg"]: o for o in self.orders if o["conid"] == AAPL and o["status"] == "Submitted"
                and o["order_group_id"].endswith("-e-s")}
        target_row, stop_row = dict(legs["take_profit"]), dict(legs["stop"])
        target_row["status_events"], stop_row["status_events"] = [], []
        cursor, filled = 0, 0.0
        for leg, fill, remaining, total in events:
            cursor += 1
            row = target_row if leg == "take_profit" else stop_row
            if leg == "take_profit":
                filled = fill
                status = "Filled" if fill >= total else "Submitted"
                event = {"cursor": cursor, "status": status, "filled_quantity": fill,
                         "remaining_quantity": total - fill, "total_quantity": total, "oca_type": 2}
                row.update(status=status, filled_quantity=fill, remaining_quantity=total - fill)
                if status == "Filled":
                    stop_row["status"] = "Cancelled"
            else:
                event = {"cursor": cursor, "status": "Submitted", "filled_quantity": 0.0,
                         "remaining_quantity": remaining, "total_quantity": total, "oca_type": 2}
                row.update(remaining_quantity=remaining, total_quantity=total)
            row["status_events"].append(event)
        self.positions[AAPL] = float(held) if held is not None else 3.0 - filled
        if self.positions[AAPL] == 0:
            for order in self.orders:
                if order["conid"] == AAPL and order["status"] == "Submitted":
                    order["status"] = "Cancelled"
        return {"generation_id": self.generation + 100, "promoted": True, "events_gapless": True,
                "orders": [target_row, stop_row], "source": "synthetic"}

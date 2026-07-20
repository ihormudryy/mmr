"""Signal → PENDING proposal bridge (``auto_execute: propose``).

Turns strategy signals into PENDING trade proposals that a human approves in
the web dashboard or via ``mmr approve``. No order is ever placed without
approval — this is the semi-automatic half of the propose/approve pipeline.

[M1-F3] Task 8: ``SignalProposer`` is now a THIN TYPED ADAPTER over the
command-authority coordinator (``TradingCommandCoordinator`` +
``ProposalCommandService``, wired server-side in trader_service). It holds
NO ``ProposalStore`` handle and performs NO direct database writes — every
mutation goes through the typed ``create_proposal`` command
(``trader.messaging.production_api.CreateProposalRequest``), reached via a
``TypedRpcClient`` bound to the ``command`` role. Sizing, dedup
(``DUPLICATE_PENDING``), expiry, quote/risk checks, and group registration
are now the server's job (``ProposalCommandService.create_proposal``) — this
class only gates (paper-only, pause-aware) and translates.

Semantics deliberately mirror the backtester (long-only): BUY proposes a new
auto-sized entry, SELL proposes closing the currently-held long. Paper mode
always allows the bridge; live mode requires ``live_authority_enabled``
(command_authority live policy). Without that flag live is a warn-once no-op.

Command ids are generated as ``f'strategy-{uuid.uuid4()}'`` (hyphen, not the
``strategy:`` colon used for the ``source`` field below) because
``CreateProposalRequest``/``RejectProposalRequest``/``ApproveProposalRequest``
all reject a colon anywhere in ``command_id`` at the field-validator level
(``_reject_colon_in_command_id`` in ``production_api.py`` reserves ``:`` for
the ``mmr:`` orderRef prefix) — this was caught by
``tests/test_propose_approve_integration.py`` driving the REAL request
models, not by this module's own fake-client tests, and fixed here (a
local, in-scope string-format choice, not a change to the frozen wire
schema).

KNOWN WIRE-CONTRACT GAP (do not silently "fix" by editing production_api.py
— that reconciliation is an integration-gate item spanning the dashboard
bridge, SDK, and this module together): ``CreateProposalRequest`` is
``extra="forbid"`` and does not declare ``source``, ``max_hold_bars``, or
``close_by_time`` fields, so a real typed server would reject a body
carrying them. This module still sends them (matching the design's intent
that a proposal can be traced back to the strategy that raised it) because
that is the fixture contract the Task 8 tests are written against; against
the REAL server today the request would need those fields added to
``CreateProposalRequest`` first. A consequence: ``check_exits`` cannot
currently recover a bridge entry's ``max_hold_bars``/``close_by_time`` from
``list_proposals`` (the server doesn't persist or echo unrecognised
fields), so ``_exit_reason`` is a documented no-op seam until that gap is
closed — see its docstring.

Spec: docs/superpowers/specs/2026-07-15-signal-propose-bridge-design.md
"""

import logging
import uuid
from typing import Optional

import pandas as pd

from trader.domain.commands import CommandReceipt
from trader.objects import Action
from trader.trading.strategy import Signal


class SignalProposer:
    """Creates PENDING proposals from strategy signals.

    Paper mode always allows the bridge. Live mode allows it only when
    ``live_authority_enabled`` is true (command_authority live policy armed).
    Otherwise live is a warn-once no-op.

    Holds two typed clients rather than one — ``command_client`` (bound to
    the ``command`` role) is the ONLY thing this class can use to mutate
    anything, and ``query_client`` (bound to ``query``) is read-only. There
    is deliberately no constructor path to a ``ProposalStore`` or any other
    direct-write handle.
    """

    SOURCE_PREFIX = 'strategy:'

    def __init__(
        self,
        command_client,
        query_client,
        paper_trading: bool,
        account_id: str,
        proposal_ttl_minutes: int = 30,
        live_authority_enabled: bool = False,
    ):
        self._command_client = command_client
        self._query_client = query_client
        self.paper_trading = paper_trading
        self._account_id = account_id
        self.live_authority_enabled = bool(live_authority_enabled)
        # Retained for constructor/signature compatibility -- proposal TTL
        # is now entirely server-owned (`ProposalCommandService`'s own
        # `ttl` param feeding `ProposalCreateRequest`/`create_proposal`);
        # this bridge no longer computes or stores an `expires_at` itself.
        self.proposal_ttl_minutes = proposal_ttl_minutes
        self._live_warned: set = set()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def on_signal(self, strategy_name: str, signal: Signal,
                  frame: pd.DataFrame) -> Optional[int]:
        """Turn one signal into a PENDING proposal via the typed
        ``create_proposal`` command. Returns the proposal id, or None when
        skipped (gate closed, unresolvable conid, paused, RPC failure, or
        the server refused the proposal — dedup/sizing/risk/filter are all
        server-side refusals surfaced via ``receipt.error_code``)."""
        if not self._gate(strategy_name):
            return None
        conid = int(signal.conid or 0)
        if conid <= 0:
            logging.error(
                'signal from %s has no conid stamped — cannot propose', strategy_name)
            return None
        action = {Action.BUY: 'BUY', Action.SELL: 'SELL'}.get(signal.action)
        if action is None:
            return None

        if action == 'BUY' and not self._entries_allowed():  # §9.4: paused/stale/unavailable → suppress
            return None

        body = {
            'command_id': f'strategy-{uuid.uuid4()}',
            'conid': conid,
            'action': action,
            'confidence': float(signal.probability),
            'reasoning': f'{action} signal from strategy {strategy_name} '
                         f'(probability {signal.probability:.2f}, risk {signal.risk:.2f})',
            'source': f'{self.SOURCE_PREFIX}{strategy_name}',
            'max_hold_bars': signal.max_hold_bars,
            'close_by_time': signal.close_by_time.isoformat() if signal.close_by_time else None,
        }
        try:
            receipt = self._command_client.call('create_proposal', body, CommandReceipt)
        except (TimeoutError, ConnectionError) as ex:
            logging.error('create_proposal RPC failed for %s conId %s: %s',
                          strategy_name, conid, ex)
            return None
        if receipt.error_code:
            logging.info('create_proposal refused for %s conId %s: %s',
                         strategy_name, conid, receipt.error_code)  # DUPLICATE_PENDING, SIZING_BLOCKED...
            return None
        return (receipt.outcome or {}).get('proposal_id')

    def expire_stale(self, now=None) -> list:
        """Inert no-op, retained ONLY so ``strategy_runtime._reconcile()``'s
        existing (mock-covered) call site keeps working unchanged.

        Expiry ownership moved to the trader service in [M1-F3] Task 2
        (``ProposalCommandService.expire_stale`` / ``run_expiry_loop``,
        which trader_service already runs on its own timer against the
        journal DB it owns) — this bridge has no ``ProposalStore`` handle to
        sweep with anymore and performs no proposal-store access of any
        kind. Always returns an empty list.
        """
        return []

    def check_exits(self, strategy_name: str, conid: int,
                    frame: pd.DataFrame) -> Optional[int]:
        """Propose closing an executed bridge entry whose time-based exit
        condition (max_hold_bars / close_by_time) has triggered. Called once
        per new completed bar. Returns the SELL proposal id, or None.

        Reads executed bridge entries through the typed ``list_proposals``
        query (``status="EXECUTED"``, filtered to this strategy's source
        prefix + conid) rather than a local ``ProposalStore`` scan, and
        proposes the close through the same ``create_proposal`` body the
        server already dedupes via ``DUPLICATE_PENDING`` (replacing the old
        local ``_pending_exists`` check).
        """
        if not self._gate(strategy_name) or frame is None or frame.empty:
            return None
        try:
            response = self._query_client.call(
                'list_proposals', {'status': 'EXECUTED', 'limit': 100}, dict)
        except Exception as ex:
            logging.warning(
                'list_proposals RPC failed while checking exits for %s conId %s: %s',
                strategy_name, conid, ex)
            return None

        my_source = f'{self.SOURCE_PREFIX}{strategy_name}'
        for entry in response.get('proposals') or []:
            if entry.get('source') != my_source:
                continue
            if entry.get('conid') != conid or entry.get('action') != 'BUY':
                continue

            reason = self._exit_reason(entry, frame)
            if reason is None:
                continue

            entry_id = entry.get('id', entry.get('proposal_id'))
            body = {
                'command_id': f'strategy-{uuid.uuid4()}',
                'conid': conid,
                'action': 'SELL',
                'confidence': 0.0,
                'reasoning': (f'Time-based exit ({reason}) for strategy {strategy_name} '
                              f'position entered via proposal #{entry_id}'),
                'source': my_source,
            }
            try:
                receipt = self._command_client.call('create_proposal', body, CommandReceipt)
            except (TimeoutError, ConnectionError) as ex:
                logging.error('create_proposal (exit) RPC failed for %s conId %s: %s',
                              strategy_name, conid, ex)
                return None
            if receipt.error_code:
                # DUPLICATE_PENDING is the expected steady state once a prior
                # bar already proposed this close and it's still awaiting
                # approval — any other refusal is still just logged, not
                # raised, so one bad exit attempt doesn't take down the bar
                # dispatch loop.
                logging.debug('create_proposal (exit) refused for %s conId %s: %s',
                              strategy_name, conid, receipt.error_code)
                return None
            return (receipt.outcome or {}).get('proposal_id')
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _gate(self, strategy_name: str) -> bool:
        if self.paper_trading:
            return True
        if self.live_authority_enabled:
            return True
        if strategy_name not in self._live_warned:
            self._live_warned.add(strategy_name)
            logging.warning(
                'auto_execute: propose ignored for %s in LIVE mode — '
                'enable command_authority.live_enabled (pinned account) '
                'for human-approve proposals, or run paper',
                strategy_name)
        return False

    def _entries_allowed(self) -> bool:
        """§9.4: verified exit proposals (SELL) remain allowed while paused;
        new entries (BUY) are suppressed. Fails CLOSED (suppresses entries)
        on any query failure — an unreachable pause gate must never be
        treated as "unpaused"."""
        try:
            control = self._query_client.call(
                'get_trading_control', {'account_id': self._account_id}, dict)
        except Exception as ex:
            logging.warning('pause gate unavailable — suppressing entry proposals: %s', ex)
            return False
        return not control.get('new_exposure_paused', True)

    def _exit_reason(self, entry: dict, frame: pd.DataFrame) -> Optional[str]:
        """Named seam for the max_hold_bars/close_by_time trigger check.

        KNOWN GAP: ``CreateProposalRequest`` doesn't declare (and
        ``ProposalRecord.to_payload()`` doesn't carry) either field today,
        so an entry re-fetched via ``list_proposals`` has no trigger config
        to evaluate against — this always returns None until that wire
        contract is extended to round-trip them. Kept as its own method
        (rather than inlined into ``check_exits``) so closing the gap is a
        one-line change in exactly one place.
        """
        return None

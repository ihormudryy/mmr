"""Signal → PENDING proposal bridge (``auto_execute: propose``).

Turns strategy signals into PENDING TradeProposals that a human approves in
the web dashboard or via ``mmr approve``. No order is ever placed without
approval — this is the semi-automatic half of the propose/approve pipeline.

Semantics deliberately mirror the backtester (long-only): BUY proposes a new
auto-sized entry, SELL proposes closing the currently-held long and is
ignored when flat. Time-based exit conditions on a BUY signal
(``max_hold_bars`` / ``close_by_time``) — which only the backtester honored
before — are recorded on the proposal and turned into SELL close proposals
by ``check_exits`` once the entry has executed and the condition triggers.

Gated to paper trading: in live mode every call is a warn-once no-op.

Bridge proposals carry ``metadata.expires_at`` and are expired
(PENDING → EXPIRED) on the next bridge invocation once past their TTL — a
stale 1-min intraday entry must not sit in the dashboard looking actionable.
Proposals from other sources are never touched.

Spec: docs/superpowers/specs/2026-07-15-signal-propose-bridge-design.md
"""

import datetime as dt
import logging
from typing import Optional

import pandas as pd

from trader.data.proposal_store import ProposalStore
from trader.objects import Action
from trader.trading.position_sizing import (
    PortfolioState,
    PositionSizer,
    PositionSizingConfig,
    VolatilityInfo,
    compute_atr,
)
from trader.trading.proposal import ExecutionSpec, ProposalStatus, TradeProposal
from trader.trading.strategy import Signal


class SignalProposer:
    """Creates PENDING proposals from strategy signals (paper mode only)."""

    SOURCE_PREFIX = 'strategy:'

    def __init__(
        self,
        proposal_store: ProposalStore,
        trader_client,
        paper_trading: bool,
        sizing_config: Optional[PositionSizingConfig] = None,
        proposal_ttl_minutes: int = 30,
    ):
        self.proposal_store = proposal_store
        self.trader_client = trader_client
        self.paper_trading = paper_trading
        self.sizing_config = sizing_config or PositionSizingConfig.load()
        self.proposal_ttl_minutes = proposal_ttl_minutes
        self._live_warned: set = set()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def on_signal(self, strategy_name: str, signal: Signal,
                  frame: pd.DataFrame) -> Optional[int]:
        """Turn one signal into a PENDING proposal. Returns the proposal id,
        or None when skipped (dedup, flat SELL, live mode, sizing blocked,
        unresolvable conid, trader_service unreachable)."""
        if not self._gate(strategy_name):
            return None

        conid = int(signal.conid or 0)
        if conid <= 0:
            logging.error(
                'signal from %s has no conid stamped — cannot propose', strategy_name)
            return None

        if signal.action == Action.BUY:
            action = 'BUY'
        elif signal.action == Action.SELL:
            action = 'SELL'
        else:
            return None

        self._expire_stale()

        if self._pending_exists(strategy_name, conid, action):
            logging.debug('proposal for %s %s conId %s already pending — skipping',
                          strategy_name, action, conid)
            return None

        secdef = self._resolve(conid, strategy_name)
        if secdef is None:
            return None

        if action == 'BUY':
            return self._propose_buy(strategy_name, signal, conid, secdef, frame)
        return self._propose_close(
            strategy_name, conid, secdef,
            reasoning=f'SELL signal from strategy {strategy_name}',
            confidence=signal.probability,
        )

    def check_exits(self, strategy_name: str, conid: int,
                    frame: pd.DataFrame) -> Optional[int]:
        """Propose closing an executed bridge entry whose time-based exit
        condition (max_hold_bars / close_by_time) has triggered. Called once
        per new completed bar. Returns the SELL proposal id, or None."""
        if not self._gate(strategy_name) or frame is None or frame.empty:
            return None

        for entry in self.proposal_store.query(status=ProposalStatus.EXECUTED.value, limit=100):
            meta = entry.metadata or {}
            if not (entry.source or '').startswith(self.SOURCE_PREFIX):
                continue
            if meta.get('strategy') != strategy_name or meta.get('conid') != conid:
                continue
            if entry.action != 'BUY' or meta.get('exit_proposed'):
                continue

            reason = self._exit_reason(entry, meta, frame)
            if reason is None:
                continue

            held = self._held_position(conid)
            if held is None:
                # trader_service unreachable — retry on the next bar rather
                # than flagging the entry and losing the exit forever.
                return None
            if held <= 0:
                # Position already closed by other means; stop re-checking.
                self.proposal_store.update_metadata(entry.id, {'exit_proposed': True})
                continue

            if self._pending_exists(strategy_name, conid, 'SELL'):
                # An exit is already awaiting approval. Leave the entry
                # unflagged so the exit re-proposes if that one expires.
                return None

            exit_id = self._add_proposal(
                strategy_name, secdef=None, conid=conid, action='SELL',
                quantity=held, symbol=entry.symbol,
                exchange=entry.exchange, currency=entry.currency,
                reasoning=(f'Time-based exit ({reason}) for strategy '
                           f'{strategy_name} position entered via proposal #{entry.id}'),
                confidence=0.0,
                extra_metadata={'exit_reason': reason, 'entry_proposal_id': entry.id},
            )
            if exit_id is not None:
                self.proposal_store.update_metadata(entry.id, {'exit_proposed': True})
            return exit_id
        return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _gate(self, strategy_name: str) -> bool:
        if self.paper_trading:
            return True
        if strategy_name not in self._live_warned:
            self._live_warned.add(strategy_name)
            logging.warning(
                'auto_execute: propose is paper-only — ignoring signals from %s '
                'in LIVE mode', strategy_name)
        return False

    def _exit_reason(self, entry: TradeProposal, meta: dict,
                     frame: pd.DataFrame) -> Optional[str]:
        close_by = meta.get('close_by_time')
        if close_by:
            try:
                if frame.index[-1].time() >= dt.time.fromisoformat(str(close_by)):
                    return 'close_by_time'
            except ValueError:
                logging.error('proposal #%s has malformed close_by_time %r',
                              entry.id, close_by)
        max_hold = meta.get('max_hold_bars')
        if max_hold is not None:
            entry_ts = entry.updated_at or entry.created_at
            if entry_ts is not None:
                bars_held = int((frame.index > pd.Timestamp(entry_ts)).sum())
                if bars_held >= int(max_hold):
                    return 'max_hold_bars'
        return None

    def _expire_stale(self) -> None:
        now = dt.datetime.now()
        for p in self.proposal_store.query(status=ProposalStatus.PENDING.value, limit=200):
            if not (p.source or '').startswith(self.SOURCE_PREFIX):
                continue
            expires_at = (p.metadata or {}).get('expires_at')
            if not expires_at:
                continue
            try:
                if dt.datetime.fromisoformat(str(expires_at)) <= now:
                    self.proposal_store.try_transition(
                        p.id, ProposalStatus.PENDING.value, ProposalStatus.EXPIRED.value)
                    logging.info('expired stale bridge proposal #%s (%s %s)',
                                 p.id, p.action, p.symbol)
            except ValueError:
                logging.error('proposal #%s has malformed expires_at %r', p.id, expires_at)

    def _pending_exists(self, strategy_name: str, conid: int, action: str) -> bool:
        for p in self.proposal_store.query(status=ProposalStatus.PENDING.value, limit=200):
            meta = p.metadata or {}
            if (p.action == action
                    and meta.get('strategy') == strategy_name
                    and meta.get('conid') == conid):
                return True
        return False

    def _resolve(self, conid: int, strategy_name: str):
        try:
            defs = self.trader_client.rpc().resolve_symbol(conid)
        except Exception as ex:
            logging.error('resolve_symbol(%s) failed for %s: %s', conid, strategy_name, ex)
            return None
        if not defs:
            logging.error(
                'conId %s for strategy %s not found in local universe DB — '
                'cannot propose (register it via `universe add`)', conid, strategy_name)
            return None
        return defs[0]

    def _portfolio_state(self) -> Optional[PortfolioState]:
        """Account snapshot for sizing. None when the account values RPC
        fails — sizing without account state would be garbage, so we refuse."""
        state = PortfolioState()
        try:
            acct = self.trader_client.rpc().get_account_values()
        except Exception as ex:
            logging.error('get_account_values RPC failed — skipping proposal: %s', ex)
            return None
        if acct:
            state.net_liquidation = float((acct.get('NetLiquidation') or {}).get('value', 0))
            state.gross_position_value = float((acct.get('GrossPositionValue') or {}).get('value', 0))
            state.available_funds = float((acct.get('AvailableFunds') or {}).get('value', 0))
        try:
            items = self.trader_client.rpc().get_portfolio()
            state.position_count = len(items or [])
        except Exception as ex:
            logging.warning('get_portfolio RPC failed — sizing without position count: %s', ex)
            state.rpc_errors.append(f'portfolio: {ex}')
        return state

    def _held_position(self, conid: int) -> Optional[float]:
        """Current position for conid. None on RPC failure (≠ flat)."""
        try:
            items = self.trader_client.rpc().get_portfolio()
        except Exception as ex:
            logging.error('get_portfolio RPC failed — cannot determine position '
                          'for conId %s: %s', conid, ex)
            return None
        total = 0.0
        for item in items or []:
            contract = getattr(item, 'contract', None)
            if contract is not None and getattr(contract, 'conId', None) == conid:
                total += float(item.position)
        return total

    def _propose_buy(self, strategy_name: str, signal: Signal, conid: int,
                     secdef, frame: pd.DataFrame) -> Optional[int]:
        state = self._portfolio_state()
        if state is None:
            return None

        price = float(frame['close'].iloc[-1]) if not frame.empty else 0.0
        volatility = None
        try:
            atr = compute_atr(frame['high'].tolist(), frame['low'].tolist(),
                              frame['close'].tolist())
            if atr and price > 0:
                volatility = VolatilityInfo(atr=atr, price=price)
        except Exception:
            pass  # sizing degrades gracefully without ATR

        result = PositionSizer(self.sizing_config).compute(
            confidence=signal.probability, portfolio_state=state,
            price=price, volatility=volatility,
        )
        if result.amount_usd <= 0:
            logging.warning(
                'sizer blocked BUY proposal for %s conId %s (%s): %s',
                strategy_name, conid, result.capped_by, '; '.join(result.warnings))
            return None

        extra = {
            'auto_sized': True,
            'sizing_result': {
                'amount': result.amount_usd,
                'reasoning': result.reasoning,
                'capped_by': result.capped_by,
                'warnings': result.warnings,
            },
        }
        if signal.max_hold_bars is not None:
            extra['max_hold_bars'] = int(signal.max_hold_bars)
        if signal.close_by_time is not None:
            extra['close_by_time'] = signal.close_by_time.isoformat()

        return self._add_proposal(
            strategy_name, secdef=secdef, conid=conid, action='BUY',
            amount=result.amount_usd,
            reasoning=(f'BUY signal from strategy {strategy_name} @ ~{price:.2f} '
                       f'(probability {signal.probability:.2f}, risk {signal.risk:.2f})'),
            confidence=signal.probability,
            extra_metadata=extra,
        )

    def _propose_close(self, strategy_name: str, conid: int, secdef,
                       reasoning: str, confidence: float) -> Optional[int]:
        held = self._held_position(conid)
        if held is None:
            return None
        if held <= 0:
            logging.info('SELL signal from %s for conId %s but position is %s — '
                         'nothing to close (long-only)', strategy_name, conid, held)
            return None
        return self._add_proposal(
            strategy_name, secdef=secdef, conid=conid, action='SELL',
            quantity=held, reasoning=reasoning, confidence=confidence,
        )

    def _add_proposal(self, strategy_name: str, secdef, conid: int, action: str,
                      reasoning: str, confidence: float,
                      quantity: Optional[float] = None,
                      amount: Optional[float] = None,
                      symbol: Optional[str] = None,
                      exchange: Optional[str] = None,
                      currency: Optional[str] = None,
                      extra_metadata: Optional[dict] = None) -> Optional[int]:
        if secdef is not None:
            symbol = secdef.symbol
            currency = secdef.currency or ''
            # Exchange hint only for non-USD listings — US resolution routes
            # via SMART; forcing a primary-exchange hint would change routing.
            exchange = secdef.primaryExchange if currency and currency != 'USD' else ''

        expires_at = (dt.datetime.now()
                      + dt.timedelta(minutes=self.proposal_ttl_minutes)).isoformat()
        metadata = {
            'strategy': strategy_name,
            'conid': conid,
            'expires_at': expires_at,
        }
        if extra_metadata:
            metadata.update(extra_metadata)

        proposal = TradeProposal(
            symbol=symbol or '',
            action=action,
            quantity=quantity,
            amount=amount,
            execution=ExecutionSpec(),
            reasoning=reasoning,
            confidence=confidence,
            source=f'{self.SOURCE_PREFIX}{strategy_name}',
            metadata=metadata,
            exchange=exchange or '',
            currency=currency or '',
        )
        try:
            pid = self.proposal_store.add(proposal)
        except Exception:
            logging.exception('failed to persist %s proposal for %s conId %s',
                              action, strategy_name, conid)
            return None
        logging.info('created PENDING proposal #%s: %s %s (strategy %s) — '
                     'approve in dashboard or `mmr approve %s`',
                     pid, action, symbol, strategy_name, pid)
        return pid

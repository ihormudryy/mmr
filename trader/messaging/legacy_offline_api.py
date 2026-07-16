"""Legacy dill/object-returning RPC surface — offline simulation ONLY (G0 Task 4).

``trader_service_api.TraderServiceApi`` used to carry five extra methods
directly: ``place_order_simple``, ``place_expressive_order``,
``place_standalone_order``, ``set_risk_limits``, ``cancel_all``. They're
pulled out into this subclass because they're exactly the production
authorization-bypass surface Task 4 closes — they place/cancel live orders
and mutate risk limits with no typed-RPC schema validation, and the
``RPCServer``/``RPCClient`` pair in ``clientserver.py`` that serves them
returns raw Python objects over msgpack with a ``dill`` fallback for
anything msgpack can't encode natively (see ``typed_rpc.py``'s module
docstring for why that's a meaningfully different trust boundary from the
authenticated, schema-validated typed transport).

``Trader.connect()`` only ever constructs this class — and only ever starts
a legacy ``RPCServer`` for it — when BOTH ``simulation=True`` AND
``unsafe_legacy_rpc=True`` (see ``production_api.validate_rpc_mode``, the
fail-closed guard that makes any other combination raise ``ValueError``
before a socket is ever bound). Production runs ONLY the typed
query/command/feed servers built from
``production_api.build_production_registry``; it never imports or
constructs ``LegacyOfflineTraderServiceApi`` at all.

``skip_risk_gate`` (on ``place_order_simple``) stays exactly what it was: an
internal keyword understood only by this offline-simulation RPC method and
``Executioner`` — it is not part of any typed-RPC request schema, and
``[M1-F3]`` is expected to replace it with a coordinator-computed
``RiskDirection`` when it builds the real command surface.
"""

from ib_async.contract import Contract
from ib_async.order import Trade
from reactivex.abc import DisposableBase
from reactivex.disposable import Disposable
from reactivex.observer import Observer
from trader.common.logging_helper import setup_logging
from trader.common.reactivex import SuccessFail
from trader.messaging.clientserver import rpcmethod
from trader.messaging.trader_service_api import TraderServiceApi
from typing import Optional

import asyncio


logging = setup_logging(module_name='legacy_offline_api')


class LegacyOfflineTraderServiceApi(TraderServiceApi):
    """``TraderServiceApi`` plus the direct-order / risk-mutation methods,
    for the offline-simulation-only legacy RPC path. See module docstring."""

    @rpcmethod
    async def place_order_simple(
        self, contract: Contract,
        action: str,
        equity_amount: Optional[float],
        quantity: Optional[float],
        limit_price: Optional[float],
        market_order: bool = False,
        stop_loss_percentage: float = 0.0,
        algo_name: str = 'global',
        debug: bool = False,
        skip_risk_gate: bool = False,
    ) -> SuccessFail[Trade]:
        # Proposal-approval gate: when require_proposal_approval is on in
        # trader.yaml, reject direct buy/sell unless the caller explicitly
        # flagged this as a liquidation (skip_risk_gate=True). All actionable
        # *new* trades have to come in via place_expressive_order, which the
        # approve() path uses after a proposal has been reviewed. Defensive
        # against LLM loops / scripts firing direct orders that weren't in
        # a reviewed plan.
        if getattr(self.trader, 'require_proposal_approval', False) and not skip_risk_gate:
            return SuccessFail.fail(
                error=(
                    'Direct order rejected: require_proposal_approval is true. '
                    'New trades must go through propose → approve (see `mmr propose` '
                    'and `mmr approve`). Set skip_risk_gate=True only for '
                    'liquidation paths (close-all, resize-positions).'
                ),
            )
        # todo: we'll have to make the cli async so we can subscribe to the trade
        # changes as orders get hit etc
        logging.warning('place_order_simple() is not complete, your mileage may vary')
        from trader.trading.trading_runtime import Action
        # Strict: any string not literally BUY/SELL must be refused, not silently
        # coerced. The old `'BUY' in action else SELL` turned a typo like 'BYU'
        # (or lowercase 'buy') into a live SELL.
        _a = str(action).strip().upper()
        if _a == 'BUY':
            act = Action.BUY
        elif _a == 'SELL':
            act = Action.SELL
        else:
            return SuccessFail.fail(
                error=f'invalid action {action!r}: expected "BUY" or "SELL"')

        task = asyncio.Event()
        disposable: DisposableBase = Disposable()
        result: Optional[SuccessFail] = None

        def on_next(trade: Trade):
            nonlocal result
            result = SuccessFail.success(obj=trade)
            task.set()

        def on_error(ex):
            nonlocal result
            result = SuccessFail.fail(exception=ex)
            task.set()

        def on_completed():
            task.set()

        observer = Observer(on_next=on_next, on_completed=on_completed, on_error=on_error)

        observable = await self.trader.place_order_simple(
            contract=contract,
            action=act,
            equity_amount=equity_amount,
            quantity=quantity,
            limit_price=limit_price,
            market_order=market_order,
            stop_loss_percentage=stop_loss_percentage,
            algo_name=algo_name,
            debug=debug,
            skip_risk_gate=skip_risk_gate,
        )
        observable.subscribe(observer)

        try:
            await asyncio.wait_for(task.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            if result is None:
                result = SuccessFail.fail(error='order placement timed out waiting for confirmation')
        disposable.dispose()
        return result if result else SuccessFail.fail()

    @rpcmethod
    def cancel_all(self) -> SuccessFail[list[int]]:
        return self.trader.cancel_all()

    @rpcmethod
    async def place_expressive_order(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        execution_spec: dict,
        algo_name: str = 'proposal',
    ) -> SuccessFail[list[Trade]]:
        """Place an order with full execution specification (brackets, trailing stops, etc.)."""
        result = await self.trader.place_expressive_order(
            contract=contract,
            action=action,
            quantity=quantity,
            execution_spec=execution_spec,
            algo_name=algo_name,
        )
        return result

    @rpcmethod
    async def place_standalone_order(
        self,
        contract: Contract,
        action: str,
        quantity: float,
        order_type: str,
        aux_price: float = 0,
        limit_price: float = 0,
        trailing_percent: float = 0,
        tif: str = 'GTC',
        outside_rth: bool = True,
    ) -> SuccessFail[Trade]:
        """Place a standalone protective order (stop, trailing stop, limit)."""
        return await self.trader.place_standalone_order(
            contract=contract,
            action=action,
            quantity=quantity,
            order_type=order_type,
            aux_price=aux_price,
            limit_price=limit_price,
            trailing_percent=trailing_percent,
            tif=tif,
            outside_rth=outside_rth,
        )

    @rpcmethod
    def set_risk_limits(self, **kwargs) -> dict:
        """Update risk gate limits. Only provided fields are changed."""
        from dataclasses import asdict
        limits = self.trader.risk_gate.limits
        for key, value in kwargs.items():
            if hasattr(limits, key):
                setattr(limits, key, type(getattr(limits, key))(value))
        return asdict(limits)

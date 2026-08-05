"""In-process ``ScannerDataProvider``: bridges ``IBIdeaScanner``'s synchronous
data calls onto the trader's async IB methods.

``IBIdeaScanner`` is synchronous and fans its history fetches out over a
``ThreadPoolExecutor``; the trader's IB methods are coroutines that must run on
the trader's own event loop (the same loop the PnL off-loop routing and
``TradingRuntimeOrderDispatch`` use). Every call therefore goes through
``run_coroutine_threadsafe`` against ``trader._main_loop``.

Why this exists: it lets the ``scan_ideas`` typed query (42101) run the enriched
IB scan pipeline in-process on the trader -- no legacy dill RPC (42001), no
subprocess. The RPC-backed sibling is
``trader.tools.idea_scanner.RpcScannerProvider``, which keeps the CLI path
unchanged.
"""
from __future__ import annotations

import asyncio
from typing import Any


class TraderScannerProvider:
    """``ScannerDataProvider`` backed by in-process ``Trader`` coroutines.

    Fails loudly when the trader has no running event loop (not connected to IB
    Gateway, or disconnected mid-scan): a missing loop means we cannot fetch
    anything, and returning empty data would let the scanner score on nothing
    and rank confidently-wrong results.
    """

    def __init__(self, trader: Any, *, timeout: float = 90.0):
        self._trader = trader
        self._timeout = timeout

    def _run(self, coro):
        loop = getattr(self._trader, '_main_loop', None)
        if loop is None or not loop.is_running():
            # Close the coroutine we just built so it doesn't emit a
            # "never awaited" RuntimeWarning on the way out.
            coro.close()
            raise RuntimeError(
                'trader is not connected to IB (no running event loop); '
                'cannot run the scanner')
        return asyncio.run_coroutine_threadsafe(coro, loop).result(self._timeout)

    def scanner_data(self, *, scan_code, location_code, num_rows):
        return self._run(self._trader.scanner_data(
            scan_code=scan_code, location_code=location_code, num_rows=num_rows))

    def get_snapshots_batch(self, contracts, delayed_ok):
        return self._run(self._trader.get_snapshots_batch(contracts, delayed_ok))

    def get_history_bars(self, contract, duration, bar_size):
        return self._run(self._trader.get_history_bars(contract, duration, bar_size))

    def resolve_contract(self, partial):
        return self._run(self._trader.resolve_contract(partial))

    def get_fundamental_data(self, contract, report_type):
        return self._run(self._trader.get_fundamental_data(contract, report_type))

    def get_news_headlines(self, con_id, provider_codes, count):
        return self._run(self._trader.get_news_headlines(con_id, provider_codes, count))

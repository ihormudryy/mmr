"""TraderScannerProvider: the in-process ScannerDataProvider.

The scanner is synchronous (and fans history fetches out over a
ThreadPoolExecutor), while the trader's IB methods are coroutines owned by the
trader's own event loop. These tests pin the bridge contract: every call is
forwarded to that loop and returns a plain value, and an absent/stopped loop
fails loudly instead of returning empty data.
"""
import asyncio
import threading

import pytest

from trader.messaging.scanner_bridge import TraderScannerProvider


class _FakeTrader:
    def __init__(self, loop):
        self._main_loop = loop
        self.calls = []

    async def scanner_data(self, *, scan_code, location_code, num_rows):
        self.calls.append(('scanner_data', scan_code, location_code, num_rows))
        return [{'symbol': 'BHP', 'conId': 1, 'rank': 1}]

    async def get_snapshots_batch(self, contracts, delayed):
        self.calls.append(('get_snapshots_batch', contracts, delayed))
        return [{'symbol': 'BHP'}]

    async def get_history_bars(self, contract, duration, bar_size):
        self.calls.append(('get_history_bars', contract, duration, bar_size))
        return [{'close': 1.0}]

    async def resolve_contract(self, partial):
        self.calls.append(('resolve_contract', partial))
        return [{'conId': 1}]

    async def get_fundamental_data(self, contract, report_type):
        self.calls.append(('get_fundamental_data', contract, report_type))
        return '<xml/>'

    async def get_news_headlines(self, conId, provider_codes, count):
        self.calls.append(('get_news_headlines', conId, provider_codes, count))
        return [{'headline': 'x'}]


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def test_bridge_forwards_and_returns_plain_values(running_loop):
    trader = _FakeTrader(running_loop)
    p = TraderScannerProvider(trader)
    rows = p.scanner_data(scan_code='TOP_PERC_GAIN', location_code='STK.AU.ASX', num_rows=30)
    assert rows == [{'symbol': 'BHP', 'conId': 1, 'rank': 1}]
    assert trader.calls[0] == ('scanner_data', 'TOP_PERC_GAIN', 'STK.AU.ASX', 30)
    assert p.get_fundamental_data(None, 'ReportSnapshot') == '<xml/>'


def test_bridge_raises_when_loop_absent():
    trader = _FakeTrader(None)
    p = TraderScannerProvider(trader)
    with pytest.raises(RuntimeError):
        p.scanner_data(scan_code='X', location_code='Y', num_rows=1)


def test_every_provider_method_is_forwarded(running_loop):
    """All six protocol methods must bridge -- a method left as a coroutine
    would hand IBIdeaScanner an un-awaited coroutine instead of data."""
    trader = _FakeTrader(running_loop)
    p = TraderScannerProvider(trader)

    assert p.get_snapshots_batch(['c'], True) == [{'symbol': 'BHP'}]
    assert p.get_history_bars('c', '60 D', '1 day') == [{'close': 1.0}]
    assert p.resolve_contract('partial') == [{'conId': 1}]
    assert p.get_news_headlines(1, '', 1) == [{'headline': 'x'}]

    forwarded = [c[0] for c in trader.calls]
    assert forwarded == [
        'get_snapshots_batch', 'get_history_bars', 'resolve_contract', 'get_news_headlines',
    ]
    # positional args arrive exactly as IBIdeaScanner passes them
    assert trader.calls[0] == ('get_snapshots_batch', ['c'], True)
    assert trader.calls[1] == ('get_history_bars', 'c', '60 D', '1 day')
    assert trader.calls[3] == ('get_news_headlines', 1, '', 1)


def test_bridge_raises_when_loop_stopped(running_loop):
    """A loop object that exists but is no longer running (trader disconnected
    mid-scan) must fail loudly, not hang forever on a future nobody will run."""
    trader = _FakeTrader(running_loop)
    running_loop.call_soon_threadsafe(running_loop.stop)
    for _ in range(200):
        if not running_loop.is_running():
            break
        threading.Event().wait(0.01)
    p = TraderScannerProvider(trader)
    with pytest.raises(RuntimeError):
        p.scanner_data(scan_code='X', location_code='Y', num_rows=1)


def test_bridge_propagates_trader_exceptions(running_loop):
    """An IB-side failure must surface to the scanner (which converts it into
    an IdeaScannerError), never be swallowed into an empty result."""
    class _BoomTrader(_FakeTrader):
        async def scanner_data(self, *, scan_code, location_code, num_rows):
            raise ValueError('IB error 162: scanner not configured')

    p = TraderScannerProvider(_BoomTrader(running_loop))
    with pytest.raises(ValueError, match='162'):
        p.scanner_data(scan_code='X', location_code='Y', num_rows=1)

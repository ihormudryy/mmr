"""``scan_ideas`` / ``scanner_locations`` typed queries (trader 42101).

These run the enriched IBIdeaScanner pipeline in-process on the trader -- no
legacy dill RPC (42001), no subprocess. The handler is synchronous (production
runs it under the registry's ``thread`` execution, off the ROUTER loop) and
reaches IB through ``TraderScannerProvider``, so these tests drive it with a
real event loop running in a background thread.

Fail-loud is the point of most of these cases: a scan that cannot produce
trustworthy rows must return a problem code, never a silent empty list.
"""
import asyncio
import json
import threading

import pytest

from trader.messaging.cli_surface import ScanIdeasRequest, register_cli_surface
from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import TypedRpcRegistry, _DispatchProblem


class _StubTrader:
    def __init__(self, loop, scanner_rows):
        self._main_loop = loop
        self.ib_account = 'DU1'
        self._scanner_rows = scanner_rows

    async def scanner_data(self, *, scan_code, location_code, num_rows):
        return self._scanner_rows

    async def get_snapshots_batch(self, contracts, delayed):
        return [{'symbol': c.symbol, 'conId': getattr(c, 'conId', 0), 'last': 45.0,
                 'open': 43.0, 'high': 46.0, 'low': 42.5, 'volume': 2_000_000,
                 'exchange': 'ASX', 'currency': 'AUD'} for c in contracts]

    async def get_history_bars(self, contract, duration, bar_size):
        return [{'close': 40.0 + i * 0.1, 'volume': 1_000_000} for i in range(30)]


_ASX_ROWS = [
    {'rank': 1, 'symbol': 'BHP', 'secType': 'STK', 'exchange': 'ASX',
     'currency': 'AUD', 'conId': 100},
    {'rank': 2, 'symbol': 'CBA', 'secType': 'STK', 'exchange': 'ASX',
     'currency': 'AUD', 'conId': 200},
]


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def _registry(trader):
    registry = TypedRpcRegistry(default_execution='inline')
    register_cli_surface(registry, TraderServiceApi(trader))
    return registry


def _handler(trader, method='scan_ideas'):
    return _registry(trader).resolve('query', method).handler


def test_scan_ideas_returns_rows(running_loop):
    out = _handler(_StubTrader(running_loop, _ASX_ROWS))(
        ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=10))
    assert isinstance(out['rows'], list) and out['rows']
    assert 'score' in out['rows'][0]


def test_scan_ideas_empty_maps_to_no_results(running_loop):
    with pytest.raises(_DispatchProblem) as e:
        _handler(_StubTrader(running_loop, []))(
            ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=10))
    assert e.value.code == 'SCANNER_NO_RESULTS'


def test_scan_ideas_bad_preset_maps_to_validation_error(running_loop):
    with pytest.raises(_DispatchProblem) as e:
        _handler(_StubTrader(running_loop, []))(
            ScanIdeasRequest(preset='bogus', location='STK.AU.ASX', num=10))
    assert e.value.code == 'VALIDATION_ERROR'


def test_scan_ideas_unavailable_when_loop_none():
    with pytest.raises(_DispatchProblem) as e:
        _handler(_StubTrader(None, []))(
            ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=10))
    assert e.value.code == 'SCANNER_UNAVAILABLE'


def test_scan_ideas_rows_are_json_wire_safe(running_loop):
    """The typed transport signs responses with ``canonical_json``
    (``allow_nan=False``), so any NaN the DataFrame carries -- e.g. an
    indicator only some candidates have -- must be None on the wire, not a
    ValueError at signing time."""
    class _PartialHistoryTrader(_StubTrader):
        """CBA has too few bars for RSI, so that column is NaN for its row while
        BHP's is a real number -- exactly the mixed-column case pandas turns
        into a float64 NaN. Verified to break canonical_json when unsanitized."""
        async def get_history_bars(self, contract, duration, bar_size):
            bars = 5 if getattr(contract, 'symbol', '') == 'CBA' else 30
            return [{'close': 40.0 + i * 0.1, 'volume': 1_000_000} for i in range(bars)]

    out = _handler(_PartialHistoryTrader(running_loop, _ASX_ROWS))(
        ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=10))
    assert len(out['rows']) == 2
    # canonical_json is what the transport actually signs with
    from trader.messaging.typed_rpc import canonical_json
    assert b'BHP' in canonical_json(out)
    assert json.dumps(out, allow_nan=False)
    # the un-computable indicator arrives as null, not NaN
    assert any(row.get('rsi') is None for row in out['rows'])
    assert not any(v != v for row in out['rows'] for v in row.values()
                   if isinstance(v, float))


def test_scan_ideas_bad_preset_checked_before_ib_work():
    """Validation must precede any IB call -- an unknown preset on a trader with
    no loop is still VALIDATION_ERROR, not SCANNER_UNAVAILABLE."""
    with pytest.raises(_DispatchProblem) as e:
        _handler(_StubTrader(None, _ASX_ROWS))(
            ScanIdeasRequest(preset='bogus', location='STK.AU.ASX', num=10))
    assert e.value.code == 'VALIDATION_ERROR'


def test_scan_ideas_num_is_bounded():
    """``num`` is bounded at the schema so a caller can't ask for a 10k-row IB
    scan; pydantic rejects it before the handler runs."""
    from pydantic import ValidationError
    for bad in (0, 51):
        with pytest.raises(ValidationError):
            ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=bad)
    assert ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=50).num == 50


def test_scan_ideas_forwards_custom_filters(running_loop):
    """Explicit filter overrides must reach the scanner: a min_price above every
    candidate filters everything out, which surfaces as no results."""
    with pytest.raises(_DispatchProblem) as e:
        _handler(_StubTrader(running_loop, _ASX_ROWS))(
            ScanIdeasRequest(preset='momentum', location='STK.AU.ASX', num=10,
                             min_price=10_000.0))
    assert e.value.code == 'SCANNER_NO_RESULTS'


def test_scanner_locations_returns_list(running_loop):
    class _LocTrader(_StubTrader):
        async def scanner_locations(self):
            return [{'code': 'STK.US.MAJOR', 'name': 'US Major',
                     'instrument_types': ['STK']}]
    out = _handler(_LocTrader(running_loop, []), 'scanner_locations')({})
    assert out['locations'][0]['code'] == 'STK.US.MAJOR'


def test_scanner_locations_unavailable_when_loop_none():
    class _LocTrader(_StubTrader):
        async def scanner_locations(self):
            return []
    with pytest.raises(_DispatchProblem) as e:
        _handler(_LocTrader(None, []), 'scanner_locations')({})
    assert e.value.code == 'SCANNER_UNAVAILABLE'

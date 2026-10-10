"""Issue #73: an IB market-data permission error must reach the caller as a named code.

``mmr snapshot AAPL --source ib`` used to answer ``INTERNAL_ERROR: internal error`` when IB
refused the request with error 354. The fake broker here is a real ``IBAIORx`` whose IB
socket is mocked: the test emits the error the way ``ib_async`` does and follows it up to the
typed handler and the CLI.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from ib_async import Contract, Ticker
from reactivex.subject import Subject

from trader import mmr_cli
from trader.listeners.ibreactive import (
    IBAIORx, IBAIORxError, MARKET_DATA_NOT_SUBSCRIBED, MarketDataNotSubscribedError)
from trader.messaging.cli_surface import GetSnapshotRequest, register_cli_surface
from trader.messaging.trader_service_api import TraderServiceApi
from trader.messaging.typed_rpc import TypedRpcRegistry, TypedRpcRemoteError, _DispatchProblem

REQ_ID = 7
AAPL = Contract(conId=265598, symbol='AAPL', secType='STK', exchange='SMART', currency='USD')
IB_354_TEXT = ('Requested market data is not subscribed. Delayed market data is available. '
               'AAPL NASDAQ.NMS/TOP/ALL')


def _fake_broker() -> IBAIORx:
    broker = IBAIORx.__new__(IBAIORx)
    broker.ib = MagicMock()
    broker.ib.client = MagicMock(_reqIdSeq=REQ_ID)
    broker.ib.reqMktData = MagicMock(return_value=Ticker())
    broker._contracts_source = MagicMock()
    broker._contracts_source.call_event_subscriber_sync = MagicMock(
        side_effect=lambda fn, asend_result=False: fn())
    broker.contracts_subject = Subject()
    broker.error_subject = Subject()
    broker.error_disposables = {}
    broker._filter_contract = lambda contract, ticker: True
    return broker


class _StubTrader:
    def __init__(self, client):
        self.client = client

    async def resolve_contract(self, contract):
        return []


def _snapshot_handler(broker: IBAIORx):
    registry = TypedRpcRegistry(default_execution='inline')
    register_cli_surface(registry, TraderServiceApi(_StubTrader(broker)))
    return registry.resolve('query', 'get_snapshot').handler


async def _snapshot_failing_with(broker: IBAIORx, code: int, text: str):
    """Run the typed handler while IB answers the snapshot request with an error."""
    task = asyncio.create_task(
        _snapshot_handler(broker)(GetSnapshotRequest(instrument_id=AAPL.conId)))
    await asyncio.sleep(0)
    broker.error_subject.on_next(IBAIORxError(REQ_ID, code, text, AAPL))
    return await task


@pytest.mark.asyncio
@pytest.mark.parametrize('ib_code', [354, 10089, 10090])
async def test_market_data_permission_error_becomes_a_named_problem(ib_code):
    with pytest.raises(_DispatchProblem) as problem:
        await _snapshot_failing_with(_fake_broker(), ib_code, IB_354_TEXT)
    assert problem.value.code == MARKET_DATA_NOT_SUBSCRIBED == 'MARKET_DATA_NOT_SUBSCRIBED'
    assert str(problem.value) == f'IB error {ib_code} for AAPL (conId=265598): {IB_354_TEXT}'


@pytest.mark.asyncio
async def test_broker_snapshot_raises_the_typed_error_with_ib_code_and_text():
    broker = _fake_broker()
    task = asyncio.create_task(broker.get_snapshot(AAPL))
    await asyncio.sleep(0)
    broker.error_subject.on_next(IBAIORxError(REQ_ID, 354, IB_354_TEXT, AAPL))
    with pytest.raises(MarketDataNotSubscribedError) as failure:
        await task
    assert failure.value.error_code == 354
    assert failure.value.error_string == IB_354_TEXT


@pytest.mark.asyncio
async def test_other_ib_error_stays_a_generic_error():
    with pytest.raises(Exception) as failure:
        await _snapshot_failing_with(_fake_broker(), 200, 'No security definition has been found')
    assert not isinstance(failure.value, (_DispatchProblem, MarketDataNotSubscribedError))
    assert 'errorCode: 200' in str(failure.value)


@pytest.mark.asyncio
async def test_no_price_is_invented_when_the_subscription_is_missing():
    broker = _fake_broker()
    task = asyncio.create_task(broker.get_snapshot(AAPL, delayed=False))
    await asyncio.sleep(0)
    broker.error_subject.on_next(IBAIORxError(REQ_ID, 354, IB_354_TEXT, AAPL))
    with pytest.raises(MarketDataNotSubscribedError):
        await task
    broker.ib.reqMarketDataType.assert_called_with(1)  # never switched to delayed on its own


class _FakeSdk:
    def __init__(self, failure: Exception):
        self._failure = failure

    def snapshot(self, symbol, **kwargs):
        raise self._failure


def _run_snapshot_cli(failure: Exception):
    args = mmr_cli.build_parser().parse_args(['snapshot', 'AAPL'])
    mmr_cli._handle_snapshot(_FakeSdk(failure), args, 'snapshot')


def test_cli_json_passes_the_code_message_and_hint_through(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, '_json_mode', True)
    _run_snapshot_cli(TypedRpcRemoteError(MARKET_DATA_NOT_SUBSCRIBED, IB_354_TEXT))
    out = json.loads(capsys.readouterr().out)
    assert out['success'] is False
    assert out['code'] == 'MARKET_DATA_NOT_SUBSCRIBED'
    assert out['message'] == IB_354_TEXT
    assert 'subscription' in out['hint'] and '--delayed' in out['hint']


def test_cli_text_prints_the_code_and_a_hint(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, '_json_mode', False)
    _run_snapshot_cli(TypedRpcRemoteError(MARKET_DATA_NOT_SUBSCRIBED, IB_354_TEXT))
    out = ' '.join(capsys.readouterr().out.split())  # rich wraps long lines
    assert 'MARKET_DATA_NOT_SUBSCRIBED' in out
    assert 'Delayed market data is available' in out
    assert 'Hint:' in out and 'market-data sharing' in out


def test_cli_leaves_other_remote_errors_to_the_generic_handler():
    with pytest.raises(TypedRpcRemoteError):
        _run_snapshot_cli(TypedRpcRemoteError('INTERNAL_ERROR', 'internal error'))

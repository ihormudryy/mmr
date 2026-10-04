import datetime as dt
from decimal import Decimal

from trader.automation.artifact_verifier import VerifiedArtifact
from trader.objects import Action
from trader.strategy.intent_emitter import IntentEmitter, IntentEmitterContext
from trader.trading.strategy import Signal

BAR = dt.datetime(2026, 10, 5, 15, 0, tzinfo=dt.timezone.utc)


class RecordingClient:
    def __init__(self):
        self.bodies = []

    def call(self, method, body, response_type):
        self.bodies.append(body)
        return {'command_id': body['command_id'], 'state': 'SUBMITTED'}


def _emitter(order_notional=Decimal('1900')):
    artifact = VerifiedArtifact(
        artifact_id='a1', manifest_digest='m1', dataset_manifest_digest='d1',
        parameters={'stop_price': '90'}, allowlist=('1001',), max_gross_allocation=0.05,
        expires_at=BAR + dt.timedelta(days=30), public_key_id='k', verification_reason_codes=())
    client = RecordingClient()
    context = IntentEmitterContext(
        enabled=True, live_enabled=False, strategy_name='trend', artifact=artifact,
        artifact_digest='m1', eligibility_attestation_digest='m1',
        artifact_bundle_digest='sha256:m1', account_mode='paper',
        strategy_source_digest='src-1', order_notional=order_notional)
    return IntentEmitter(command_client=client, context=context, now=lambda: BAR), client


def _signal(quantity=0):
    return Signal(source_name='trend', action=Action.BUY, probability=0.5, risk=0.5,
                  conid=1001, quantity=quantity)


def test_buy_without_quantity_uses_the_attested_notional():
    emitter, client = _emitter()
    emitter.on_signal(strategy_name='trend', signal=_signal(), completed_bar_timestamp=BAR,
                      session_id='s', reference_price=100.0)
    assert client.bodies[0]['requested_quantity'] == '19'
    assert client.bodies[0]['strategy_source_digest'] == 'src-1'


def test_explicit_quantity_is_kept():
    emitter, client = _emitter()
    emitter.on_signal(strategy_name='trend', signal=_signal(quantity=5), completed_bar_timestamp=BAR,
                      session_id='s', reference_price=100.0)
    assert client.bodies[0]['requested_quantity'] == '5'


def test_no_notional_and_no_quantity_sends_none():
    emitter, client = _emitter(order_notional=None)
    emitter.on_signal(strategy_name='trend', signal=_signal(), completed_bar_timestamp=BAR,
                      session_id='s', reference_price=100.0)
    assert client.bodies[0]['requested_quantity'] is None

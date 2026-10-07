"""``TypedRpcClient.call`` returns a ``CommandReceipt`` (a stdlib dataclass).

Issue #79: the SDK passed ``CommandReceipt`` as ``response_model``, but the
client only knew ``model_validate``. The trader ran the command and the client
then raised ``AttributeError``. These tests drive a REAL signed client against
a REAL ``TypedRpcServer`` (the production command registry), through the SDK
methods that use ``CommandReceipt``.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from tests.rpc_identity_fixtures import AllowAllAcl, ServedStack, make_identities
from tests.test_propose_approve_integration import (
    AMD_CONID,
    FakeSecurityDefinition,
    _build_stack,
    _make_mmr,
    _ResolveAwareTypedClient,
    _StubRPCClient,
)
from trader.common.reactivex import SuccessFailEnum
from trader.domain.commands import CommandReceipt
from trader.messaging.production_api import (
    StartExperimentRequest,
    _experiment_command_rpc_handler,
)
from trader.messaging.typed_rpc import MalformedReplyError, TypedRpcRegistry


@pytest.fixture
def served(tmp_path):
    command_stack = _build_stack(tmp_path)
    command_stack.coordinator.register_action(
        "start_experiment", lambda command: {"experiment_id": "exp-test"}, requires_preflight=False)
    command_stack.registry.register(
        "command", "start_experiment", StartExperimentRequest, dict,
        _experiment_command_rpc_handler(command_stack.coordinator, "DU111111", "start_experiment"),
        with_caller=True)
    stack = ServedStack(
        {("trader", "command"): command_stack.registry, ("trader", "query"): command_stack.registry},
        make_identities())
    stack.command_stack = command_stack
    yield stack
    stack.close()


@pytest.fixture
def mmr(served):
    rpc = _StubRPCClient({'AMD': [FakeSecurityDefinition(symbol='AMD', conId=AMD_CONID)]})
    sdk = _make_mmr(served.command_stack, rpc)
    sdk._typed_command_client = served.client('cli', 'trader', 'command')
    sdk._typed_query_client = _ResolveAwareTypedClient(
        served.client('cli', 'trader', 'query'), rpc.secdefs)
    return sdk


class TestSdkCommandsOverRealTransport:
    def test_propose_returns_a_proposal(self, mmr, served):
        result = mmr.propose(symbol='AMD', action='BUY', quantity=10, reasoning='issue 79')

        assert result.success_fail == SuccessFailEnum.SUCCESS, result.error
        assert served.command_stack.repo.get(result.obj['proposal_id']) is not None

    def test_reject_resolves_the_proposal(self, mmr, served):
        proposal_id = mmr.propose(symbol='AMD', action='BUY', quantity=10).obj['proposal_id']

        assert mmr.reject(proposal_id, 'not now') is True
        assert served.command_stack.repo.get(proposal_id).status == 'REJECTED'

    def test_approve_returns_the_order_ids(self, mmr, served):
        proposal_id = mmr.propose(symbol='AMD', action='BUY', quantity=10).obj['proposal_id']

        result = mmr.approve(proposal_id)

        assert result.success_fail == SuccessFailEnum.SUCCESS, result.error
        assert result.obj == served.command_stack.orders.submissions[0].order_ids

    def test_experiment_command_returns_its_outcome(self, mmr):
        result = mmr.experiment_start('issue 79')

        assert result.success_fail == SuccessFailEnum.SUCCESS, result.error
        assert result.obj == {"experiment_id": "exp-test"}


def _call_with_reply(reply):
    registry = TypedRpcRegistry(acl=AllowAllAcl())
    registry.register("command", "ping_receipt", dict, dict, lambda _body: reply)
    stack = ServedStack({("trader", "command"): registry}, make_identities())
    try:
        return stack.client('cli', 'trader', 'command').call('ping_receipt', {}, CommandReceipt)
    finally:
        stack.close()


class TestClientParsesDataclassResponse:
    def test_builds_the_dataclass_from_an_exact_dict(self):
        receipt = CommandReceipt('c1', 'k1', 'RESOLVED', {'a': 1}, None, False)

        assert _call_with_reply(asdict(receipt)) == receipt

    def test_missing_field_is_named(self):
        reply = asdict(CommandReceipt('c1', 'k1', 'RESOLVED', None, None, False))
        del reply['retryable']

        with pytest.raises(MalformedReplyError, match='retryable'):
            _call_with_reply(reply)

    def test_extra_field_is_named(self):
        reply = {**asdict(CommandReceipt('c1', 'k1', 'RESOLVED', None, None, False)), 'surprise': 1}

        with pytest.raises(MalformedReplyError, match='surprise'):
            _call_with_reply(reply)

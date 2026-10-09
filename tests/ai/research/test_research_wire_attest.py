"""AttestReply and Binding against the real JudgmentAttest: a real evaluation, a real signed bundle.

The fixtures are the research service's own (tests/research/test_judgment_attest.py); the slow one, a synthetic
evaluation, runs once for the module.
"""
import pytest

from tests.ai.research import cases
from tests.research.test_judgment_attest import AI, evaluated, world  # noqa: F401
from trader.ai.research_wire import AttestReply, parse_reply

pytestmark = pytest.mark.timeout(240)


def attest(world):  # noqa: F811
    return world.attest.attest({"judgment_id": "jdg-00000001"}, AI)


def test_attested_and_duplicate_carry_the_binding_the_signed_bundle_holds(world):  # noqa: F811
    world.judge()
    first_reply, repeat_reply = attest(world), attest(world)
    first = parse_reply(AttestReply, "attest_from_judgment", first_reply)
    repeat = parse_reply(AttestReply, "attest_from_judgment", repeat_reply)
    assert (first.status, repeat.status) == ("ATTESTED", "DUPLICATE")
    assert first.bundle_digest == repeat.bundle_digest and first.binding == repeat.binding
    assert first.binding.file_hash == world.file_hash and first.binding.params == dict(world.spec.params)
    assert (first.binding.strategy_path, first.binding.class_name) == ("strategies/time_of_day.py", "TimeOfDay")
    assert first.binding.order_notional > 0 and first.binding.conids == sorted(first.binding.conids)
    assert set(cases.attested()) == set(first_reply) and set(cases.binding()) == set(first_reply["binding"])


def test_a_missing_judgment_is_a_refusal_with_no_binding(world):  # noqa: F811
    reply = parse_reply(AttestReply, "attest_from_judgment", attest(world))
    assert (reply.status, reply.code, reply.binding, reply.bundle_digest, reply.retryable) == (
        "REFUSED", "JUDGMENT_MISSING", None, None, False)


def test_a_judgment_that_is_not_a_deploy_is_a_refusal(world):  # noqa: F811
    world.judge("SHADOW", narrative=None)
    reply = parse_reply(AttestReply, "attest_from_judgment", attest(world))
    assert (reply.status, reply.code) == ("REFUSED", "JUDGMENT_NOT_DEPLOY")


def test_a_retryable_export_failure_parses_as_retryable_and_the_retry_as_a_duplicate(world, monkeypatch):  # noqa: F811
    from trader.research.bundle import ResearchBundle
    world.judge()
    real_export = ResearchBundle.export
    calls = []

    def flaky(self, artifact_id, path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("disk full")
        return real_export(self, artifact_id, path)
    monkeypatch.setattr(ResearchBundle, "export", flaky)
    failed = parse_reply(AttestReply, "attest_from_judgment", attest(world))
    assert (failed.status, failed.code, failed.retryable, failed.binding) == (
        "REFUSED", "ATTEST_EXPORT_FAILED", True, None)
    retried = parse_reply(AttestReply, "attest_from_judgment", attest(world))
    assert (retried.status, retried.retryable) == ("DUPLICATE", False) and retried.binding is not None


def test_a_refusal_while_the_account_is_not_paper_is_not_retryable(world):  # noqa: F811
    world.judge()
    world.paper.update(value=False)
    reply = parse_reply(AttestReply, "attest_from_judgment", attest(world))
    assert (reply.status, reply.code, reply.retryable) == ("REFUSED", "ACCOUNT_NOT_PAPER", False)

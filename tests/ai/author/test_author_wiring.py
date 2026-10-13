import datetime as dt
import logging
from types import SimpleNamespace

import pytest

import trader.messaging.typed_rpc  # noqa: F401  (dictConfig before caplog, as test_ai_service does)
from tests.ai.author.helpers import author_text, load
from tests.ai.fakes import FakeClock, config_text
from tests.rpc_identity_fixtures import KNOWN_PRINCIPALS, write_keyset
from trader.ai.author_loop import AuthorLoop
from trader.ai.authored_offers import AuthoredOffers
from trader.ai.config import AiConfigError
from trader.ai.rpc_clients import AiRpcClients
from trader.ai.store import AiStore
from trader.messaging.rpc_keys import RpcKeyError
from trader.ai_service import (EXIT_REFUSED, ServiceSettings, build_author_loop, build_research_cycle,
                               build_session_slots, run_service)

NIGHT = dt.datetime(2026, 10, 8, 21, tzinfo=dt.timezone.utc)


def build(config, clients):
    return build_author_loop(config, store=object(), clock=FakeClock(NIGHT), slots=build_session_slots(config),
                             leadership=object(), watch=object(), clients=clients, gateway=object())


def test_authoring_off_builds_no_author_loop(tmp_path):
    assert build(load(tmp_path, config_text()), SimpleNamespace(author=None, supervisor=object())) is None


def test_authoring_on_builds_the_loop_on_the_author_client(tmp_path):
    config = load(tmp_path, author_text(authoring="authoring:\n  run_after_close_minutes: 90\n"))
    assert isinstance(build(config, SimpleNamespace(author=object(), supervisor=object())), AuthorLoop)
    slot = build_session_slots(config).author_slot(NIGHT)                 # 17:00 New York: before 17:30
    assert slot.attempt_id == "aut-20261007"
    with pytest.raises(AiConfigError) as exc:
        build(config, SimpleNamespace(author=None, supervisor=object()))
    assert exc.value.code == "AUTHOR_CLIENT_MISSING"


def test_the_session_slots_carry_the_configured_author_minutes(tmp_path):
    (tmp_path / "late").mkdir()
    (tmp_path / "default").mkdir()
    late = build_session_slots(load(tmp_path / "late", author_text(authoring="authoring:\n  run_after_close_minutes: 90\n")))
    default = build_session_slots(load(tmp_path / "default", author_text()))      # 60 minutes after the 16:00 close
    assert default.author_slot(NIGHT).attempt_id == "aut-20261008"
    assert late.author_slot(NIGHT).attempt_id == "aut-20261007"                  # 17:30 has not come yet


def test_the_research_cycle_offers_authored_files_only_with_an_author_client(tmp_path):
    config = load(tmp_path, author_text())
    store = AiStore(tmp_path / "ai.duckdb", clock=FakeClock(NIGHT))

    def cycle(author):
        return build_research_cycle(config, store=store, clock=FakeClock(NIGHT), slots=build_session_slots(config),
                                    leadership=None, watch=None, gateway=object(),
                                    clients=SimpleNamespace(lab=object(), research=object(), author=author))
    assert isinstance(cycle(object())._authored, AuthoredOffers)
    assert cycle(None)._authored is None


@pytest.mark.parametrize("authoring", [False, True])
def test_serve_connects_the_author_client_only_when_authoring_is_on(tmp_path, monkeypatch, caplog, authoring):
    seen = []

    def connect(**kwargs):
        seen.append(kwargs["author"])
        raise AiConfigError("TEST_STOP", "stop right after connect")
    monkeypatch.setattr(AiRpcClients, "connect", staticmethod(connect))
    code, log = run_with_keys(tmp_path, caplog, authoring, author_key=True)
    assert code == EXIT_REFUSED and seen == [authoring] and "TEST_STOP" in log


def run_with_keys(tmp_path, caplog, authoring, *, author_key):
    keys = tmp_path / "keys"
    write_keyset(keys, [name for name in KNOWN_PRINCIPALS if author_key or name != "ai_author"])
    block = f"database_path: {tmp_path / 'ai' / 'ai.duckdb'}\n"
    text = author_text(authoring=block) if authoring else config_text(extra_top_level=block)
    (tmp_path / "ai.yaml").write_text(text)
    caplog.set_level(logging.ERROR)
    code = run_service(ServiceSettings(config_path=str(tmp_path / "ai.yaml"), keys_dir=str(keys)),
                       environ={"OPENROUTER_API_KEY": "test-only"})
    return code, caplog.text


def test_the_service_starts_with_authoring_off_and_no_author_key_file(tmp_path, monkeypatch, caplog):
    def stop_after_connect(*args, **kwargs):
        raise AiConfigError("TEST_STOP", "the clients connected")
    monkeypatch.setattr("trader.ai_service.build_gateway", stop_after_connect)
    code, log = run_with_keys(tmp_path, caplog, False, author_key=False)
    assert code == EXIT_REFUSED and "TEST_STOP" in log


def test_authoring_on_without_the_author_key_file_fails_at_connect(tmp_path, monkeypatch, caplog):
    with pytest.raises(RpcKeyError, match="ai_author.key is missing"):
        run_with_keys(tmp_path, caplog, True, author_key=False)

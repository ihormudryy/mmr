"""Test helpers for RPC identities: real key files in temp dirs, in-memory identities."""

from __future__ import annotations

import os
import time
from pathlib import Path
from collections.abc import Mapping
from typing import Iterable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trader.messaging.principals import KNOWN_PRINCIPALS, peers_for


def write_keyset(directory: Path, principals: Iterable[str] = KNOWN_PRINCIPALS) -> dict[str, Ed25519PrivateKey]:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    keys = {}
    for principal in principals:
        key = Ed25519PrivateKey.generate()
        private_path = directory / f"{principal}.key"
        public_path = directory / f"{principal}.pub"
        private_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
        public_path.write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        os.chmod(private_path, 0o600)
        os.chmod(public_path, 0o644)
        keys[principal] = key
    return keys


def _identity_class():
    from trader.messaging.typed_rpc import ServiceIdentity

    class TestServiceIdentity(ServiceIdentity):
        def _private_key_for_tests(self):
            return self._ServiceIdentity__private_key

    return TestServiceIdentity


def make_identities(now=time.time, keys=None):
    """One in-memory identity per known principal, keyrings per ``peers_for``."""
    from trader.messaging.rpc_keys import RpcKeyring
    from trader.messaging.typed_rpc import ReplayNonceCache

    keys = keys or {p: Ed25519PrivateKey.generate() for p in KNOWN_PRINCIPALS}
    cls = _identity_class()
    identities = {}
    for principal in KNOWN_PRINCIPALS:
        keyring = RpcKeyring.from_public_keys(
            {peer: keys[peer].public_key() for peer in peers_for(principal)})
        identities[principal] = cls(principal, keys[principal], keyring, now=now,
                                    nonce_cache=ReplayNonceCache(now=now))
    return identities


def free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServedStack:
    """Typed RPC servers on 127.0.0.1 in a background asyncio loop, plus raw helpers.

    ``registries`` maps ``(server_principal, role)`` to a ``TypedRpcRegistry``.
    """

    def __init__(self, registries, identities):
        import asyncio
        import threading

        from trader.messaging.typed_rpc import TypedRpcServer

        self.identities = identities
        self.registries = registries
        self.ports = {key: free_port() for key in registries}
        self.servers = {
            key: TypedRpcServer(key[1], registry, identities[key[0]], port=self.ports[key])
            for key, registry in registries.items()
        }
        self._clients = []
        ready = threading.Event()
        state = {}

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            state["loop"] = loop
            loop.run_until_complete(asyncio.gather(*(s.serve() for s in self.servers.values())))
            ready.set()
            try:
                loop.run_forever()
            finally:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        assert ready.wait(timeout=5.0), "typed RPC servers did not start"
        time.sleep(0.05)
        self._state = state

    def client(self, caller, server="trader", role="query", timeout=5.0, identity=None):
        from trader.messaging.typed_rpc import TypedRpcClient

        client = TypedRpcClient(role, identity or self.identities[caller], server=server,
                                port=self.ports[(server, role)], timeout=timeout)
        client.connect()
        self._clients.append(client)
        return client

    def signed(self, caller, server="trader", role="query", method="get_status", body=None, *,
               claim=None, tamper_body=False, identity=None, on_behalf_of=None, controller_epoch=None):
        import uuid

        signer = identity or self.identities[caller]
        request = signer.sign_request(server=server, role=role, method=method,
                                      request_id=str(uuid.uuid4()), nonce=uuid.uuid4().hex,
                                      body=dict(body or {}), on_behalf_of=on_behalf_of,
                                      controller_epoch=controller_epoch)
        if claim is not None:
            request = request.model_copy(update={"principal": claim})
        if tamper_body:
            request = request.model_copy(update={"body": {**request.body, "tampered": True}})
        return request

    def send_raw(self, request_or_bytes, *, to=None, role=None, timeout=5.0):
        """Send raw bytes on a DEALER; return the reply, verified with the server's real key."""
        import zmq

        from trader.messaging.typed_rpc import (
            AuthenticationError, canonical_json, decode_response, response_signing_bytes,
        )
        from trader.research.signing import verify_bytes

        if isinstance(request_or_bytes, (bytes, bytearray)):
            payload = bytes(request_or_bytes)
            server, sock_role = to, role or "query"
        else:
            payload = canonical_json(request_or_bytes.model_dump(mode="json"))
            server = to or request_or_bytes.server
            sock_role = role or request_or_bytes.role
        ctx = zmq.Context()
        sock = ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.LINGER, 0)
        try:
            sock.connect(f"tcp://127.0.0.1:{self.ports[(server, sock_role)]}")
            sock.send(payload)
            if not sock.poll(int(timeout * 1000)):
                raise TimeoutError("no reply")
            reply = decode_response(sock.recv_multipart()[-1])
        finally:
            sock.close(linger=0)
            ctx.term()
        server_key = self.servers[(server, sock_role)].identity.public_key
        unsigned = reply.model_copy(update={"signature": None})
        verify_bytes(server_key, response_signing_bytes(unsigned), reply.signature)
        if reply.server != server:
            raise AuthenticationError("reply names another server")
        return reply

    def raw_code(self, request_or_bytes, **kw):
        reply = self.send_raw(request_or_bytes, **kw)
        return "OK" if reply.ok else reply.problem.code

    def close(self):
        for client in self._clients:
            client.close()
        loop = self._state.get("loop")
        if loop:
            for server in self.servers.values():
                loop.call_soon_threadsafe(server.close)
            loop.call_soon_threadsafe(loop.stop)
        self._thread.join(timeout=5.0)


def legacy_hmac_envelope_bytes(now=None) -> bytes:
    """A pre-cutover HMAC-shaped request envelope (no principal/server/role)."""
    from trader.messaging.typed_rpc import canonical_json
    return canonical_json({"method": "get_status", "request_id": "legacy", "nonce": "legacy-nonce",
                           "timestamp": now if now is not None else time.time(), "body": {},
                           "signature": "00" * 32})


class AllowAllAcl(Mapping):
    """Test-only allow-list: every (role, method) is open to every known principal.

    For tests whose subject is not authorization. Production uses the
    tables in ``trader.messaging.principals``.
    """

    def __getitem__(self, key):
        return KNOWN_PRINCIPALS

    def __contains__(self, key):
        return True

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0


ALLOW_ALL = AllowAllAcl()


class RecordingCoordinator:
    """Stands in for the coordinator: records each CommandRequest, returns a RESOLVED receipt."""

    def __init__(self):
        from unittest.mock import MagicMock
        self.requests = []
        self._mock = MagicMock(name="coordinator")

    @property
    def last_request(self):
        return self.requests[-1]

    def execute(self, request):
        from trader.domain.commands import CommandReceipt
        self.requests.append(request)
        return CommandReceipt(request.command_id, request.command_id, "RESOLVED", {}, None, False)

    def __getattr__(self, name):
        return getattr(self._mock, name)


def full_command_stack_stub(coordinator=None):
    """A command stack whose every optional service is a Mock, so every method registers."""
    from unittest.mock import MagicMock
    from types import SimpleNamespace

    names = ("coordinator", "proposal_service", "repository", "controls", "nonces",
             "approval_service", "cancel_service", "liquidation_service", "canary_service",
             "allocation_service", "paper_automation_service", "automated_intent_service",
             "strategy_control_service", "journal", "ai_paper")
    stack = SimpleNamespace(**{name: MagicMock(name=name) for name in names})
    if coordinator is not None:
        stack.coordinator = coordinator
    stack.account_mode = "paper"
    stack.resume_ready = lambda: True
    stack.reconciliation_complete = lambda command_id: True
    return stack


def build_full_production_registry(identity=None, coordinator=None):
    """The production trader registry with every optional surface registered."""
    from unittest.mock import MagicMock

    from trader.messaging.production_api import build_production_registry

    trader = MagicMock(name="trader")
    trader.ib_account = "DU111111"
    trader.paper_trading = True
    return build_production_registry(
        trader, identity or make_identities()["trader"],
        snapshot_service=MagicMock(name="snapshot"), feed_service=MagicMock(name="feed"),
        command_stack=full_command_stack_stub(coordinator),
    )

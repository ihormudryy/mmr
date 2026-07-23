from __future__ import annotations

import hashlib
import hmac
from typing import Any, Literal

from trader.messaging.typed_rpc import TypedRpcRemoteError


def capability_from_remote_error(code: str) -> Literal["absent", "present"]:
    if code == "METHOD_NOT_ALLOWED":
        return "absent"
    return "present"


def derive_html_form_csrf(session_secret: str) -> str:
    if len(session_secret) < 32:
        raise ValueError("session secret must be at least 32 characters")
    return hmac.new(
        session_secret.encode(),
        b"mmr-dashboard-html-form-csrf-v1",
        hashlib.sha256,
    ).hexdigest()


def probe_command_registered(client: Any, method: str) -> bool:
    try:
        client.call(method, {}, dict)
        return True
    except TypedRpcRemoteError as exc:
        if exc.code == "METHOD_NOT_ALLOWED":
            return False
        return True

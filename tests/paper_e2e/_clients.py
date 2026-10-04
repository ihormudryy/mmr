"""Small public facades for the live paper E2E harness."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from trader.sdk import MMR


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _read_token(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _dotenv_token(path: Path) -> str:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        if line.startswith("DASHBOARD_TOKEN="):
            return line.partition("=")[2].strip().strip("'\"")
    return ""


def _dashboard_token() -> str:
    token = (os.environ.get("DASHBOARD_TOKEN") or "").strip()
    if token:
        return token
    path = (os.environ.get("DASHBOARD_TOKEN_FILE") or "").strip()
    if path:
        token = _read_token(Path(path))
        if token:
            return token
    token = _read_token(Path.home() / ".config" / "mmr" / "dashboard.token")
    if token:
        return token
    token = _dotenv_token(_repo_root() / ".env")
    if token:
        return token
    raise RuntimeError(
        "Dashboard login requires a token. Set DASHBOARD_TOKEN, set "
        "DASHBOARD_TOKEN_FILE, create ~/.config/mmr/dashboard.token, or add "
        "DASHBOARD_TOKEN=... to the repository .env."
    )


class DashboardClient(httpx.Client):
    """Dashboard client with the session and JSON-command auth helpers."""

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url=base_url, follow_redirects=True, timeout=10.0)

    def login(self) -> httpx.Response:
        return self.post("/session", data={"token": _dashboard_token()})

    def csrf_headers(self) -> dict[str, str]:
        response = self.get("/api/commands/csrf-token")
        response.raise_for_status()
        return {"X-CSRF-Token": response.json()["csrf_token"]}


class _TypedCallSurface:
    """Expose only the wire-level call contract needed by E2E tests."""

    def __init__(self, sdk: MMR, role: str) -> None:
        self._sdk = sdk
        self._role = role

    def call(self, method: str, body: dict[str, Any], model: Any) -> Any:
        if self._role == "query":
            return self._sdk._typed_query.call(method, body, model)
        return self._sdk._typed_command.call(method, body, model)


class TypedRpc:
    """Public typed-RPC facade; SDK implementation details remain internal."""

    def __init__(self, timeout: int = 10) -> None:
        sdk = MMR(timeout=timeout)
        self.query = _TypedCallSurface(sdk, "query")
        self.command = _TypedCallSurface(sdk, "command")
        self._sdk = sdk

    def _close(self) -> None:
        self._sdk.close()

    def _configured_trading_mode(self) -> str:
        return str(self._sdk._container.config().get("trading_mode", ""))

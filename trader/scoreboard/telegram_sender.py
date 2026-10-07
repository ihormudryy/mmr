"""Send-only Telegram delivery from the outbox (Plan 5 rulings 17-18).

Plain text, no parse_mode, only the configured chat. The token is redacted
from every log line and stored error; ``http_post`` never logs the URL.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from trader.scoreboard.telegram_config import TelegramConfig, load_telegram_config
from trader.scoreboard.telegram_outbox import TelegramOutbox

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
TIMEOUT_SECONDS = 10.0
REDACTED = "<token>"


@dataclass(frozen=True)
class PostResult:
    status: int
    message_id: Optional[int]
    retry_after: Optional[float]


class TelegramTransportError(RuntimeError):
    """The request did not complete; carries only the error type, never the URL."""


def http_post(url: str, payload: Mapping[str, Any]) -> PostResult:
    import httpx
    try:
        response = httpx.post(url, json=dict(payload), timeout=TIMEOUT_SECONDS, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise TelegramTransportError(f"telegram request failed: {type(exc).__name__}") from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    result = body.get("result") if isinstance(body, dict) else None
    parameters = body.get("parameters") if isinstance(body, dict) else None
    message_id = result.get("message_id") if isinstance(result, dict) else None
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    return PostResult(response.status_code, message_id if isinstance(message_id, int) else None,
                      float(retry_after) if isinstance(retry_after, (int, float)) else None)


class TelegramSender:
    def __init__(self, outbox: TelegramOutbox, config: TelegramConfig, *,
                 post: Callable[[str, Mapping[str, Any]], PostResult], redact_extra: Iterable[str] = ()):
        self._outbox = outbox
        self._config = config
        self._post = post
        self._secrets = [secret for secret in (config.token, *redact_extra) if secret]

    def _redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def drain(self) -> int:
        """Send what is due; returns how many were sent. One failure never blocks the next row."""
        url = f"{API}/bot{self._config.token}/sendMessage"
        sent = 0
        for row in self._outbox.due():
            try:
                result = self._post(url, {"chat_id": self._config.chat_id, "text": row.text})
            except Exception as exc:
                error = self._redact(f"{type(exc).__name__}: {exc}")
                self._outbox.mark_failed(row.event_id, error)
                logger.error("telegram %s not sent (retried later): %s", row.event_id, error)
                continue
            if result.status == 200 and result.message_id is not None:
                self._outbox.mark_sent(row.event_id, result.message_id)
                sent += 1
            elif result.status == 429:
                self._outbox.mark_failed(row.event_id, "HTTP 429", retry_after=result.retry_after)
                logger.warning("telegram %s rate limited; retry after %s s", row.event_id, result.retry_after)
            else:
                self._outbox.mark_failed(row.event_id, f"HTTP {result.status}")
                logger.error("telegram %s refused with HTTP %s (check the bot token and chat id); kept pending",
                             row.event_id, result.status)
        return sent


def build_telegram(section: Optional[Mapping[str, Any]], db: Any, now: Callable,
                   post: Callable[[str, Mapping[str, Any]], PostResult] = http_post):
    """``(outbox, sender)`` when enabled, else ``(None, None)``: no HTTP and no outbox rows while off."""
    config = load_telegram_config(section)
    if config is None:
        return None, None
    outbox = TelegramOutbox(db, now=now)
    return outbox, TelegramSender(outbox, config, post=post)

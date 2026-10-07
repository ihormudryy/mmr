"""Provider-neutral model client: request and response types, error taxonomy, adapters.

How the error classes split (the journal and budget rely on this):
- NotSentError: only when the request provably never left the process (connect failure,
  bad local parameters). The reservation is released.
- ProviderRejectedError: a definite refusal (HTTP 4xx except 408). No tokens were made.
  The reservation is released.
- OutcomeUnknownError: anything else after the transport started (read timeout, 5xx,
  dropped connection). The reservation stays counted.
- MalformedResponseError / MalformedUsageError: a response arrived but it cannot be used
  or priced. Outcome COST_UNKNOWN. The reservation stays counted at the worst case.
  Malformed usage is never zero cost.

The attempt_key field of a ModelRequest is set by the gateway only. Adapters never read
it; replay does.
"""
from __future__ import annotations
import asyncio
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Protocol
import httpx
from trader.ai.config import AiConfigError, RoleConfig


NOT_SENT = "NOT_SENT"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"
COST_UNKNOWN = "COST_UNKNOWN"

MAX_REPORTED_TOKENS = 100_000_000
MAX_DETAIL_CHARS = 300
CHAT_ROLES = ("system", "user", "assistant")


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in CHAT_ROLES:
            raise ValueError(f"role must be one of {', '.join(CHAT_ROLES)}")
        if not isinstance(self.content, str):
            raise ValueError("content must be a string")


@dataclass(frozen=True)
class ModelRequest:
    request_key: str
    messages: tuple[ChatMessage, ...]
    max_output_tokens: int
    temperature: float = 0.0
    attempt_key: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.request_key, str) or not self.request_key or "#" in self.request_key:
            raise ValueError("request_key must be a non-empty string without '#'")
        if not self.messages or not all(isinstance(m, ChatMessage) for m in self.messages):
            raise ValueError("messages must be a non-empty sequence of ChatMessage")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be a positive integer")
        temperature = self.temperature
        if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
            raise ValueError("temperature must be a number between 0 and 2")


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        for value in (self.input_tokens, self.output_tokens):
            if type(value) is not int or not 0 <= value <= MAX_REPORTED_TOKENS:
                raise ValueError("token counts must be plain integers within range")


@dataclass(frozen=True)
class ModelResponse:
    text: str
    usage: Usage
    model: str
    backend: str
    finish_reason: str
    provider_request_id: Optional[str] = None


class ModelClient(Protocol):
    backend: str
    model_id: str

    async def complete(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse: ...

    async def aclose(self) -> None: ...


class ModelCallError(Exception):
    outcome = ""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class NotSentError(ModelCallError):
    outcome = NOT_SENT


class ProviderRejectedError(ModelCallError):
    outcome = REJECTED


class OutcomeUnknownError(ModelCallError):
    outcome = UNKNOWN


class MalformedResponseError(OutcomeUnknownError):
    outcome = COST_UNKNOWN


class MalformedUsageError(MalformedResponseError):
    pass


def estimate_input_tokens(messages: Iterable[ChatMessage]) -> int:
    """One token per 3 UTF-8 bytes plus 4 per message. Conservative for English."""
    return sum(math.ceil(len(m.content.encode("utf-8")) / 3) + 4 for m in messages)


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, "***")
    return text[:MAX_DETAIL_CHARS]


def parse_usage(raw: object, *, input_key: str, output_key: str) -> Usage:
    """Strict. A missing, non-integer, negative or zero-input usage is an error, never zero cost."""
    if not isinstance(raw, Mapping):
        raise MalformedUsageError("USAGE_MISSING")
    counts = []
    for key in (input_key, output_key):
        value = raw.get(key)
        if type(value) is not int or value < 0 or value > MAX_REPORTED_TOKENS:
            raise MalformedUsageError("USAGE_INVALID", f"{key} is not a valid token count")
        counts.append(value)
    if counts[0] < 1:
        raise MalformedUsageError("USAGE_INVALID", f"{input_key} must be at least 1")
    return Usage(counts[0], counts[1])


def classify_http_status(status: int, detail: str) -> ModelCallError:
    if status == 408 or status >= 500 or status < 400:
        return OutcomeUnknownError(f"HTTP_{status}", detail)
    return ProviderRejectedError(f"HTTP_{status}", detail)


def classify_httpx_error(exc: Exception) -> ModelCallError:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)):
        return NotSentError("CONNECT_FAILED", type(exc).__name__)
    if isinstance(exc, httpx.InvalidURL):
        return NotSentError("INVALID_URL", type(exc).__name__)
    return OutcomeUnknownError("TRANSPORT_FAILED", type(exc).__name__)


class _ChatCompletionsAdapter:
    """OpenAI-style chat completions over httpx. OpenRouter and Azure differ only in URL and headers."""

    backend = ""

    def __init__(self, *, model_id: str, api_key: str, http_client: httpx.AsyncClient):
        if not model_id or not api_key:
            raise ValueError("model_id and api_key are required")
        self.model_id = model_id
        self._api_key = api_key
        self._http = http_client

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model_id={self.model_id!r})"

    def _url(self) -> str:
        raise NotImplementedError

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _params(self) -> dict[str, str]:
        return {}

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        raise NotImplementedError

    def _secrets(self) -> list[str]:
        return [self._api_key]

    async def complete(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        try:
            response = await self._http.post(
                self._url(), headers=self._headers(), params=self._params(), json=self._body(request), timeout=timeout
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise classify_httpx_error(exc) from None
        return self._parse(response)

    def _parse(self, response: httpx.Response) -> ModelResponse:
        if not 200 <= response.status_code < 300:
            raise classify_http_status(response.status_code, redact(response.text, self._secrets()))
        try:
            data = response.json()
        except ValueError:
            raise MalformedResponseError("RESPONSE_NOT_JSON") from None
        if not isinstance(data, dict) or "error" in data:
            raise MalformedResponseError("RESPONSE_SHAPE")
        usage = parse_usage(data.get("usage"), input_key="prompt_tokens", output_key="completion_tokens")
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
            finish_reason = choice.get("finish_reason") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise MalformedResponseError("RESPONSE_SHAPE") from None
        if not isinstance(text, str) or not isinstance(finish_reason, str):
            raise MalformedResponseError("RESPONSE_SHAPE")
        request_id = data.get("id")
        model = data.get("model")
        return ModelResponse(
            text=text,
            usage=usage,
            model=model if isinstance(model, str) and model else self.model_id,
            backend=self.backend,
            finish_reason=finish_reason,
            provider_request_id=request_id if isinstance(request_id, str) else None,
        )

    async def aclose(self) -> None:
        await self._http.aclose()


def _wire_messages(request: ModelRequest) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in request.messages]


class OpenRouterAdapter(_ChatCompletionsAdapter):
    backend = "openrouter"

    def __init__(self, *, model_id: str, api_key: str, http_client: httpx.AsyncClient,
                 base_url: str = "https://openrouter.ai/api/v1"):
        super().__init__(model_id=model_id, api_key=api_key, http_client=http_client)
        self._base_url = base_url.rstrip("/")

    def _url(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    def _body(self, request: ModelRequest) -> dict[str, Any]:
        return {
            "model": self.model_id,
            "messages": _wire_messages(request),
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
        }

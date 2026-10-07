import httpx
import pytest
from trader.ai.model_client import (
    ChatMessage,
    MalformedResponseError,
    MalformedUsageError,
    ModelRequest,
    NotSentError,
    OpenRouterAdapter,
    OutcomeUnknownError,
    ProviderRejectedError,
    Usage,
    estimate_input_tokens,
)


API_KEY = "sk-or-test-key-123456"


def request(**kwargs) -> ModelRequest:
    values = dict(request_key="d1/jev/1", messages=(ChatMessage("user", "hello"),), max_output_tokens=100)
    values.update(kwargs)
    return ModelRequest(**values)


def completion(usage="default", content="hi", **extra) -> dict:
    body = {
        "id": "gen-1", "model": "vendor/orch-1",
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5} if usage == "default" else usage,
    }
    body.update(extra)
    return body


def adapter(handler) -> OpenRouterAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterAdapter(model_id="vendor/orch-1", api_key=API_KEY, http_client=client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [None, {}, {"prompt_tokens": 10}, {"prompt_tokens": "10", "completion_tokens": 5},
     {"prompt_tokens": 10.0, "completion_tokens": 5}, {"prompt_tokens": True, "completion_tokens": 5},
     {"prompt_tokens": -1, "completion_tokens": 5}, {"prompt_tokens": 0, "completion_tokens": 5},
     {"prompt_tokens": 10, "completion_tokens": None}, "free"],
)
async def test_malformed_usage_is_an_error_never_zero_cost(usage):
    with pytest.raises(MalformedUsageError) as caught:
        await adapter(lambda r: httpx.Response(200, json=completion(usage=usage))).complete(request(), timeout_seconds=5)
    assert isinstance(caught.value, OutcomeUnknownError) and caught.value.outcome == "COST_UNKNOWN"


@pytest.mark.asyncio
async def test_credentials_never_appear_in_errors_or_repr():
    echo = httpx.Response(401, text=f"bad key {API_KEY} for you")
    target = adapter(lambda r: echo)
    with pytest.raises(ProviderRejectedError) as caught:
        await target.complete(request(), timeout_seconds=5)
    assert API_KEY not in str(caught.value) and API_KEY not in caught.value.detail
    assert API_KEY not in repr(target)


@pytest.mark.asyncio
async def test_success_parses_text_and_strict_usage():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers["authorization"]
        seen["body"] = req.read()
        return httpx.Response(200, json=completion())

    response = await adapter(handler).complete(request(), timeout_seconds=5)
    assert response.text == "hi"
    assert response.usage == Usage(10, 5)
    assert response.backend == "openrouter" and response.model == "vendor/orch-1"
    assert response.finish_reason == "stop" and response.provider_request_id == "gen-1"
    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert b'"max_tokens":100' in seen["body"].replace(b" ", b"")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json=["a list"]),
        httpx.Response(200, json={"error": {"message": "upstream failed"}, "usage": {"prompt_tokens": 1, "completion_tokens": 1}}),
        httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}, "choices": []}),
        httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}, "choices": [{"message": {"content": None}}]}),
    ],
)
async def test_unusable_200_body_is_a_malformed_response(response):
    with pytest.raises(MalformedResponseError) as caught:
        await adapter(lambda r: response).complete(request(), timeout_seconds=5)
    assert caught.value.outcome == "COST_UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expected",
    [(400, ProviderRejectedError), (401, ProviderRejectedError), (402, ProviderRejectedError),
     (404, ProviderRejectedError), (429, ProviderRejectedError), (408, OutcomeUnknownError),
     (500, OutcomeUnknownError), (502, OutcomeUnknownError), (503, OutcomeUnknownError)],
)
async def test_http_status_classification(status, expected):
    with pytest.raises(expected) as caught:
        await adapter(lambda r: httpx.Response(status, text="no")).complete(request(), timeout_seconds=5)
    assert type(caught.value) is expected
    assert caught.value.code == f"HTTP_{status}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected",
    [(httpx.ConnectError("refused"), NotSentError), (httpx.ConnectTimeout("slow"), NotSentError),
     (httpx.ReadTimeout("slow"), OutcomeUnknownError), (httpx.RemoteProtocolError("dropped"), OutcomeUnknownError),
     (httpx.ReadError("reset"), OutcomeUnknownError)],
)
async def test_transport_errors_split_not_sent_from_unknown(error, expected):
    def handler(req):
        raise error

    with pytest.raises(expected) as caught:
        await adapter(handler).complete(request(), timeout_seconds=5)
    assert type(caught.value) is expected


def test_request_validation_is_strict():
    ok = (ChatMessage("user", "x"),)
    with pytest.raises(ValueError):
        ChatMessage("tool", "x")
    with pytest.raises(ValueError):
        ChatMessage("user", 5)
    for bad in (dict(request_key=""), dict(request_key="a#1"), dict(request_key=5), dict(messages=()),
                dict(messages=("text",)), dict(max_output_tokens=0), dict(max_output_tokens=True),
                dict(max_output_tokens=1.5), dict(temperature=-0.1), dict(temperature=2.5),
                dict(temperature="hot")):
        values = dict(request_key="d/o/1", messages=ok, max_output_tokens=10)
        values.update(bad)
        with pytest.raises(ValueError):
            ModelRequest(**values)
    ModelRequest(request_key="d/o/1", messages=ok, max_output_tokens=10, temperature=2)
    with pytest.raises(ValueError):
        Usage(True, 1)
    with pytest.raises(ValueError):
        Usage(1, -1)
    with pytest.raises(ValueError):
        Usage(1.0, 1)


def test_input_estimate_is_an_upper_bound_style_guess():
    assert estimate_input_tokens((ChatMessage("user", "abc"),)) == 1 + 4
    assert estimate_input_tokens((ChatMessage("user", "abcd"),)) == 2 + 4
    two = (ChatMessage("system", "a" * 30), ChatMessage("user", "é" * 3))
    assert estimate_input_tokens(two) == (10 + 4) + (2 + 4)

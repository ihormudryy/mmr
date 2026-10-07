import httpx
import pytest
from botocore import exceptions as bx
from trader.ai.config import AiConfigError, RoleConfig
from trader.ai.model_client import (
    AzureOpenAIAdapter,
    BedrockAdapter,
    ChatMessage,
    MalformedResponseError,
    MalformedUsageError,
    ModelRequest,
    NotSentError,
    OpenRouterAdapter,
    OutcomeUnknownError,
    ProviderRejectedError,
    Usage,
    build_model_client,
)


KEY = "azure-secret-key-9876"


def request() -> ModelRequest:
    return ModelRequest(request_key="d/o/1", max_output_tokens=50,
                        messages=(ChatMessage("system", "be brief"), ChatMessage("user", "hi")))


def bedrock_reply(**overrides) -> dict:
    reply = {"output": {"message": {"role": "assistant", "content": [{"text": "done"}]}},
             "usage": {"inputTokens": 11, "outputTokens": 3, "totalTokens": 14},
             "stopReason": "end_turn", "ResponseMetadata": {"RequestId": "req-9"}}
    reply.update(overrides)
    return reply


def client_error(code: str) -> bx.ClientError:
    return bx.ClientError({"Error": {"Code": code, "Message": "m"}}, "Converse")


def azure(handler) -> AzureOpenAIAdapter:
    return AzureOpenAIAdapter(
        deployment="my-deploy", endpoint="https://res.openai.azure.com/", api_key=KEY, api_version="2024-10-21",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected",
    [(client_error("ThrottlingException"), ProviderRejectedError),
     (client_error("ValidationException"), ProviderRejectedError),
     (client_error("ModelTimeoutException"), OutcomeUnknownError),
     (client_error("InternalServerException"), OutcomeUnknownError),
     (bx.NoCredentialsError(), NotSentError),
     (bx.EndpointConnectionError(endpoint_url="https://x"), NotSentError),
     (bx.ReadTimeoutError(endpoint_url="https://x"), OutcomeUnknownError),
     (RuntimeError("boom"), OutcomeUnknownError)],
)
async def test_bedrock_error_classification(error, expected):
    def converse(**arguments):
        raise error

    with pytest.raises(expected) as caught:
        await BedrockAdapter(model_id="p", converse=converse).complete(request(), timeout_seconds=5)
    assert type(caught.value) is expected


@pytest.mark.asyncio
async def test_azure_uses_deployment_url_api_version_and_api_key_header():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = req.url
        seen["headers"] = req.headers
        seen["body"] = req.read()
        return httpx.Response(200, json={
            "id": "az-1", "model": "gpt-x",
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        })

    response = await azure(handler).complete(request(), timeout_seconds=5)
    assert response.text == "ok" and response.usage == Usage(7, 2) and response.backend == "azure"
    assert seen["url"].path == "/openai/deployments/my-deploy/chat/completions"
    assert seen["url"].params["api-version"] == "2024-10-21"
    assert seen["headers"]["api-key"] == KEY and "authorization" not in seen["headers"]
    assert b"max_completion_tokens" in seen["body"] and b'"model"' not in seen["body"]


@pytest.mark.asyncio
async def test_azure_error_body_that_echoes_the_key_is_redacted():
    target = azure(lambda r: httpx.Response(401, text=f"invalid key {KEY}"))
    with pytest.raises(ProviderRejectedError) as caught:
        await target.complete(request(), timeout_seconds=5)
    assert KEY not in str(caught.value) and KEY not in caught.value.detail and KEY not in repr(target)


@pytest.mark.asyncio
async def test_bedrock_converse_arguments_and_parse():
    seen = {}

    def converse(**arguments):
        seen.update(arguments)
        return bedrock_reply()

    response = await BedrockAdapter(model_id="profile-1", converse=converse).complete(request(), timeout_seconds=5)
    assert seen["modelId"] == "profile-1"
    assert seen["system"] == [{"text": "be brief"}]
    assert seen["messages"] == [{"role": "user", "content": [{"text": "hi"}]}]
    assert seen["inferenceConfig"] == {"maxTokens": 50, "temperature": 0.0}
    assert response.text == "done" and response.usage == Usage(11, 3)
    assert response.finish_reason == "end_turn" and response.provider_request_id == "req-9"
    assert response.backend == "bedrock" and response.model == "profile-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [None, {}, {"inputTokens": 5}, {"inputTokens": "5", "outputTokens": 1}, {"inputTokens": 0, "outputTokens": 1},
     {"inputTokens": True, "outputTokens": 1}],
)
async def test_bedrock_malformed_usage_is_an_error(usage):
    adapter = BedrockAdapter(model_id="p", converse=lambda **a: bedrock_reply(usage=usage))
    with pytest.raises(MalformedUsageError) as caught:
        await adapter.complete(request(), timeout_seconds=5)
    assert caught.value.outcome == "COST_UNKNOWN"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [bedrock_reply(output={"message": {"role": "assistant", "content": []}}),
     bedrock_reply(output={"message": {"role": "assistant", "content": [{"toolUse": {}}]}}),
     bedrock_reply(output={}), "not a mapping"],
)
async def test_bedrock_response_without_text_is_malformed(reply):
    adapter = BedrockAdapter(model_id="p", converse=lambda **a: reply)
    with pytest.raises(MalformedResponseError):
        await adapter.complete(request(), timeout_seconds=5)


def test_build_model_client_picks_the_adapter_and_checks_env():
    role = lambda backend: RoleConfig(backend=backend, model="m-1")  # noqa: E731
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))

    router = build_model_client(role("openrouter"), environ={"OPENROUTER_API_KEY": "k-12345"}, http_client=http)
    assert isinstance(router, OpenRouterAdapter)

    env = {"AZURE_OPENAI_ENDPOINT": "https://x", "AZURE_OPENAI_API_KEY": "k-12345", "AZURE_OPENAI_API_VERSION": "v1"}
    assert isinstance(build_model_client(role("azure"), environ=env, http_client=http), AzureOpenAIAdapter)

    converse = lambda **a: bedrock_reply()  # noqa: E731
    bedrock = build_model_client(role("bedrock"), environ={"AWS_REGION": "us-east-1"}, bedrock_converse=converse)
    assert isinstance(bedrock, BedrockAdapter)
    fallback = build_model_client(role("bedrock"), environ={"AWS_DEFAULT_REGION": "eu-west-1"}, bedrock_converse=converse)
    assert isinstance(fallback, BedrockAdapter)

    for backend, environ in (("openrouter", {}), ("azure", {"AZURE_OPENAI_ENDPOINT": "https://x"}), ("bedrock", {})):
        with pytest.raises(AiConfigError) as caught:
            build_model_client(role(backend), environ=environ, http_client=http, bedrock_converse=converse)
        assert caught.value.code == "CREDENTIALS_MISSING"
    with pytest.raises(AiConfigError) as caught:
        build_model_client(role("vertex"), environ={}, http_client=http)
    assert caught.value.code == "ROLE_BACKEND_UNSUPPORTED"

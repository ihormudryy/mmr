from decimal import Decimal
from pathlib import Path
import pytest
from tests.ai.fakes import config_text, load_test_config, write_config
from trader.ai.config import (
    AiConfigError,
    ModelPrice,
    check_credentials,
    load_ai_config,
    micros_to_usd_str,
    usd_to_micros_floor,
)


TEMPLATE = Path(__file__).resolve().parents[2] / "config_defaults" / "ai.yaml"


def refused(tmp_path, text: str) -> AiConfigError:
    with pytest.raises(AiConfigError) as caught:
        load_ai_config(str(write_config(tmp_path, text)))
    return caught.value


def test_shipped_template_refuses_to_start_without_model_ids():
    with pytest.raises(AiConfigError) as caught:
        load_ai_config(str(TEMPLATE))
    assert caught.value.code == "ROLE_MODEL_MISSING"


def test_jev_must_use_openrouter(tmp_path):
    assert refused(tmp_path, config_text(jev_backend="azure")).code == "JEV_BACKEND_NOT_OPENROUTER"


def test_valid_config_loads_with_spec_defaults(tmp_path):
    config = load_test_config(tmp_path)
    orchestrator = config.role("orchestrator")
    assert orchestrator.backend == "openrouter" and orchestrator.model == "vendor/orch-1"
    assert orchestrator.max_input_tokens == 60000
    assert orchestrator.max_output_tokens == 4000
    assert not hasattr(config.budget, "model_budget_usd_per_day")
    assert config.budget.calls_per_hour == 120
    assert config.budget.max_in_flight == 2
    assert config.budget.decision_deadline_seconds == 60.0
    assert config.database_path == "~/.local/share/mmr_ai/ai.duckdb"


def test_missing_file_is_a_loud_error(tmp_path):
    with pytest.raises(AiConfigError) as caught:
        load_ai_config(str(tmp_path / "nope.yaml"))
    assert caught.value.code == "AI_CONFIG_NOT_FOUND"


def test_orchestrator_may_use_bedrock_or_azure(tmp_path):
    for backend in ("bedrock", "azure"):
        config = load_test_config(tmp_path, orchestrator_backend=backend)
        assert config.role("orchestrator").backend == backend


def test_unsupported_backend_is_refused(tmp_path):
    error = refused(tmp_path, config_text(orchestrator_backend="vertex"))
    assert error.code == "ROLE_BACKEND_UNSUPPORTED"


def test_blank_model_is_refused_and_names_the_role(tmp_path):
    error = refused(tmp_path, config_text(jev_model="  "))
    assert error.code == "ROLE_MODEL_MISSING" and "jev" in error.message


def test_max_in_flight_above_two_is_refused(tmp_path):
    assert refused(tmp_path, config_text(max_in_flight=3)).code == "MAX_IN_FLIGHT_ABOVE_LIMIT"


def test_unknown_key_is_refused_and_the_value_is_not_echoed(tmp_path):
    error = refused(tmp_path, config_text(extra_top_level="surprise: hunter2-secret-value"))
    assert error.code == "AI_CONFIG_INVALID"
    assert "hunter2" not in str(error)
    assert "surprise" in error.message


def test_a_cap_in_ai_yaml_is_an_unknown_key(tmp_path):
    text = config_text().replace("budget:\n", "budget:\n  model_budget_usd_per_day: 5000\n")
    error = refused(tmp_path, text)
    assert error.code == "AI_CONFIG_INVALID"
    assert "budget.model_budget_usd_per_day" in error.message
    assert "5000" not in str(error)


@pytest.mark.parametrize("key,value", [("calls_per_hour", "0"), ("calls_per_hour", "1.5"), ("calls_per_hour", "'5'"),
                                       ("max_in_flight", "true"), ("decision_deadline_seconds", ".nan"),
                                       ("decision_deadline_seconds", ".inf"), ("decision_deadline_seconds", "-1"),
                                       ("decision_deadline_seconds", "'60'"), ("decision_deadline_seconds", "null")])
def test_budget_limits_must_be_real_positive_numbers(tmp_path, key, value):
    text = config_text().replace("budget:\n", f"budget:\n  {key}: {value}\n", 1)
    # the later duplicate line in the fixture would win in YAML, so drop it
    lines = text.splitlines()
    first = next(i for i, line in enumerate(lines) if line.startswith(f"  {key}:"))
    lines = [line for i, line in enumerate(lines) if i == first or not line.startswith(f"  {key}:")]
    assert refused(tmp_path, "\n".join(lines)).code == "AI_CONFIG_INVALID"


def test_missing_price_is_not_a_load_error_but_the_price_is_absent(tmp_path):
    config = load_test_config(tmp_path, orchestrator_model="vendor/unpriced")
    assert config.prices.price_for("openrouter", "vendor/unpriced") is None
    assert config.prices.price_for("openrouter", "vendor/jev-1") is not None


def test_cost_rounds_up_to_whole_micro_usd():
    price = ModelPrice(Decimal("3.0"), Decimal("15.0"))
    assert price.cost_micros(60000, 4000) == 240_000
    assert ModelPrice(Decimal("0.5"), Decimal("0")).cost_micros(1, 0) == 1
    assert ModelPrice(Decimal("0"), Decimal("0")).cost_micros(10, 10) == 0


def test_money_helpers():
    assert usd_to_micros_floor(2000) == 2_000_000_000
    assert usd_to_micros_floor(0.0000019) == 1
    assert usd_to_micros_floor(Decimal("1.2345678")) == 1_234_567
    assert micros_to_usd_str(240_000) == "0.240000"
    assert micros_to_usd_str(0) == "0.000000"


def test_digest_changes_when_a_price_changes(tmp_path):
    first = load_test_config(tmp_path)
    again = load_test_config(tmp_path)
    assert first.digest() == again.digest()
    text = config_text().replace("input_usd_per_million: 3.0", "input_usd_per_million: 3.5")
    changed = load_ai_config(str(write_config(tmp_path, text)))
    assert changed.digest() != first.digest()


def test_credentials_are_checked_by_name_only(tmp_path):
    config = load_test_config(tmp_path)
    with pytest.raises(AiConfigError) as caught:
        check_credentials(config, {})
    assert caught.value.code == "CREDENTIALS_MISSING" and "OPENROUTER_API_KEY" in caught.value.message
    check_credentials(config, {"OPENROUTER_API_KEY": "sk-secret-value"})
    with pytest.raises(AiConfigError) as caught:
        check_credentials(config, {"OPENROUTER_API_KEY": ""})
    assert "sk-secret-value" not in str(caught.value)


def test_azure_and_bedrock_credentials(tmp_path):
    config = load_test_config(tmp_path, orchestrator_backend="azure")
    env = {"OPENROUTER_API_KEY": "k"}
    with pytest.raises(AiConfigError) as caught:
        check_credentials(config, env)
    for name in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION"):
        assert name in caught.value.message
    env.update(AZURE_OPENAI_ENDPOINT="https://x", AZURE_OPENAI_API_KEY="k", AZURE_OPENAI_API_VERSION="v")
    check_credentials(config, env)

    bedrock = load_test_config(tmp_path, orchestrator_backend="bedrock")
    with pytest.raises(AiConfigError) as caught:
        check_credentials(bedrock, {"OPENROUTER_API_KEY": "k"}, aws_credentials_present=lambda: False)
    assert "AWS_REGION" in caught.value.message and "AWS credentials" in caught.value.message
    check_credentials(
        bedrock, {"OPENROUTER_API_KEY": "k", "AWS_REGION": "us-east-1"}, aws_credentials_present=lambda: True
    )
    check_credentials(
        bedrock, {"OPENROUTER_API_KEY": "k", "AWS_DEFAULT_REGION": "us-east-1"}, aws_credentials_present=lambda: True
    )

"""The names Plans 5 and 6 import. Renaming one of these breaks them, so the test pins them."""
import importlib

import pytest

PUBLIC_NAMES = {
    "trader.ai.clock": ["Clock", "SystemClock"],
    "trader.ai.config": ["AiConfig", "AiConfigError", "BudgetConfig", "ModelPrice", "PriceBook", "RoleConfig",
                         "ROLE_NAMES", "check_credentials", "load_ai_config", "micros_to_usd_str",
                         "usd_to_micros_floor"],
    "trader.ai.model_client": ["AzureOpenAIAdapter", "BedrockAdapter", "ChatMessage", "MalformedResponseError",
                               "MalformedUsageError", "ModelCallError", "ModelClient", "ModelRequest",
                               "ModelResponse", "NotSentError", "OpenRouterAdapter", "OutcomeUnknownError",
                               "ProviderRejectedError", "Usage", "build_model_client", "estimate_input_tokens"],
    "trader.ai.schema": ["FOUNDATION_MIGRATIONS", "Migration"],
    "trader.ai.store": ["AiStore", "to_utc"],
    "trader.ai.journal": ["AttemptJournal", "AttemptRecord", "CostEvent", "JournalError", "COST_CONFIRMED",
                          "COST_CORRECTION", "COST_ESTIMATED_UNKNOWN", "COST_NONE"],
    "trader.ai.budget": ["Budget", "BudgetConflict", "BudgetExhausted", "BudgetSnapshot", "HourlyLimitReached",
                         "next_window_start", "window_date"],
    "trader.ai.gateway": ["CallFailed", "CallRefused", "DecisionDeadline", "GatewayResult", "ModelCaller",
                          "ModelGateway", "build_gateway"],
    "trader.ai.replay": ["ExternalAdapterCounter", "ExternalCallInReplay", "RecordingClock", "ReplayClock",
                         "ReplayDiverged", "ReplayEvidence", "ReplayGateway", "ReplayIncomplete",
                         "ReplayModelClient", "ReplayRecorder", "ReplayResult", "ReplaySession"],
    "trader.ai.untrusted": ["OutputRefusal", "ParsedOutput", "RESERVED_FIELD_NAMES", "StrictModelOutput",
                            "fence_untrusted", "parse_model_output"],
}


@pytest.mark.parametrize("module,names", sorted(PUBLIC_NAMES.items()))
def test_public_names_exist(module, names):
    loaded = importlib.import_module(module)
    assert [name for name in names if not hasattr(loaded, name)] == []

from __future__ import annotations
import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Annotated, Callable, Mapping, Optional
import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictStr, ValidationError


SUPPORTED_BACKENDS = ("openrouter", "bedrock", "azure")
ROLE_NAMES = ("orchestrator", "jev")
MAX_IN_FLIGHT_LIMIT = 2
MICROS_PER_USD = 1_000_000
DEFAULT_CONFIG_PATH = "~/.config/mmr/ai.yaml"


class AiConfigError(ValueError):
    """A startup problem. The message never contains a value read from the file."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _as_int(value: object) -> int:
    if type(value) is not int:
        raise ValueError("must be an integer")
    return value


def _as_number(value: object) -> float:
    if type(value) not in (int, float):
        raise ValueError("must be a number")
    return float(value)


Whole = Annotated[int, BeforeValidator(_as_int)]
Number = Annotated[float, BeforeValidator(_as_number)]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, protected_namespaces=())


class RoleConfig(_Section):
    backend: StrictStr = ""
    model: StrictStr = ""
    max_input_tokens: Whole = Field(60000, gt=0, le=2_000_000)
    max_output_tokens: Whole = Field(4000, gt=0, le=200_000)
    call_timeout_seconds: Number = Field(45.0, gt=0, le=600, allow_inf_nan=False)


class BudgetConfig(_Section):
    # No cap here: the owner's cap is ai_paper.model_budget_usd_per_day in trader.yaml.
    calls_per_hour: Whole = Field(120, gt=0)
    max_in_flight: Whole = Field(MAX_IN_FLIGHT_LIMIT, gt=0)
    decision_deadline_seconds: Number = Field(60.0, gt=0, le=600, allow_inf_nan=False)


class ControllerConfig(_Section):
    """The controller runtime (SP2 Plan 5). Defaults follow the index: lease 60 s, renew 20 s, slots 15 min."""
    trader_query_port: Whole = Field(42101, gt=0, lt=65536)
    trader_command_port: Whole = Field(42102, gt=0, lt=65536)
    rpc_timeout_seconds: Number = Field(10.0, gt=0, le=60, allow_inf_nan=False)
    lease_seconds: Whole = Field(60, ge=10, le=600)
    renew_seconds: Whole = Field(20, gt=0, le=200)
    held_retry_seconds: Number = Field(5.0, gt=0, le=60, allow_inf_nan=False)
    entry_slot_minutes: Whole = Field(15, ge=5, le=60)
    position_slot_minutes: Whole = Field(15, ge=5, le=60)
    slot_start_grace_seconds: Whole = Field(120, ge=10, le=600)
    signal_poll_seconds: Number = Field(5.0, gt=0, le=60, allow_inf_nan=False)
    signal_page_limit: Whole = Field(100, ge=1, le=500)
    signal_max_age_seconds: Whole = Field(300, ge=30, le=3600)
    decision_ttl_seconds: Whole = Field(300, ge=30, le=900)
    not_found_settle_seconds: Whole = Field(120, ge=60, le=900)
    reconcile_seconds: Number = Field(10.0, gt=0, le=300, allow_inf_nan=False)
    outbox_seconds: Number = Field(5.0, gt=0, le=300, allow_inf_nan=False)
    experiment_poll_seconds: Number = Field(10.0, gt=0, le=300, allow_inf_nan=False)
    heartbeat_seconds: Number = Field(10.0, gt=0, le=60, allow_inf_nan=False)
    budget_cap_poll_seconds: Number = Field(60.0, gt=0, le=300, allow_inf_nan=False)
    heartbeat_path: StrictStr = "/tmp/mmr_ai_heartbeat.json"


class _PriceRow(_Section):
    input_usd_per_million: Number = Field(ge=0, allow_inf_nan=False)
    output_usd_per_million: Number = Field(ge=0, allow_inf_nan=False)


class _RawConfig(_Section):
    roles: dict[StrictStr, RoleConfig]
    pricing: dict[StrictStr, dict[StrictStr, _PriceRow]] = Field(default_factory=dict)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    database_path: StrictStr = "~/.local/share/mmr_ai/ai.duckdb"
    controller: ControllerConfig = Field(default_factory=ControllerConfig)


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal

    def cost_micros(self, input_tokens: int, output_tokens: int) -> int:
        """USD per million tokens equals micro-USD per token. Always rounds up."""
        total = Decimal(input_tokens) * self.input_usd_per_million
        total += Decimal(output_tokens) * self.output_usd_per_million
        return int(total.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class PriceBook:
    rows: Mapping[tuple[str, str], ModelPrice]

    def price_for(self, backend: str, model: str) -> Optional[ModelPrice]:
        return self.rows.get((backend, model))


@dataclass(frozen=True)
class AiConfig:
    roles: Mapping[str, RoleConfig]
    prices: PriceBook
    budget: BudgetConfig
    database_path: str
    controller: ControllerConfig = ControllerConfig()

    def role(self, name: str) -> RoleConfig:
        try:
            return self.roles[name]
        except KeyError:
            raise AiConfigError("ROLE_UNKNOWN", f"unknown role {name!r}") from None

    def digest(self) -> str:
        """Stable hash of everything that is not a secret. Replay records it."""
        body = {
            "roles": {name: role.model_dump() for name, role in sorted(self.roles.items())},
            "prices": {
                f"{backend}/{model}": [str(p.input_usd_per_million), str(p.output_usd_per_million)]
                for (backend, model), p in sorted(self.prices.rows.items())
            },
            "budget": self.budget.model_dump(),
            "controller": self.controller.model_dump(),
        }
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def load_ai_config(path: str = DEFAULT_CONFIG_PATH) -> AiConfig:
    file = Path(path).expanduser()
    if not file.is_file():
        raise AiConfigError("AI_CONFIG_NOT_FOUND", f"{file} does not exist")
    try:
        raw = yaml.safe_load(file.read_text())
    except yaml.YAMLError as exc:
        raise AiConfigError("AI_CONFIG_INVALID", f"not valid YAML ({type(exc).__name__})") from None
    if not isinstance(raw, dict):
        raise AiConfigError("AI_CONFIG_INVALID", "the file must be a mapping")
    try:
        parsed = _RawConfig.model_validate(raw)
    except ValidationError as exc:
        where = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors())
        raise AiConfigError("AI_CONFIG_INVALID", where) from None
    return _check(parsed)


def _check(parsed: _RawConfig) -> AiConfig:
    for name in ROLE_NAMES:
        if name not in parsed.roles:
            raise AiConfigError("ROLE_MISSING", f"role {name!r} is not configured")
    for name, role in parsed.roles.items():
        if name not in ROLE_NAMES:
            raise AiConfigError("ROLE_UNKNOWN", f"unknown role {name!r}")
        if not role.model.strip():
            raise AiConfigError("ROLE_MODEL_MISSING", f"role {name!r} has no model id")
        if role.backend not in SUPPORTED_BACKENDS:
            raise AiConfigError(
                "ROLE_BACKEND_UNSUPPORTED",
                f"role {name!r}: backend must be one of {', '.join(SUPPORTED_BACKENDS)}",
            )
        if role.call_timeout_seconds > parsed.budget.decision_deadline_seconds:
            raise AiConfigError("CALL_TIMEOUT_ABOVE_DEADLINE", f"role {name!r} call timeout is above the decision deadline")
    if parsed.roles["jev"].backend != "openrouter":
        raise AiConfigError("JEV_BACKEND_NOT_OPENROUTER", "Jev runs on openrouter only")
    if parsed.budget.max_in_flight > MAX_IN_FLIGHT_LIMIT:
        raise AiConfigError("MAX_IN_FLIGHT_ABOVE_LIMIT", f"max_in_flight may not exceed {MAX_IN_FLIGHT_LIMIT}")
    rows: dict[tuple[str, str], ModelPrice] = {}
    for backend, models in parsed.pricing.items():
        if backend not in SUPPORTED_BACKENDS:
            raise AiConfigError("PRICING_BACKEND_UNSUPPORTED", f"pricing names an unsupported backend {backend!r}")
        for model, row in models.items():
            rows[(backend, model)] = ModelPrice(
                Decimal(repr(row.input_usd_per_million)), Decimal(repr(row.output_usd_per_million))
            )
    controller = parsed.controller
    if controller.renew_seconds * 3 > controller.lease_seconds:
        raise AiConfigError("CONTROLLER_RENEW_TOO_SLOW", "renew_seconds must be at most a third of lease_seconds")
    shortest_slot = min(controller.entry_slot_minutes, controller.position_slot_minutes) * 60
    if controller.slot_start_grace_seconds >= shortest_slot:
        raise AiConfigError("CONTROLLER_GRACE_TOO_LONG", "slot_start_grace_seconds must be shorter than a slot")
    return AiConfig(dict(parsed.roles), PriceBook(rows), parsed.budget, parsed.database_path, controller)


def usd_to_micros_floor(usd: float | Decimal) -> int:
    return int((Decimal(str(usd)) * MICROS_PER_USD).to_integral_value(rounding=ROUND_FLOOR))


def micros_to_usd_str(micros: int) -> str:
    return f"{Decimal(micros) / MICROS_PER_USD:.6f}"


def _aws_chain_has_credentials() -> bool:
    import boto3

    return boto3.Session().get_credentials() is not None


_BACKEND_ENV: dict[str, tuple[str, ...]] = {
    "openrouter": ("OPENROUTER_API_KEY",),
    "azure": ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_VERSION"),
}
_AWS_CREDENTIALS_TEXT = "AWS credentials (AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY or AWS_PROFILE)"


def check_credentials(
    config: AiConfig,
    environ: Mapping[str, str],
    *,
    aws_credentials_present: Callable[[], bool] = _aws_chain_has_credentials,
) -> None:
    """Names only, never values."""
    missing: list[str] = []
    for backend in sorted({role.backend for role in config.roles.values()}):
        missing.extend(name for name in _BACKEND_ENV.get(backend, ()) if not environ.get(name))
        if backend == "bedrock":
            if not (environ.get("AWS_REGION") or environ.get("AWS_DEFAULT_REGION")):
                missing.append("AWS_REGION")
            if not aws_credentials_present():
                missing.append(_AWS_CREDENTIALS_TEXT)
    if missing:
        raise AiConfigError("CREDENTIALS_MISSING", "missing: " + ", ".join(missing))

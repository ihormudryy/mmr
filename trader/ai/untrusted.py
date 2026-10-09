"""Untrusted-input helpers (spec 8). Model text grants no authority.

`parse_model_output` never raises on bad model text. It returns a typed OutputRefusal for
anything malformed, off-menu or not exactly what the schema allows. Callers must treat a
refusal as "do not act" (never as TAKE, never as a default).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, Union

from pydantic import BaseModel, ConfigDict, ValidationError

MAX_OUTPUT_CHARS = 200_000
MAX_DEPTH = 12
MAX_DETAIL_CHARS = 300

# Names that only code may set. A model schema may not declare them, so model output can never
# carry a value that code would then trust.
RESERVED_FIELD_NAMES = frozenset({
    "decision_id", "command_id", "experiment_id", "account_id", "controller_epoch", "epoch",
    "evidence", "policy_revision", "expires_at", "principal", "attempt_key", "request_key",
})

_FENCE = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL | re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

OUTPUT_EMPTY = "OUTPUT_EMPTY"
OUTPUT_TOO_LARGE = "OUTPUT_TOO_LARGE"
OUTPUT_NO_JSON = "OUTPUT_NO_JSON"
OUTPUT_MULTIPLE_JSON = "OUTPUT_MULTIPLE_JSON"
OUTPUT_BAD_JSON = "OUTPUT_BAD_JSON"
OUTPUT_DUPLICATE_KEY = "OUTPUT_DUPLICATE_KEY"
OUTPUT_NOT_OBJECT = "OUTPUT_NOT_OBJECT"
OUTPUT_TOO_DEEP = "OUTPUT_TOO_DEEP"
OUTPUT_SCHEMA_VIOLATION = "OUTPUT_SCHEMA_VIOLATION"


class StrictModelOutput(BaseModel):
    """Base for every schema a model must fill. Unknown fields, wrong types and coercions are errors."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: Any) -> None:
        super().__pydantic_init_subclass__(**kwargs)
        names = set(cls.model_fields) | {f.alias for f in cls.model_fields.values() if f.alias}
        clash = RESERVED_FIELD_NAMES & names
        if clash:
            raise TypeError(f"{cls.__name__} declares code-owned field(s): {', '.join(sorted(clash))}")


T = TypeVar("T", bound=StrictModelOutput)


@dataclass(frozen=True)
class ParsedOutput(Generic[T]):
    value: T


@dataclass(frozen=True)
class OutputRefusal:
    code: str
    detail: str = ""


class _Refuse(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _Refuse(OUTPUT_DUPLICATE_KEY)
    return dict(pairs)


def _reject_constant(name: str) -> Any:
    raise _Refuse(OUTPUT_BAD_JSON, f"{name} is not allowed")


def _depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def extract_json_text(text: str) -> str:
    """Accept exactly one JSON object: the whole text, or the one fenced block in it.
    Prose around a bare object is refused, as are two blocks."""
    if not isinstance(text, str) or not text.strip():
        raise _Refuse(OUTPUT_EMPTY)
    if len(text) > MAX_OUTPUT_CHARS:
        raise _Refuse(OUTPUT_TOO_LARGE)
    stripped = text.strip()
    if stripped.startswith("{"):
        return stripped
    blocks = _FENCE.findall(stripped)
    if len(blocks) > 1:
        raise _Refuse(OUTPUT_MULTIPLE_JSON)
    if not blocks:
        raise _Refuse(OUTPUT_NO_JSON)
    return blocks[0].strip()


def parse_model_output(text: str, schema: type[T]) -> Union[ParsedOutput[T], OutputRefusal]:
    if not (isinstance(schema, type) and issubclass(schema, StrictModelOutput)):
        raise TypeError("schema must subclass StrictModelOutput")  # a programming error, not model input
    try:
        candidate = extract_json_text(text)
        try:
            loaded = json.loads(candidate, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
        except (ValueError, RecursionError):        # JSONDecodeError, or an integer literal over 4300 digits
            raise _Refuse(OUTPUT_BAD_JSON) from None
        if not isinstance(loaded, dict):
            raise _Refuse(OUTPUT_NOT_OBJECT)
        if _depth(loaded) > MAX_DEPTH:
            raise _Refuse(OUTPUT_TOO_DEEP)
        try:
            return ParsedOutput(schema.model_validate_json(candidate))
        except ValidationError as error:
            where = "; ".join(f"{'.'.join(map(str, e['loc']))}:{e['type']}" for e in error.errors())
            raise _Refuse(OUTPUT_SCHEMA_VIOLATION, where[:MAX_DETAIL_CHARS]) from None
    except _Refuse as refusal:
        return OutputRefusal(refusal.code, refusal.detail)


def fence_untrusted(label: str, text: str, *, max_chars: int) -> str:
    """Wrap untrusted text (news, headlines) for a prompt. The text cannot close the block,
    control characters are removed and the length is capped. The wrapper grants no authority."""
    if not re.fullmatch(r"[a-z0-9_]{1,40}", label):
        raise ValueError("label must be lower-case letters, digits or underscore")
    cleaned = _CONTROL.sub("", text)
    cleaned = re.sub(r"</?\s*untrusted[^>]*>", "[removed]", cleaned, flags=re.IGNORECASE)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + " [truncated]"
    return f'<untrusted source="{label}">\n{cleaned}\n</untrusted>'

from typing import Any
from trader.research.canonical import sha256_digest

def derive_intent_id(fields: dict[str, Any]) -> str:
    """Derive deterministic intent ID from canonical fields."""
    body = {k: v for k, v in fields.items() if k not in ("intent_id", "command_id")}
    return f"intent-{sha256_digest('intent', body)}"

def derive_command_id(intent_id: str) -> str:
    """Derive deterministic command ID from an intent ID."""
    return f"auto-{sha256_digest('auto', {'intent_id': intent_id})}"

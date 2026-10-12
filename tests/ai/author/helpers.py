"""Shared text for the author tests. ai.yaml with an author role is config_text plus three insertions."""
from tests.ai.fakes import config_text, write_config
from trader.ai.config import load_ai_config

UNIVERSES = "{b_big: [11, 12, 13, 14, 15, 16, 17, 18], a_small: [1, 2, 3, 4, 5, 6, 7, 8]}"
RESEARCH_BLOCK = ('research:\n  enabled: true\n  strategy_keys: ["strategies/time_of_day.py:TimeOfDay"]\n'
                  f"  universes: {UNIVERSES}\n  bar_sizes: [\"5 mins\", \"15 mins\"]\n")
AUTHOR_PRICE = '    "vendor/author-1": {input_usd_per_million: 2.0, output_usd_per_million: 8.0}\n'


def author_text(*, model: str = "vendor/author-1", role_extra: str = "", research: str = RESEARCH_BLOCK,
                price: bool = True, authoring: str = "", backend: str = "openrouter") -> str:
    text = config_text(extra_top_level=research + authoring)
    role = f'  author: {{backend: {backend}, model: "{model}", max_output_tokens: 8000{role_extra}}}\n'
    text = text.replace("pricing:\n  openrouter:\n", "pricing:\n  openrouter:\n" + (AUTHOR_PRICE if price else ""))
    return text.replace("roles:\n", "roles:\n" + role)


def load(tmp_path, text: str):
    return load_ai_config(str(write_config(tmp_path, text)))


def authored_reply(*, enabled=True, counted=0, max_active=3, strategies=(), bar_sizes=("5 mins", "15 mins"),
                   judged_count=0):
    """get_authored_strategies in the shape of AuthoredStrategyStore.overview()."""
    return {"enabled": enabled, "max_active": max_active, "counted": counted, "room": max(max_active - counted, 0),
            "judged_count": judged_count, "bar_sizes": list(bar_sizes), "records": list(strategies)}


def record(hex_digit="a", *, state="REJECTED", reason="JEV_REJECT", note="fade the first push after the open",
           class_name="Old", tunables=None):
    """One record in the shape of AuthoredRecord.to_json()."""
    digest = "sha256:" + hex_digit * 64
    return {"digest": digest, "key": f"authored:{digest}:{class_name}", "class_name": class_name,
            "bar_size": "15 mins", "tunables": tunables or {"WINDOW": {"low": 2.0, "high": 10.0, "default": 5.0}},
            "note": note,
            "author_model": "vendor/author-1", "state": state, "reason": reason, "attempts": 0,
            "evaluation_request_id": None, "sandbox_image_digest": None, "verdict": None, "judgment_id": None,
            "deployment_version": None, "created_at": "2026-10-07T21:05:00+00:00",
            "state_changed_at": "2026-10-07T21:05:00+00:00", "history": []}


def submit_reply(status="STORED", *, digest=None, class_name="OpeningRangeBreakout", code=None, retryable=False):
    """submit_authored_strategy in the shape of the store's reply: the stored record, or a refusal."""
    view = None
    if status != "REFUSED":
        view = {**record(state="STORED", reason=None, class_name=class_name), "digest": digest,
                "key": f"authored:{digest}:{class_name}"}
    return {"status": status, "code": code, "detail": None if code is None else code.lower(), "retryable": retryable,
            "record": view}

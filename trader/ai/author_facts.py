"""The author's prompt facts (SP3 spec 7.4), built by code from ai.duckdb and two trader reads.

Code facts are plain values. Text a model wrote earlier (the author's notes, Jev's reasons, a class name) goes in an
<untrusted> fence (Plan 5 ruling 15). A table or record that cannot be read raises AuthorFactsError: it is never
an empty list that looks like "no history". Nothing here reaches the sandbox or the store.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import trader.authored.api as authored_api
from trader.ai.author_roles import AUTHOR_RULES
from trader.ai.author_wire import AuthoredRecord
from trader.ai.research_roles import parse_backtest_ruling
from trader.ai.untrusted import OutputRefusal, fence_untrusted
from trader.authored.allowed_api import ALLOWED_FROM_IMPORT_NAMES, ALLOWED_IMPORTS, ALLOWED_MODULE_ATTRIBUTES
from trader.authored.keys import is_authored_key, is_valid_class_name

MAX_CATALOGUE_ROWS = 20
MAX_AUTHORED_ROWS = 30
FENCE_CHARS = 600
CLASS_NAME_FENCE_CHARS = 64
DECIDED_VERDICTS = ("DEPLOY", "SHADOW", "REJECT")
SUMMARY_FIELDS = ("bar_size", "stage", "rules_passed", "holdout_passed", "metrics", "strategy_trials")


class AuthorFactsError(RuntimeError):
    """A stored row the author's facts depend on cannot be read."""


def api_reference() -> dict:
    """The one repo module an authored file may import, as text, and Plan 1's allow-lists."""
    return {"api_source": Path(authored_api.__file__).read_text(encoding="utf-8"),
            "allowed_imports": sorted(ALLOWED_IMPORTS),
            "allowed_from_import_names": {module: sorted(names)
                                          for module, names in sorted(ALLOWED_FROM_IMPORT_NAMES.items())},
            "allowed_module_attributes": {module: sorted(names)
                                          for module, names in sorted(ALLOWED_MODULE_ATTRIBUTES.items())},
            "rules": list(AUTHOR_RULES)}


async def catalogue_evaluations(store: Any) -> list[dict]:
    """The newest evaluated case per catalogue strategy key, with Jev's verdict: code facts from ai.duckdb."""
    rows = await store.aquery(
        "SELECT c.candidate_id, c.strategy_key, c.summary_json, j.verdict, j.code FROM ai_research_candidates c "
        "LEFT JOIN ai_backtest_judgments j ON j.candidate_id = c.candidate_id "
        "WHERE c.kind = 'INITIAL' AND c.summary_json IS NOT NULL ORDER BY c.updated_at DESC, c.candidate_id")
    newest: dict[str, dict] = {}
    for candidate_id, strategy_key, summary_json, verdict, code in rows:
        if is_authored_key(strategy_key) or strategy_key in newest:
            continue
        summary = _read_summary(candidate_id, summary_json)
        newest[strategy_key] = {"strategy_key": strategy_key, **{name: summary[name] for name in SUMMARY_FIELDS},
                                "verdict": verdict, "code": code}
        if len(newest) == MAX_CATALOGUE_ROWS:
            break
    return list(newest.values())


def _read_summary(candidate_id: str, summary_json: str) -> Mapping[str, Any]:
    try:
        summary = json.loads(summary_json)
        missing = [name for name in SUMMARY_FIELDS if name not in summary]
    except (TypeError, ValueError) as error:
        raise AuthorFactsError(f"candidate {candidate_id}: unreadable evaluation summary") from error
    if missing:
        raise AuthorFactsError(f"candidate {candidate_id}: evaluation summary lacks {', '.join(missing)}")
    return summary


async def authored_history(store: Any, records: Sequence[AuthoredRecord]) -> list[dict]:
    """Every earlier authored file (the newest 30) with its outcome and Jev's verdict."""
    history = []
    for record in list(records)[-MAX_AUTHORED_ROWS:]:
        history.append({"digest": record.digest, "class_name": _fenced_class_name(record),
                        "state": record.state, "reason": record.reason, "bar_size": record.bar_size,
                        "attempts": record.attempts, "tunables": sorted(record.tunables),
                        "note": fence_untrusted("author_note", record.note, max_chars=FENCE_CHARS),
                        "jev": await jev_outcome(store, record.key)})
    return history


def _fenced_class_name(record: AuthoredRecord) -> str:
    if not is_valid_class_name(record.class_name):
        raise AuthorFactsError(f"authored record {record.digest}: class name is not an identifier")
    return fence_untrusted("author_class_name", record.class_name, max_chars=CLASS_NAME_FENCE_CHARS)


async def jev_outcome(store: Any, strategy_key: str) -> Optional[dict]:
    row = await store.aquery(
        "SELECT j.judgment_id, j.verdict, j.code, j.menu_json FROM ai_backtest_judgments j "
        "JOIN ai_research_candidates c ON c.candidate_id = j.candidate_id WHERE c.strategy_key = ? "
        "ORDER BY j.created_at DESC LIMIT 1", [strategy_key], fetch="one")
    if row is None:
        return None
    judgment_id, verdict, code, menu_json = row
    reason = None
    if verdict in DECIDED_VERDICTS:
        reason = await _jev_reason(store, judgment_id, tuple(json.loads(menu_json)))
    return {"verdict": verdict, "code": code, "reason": reason}


async def _jev_reason(store: Any, judgment_id: str, menu: tuple[str, ...]) -> str:
    """Jev's words are not stored with the judgment; they are re-read from its journaled answer.

    A decided verdict was parsed from this very answer, so a missing or unreadable one is a broken record.
    """
    row = await store.aquery(
        "SELECT response_text FROM ai_model_attempts WHERE request_key = ? AND response_text IS NOT NULL "
        "ORDER BY attempt_no DESC LIMIT 1", [f"{judgment_id}/jev/1"], fetch="one")
    if row is None:
        raise AuthorFactsError(f"judgment {judgment_id}: no journaled answer for its verdict")
    ruling = parse_backtest_ruling(row[0], menu=menu)
    if isinstance(ruling, OutputRefusal):
        raise AuthorFactsError(f"judgment {judgment_id}: journaled answer is unreadable ({ruling.code})")
    return fence_untrusted("jev_reason", ruling.reason, max_chars=FENCE_CHARS)


def scoreboard_facts(report: Mapping[str, Any]) -> dict:
    """The trader's PAPER report, cut to what an author can learn from (trader-built numbers)."""
    return {"label": report.get("label"), "account": report.get("account"),
            "by_strategy_digest": (report.get("splits") or {}).get("strategy_version"),
            "authored_vs_catalogue": report.get("authored_vs_catalogue")}


def build_author_facts(*, session_date: dt.date, universe: tuple[str, tuple[int, ...]], bar_sizes: Sequence[str],
                       catalogue: list[dict], scoreboard: dict, authored: list[dict], trial_count: int,
                       max_active: int, counted: int) -> dict:
    name, conids = universe
    return {"session_date": session_date.isoformat(), **api_reference(),
            "research_universe": {"name": name, "conids": list(conids)}, "bar_sizes": list(bar_sizes),
            "catalogue_evaluations": catalogue, "scoreboard": scoreboard, "earlier_authored": authored,
            "authored_trial_count": trial_count, "authored_cap": {"max_active": max_active, "counted": counted}}

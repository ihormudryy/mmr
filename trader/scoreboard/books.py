"""Separate baseline books and cost totals, built at read time from sealed rows (SP2 Plan 2, rulings 1, 15, 16).

A book is the group of simulated decisions with one (baseline_id, cohort). Books are never summed together.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Any, Mapping, Optional, Sequence

PNL_BASIS = "gross, no commissions or slippage"
LABEL = "simulated"
SHADOW_LABEL = "SHADOW"
SHADOW_BASIS = "forward replay: same backtester, cost model and live rules; never summed across verdicts"
SHADOW_VERDICTS = ("DEPLOY", "SHADOW", "REJECT")


def build_books(decisions: Sequence[Mapping[str, Any]], outcomes: Sequence[Mapping[str, Any]]) -> list[dict]:
    outcome_of = {o["record_id"]: o for o in outcomes}
    grouped: dict[tuple[str, str], list[Mapping]] = defaultdict(list)
    for decision in decisions:
        grouped[(decision["baseline_id"], decision["cohort"])].append(decision)
    books = []
    for (baseline_id, cohort), members in sorted(grouped.items()):
        found = [outcome_of.get(m["record_id"]) for m in members]
        complete = [o for o in found if o is not None and o["status"] == "COMPLETE"]
        incomplete = [o for o in found if o is not None and o["status"] == "INCOMPLETE"]
        pending = len(found) - len(complete) - len(incomplete)
        status = "INCOMPLETE" if incomplete else ("PENDING" if pending else "COMPLETE")
        known = float(sum(o["pnl_usd"] for o in complete))
        books.append({
            "baseline_id": baseline_id, "cohort": cohort, "label": LABEL, "pnl_basis": PNL_BASIS,
            "status": status, "records": len(members), "complete": len(complete), "incomplete": len(incomplete),
            "pending": pending, "trades": sum(o["trades"] or 0 for o in complete),
            "pnl_usd": known if status == "COMPLETE" else None,
            "known_pnl_usd": known if complete else None,
            "incomplete_reasons": dict(Counter(o["reason"] for o in incomplete)),
        })
    return books


def _day(value: Any) -> dt.date:
    return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value))


def build_shadow_books(rows: Sequence[Mapping[str, Any]],
                       owed: Optional[Mapping[str, Mapping[str, Any]]] = None) -> list[dict]:
    """SP2c spec 7: one book per verdict, so DEPLOY and REJECT forward results sit side by side.

    ``owed`` maps each judgment to its verdict and the closed sessions of its window (``owed_shadow_sessions``).
    A book is COMPLETE, with a final ``pnl_usd``, only when every owed session has a COMPLETE row; a stored
    row alone proves nothing. Without ``owed`` no book can be COMPLETE."""
    by_verdict: dict[str, list[Mapping]] = defaultdict(list)
    for row in rows:
        by_verdict[row["verdict"]].append(row)
    owed_by_verdict: dict[str, dict[str, Mapping]] = defaultdict(dict)
    for judgment_id, judgment in (owed or {}).items():
        owed_by_verdict[judgment["verdict"]][judgment_id] = judgment
    books = []
    for verdict in SHADOW_VERDICTS:
        members = by_verdict.get(verdict, [])
        judgments = owed_by_verdict.get(verdict, {})
        if not members and not judgments:
            continue
        complete = [r for r in members if r["status"] == "COMPLETE"]
        incomplete = [r for r in members if r["status"] == "INCOMPLETE"]
        due = {(judgment_id, day) for judgment_id, judgment in judgments.items() for day in judgment["sessions"]}
        missing = due - {(r["judgment_id"], _day(r["session_date"])) for r in members}
        proven = owed is not None and bool(due) and not missing and all(r["judgment_id"] in judgments for r in members)
        status = "INCOMPLETE" if incomplete else ("COMPLETE" if proven else "PENDING")
        known = float(sum(r["pnl_usd"] for r in complete))
        books.append({
            "verdict": verdict, "label": SHADOW_LABEL, "basis": SHADOW_BASIS,
            "judgments": len({r["judgment_id"] for r in members} | set(judgments)), "sessions": len(members),
            "complete": len(complete), "incomplete": len(incomplete),
            "owed_sessions": None if owed is None else len(due),
            "missing_sessions": None if owed is None else len(missing),
            "status": status,
            "pnl_usd": known if status == "COMPLETE" else None, "known_pnl_usd": known if complete else None,
            "fees_usd": float(sum(r["fees_usd"] for r in complete)) if complete else None,
            "trades": sum(r["trades"] for r in complete) if complete else None,
            "incomplete_reasons": dict(Counter(r["reason"] for r in incomplete))})
    return books


def _effective_costs(rows: Sequence[Mapping[str, Any]]) -> tuple[list[Mapping], int]:
    originals = [r for r in rows if r["corrects_record_id"] is None]
    latest: dict[str, Mapping] = {}
    for row in rows:
        target = row["corrects_record_id"]
        if target is not None and (target not in latest or row["correction_seq"] > latest[target]["correction_seq"]):
            latest[target] = row
    return [latest.get(o["record_id"], o) for o in originals], len(rows) - len(originals)


def summarize_costs(rows: Sequence[Mapping[str, Any]]) -> dict:
    effective, corrections = _effective_costs(rows)
    confirmed = float(sum(r["cost_usd"] for r in effective if r["cost_status"] == "confirmed"))
    estimated = float(sum(r["cost_usd"] for r in effective if r["cost_status"] == "estimated"))
    unknown = sum(1 for r in effective if r["cost_status"] == "unknown")
    if not effective:
        status = "NONE"
    elif unknown:
        status = "INCOMPLETE"
    else:
        status = "ESTIMATED" if estimated or any(r["cost_status"] == "estimated" for r in effective) else "CONFIRMED"
    return {"status": status, "calls": len(effective), "confirmed_usd": confirmed, "estimated_usd": estimated,
            "unknown_calls": unknown, "corrections": corrections,
            "total_usd": None if status in ("NONE", "INCOMPLETE") else confirmed + estimated}

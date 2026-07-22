#!/usr/bin/env python3
"""P4 Task 2 -- deterministic paper evidence JSON report.

Projects a strategy's durable promotion evidence window
(``trader.promotion.evidence_store.EvidenceStore``), evaluates it against
``PaperGate``'s exact simultaneous floors (30 calendar days, 20 completed
sessions, 50 round trips, five instruments) and safety blockers, and emits
ONE deterministic JSON document: canonical (sorted-key, compact) bytes, so
the same window content always produces byte-identical output regardless of
process or platform (``trader.research.canonical.canonical_json_bytes`` --
the same primitive the research evidence chain signs over).

The report never edits evidence and never mutates promotion stage -- it is
read-only reporting. It loads via ``EvidenceStore.rebuild_window`` (a pure
list-and-project rebuild) rather than ``EvidenceStore.project`` specifically
because ``project`` persists the derived window and emits a domain event as
a side effect -- exactly the kind of write a tool billed as "read-only
reporting" must never perform just because someone ran a report.
``build_report`` is the pure function tests exercise directly; ``main`` is a
thin CLI wrapper that loads a window from a real DuckDB file.

Usage:
    python3 scripts/paper_evidence_report.py --db ~/.local/share/mmr/mmr.duckdb \\
        --strategy orb_breakout
    python3 scripts/paper_evidence_report.py --db mmr.duckdb --strategy orb_breakout \\
        --as-of 2026-07-18T14:30:00Z --out report.json
    python3 scripts/paper_evidence_report.py --db mmr.duckdb --strategy orb_breakout \\
        --previous-window-json old_window.json   # adds a correction_impact block
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from trader.data.domain_journal import DomainJournal  # noqa: E402
from trader.data.duckdb_store import DuckDBConnection  # noqa: E402
from trader.data.schema_migrations import SchemaMigrator  # noqa: E402
from trader.promotion.evidence_store import (  # noqa: E402
    EvidenceStore,
    EvidenceWindow,
    apply_evidence_migrations,
)
from trader.promotion.paper_gate import (  # noqa: E402
    PaperGate,
    clean_session_streak,
    correction_impact,
)
from trader.research.canonical import canonical_json_bytes  # noqa: E402


def build_report(
    window: EvidenceWindow,
    *,
    previous_window: Optional[EvidenceWindow] = None,
    gate: Optional[PaperGate] = None,
) -> dict[str, Any]:
    """Pure: assemble the deterministic report payload from an
    already-projected window (and, optionally, the window as it stood
    immediately before the most recent correction, for a
    ``correction_impact`` block). Never touches a database or the clock.
    """
    decision = (gate or PaperGate()).evaluate(window)
    report: dict[str, Any] = {
        "report_kind": "paper_evidence_report",
        "strategy_id": window.strategy_id,
        "as_of": window.as_of.astimezone(dt.timezone.utc).isoformat(),
        "window": window.to_payload(),
        "decision": decision.to_payload(),
        "clean_session_streak": clean_session_streak(window),
    }
    if previous_window is not None:
        report["correction_impact"] = correction_impact(previous_window, window)
    return report


def _load_window(db_path: str, strategy_id: str, as_of: Optional[dt.datetime]) -> EvidenceWindow:
    db = DuckDBConnection.get_instance(db_path)
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    apply_evidence_migrations(migrator)
    store = EvidenceStore(journal=journal, db=db)
    # Read-only rebuild -- NOT store.project(), which persists the derived
    # window and emits a domain event. A report must never write as a side
    # effect of being generated.
    return store.rebuild_window(strategy_id, as_of=as_of)


def _parse_as_of(value: Optional[str]) -> Optional[dt.datetime]:
    if value is None:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path to the trader DuckDB file")
    parser.add_argument("--strategy", required=True, help="strategy_id to report on")
    parser.add_argument("--as-of", default=None, help="ISO-8601 timestamp (default: now)")
    parser.add_argument(
        "--previous-window-json", default=None,
        help="Path to a previously-saved EvidenceWindow.to_payload() JSON file "
             "(e.g. captured just before a correction) -- adds a correction_impact block",
    )
    parser.add_argument("--out", default=None, help="Write JSON to this path instead of stdout")
    args = parser.parse_args(argv)

    as_of = _parse_as_of(args.as_of)
    window = _load_window(args.db, args.strategy, as_of)

    previous_window = None
    if args.previous_window_json:
        payload = json.loads(Path(args.previous_window_json).read_text(encoding="utf-8"))
        previous_window = EvidenceWindow.from_payload(payload)

    report = build_report(window, previous_window=previous_window)
    body = canonical_json_bytes(report)

    if args.out:
        Path(args.out).write_bytes(body + b"\n")
        print(f"wrote {args.out}")
    else:
        sys.stdout.buffer.write(body + b"\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

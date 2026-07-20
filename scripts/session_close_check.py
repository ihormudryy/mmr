#!/usr/bin/env python3
"""Run post-session automation checklist (P4 Task 7)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from trader.operations.session_checklist import SessionChecklist, SessionChecklistContext, SessionChecklistStore


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Post-session automation checklist")
    p.add_argument("--json-file", required=True, help="SessionChecklistContext JSON")
    p.add_argument("--journal", required=True, help="Journal DuckDB path")
    args = p.parse_args(argv)

    from trader.data.duckdb_store import DuckDBConnection
    from trader.data.domain_journal import DomainJournal
    from trader.data.schema_migrations import SchemaMigrator
    from trader.operations.session_checklist import apply_session_checklist_migration

    payload = json.loads(Path(args.json_file).read_text())
    ctx = SessionChecklistContext(**payload)
    db = DuckDBConnection.get_instance(args.journal)
    journal = DomainJournal(db)
    journal.migrate(SchemaMigrator(db))
    apply_session_checklist_migration(SchemaMigrator(db))
    result = SessionChecklist(SessionChecklistStore(journal)).run_post(ctx)
    print(json.dumps({"passed": result.passed, "failed": list(result.failed), "digest": result.result_digest}, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())

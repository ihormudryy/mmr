"""P4 Task 2 -- deterministic paper evidence JSON report (scripts/paper_evidence_report.py)."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
STRATEGY = "orb_breakout"


def _db(tmp_path: Path, name: str = "report.duckdb"):
    db = DuckDBConnection.get_instance(str(tmp_path / name))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    return db, migrator, journal


def _clean_window(sessions: int = 20):
    from trader.promotion.evidence_store import EvidenceWindow

    return EvidenceWindow(
        strategy_id=STRATEGY, as_of=NOW, window_reset_at=None,
        first_event_at=NOW - dt.timedelta(days=30), last_event_at=NOW,
        calendar_days=tuple(
            (dt.date(2026, 5, 1) + dt.timedelta(days=i)).isoformat() for i in range(30)
        ),
        session_ids=tuple(f"s{i}" for i in range(sessions)),
        round_trip_ids=tuple(f"rt-{i}" for i in range(50)),
        instrument_ids=tuple(str(1000 + i) for i in range(5)),
        corrections=(), breaker_trips=(), cost_breaches=(), drawdown_breaches=(),
        stale=False, event_count=100,
        round_trip_records=tuple(
            {"round_trip_id": f"rt-{i}", "instrument_id": str(1000 + (i % 5)), "pnl_after_cost": 10.0}
            for i in range(50)
        ),
    )


# ---------------------------------------------------------------------------
# build_report: pure function
# ---------------------------------------------------------------------------

def test_build_report_contains_window_decision_and_streak():
    from paper_evidence_report import build_report

    report = build_report(_clean_window())
    assert report["report_kind"] == "paper_evidence_report"
    assert report["strategy_id"] == STRATEGY
    assert report["decision"]["passed"] is True
    assert report["window"]["session_ids"] == [f"s{i}" for i in range(20)]
    assert report["clean_session_streak"] == 20
    assert "correction_impact" not in report


def test_build_report_is_deterministic_json():
    from paper_evidence_report import build_report
    from trader.research.canonical import canonical_json_bytes

    report_a = build_report(_clean_window())
    report_b = build_report(_clean_window())
    assert canonical_json_bytes(report_a) == canonical_json_bytes(report_b)


def test_build_report_with_previous_window_adds_correction_impact():
    from paper_evidence_report import build_report

    old = _clean_window(sessions=20)
    new = _clean_window(sessions=15)
    report = build_report(new, previous_window=old)
    assert "correction_impact" in report
    assert len(report["correction_impact"]["removed_sessions"]) == 5


def test_build_report_reports_failing_gate_without_raising():
    from paper_evidence_report import build_report
    from trader.promotion.evidence_store import EvidenceWindow

    window = EvidenceWindow(
        strategy_id=STRATEGY, as_of=NOW, window_reset_at=None,
        first_event_at=NOW, last_event_at=NOW, calendar_days=(),
        session_ids=(), round_trip_ids=(), instrument_ids=(),
        corrections=(), breaker_trips=(), cost_breaches=(), drawdown_breaches=(),
        stale=True, event_count=0,
    )
    report = build_report(window)
    assert report["decision"]["passed"] is False
    assert "stale_evidence" in report["decision"]["blockers"]


# ---------------------------------------------------------------------------
# CLI end-to-end: real EvidenceStore -> project -> report
# ---------------------------------------------------------------------------

def test_cli_main_writes_deterministic_report_from_real_store(tmp_path):
    from paper_evidence_report import main
    from trader.promotion.evidence_store import EvidenceEvent, apply_evidence_migrations

    db_path = tmp_path / "mmr.duckdb"
    db, migrator, journal = _db(tmp_path, name="mmr.duckdb")
    apply_evidence_migrations(migrator)

    from trader.promotion.evidence_store import EvidenceStore
    store = EvidenceStore(journal=journal, db=db, now=lambda: NOW)
    day1 = dt.datetime(2026, 5, 1, 15, 0, tzinfo=UTC)
    store.append(EvidenceEvent(
        source_event_id="sess-1", strategy_id=STRATEGY, event_kind="session",
        payload={"session_id": "2026-05-01"}, source_timestamp=day1,
    ))

    out_path = tmp_path / "report.json"
    exit_code = main([
        "--db", str(db_path), "--strategy", STRATEGY,
        "--as-of", "2026-05-01T15:00:00Z", "--out", str(out_path),
    ])
    assert exit_code == 0
    payload = json.loads(out_path.read_text())
    assert payload["strategy_id"] == STRATEGY
    assert payload["decision"]["passed"] is False  # floors nowhere near met
    assert payload["window"]["session_ids"] == ["2026-05-01"]

    # IMPORTANT: a report is read-only reporting -- generating one must
    # never persist a derived window or emit a domain event as a side
    # effect (previously it called EvidenceStore.project(), which does).
    assert store.load_window(STRATEGY) is None
    row_count = db.execute("SELECT COUNT(*) FROM promotion_evidence_windows", fetch="one")
    assert row_count == (0,)
    kinds = {
        row[0]
        for row in db.execute(
            "SELECT event_type FROM domain_event_journal "
            "WHERE entity_type = 'promotion_evidence_window'",
            fetch="all",
        )
    }
    assert "promotion.evidence_window_rebuilt" not in kinds

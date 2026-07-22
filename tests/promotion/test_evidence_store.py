"""P4 Task 1 — durable, append-only promotion evidence + derived windows.

Contract:
* Raw evidence (``promotion_evidence_events``) is append-only and idempotent
  by ``source_event_id``; a duplicate id is a no-op, never an overwrite.
* Derived windows (``promotion_evidence_windows``) are a PURE rebuild from
  raw evidence -- recomputing twice from the same raw rows always yields the
  same window, independent of append order (out-of-order delivery) or of
  process restarts (crash between append and project still recovers).
* A correction event (scope one of code/config/allowlist/risk/data) resets
  the counted window: sessions/round-trips/instruments/days before the most
  recent correction are excluded from counts, though the full correction
  history is always retained for audit.
* 30 calendar days without new evidence marks the window ``stale``.
* Breaker trips / cost breaches / drawdown breaches surface as explicit,
  non-empty lists on the window -- never silently averaged away.
* Migrations 40-41: promotion_evidence_events, promotion_evidence_windows.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from trader.data.domain_journal import DomainJournal
from trader.data.duckdb_store import DuckDBConnection
from trader.data.schema_migrations import SchemaMigrator

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
STRATEGY = "orb_breakout"


def _db(tmp_path: Path, name: str = "evidence.duckdb"):
    db = DuckDBConnection.get_instance(str(tmp_path / name))
    migrator = SchemaMigrator(db)
    journal = DomainJournal(db)
    journal.migrate(migrator)
    return db, migrator, journal


def _store(tmp_path: Path, **overrides):
    from trader.promotion.evidence_store import EvidenceStore, apply_evidence_migrations

    db, migrator, journal = _db(tmp_path)
    apply_evidence_migrations(migrator)
    store = EvidenceStore(
        journal=journal, db=db, now=overrides.pop("now", lambda: NOW), **overrides,
    )
    return store, journal, db, migrator


def _event(
    kind: str,
    *,
    strategy_id: str = STRATEGY,
    source_event_id: str,
    ts: dt.datetime = NOW,
    **payload,
):
    from trader.promotion.evidence_store import EvidenceEvent

    return EvidenceEvent(
        source_event_id=source_event_id,
        strategy_id=strategy_id,
        event_kind=kind,
        payload=payload,
        source_timestamp=ts,
    )


# ---------------------------------------------------------------------------
# Migrations 40-41
# ---------------------------------------------------------------------------

def test_migrations_40_41_create_append_only_and_derived_tables(tmp_path):
    from trader.promotion.evidence_store import (
        EVIDENCE_MIGRATION_VERSIONS,
        apply_evidence_migrations,
    )

    db, migrator, _ = _db(tmp_path, "mig.duckdb")
    assert apply_evidence_migrations(migrator) is True
    assert EVIDENCE_MIGRATION_VERSIONS == (40, 41)
    assert apply_evidence_migrations(migrator) is False

    for table in ("promotion_evidence_events", "promotion_evidence_windows"):
        cols = {row[1] for row in db.execute(f"PRAGMA table_info('{table}')", fetch="all")}
        assert cols, f"missing table {table}"
    assert "strategy_id" in {
        row[1] for row in db.execute("PRAGMA table_info('promotion_evidence_events')", fetch="all")
    }


# ---------------------------------------------------------------------------
# Idempotent ingestion by source event id
# ---------------------------------------------------------------------------

def test_append_is_idempotent_by_source_event_id(tmp_path):
    store, *_ = _store(tmp_path)
    ev = _event("session", source_event_id="sess-1", session_id="s1")
    assert store.append(ev) is True
    assert store.append(ev) is False

    rows = store.db.execute(
        "SELECT COUNT(*), COUNT(DISTINCT evidence_key) FROM promotion_evidence_events",
        fetch="one",
    )
    assert rows == (1, 1)


def test_append_duplicate_id_never_overwrites_payload(tmp_path):
    store, *_ = _store(tmp_path)
    store.append(_event("session", source_event_id="sess-1", session_id="s1", note="first"))
    # Same id, different payload -- must be rejected, not merged.
    store.append(_event("session", source_event_id="sess-1", session_id="s1", note="second"))

    import json
    row = store.db.execute(
        "SELECT payload FROM promotion_evidence_events WHERE evidence_key = ?",
        ["sess-1"], fetch="one",
    )
    assert json.loads(row[0])["note"] == "first"


# ---------------------------------------------------------------------------
# Pure projection: sessions, round trips, instruments, calendar days
# ---------------------------------------------------------------------------

def test_project_counts_sessions_round_trips_and_instruments(tmp_path):
    store, *_ = _store(tmp_path)
    day1 = dt.datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    day2 = dt.datetime(2026, 6, 2, 15, 0, tzinfo=UTC)

    store.append(_event("session", source_event_id="sess-1", ts=day1, session_id="2026-06-01"))
    store.append(_event("session", source_event_id="sess-2", ts=day2, session_id="2026-06-02"))
    store.append(_event(
        "round_trip", source_event_id="rt-1", ts=day1,
        round_trip_id="cmd-1", conid=265598,
    ))
    store.append(_event(
        "round_trip", source_event_id="rt-2", ts=day2,
        round_trip_id="cmd-2", conid=272093,
    ))

    window = store.project(STRATEGY, as_of=day2)
    assert window.session_count == 2
    assert window.round_trip_count == 2
    assert window.instrument_count == 2
    assert window.calendar_day_count == 2
    assert window.stale is False


def test_project_is_pure_and_deterministic_regardless_of_append_order(tmp_path):
    from trader.promotion.evidence_store import project_evidence_window

    store_a, *_ = _store(tmp_path / "a")
    store_b, *_ = _store(tmp_path / "b")

    day1 = dt.datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    day2 = dt.datetime(2026, 6, 2, 15, 0, tzinfo=UTC)
    events = [
        _event("session", source_event_id="sess-1", ts=day1, session_id="2026-06-01"),
        _event("session", source_event_id="sess-2", ts=day2, session_id="2026-06-02"),
        _event("round_trip", source_event_id="rt-1", ts=day1, round_trip_id="cmd-1", conid=265598),
    ]
    for ev in events:
        store_a.append(ev)
    for ev in reversed(events):
        store_b.append(ev)

    window_a = store_a.project(STRATEGY, as_of=day2)
    window_b = store_b.project(STRATEGY, as_of=day2)
    assert window_a.to_payload() == window_b.to_payload()

    # Rebuilding from the same raw rows (pure function) never changes result.
    raw = store_a.list_events(STRATEGY)
    rebuilt_once = project_evidence_window(STRATEGY, raw, day2)
    rebuilt_twice = project_evidence_window(STRATEGY, raw, day2)
    assert rebuilt_once == rebuilt_twice


def test_out_of_order_events_do_not_change_projection(tmp_path):
    store, *_ = _store(tmp_path)
    day1 = dt.datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    day2 = dt.datetime(2026, 6, 5, 15, 0, tzinfo=UTC)

    # Append the later event first, earlier event second.
    store.append(_event("session", source_event_id="sess-2", ts=day2, session_id="2026-06-05"))
    store.append(_event("session", source_event_id="sess-1", ts=day1, session_id="2026-06-01"))

    window = store.project(STRATEGY, as_of=day2)
    assert window.calendar_days == ("2026-06-01", "2026-06-05")
    assert window.first_event_at == day1
    assert window.last_event_at == day2


# ---------------------------------------------------------------------------
# Corrections reset the counted window; full history retained
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scope", ["code", "config", "allowlist", "risk", "data"])
def test_correction_resets_window_but_keeps_full_history(tmp_path, scope):
    store, *_ = _store(tmp_path)
    day1 = dt.datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    correction_ts = dt.datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    day3 = dt.datetime(2026, 6, 15, 15, 0, tzinfo=UTC)

    store.append(_event("session", source_event_id="sess-1", ts=day1, session_id="2026-06-01"))
    store.append(_event(
        "correction", source_event_id="corr-1", ts=correction_ts,
        scope=scope, description="deployed new allowlist",
    ))
    store.append(_event("session", source_event_id="sess-2", ts=day3, session_id="2026-06-15"))

    window = store.project(STRATEGY, as_of=day3)
    # Pre-correction session excluded from the counted window...
    assert window.session_count == 1
    assert window.calendar_days == ("2026-06-15",)
    assert window.window_reset_at == correction_ts
    # ...but the correction itself is always visible in full history.
    assert len(window.corrections) == 1
    assert window.corrections[0]["scope"] == scope


def test_multiple_corrections_use_the_most_recent_boundary(tmp_path):
    store, *_ = _store(tmp_path)
    t0 = dt.datetime(2026, 6, 1, tzinfo=UTC)
    t1 = dt.datetime(2026, 6, 5, tzinfo=UTC)
    t2 = dt.datetime(2026, 6, 10, tzinfo=UTC)
    t3 = dt.datetime(2026, 6, 15, tzinfo=UTC)

    store.append(_event("session", source_event_id="sess-0", ts=t0, session_id="s0"))
    store.append(_event("correction", source_event_id="corr-1", ts=t1, scope="config"))
    store.append(_event("session", source_event_id="sess-1", ts=t2 - dt.timedelta(hours=1), session_id="s1"))
    store.append(_event("correction", source_event_id="corr-2", ts=t2, scope="risk"))
    store.append(_event("session", source_event_id="sess-2", ts=t3, session_id="s2"))

    window = store.project(STRATEGY, as_of=t3)
    assert window.session_count == 1  # only sess-2, at/after the LATEST correction
    assert len(window.corrections) == 2
    assert window.window_reset_at == t2


# ---------------------------------------------------------------------------
# Elapsed calendar-day span vs. distinct session-date count
# ---------------------------------------------------------------------------

def test_elapsed_calendar_days_is_span_not_distinct_date_count(tmp_path):
    """A normal ~30-calendar-day paper soak with only ~20 trading sessions
    (weekends/holidays skipped) has FEWER distinct session dates than its
    elapsed span -- ``elapsed_calendar_days`` must reflect the span."""
    store, *_ = _store(tmp_path)
    base = dt.datetime(2026, 5, 1, 15, 0, tzinfo=UTC)
    offsets = (0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 18, 19, 20, 21, 29)
    for i, offset in enumerate(offsets):
        store.append(_event(
            "session", source_event_id=f"sess-{i}", ts=base + dt.timedelta(days=offset),
            session_id=f"s{i}",
        ))

    window = store.project(STRATEGY, as_of=base + dt.timedelta(days=29))
    assert window.calendar_day_count == 20  # 20 distinct dates
    assert window.elapsed_calendar_days == 30  # but a 30-day elapsed span


def test_elapsed_calendar_days_matches_count_for_consecutive_days(tmp_path):
    """For consecutive daily sessions, span and distinct-date count agree --
    confirms the fix doesn't change behavior for the simple case."""
    store, *_ = _store(tmp_path)
    day1 = dt.datetime(2026, 6, 1, 15, 0, tzinfo=UTC)
    day2 = dt.datetime(2026, 6, 2, 15, 0, tzinfo=UTC)
    store.append(_event("session", source_event_id="sess-1", ts=day1, session_id="s1"))
    store.append(_event("session", source_event_id="sess-2", ts=day2, session_id="s2"))

    window = store.project(STRATEGY, as_of=day2)
    assert window.calendar_day_count == 2
    assert window.elapsed_calendar_days == 2


def test_elapsed_calendar_days_is_zero_for_empty_window(tmp_path):
    store, *_ = _store(tmp_path)
    window = store.project("never_seen_strategy", as_of=NOW)
    assert window.elapsed_calendar_days == 0


# ---------------------------------------------------------------------------
# rebuild_window: read-only rebuild (no persistence, no domain event)
# ---------------------------------------------------------------------------

def test_rebuild_window_matches_project_content_without_persisting(tmp_path):
    store, journal, db, _ = _store(tmp_path)
    store.append(_event("session", source_event_id="sess-1", ts=NOW, session_id="s1"))
    store.append(_event("round_trip", source_event_id="rt-1", ts=NOW, round_trip_id="cmd-1", conid=1))

    rebuilt = store.rebuild_window(STRATEGY, as_of=NOW)
    assert rebuilt.session_count == 1
    assert rebuilt.round_trip_count == 1

    # Never wrote to promotion_evidence_windows...
    assert store.load_window(STRATEGY) is None
    row_count = db.execute(
        "SELECT COUNT(*) FROM promotion_evidence_windows", fetch="one",
    )
    assert row_count == (0,)

    # ...and never emitted a domain event either.
    kinds = {
        row[0]
        for row in db.execute(
            "SELECT event_type FROM domain_event_journal "
            "WHERE entity_type = 'promotion_evidence_window'",
            fetch="all",
        )
    }
    assert "promotion.evidence_window_rebuilt" not in kinds

    # Content matches what project() would have produced (and persisted).
    projected = store.project(STRATEGY, as_of=NOW)
    assert rebuilt.to_payload() == projected.to_payload()


def test_rebuild_window_is_pure_and_repeatable(tmp_path):
    store, *_ = _store(tmp_path)
    store.append(_event("session", source_event_id="sess-1", ts=NOW, session_id="s1"))

    a = store.rebuild_window(STRATEGY, as_of=NOW)
    b = store.rebuild_window(STRATEGY, as_of=NOW)
    assert a.to_payload() == b.to_payload()


# ---------------------------------------------------------------------------
# 30-day evidence inactivity
# ---------------------------------------------------------------------------

def test_window_is_stale_after_30_days_of_inactivity(tmp_path):
    store, *_ = _store(tmp_path)
    last_activity = dt.datetime(2026, 5, 1, tzinfo=UTC)
    store.append(_event("session", source_event_id="sess-1", ts=last_activity, session_id="s1"))

    fresh_as_of = last_activity + dt.timedelta(days=29)
    stale_as_of = last_activity + dt.timedelta(days=31)

    assert store.project(STRATEGY, as_of=fresh_as_of).stale is False
    assert store.project(STRATEGY, as_of=stale_as_of).stale is True


def test_empty_window_is_stale(tmp_path):
    store, *_ = _store(tmp_path)
    window = store.project("never_seen_strategy", as_of=NOW)
    assert window.stale is True
    assert window.event_count == 0


# ---------------------------------------------------------------------------
# Breaker trip / cost breach / drawdown breach surfaced explicitly
# ---------------------------------------------------------------------------

def test_breaker_trip_cost_breach_and_drawdown_breach_are_explicit(tmp_path):
    store, *_ = _store(tmp_path)
    store.append(_event("breaker_trip", source_event_id="brk-1", incident_id="inc-1", reason_code="PROTECTIVE_ORDER_FAILURE"))
    store.append(_event("cost_breach", source_event_id="cost-1", metric="stressed_cost_bps", value=42))
    store.append(_event("drawdown_breach", source_event_id="dd-1", drawdown_pct=3.5))

    window = store.project(STRATEGY, as_of=NOW)
    assert len(window.breaker_trips) == 1
    assert window.breaker_trips[0]["incident_id"] == "inc-1"
    assert len(window.cost_breaches) == 1
    assert len(window.drawdown_breaches) == 1
    assert window.is_clean is False


def test_clean_window_has_no_safety_evidence_and_is_not_stale(tmp_path):
    store, *_ = _store(tmp_path)
    store.append(_event("session", source_event_id="sess-1", ts=NOW, session_id="s1"))
    window = store.project(STRATEGY, as_of=NOW)
    assert window.is_clean is True


# ---------------------------------------------------------------------------
# Restart / crash recovery: derived rows may be wiped and rebuilt
# ---------------------------------------------------------------------------

def test_crash_between_append_and_project_recovers_deterministically(tmp_path):
    from trader.promotion.evidence_store import EvidenceStore

    store, journal, db, migrator = _store(tmp_path)
    store.append(_event("session", source_event_id="sess-1", ts=NOW, session_id="s1"))
    store.append(_event("round_trip", source_event_id="rt-1", ts=NOW, round_trip_id="cmd-1", conid=1))
    store.project(STRATEGY, as_of=NOW)

    # Simulate crash: wipe only the derived table. Raw evidence survives.
    db.execute("DELETE FROM promotion_evidence_windows")
    assert store.load_window(STRATEGY) is None

    # Fresh store instance (process restart), same db/journal.
    recovered = EvidenceStore(journal=journal, db=db, now=lambda: NOW)
    a = recovered.project(STRATEGY, as_of=NOW)
    b = recovered.project(STRATEGY, as_of=NOW)
    assert a.to_payload() == b.to_payload()
    assert a.session_count == 1
    assert a.round_trip_count == 1


def test_load_window_reads_persisted_projection(tmp_path):
    store, *_ = _store(tmp_path)
    store.append(_event("session", source_event_id="sess-1", ts=NOW, session_id="s1"))
    projected = store.project(STRATEGY, as_of=NOW)
    loaded = store.load_window(STRATEGY)
    assert loaded is not None
    assert loaded.to_payload() == projected.to_payload()


# ---------------------------------------------------------------------------
# Session boundaries: distinct session ids, not raw event counts
# ---------------------------------------------------------------------------

def test_multiple_events_same_session_id_count_once(tmp_path):
    store, *_ = _store(tmp_path)
    store.append(_event("session", source_event_id="sess-1a", ts=NOW, session_id="s1"))
    store.append(_event("session", source_event_id="sess-1b", ts=NOW + dt.timedelta(hours=1), session_id="s1"))

    window = store.project(STRATEGY, as_of=NOW)
    assert window.session_count == 1


# ---------------------------------------------------------------------------
# Domain events (observability only; raw table remains authoritative)
# ---------------------------------------------------------------------------

def test_append_and_project_emit_domain_events(tmp_path):
    store, journal, db, _ = _store(tmp_path)
    store.append(_event("session", source_event_id="sess-1", ts=NOW, session_id="s1"))
    store.project(STRATEGY, as_of=NOW)

    kinds = {
        row[0]
        for row in db.execute(
            "SELECT event_type FROM domain_event_journal "
            "WHERE entity_type IN ('promotion_evidence', 'promotion_evidence_window')",
            fetch="all",
        )
    }
    assert "promotion.evidence_appended" in kinds
    assert "promotion.evidence_window_rebuilt" in kinds


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_correction_event_requires_known_scope(tmp_path):
    from trader.promotion.evidence_store import EvidenceEvent

    with pytest.raises(ValueError):
        EvidenceEvent(
            source_event_id="corr-bad",
            strategy_id=STRATEGY,
            event_kind="correction",
            payload={"scope": "not_a_real_scope"},
            source_timestamp=NOW,
        )


def test_append_rejects_unknown_event_kind(tmp_path):
    """IMPORTANT fail-closed contract: a typo'd/unsupported event_kind must
    never be silently accepted then dropped from projection -- that would
    make safety evidence (e.g. a mistyped "braker_trip") vanish without any
    error. Reject at construction, which precedes every append() call."""
    from trader.promotion.evidence_store import EvidenceEvent

    with pytest.raises(ValueError):
        EvidenceEvent(
            source_event_id="typo-1",
            strategy_id=STRATEGY,
            event_kind="braker_trip",
            payload={"incident_id": "inc-1"},
            source_timestamp=NOW,
        )


def test_append_rejects_unknown_event_kind_end_to_end(tmp_path):
    """End-to-end: an unsupported kind can never make it into the raw table
    or the derived projection via the real append() path."""
    from trader.promotion.evidence_store import EvidenceEvent

    store, *_ = _store(tmp_path)
    with pytest.raises(ValueError):
        store.append(EvidenceEvent(
            source_event_id="typo-2",
            strategy_id=STRATEGY,
            event_kind="cost_breech",  # typo of cost_breach
            payload={"metric": "stressed_cost_bps"},
            source_timestamp=NOW,
        ))

    rows = store.db.execute("SELECT COUNT(*) FROM promotion_evidence_events", fetch="one")
    assert rows == (0,)
    window = store.project(STRATEGY, as_of=NOW)
    assert window.event_count == 0


def test_naive_timestamp_is_rejected(tmp_path):
    from trader.promotion.evidence_store import EvidenceEvent

    with pytest.raises(ValueError):
        EvidenceEvent(
            source_event_id="sess-naive",
            strategy_id=STRATEGY,
            event_kind="session",
            payload={"session_id": "s1"},
            source_timestamp=dt.datetime(2026, 6, 1),
        )

import datetime as dt

import pytest

from trader.domain.events import DomainEvent, SnapshotWithCursor
from web.command_center.state import (
    REPLAY_RING_MAX_EVENTS,
    TERMINAL_CAP,
    TERMINAL_TTL_SECONDS,
    DashboardState,
)

UTC = dt.timezone.utc


def _event(**overrides) -> DomainEvent:
    base = dict(
        event_id="evt-1",
        source_cursor=1,
        entity_revision=1,
        event_type="position.updated",
        entity_type="position",
        entity_id="DU123:265598",
        operation="upsert",
        account_id="DU123",
        source="trader_service",
        source_timestamp=dt.datetime(2026, 7, 15, 12, 0, tzinfo=UTC),
        correlation_id=None,
        payload={"quantity": 100, "symbol": "AAPL", "currency": "USD"},
    )
    base.update(overrides)
    return DomainEvent(**base)


@pytest.fixture
def state():
    fake = {"wall": 1_752_580_800.0, "mono": 1000.0}
    dashboard_state = DashboardState(
        clock=lambda: fake["wall"], monotonic=lambda: fake["mono"]
    )
    dashboard_state.install_baseline(
        SnapshotWithCursor(source_cursor=0, broker_generation=1, entities={}),
        stream_id="stream-a",
    )
    dashboard_state._test_clock = fake
    return dashboard_state


class TestReducer:
    def test_upsert_replaces_whole_entity_and_assigns_sequence(self, state):
        env1 = state.apply(_event(payload={"quantity": 100, "stale_field": True}))
        env2 = state.apply(
            _event(
                event_id="evt-2",
                source_cursor=2,
                entity_revision=2,
                payload={"quantity": 50},
            )
        )
        assert (env1["sequence"], env2["sequence"]) == (1, 2)
        row = state.snapshot_view()["positions"][0]
        assert row["quantity"] == 50
        assert "stale_field" not in row

    def test_revision_regression_is_idempotently_ignored(self, state):
        state.apply(_event(entity_revision=5, source_cursor=5))
        assert (
            state.apply(_event(event_id="evt-old", entity_revision=4, source_cursor=6))
            is None
        )
        assert state.sequence == 1

    def test_tombstone_removes_entity(self, state):
        state.apply(_event())
        env = state.apply(
            _event(
                event_id="evt-del",
                source_cursor=2,
                entity_revision=2,
                operation="delete",
                payload=None,
            )
        )
        assert env["operation"] == "delete"
        assert state.snapshot_view()["positions"] == []

    def test_envelope_carries_full_contract(self, state):
        env = state.apply(_event(correlation_id="cmd-9"))
        for key in (
            "schema_version",
            "stream_id",
            "sequence",
            "event_id",
            "source_cursor",
            "entity_revision",
            "event_type",
            "entity_type",
            "entity_id",
            "operation",
            "account_id",
            "source",
            "source_timestamp",
            "received_timestamp",
            "correlation_id",
            "payload",
        ):
            assert key in env
        assert env["stream_id"] == "stream-a"

    def test_event_identity_and_revision_override_payload_values(self, state):
        state.apply(
            _event(
                entity_id="DU123:authoritative",
                entity_revision=7,
                payload={"entity_id": "forged", "entity_revision": 999, "quantity": 50},
            )
        )
        row = state.snapshot_view()["positions"][0]
        assert row["entity_id"] == "DU123:authoritative"
        assert row["entity_revision"] == 7


class TestLifecycleCollections:
    def test_terminal_proposal_moves_out_of_active(self, state):
        state.apply(
            _event(
                entity_type="proposal",
                entity_id="42",
                event_type="proposal.updated",
                payload={"status": "PENDING", "symbol": "AMD"},
            )
        )
        assert len(state.snapshot_view()["proposals"]["active"]) == 1
        state.apply(
            _event(
                event_id="evt-2",
                source_cursor=2,
                entity_revision=2,
                entity_type="proposal",
                entity_id="42",
                event_type="proposal.updated",
                payload={"status": "REJECTED", "symbol": "AMD"},
            )
        )
        view = state.snapshot_view()
        assert view["proposals"]["active"] == []
        assert view["proposals"]["terminal"][0]["status"] == "REJECTED"

    def test_order_update_moves_record_out_of_terminal_collection(self, state):
        state.apply(
            _event(
                entity_type="order",
                entity_id="order-1",
                event_type="order.updated",
                payload={"status": "FILLED"},
            )
        )
        state.apply(
            _event(
                event_id="evt-2",
                source_cursor=2,
                entity_revision=2,
                entity_type="order",
                entity_id="order-1",
                event_type="order.updated",
                payload={"status": "SUBMITTED"},
            )
        )
        view = state.snapshot_view()["orders"]
        assert view["terminal"] == []
        assert view["active"] == [{"entity_id": "order-1", "entity_revision": 2, "status": "SUBMITTED"}]

    def test_terminal_collections_cap_at_500(self, state):
        for i in range(TERMINAL_CAP + 40):
            state.apply(
                _event(
                    event_id=f"evt-{i}",
                    source_cursor=i + 1,
                    entity_type="fill",
                    entity_id=f"DU123:exec-{i}",
                    event_type="fill.received",
                    payload={"exec_id": f"exec-{i}", "status": "FILLED"},
                )
            )
        assert len(state.snapshot_view()["fills"]) == TERMINAL_CAP

    def test_terminal_ttl_is_24_hours(self, state):
        state.apply(
            _event(
                entity_type="fill",
                entity_id="DU123:exec-1",
                event_type="fill.received",
                payload={"exec_id": "exec-1"},
            )
        )
        state._test_clock["mono"] += TERMINAL_TTL_SECONDS + 61
        state.maybe_cleanup()
        assert state.snapshot_view()["fills"] == []


class TestReplayRing:
    def test_replay_after_returns_events_strictly_after_sequence(self, state):
        for i in range(5):
            state.apply(
                _event(
                    event_id=f"evt-{i}", source_cursor=i + 1, entity_revision=i + 1
                )
            )
        replay = state.replay_after("stream-a", 3)
        assert [env["sequence"] for env in replay] == [4, 5]

    def test_stream_mismatch_requires_resync(self, state):
        state.apply(_event())
        assert state.replay_after("stream-STALE", 0) is None

    def test_aged_out_cursor_requires_resync(self, state):
        for i in range(REPLAY_RING_MAX_EVENTS + 10):
            state.apply(
                _event(
                    event_id=f"evt-{i}", source_cursor=i + 1, entity_revision=i + 1
                )
            )
        assert state.replay_after("stream-a", 0) is None
        assert len(state.replay_after("stream-a", 15)) == REPLAY_RING_MAX_EVENTS - 5

    def test_quotes_never_enter_ring_or_sequence(self, state):
        state.apply_quotes({"265598": {"instrument_id": "265598", "last": 199.5}})
        assert state.sequence == 0
        assert state.replay_after("stream-a", 0) == []
        assert state.snapshot_view()["quotes"]["265598"]["last"] == 199.5


class TestBaseline:
    def test_baseline_installs_rows_and_rotates_stream(self, state):
        snapshot = SnapshotWithCursor(
            source_cursor=88,
            broker_generation=2,
            entities={
                "position": [
                    {
                        "entity_id": "DU123:4815747",
                        "entity_revision": 7,
                        "quantity": 10,
                    }
                ]
            },
        )
        state.apply(_event())
        state.install_baseline(snapshot, stream_id="stream-b")
        assert state.stream_id == "stream-b"
        assert state.sequence == 0
        assert state.replay_after("stream-a", 0) is None
        assert state.snapshot_view()["positions"][0]["quantity"] == 10
        assert state.apply(_event(entity_id="DU123:4815747", entity_revision=6)) is None

    def test_baseline_row_without_identity_fails_loudly(self, state):
        bad = SnapshotWithCursor(
            source_cursor=1, broker_generation=1, entities={"position": [{"quantity": 10}]}
        )
        with pytest.raises(ValueError, match="entity_id"):
            state.install_baseline(bad, stream_id="stream-c")

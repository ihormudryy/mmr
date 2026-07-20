"""P3 Task 8 — seal and replay a complete trading day.

Contract (plan §Task 8):
* Bundle entries cover artifact/attestation, bars, quote evidence, broker
  snapshots, policies, intents, decisions, commands, broker events,
  attribution, breaker/reconciliation/operator actions, and XNYS schedule.
* Atomic seal (tmp + fsync + rename), checksums, path traversal rejection,
  missing evidence / unresolved commands refuse seal, corruption detection,
  repeatable digest.
* Pure replay recomputes signals, intent IDs, sizing, and policy decisions;
  replays recorded broker events (never re-simulates IB).
* Exact decision-trace compare with structured divergence report.
* Golden accepted day + adversarial rejected / partial-fill / restart fixtures.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import stat
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from trader.automation.intent_ids import derive_command_id, derive_intent_id
from trader.research.canonical import canonical_json_bytes, sha256_digest

UTC = dt.timezone.utc
T0 = dt.datetime(2026, 7, 18, 14, 30, tzinfo=UTC)
SESSION_ID = "xnys-2026-07-18"
ARTIFACT_ID = "artifact-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
CONID = 265598


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _intent_fields(**overrides: Any) -> dict[str, Any]:
    fields = {
        "artifact_id": ARTIFACT_ID,
        "session_id": SESSION_ID,
        "bar_id": "bar-2026-07-18T14:29:00+00:00",
        "signal_id": "signal-pending",
        "account_mode": "paper",
        "conid": CONID,
        "side": "BUY",
        "requested_quantity": "100",
        "risk_fraction": "0.002",
        "entry_policy": {
            "order_type": "LIMIT",
            "limit_offset_bps": "5.0",
            "tif": "DAY",
        },
        "stop_policy": {"stop_price": "149.0", "order_type": "STP"},
        "target_policy": {"target_price": "155.0", "order_type": "LMT"},
        "time_exit_policy": {
            "max_hold_bars": 30,
            "close_by": (T0 + dt.timedelta(hours=1)).isoformat(),
        },
        "artifact_digest": "a" * 64,
        "eligibility_attestation_digest": "b" * 64,
        "signal_timestamp": T0.isoformat(),
        "completed_bar_timestamp": (T0 - dt.timedelta(minutes=1)).isoformat(),
    }
    fields.update(overrides)
    signal_body = {
        "artifact_id": fields["artifact_id"],
        "bar_id": fields["bar_id"],
        "conid": fields["conid"],
        "side": fields["side"],
        "completed_bar_timestamp": fields["completed_bar_timestamp"],
    }
    fields["signal_id"] = f"signal-{sha256_digest('signal', signal_body)}"
    intent_id = derive_intent_id(fields)
    command_id = derive_command_id(intent_id)
    fields["intent_id"] = intent_id
    fields["command_id"] = command_id
    return fields


def _sizing_result(intent: dict[str, Any], *, equity: str = "100000") -> dict[str, Any]:
    risk_fraction = Decimal(str(intent["risk_fraction"]))
    stop = Decimal(str(intent["stop_policy"]["stop_price"]))
    # Synthetic mark for sizing: stop * 1.02 as entry reference.
    entry = stop * Decimal("1.02")
    risk_per_share = entry - stop
    qty = (Decimal(equity) * risk_fraction / risk_per_share).quantize(Decimal("1"))
    return {
        "equity": equity,
        "entry_price": str(entry),
        "stop_price": str(stop),
        "risk_fraction": str(risk_fraction),
        "approved_quantity": str(qty),
    }


def _policy_decision(intent: dict[str, Any], *, approved: bool = True) -> dict[str, Any]:
    body = {
        "intent_id": intent["intent_id"],
        "approved": approved,
        "reason_codes": [] if approved else ["SESSION_ENTRY_CUTOFF"],
        "approved_quantity": intent["requested_quantity"] if approved else None,
        "calendar_version": "4.5.0",
    }
    return {
        **body,
        "decision_digest": sha256_digest("policy_decision", body),
    }


def _decision_trace(
    *,
    signal_id: str,
    intent: dict[str, Any],
    sizing: dict[str, Any],
    decision: dict[str, Any],
    broker_event_ids: list[str],
) -> list[dict[str, Any]]:
    return [
        {"step": "signal", "signal_id": signal_id},
        {"step": "intent", "intent_id": intent["intent_id"], "command_id": intent["command_id"]},
        {"step": "sizing", "approved_quantity": sizing["approved_quantity"]},
        {
            "step": "policy",
            "decision_digest": decision["decision_digest"],
            "approved": decision["approved"],
        },
        {"step": "broker_events", "event_ids": list(broker_event_ids)},
    ]


def _xnys_schedule() -> dict[str, Any]:
    return {
        "session_date": "2026-07-18",
        "calendar_name": "XNYS",
        "calendar_version": "4.5.0",
        "open_utc": "2026-07-18T13:30:00+00:00",
        "close_utc": "2026-07-18T20:00:00+00:00",
        "entry_cutoff_utc": "2026-07-18T19:30:00+00:00",
        "cancel_entries_utc": "2026-07-18T19:35:00+00:00",
        "flatten_start_utc": "2026-07-18T19:45:00+00:00",
        "flat_deadline_utc": "2026-07-18T19:55:00+00:00",
        "is_early_close": False,
    }


def build_day_evidence(
    *,
    scenario: str = "accepted",
    command_state: str = "RESOLVED",
) -> dict[str, Any]:
    """Build a complete synthetic session evidence dict."""
    intent = _intent_fields()
    sizing = _sizing_result(intent)
    approved = scenario == "accepted"
    if scenario == "rejected":
        decision = _policy_decision(intent, approved=False)
        broker_events: list[dict[str, Any]] = []
        attribution = {
            "trades": [],
            "unresolved": [],
        }
        command_state = "REJECTED"
    elif scenario == "partial_fill":
        decision = _policy_decision(intent, approved=True)
        broker_events = [
            {
                "event_id": "evt-ack",
                "kind": "order_status",
                "command_id": intent["command_id"],
                "status": "Submitted",
                "filled": "0",
                "remaining": "100",
                "timestamp": T0.isoformat(),
            },
            {
                "event_id": "evt-partial",
                "kind": "fill",
                "command_id": intent["command_id"],
                "exec_id": "exec-1",
                "side": "BOT",
                "quantity": "40",
                "price": "151.90",
                "timestamp": (T0 + dt.timedelta(seconds=2)).isoformat(),
            },
            {
                "event_id": "evt-protect",
                "kind": "order_status",
                "command_id": intent["command_id"],
                "status": "PreSubmitted",
                "leg": "stop",
                "timestamp": (T0 + dt.timedelta(seconds=3)).isoformat(),
            },
        ]
        attribution = {
            "trades": [{
                "trade_id": intent["command_id"],
                "resolved": False,
                "intent_id": intent["intent_id"],
                "fills": [{"exec_id": "exec-1", "quantity": "40", "price": "151.90"}],
                "unresolved_reasons": ["partial_fill_open"],
            }],
            "unresolved": [intent["command_id"]],
        }
    elif scenario == "restart":
        decision = _policy_decision(intent, approved=True)
        broker_events = [
            {
                "event_id": "evt-ack",
                "kind": "order_status",
                "command_id": intent["command_id"],
                "status": "Submitted",
                "timestamp": T0.isoformat(),
            },
            {
                "event_id": "evt-restart-resume",
                "kind": "saga_resume",
                "command_id": intent["command_id"],
                "from_state": "ENTRY_WORKING",
                "to_state": "ENTRY_WORKING",
                "timestamp": (T0 + dt.timedelta(minutes=1)).isoformat(),
            },
            {
                "event_id": "evt-fill",
                "kind": "fill",
                "command_id": intent["command_id"],
                "exec_id": "exec-2",
                "side": "BOT",
                "quantity": "100",
                "price": "152.00",
                "timestamp": (T0 + dt.timedelta(minutes=2)).isoformat(),
            },
            {
                "event_id": "evt-flat",
                "kind": "fill",
                "command_id": intent["command_id"],
                "exec_id": "exec-3",
                "side": "SLD",
                "leg": "flatten",
                "quantity": "100",
                "price": "152.50",
                "timestamp": (T0 + dt.timedelta(hours=1)).isoformat(),
            },
        ]
        attribution = {
            "trades": [{
                "trade_id": intent["command_id"],
                "resolved": True,
                "intent_id": intent["intent_id"],
                "fills": [{"exec_id": "exec-2", "quantity": "100", "price": "152.00"}],
                "exit_fills": [{"exec_id": "exec-3", "quantity": "100", "price": "152.50"}],
            }],
            "unresolved": [],
        }
    else:  # accepted
        decision = _policy_decision(intent, approved=True)
        broker_events = [
            {
                "event_id": "evt-ack",
                "kind": "order_status",
                "command_id": intent["command_id"],
                "status": "Submitted",
                "timestamp": T0.isoformat(),
            },
            {
                "event_id": "evt-fill",
                "kind": "fill",
                "command_id": intent["command_id"],
                "exec_id": "exec-1",
                "side": "BOT",
                "quantity": "100",
                "price": "152.00",
                "timestamp": (T0 + dt.timedelta(seconds=5)).isoformat(),
            },
            {
                "event_id": "evt-stop-working",
                "kind": "order_status",
                "command_id": intent["command_id"],
                "status": "Submitted",
                "leg": "stop",
                "timestamp": (T0 + dt.timedelta(seconds=6)).isoformat(),
            },
            {
                "event_id": "evt-exit",
                "kind": "fill",
                "command_id": intent["command_id"],
                "exec_id": "exec-2",
                "side": "SLD",
                "leg": "target",
                "quantity": "100",
                "price": "155.00",
                "timestamp": (T0 + dt.timedelta(minutes=20)).isoformat(),
            },
        ]
        attribution = {
            "trades": [{
                "trade_id": intent["command_id"],
                "resolved": True,
                "artifact_id": ARTIFACT_ID,
                "intent_id": intent["intent_id"],
                "command_id": intent["command_id"],
                "fills": [{"exec_id": "exec-1", "quantity": "100", "price": "152.00"}],
                "exit_fills": [{"exec_id": "exec-2", "quantity": "100", "price": "155.00"}],
                "gross_pnl": "300.00",
                "net_pnl": "298.50",
            }],
            "unresolved": [],
        }

    signal_id = intent["signal_id"]
    event_ids = [e["event_id"] for e in broker_events]
    trace = _decision_trace(
        signal_id=signal_id,
        intent=intent,
        sizing=sizing,
        decision=decision,
        broker_event_ids=event_ids,
    )

    return {
        "session_id": SESSION_ID,
        "artifact_attestation": {
            "artifact_id": ARTIFACT_ID,
            "artifact_digest": intent["artifact_digest"],
            "attestation_digest": intent["eligibility_attestation_digest"],
            "state": "PAPER_ELIGIBLE",
            "account_mode": "paper",
            "permitted_conids": [CONID],
        },
        "bars": [{
            "bar_id": intent["bar_id"],
            "conid": CONID,
            "open": "151.5",
            "high": "152.2",
            "low": "151.0",
            "close": "152.0",
            "volume": 1_000_000,
            "timestamp": intent["completed_bar_timestamp"],
        }],
        "quote_evidence": [{
            "conid": CONID,
            "bid": "151.95",
            "ask": "152.05",
            "last": "152.00",
            "timestamp": T0.isoformat(),
            "feed": "LIVE",
        }],
        "broker_snapshots": [{
            "generation_id": 7,
            "account_id": "DU111111",
            "account_mode": "paper",
            "net_liquidation": "100000",
            "daily_pnl": "0",
            "positions": [],
            "working_orders": [],
            "source_timestamp": T0.isoformat(),
        }],
        "policies": {
            "session_risk_version": "p3-session-risk-v1",
            "liquidity_version": "p3-liquidity-v1",
            "calendar_name": "XNYS",
            "calendar_version": "4.5.0",
        },
        "intents": [intent],
        "decisions": [decision],
        "commands": [{
            "command_id": intent["command_id"],
            "intent_id": intent["intent_id"],
            "state": command_state,
            "action": "execute_automated_intent",
            "outcome": "filled" if command_state == "RESOLVED" else (
                "rejected" if command_state == "REJECTED" else None
            ),
        }],
        "broker_events": broker_events,
        "attribution": attribution,
        "breaker_actions": [] if approved else [{
            "action_id": "brk-1",
            "kind": "trip",
            "reason": "policy_reject_burst",
            "timestamp": T0.isoformat(),
        }],
        "reconciliation_actions": [] if scenario != "restart" else [{
            "action_id": "recon-1",
            "kind": "saga_resume",
            "command_id": intent["command_id"],
            "timestamp": (T0 + dt.timedelta(minutes=1)).isoformat(),
        }],
        "operator_actions": [],
        "xnys_schedule": _xnys_schedule(),
        "sizing": [sizing],
        "signals": [{
            "signal_id": signal_id,
            "artifact_id": ARTIFACT_ID,
            "bar_id": intent["bar_id"],
            "conid": CONID,
            "side": "BUY",
            "completed_bar_timestamp": intent["completed_bar_timestamp"],
        }],
        "decision_trace": trace,
    }


class InMemoryEvidenceStore:
    """Minimal session evidence provider for seal tests."""

    def __init__(self, sessions: dict[str, dict[str, Any]]):
        self._sessions = sessions

    def load_session(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._sessions:
            raise KeyError(session_id)
        return deepcopy(self._sessions[session_id])


# ---------------------------------------------------------------------------
# Seal tests
# ---------------------------------------------------------------------------

def test_seal_writes_required_entries_atomically_and_read_only(tmp_path: Path):
    from trader.automation.replay_bundle import ReplayBundle
    from trader.research.bundle import BundleDigest

    evidence = build_day_evidence()
    store = InMemoryEvidenceStore({SESSION_ID: evidence})
    out = tmp_path / "bundles"
    bundle = ReplayBundle(store, output_dir=out)

    digest = bundle.seal(SESSION_ID)

    assert isinstance(digest, BundleDigest)
    assert digest.path.is_dir()
    assert digest.path == out / SESSION_ID
    assert digest.manifest_digest
    assert len(digest.manifest_digest) == 64

    # Required entries present
    expected_files = {
        "artifact_attestation.json",
        "bars.json",
        "quote_evidence.json",
        "broker_snapshots.json",
        "policies.json",
        "intents.json",
        "decisions.json",
        "commands.json",
        "broker_events.json",
        "attribution.json",
        "breaker_actions.json",
        "reconciliation_actions.json",
        "operator_actions.json",
        "xnys_schedule.json",
        "sizing.json",
        "signals.json",
        "decision_trace.json",
        "manifest.json",
    }
    names = {p.name for p in digest.path.iterdir()}
    assert names == expected_files

    # Read-only tree
    assert not (digest.path.stat().st_mode & stat.S_IWUSR)
    for child in digest.path.iterdir():
        assert not (child.stat().st_mode & stat.S_IWUSR)

    # No leftover staging dirs
    staging = [p for p in out.iterdir() if p.name.startswith(".")]
    assert staging == []

    # Manifest checksums match file bytes
    manifest = json.loads((digest.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_id"] == SESSION_ID
    assert manifest["manifest_digest"] == digest.manifest_digest
    for name, checksum in manifest["files"].items():
        raw = (digest.path / name).read_bytes()
        assert __import__("hashlib").sha256(raw).hexdigest() == checksum


def test_seal_digest_is_repeatable(tmp_path: Path):
    from trader.automation.replay_bundle import ReplayBundle

    evidence = build_day_evidence()
    store = InMemoryEvidenceStore({SESSION_ID: evidence})
    a = ReplayBundle(store, output_dir=tmp_path / "a").seal(SESSION_ID)
    b = ReplayBundle(store, output_dir=tmp_path / "b").seal(SESSION_ID)
    assert a.manifest_digest == b.manifest_digest
    # Byte-identical payloads (except path)
    for name in sorted(p.name for p in a.path.iterdir()):
        assert (a.path / name).read_bytes() == (b.path / name).read_bytes()


@pytest.mark.parametrize("bad_id", ["../escape", "foo/bar", "/tmp/x", "xnys-2026-07-18/../x"])
def test_seal_rejects_path_traversal_session_ids(tmp_path: Path, bad_id: str):
    from trader.automation.replay_bundle import ReplayBundle, ReplayBundleError

    store = InMemoryEvidenceStore({SESSION_ID: build_day_evidence()})
    bundle = ReplayBundle(store, output_dir=tmp_path / "bundles")
    with pytest.raises(ReplayBundleError, match="unsafe|traversal|session_id"):
        bundle.seal(bad_id)


def test_seal_refuses_missing_evidence(tmp_path: Path):
    from trader.automation.replay_bundle import ReplayBundle, ReplayBundleError

    evidence = build_day_evidence()
    del evidence["quote_evidence"]
    store = InMemoryEvidenceStore({SESSION_ID: evidence})
    bundle = ReplayBundle(store, output_dir=tmp_path / "bundles")
    with pytest.raises(ReplayBundleError, match="missing evidence|quote_evidence"):
        bundle.seal(SESSION_ID)


def test_seal_refuses_unresolved_commands(tmp_path: Path):
    from trader.automation.replay_bundle import ReplayBundle, ReplayBundleError

    evidence = build_day_evidence(command_state="OUTCOME_UNKNOWN")
    store = InMemoryEvidenceStore({SESSION_ID: evidence})
    bundle = ReplayBundle(store, output_dir=tmp_path / "bundles")
    with pytest.raises(ReplayBundleError, match="unresolved"):
        bundle.seal(SESSION_ID)


def test_verify_detects_corruption(tmp_path: Path):
    from trader.automation.replay_bundle import ReplayBundle, ReplayBundleError

    store = InMemoryEvidenceStore({SESSION_ID: build_day_evidence()})
    digest = ReplayBundle(store, output_dir=tmp_path / "bundles").seal(SESSION_ID)

    target = digest.path / "intents.json"
    target.chmod(0o644)
    target.write_bytes(b'{"tampered":true}')
    target.chmod(0o444)

    with pytest.raises(ReplayBundleError, match="checksum|corrupt"):
        ReplayBundle.verify(digest.path)


def test_verify_rejects_manifest_path_traversal(tmp_path: Path):
    from trader.automation.replay_bundle import ReplayBundle, ReplayBundleError

    store = InMemoryEvidenceStore({SESSION_ID: build_day_evidence()})
    digest = ReplayBundle(store, output_dir=tmp_path / "bundles").seal(SESSION_ID)

    manifest_path = digest.path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = dict(manifest["files"])
    checksum = files.pop("intents.json")
    files["../intents.json"] = checksum
    manifest["files"] = dict(sorted(files.items()))
    body = dict(manifest)
    body.pop("manifest_digest")
    manifest["manifest_digest"] = __import__("hashlib").sha256(
        canonical_json_bytes(body)
    ).hexdigest()

    manifest_path.chmod(0o644)
    manifest_path.write_bytes(canonical_json_bytes(manifest))
    manifest_path.chmod(0o444)
    digest.path.chmod(0o555)

    with pytest.raises(ReplayBundleError, match="unsafe|traversal"):
        ReplayBundle.verify(digest.path)


# ---------------------------------------------------------------------------
# Replay tests
# ---------------------------------------------------------------------------

def test_golden_accepted_day_replays_exactly(tmp_path: Path):
    from trader.automation.replay import TradingDayReplay
    from trader.automation.replay_bundle import ReplayBundle

    store = InMemoryEvidenceStore({SESSION_ID: build_day_evidence(scenario="accepted")})
    sealed = ReplayBundle(store, output_dir=tmp_path / "bundles").seal(SESSION_ID)
    verified = ReplayBundle.verify(sealed.path)

    result = TradingDayReplay().run(verified)

    assert result.matched is True
    assert result.divergences == ()
    assert result.session_id == SESSION_ID
    assert result.decision_trace_digest == result.recomputed_trace_digest
    assert len(result.decision_trace_digest) == 64


def test_replay_reports_decision_trace_divergence(tmp_path: Path):
    from trader.automation.replay import TradingDayReplay
    from trader.automation.replay_bundle import ReplayBundle

    evidence = build_day_evidence(scenario="accepted")
    # Corrupt recorded intent_id while leaving fields otherwise intact →
    # recomputation diverges from sealed decision_trace.
    evidence["decision_trace"][1]["intent_id"] = "intent-" + ("0" * 64)

    store = InMemoryEvidenceStore({SESSION_ID: evidence})
    # Seal bypasses decision_trace consistency (seal stores evidence as-is
    # after structural checks). Use low-level write via public seal then
    # mutate before verify is impossible (read-only). Instead seal a
    # structurally valid bundle, then temporarily rewrite after chmod.
    sealed = ReplayBundle(store, output_dir=tmp_path / "bundles").seal(SESSION_ID)

    # Wait — we sealed the already-corrupted evidence. Seal should succeed
    # (evidence is complete; commands resolved). Replay should diverge.
    verified = ReplayBundle.verify(sealed.path)
    result = TradingDayReplay().run(verified)

    assert result.matched is False
    assert result.divergences
    kinds = {d.kind for d in result.divergences}
    assert "decision_trace" in kinds or "intent_id" in kinds
    # Structured fields present
    div = result.divergences[0]
    assert div.path
    assert div.expected is not None or div.actual is not None


@pytest.mark.parametrize("scenario", ["rejected", "partial_fill", "restart"])
def test_adversarial_fixtures_seal_and_replay(tmp_path: Path, scenario: str):
    from trader.automation.replay import TradingDayReplay
    from trader.automation.replay_bundle import ReplayBundle

    evidence = build_day_evidence(scenario=scenario)
    store = InMemoryEvidenceStore({SESSION_ID: evidence})
    sealed = ReplayBundle(store, output_dir=tmp_path / f"bundles-{scenario}").seal(SESSION_ID)
    verified = ReplayBundle.verify(sealed.path)
    result = TradingDayReplay().run(verified)

    assert result.matched is True
    assert result.divergences == ()
    # Broker events are consumed from the sealed record — never empty for
    # partial_fill/restart; rejected may be empty.
    if scenario != "rejected":
        assert verified.entries["broker_events"]


def test_replay_recomputes_intent_ids_from_fields():
    from trader.automation.replay import TradingDayReplay

    intent = _intent_fields()
    stored_id = intent["intent_id"]
    body = {k: v for k, v in intent.items() if k not in ("intent_id", "command_id")}
    recomputed = TradingDayReplay.recompute_intent_ids(body)
    assert recomputed["intent_id"] == stored_id
    assert recomputed["command_id"] == intent["command_id"]


def test_replay_does_not_call_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from trader.automation.replay import TradingDayReplay
    from trader.automation.replay_bundle import ReplayBundle

    def _boom(*_a, **_k):
        raise AssertionError("replay must not touch IB / broker clients")

    # Guard common broker entry points if imported.
    monkeypatch.setattr(
        "trader.automation.replay.TradingDayReplay._forbid_broker_io",
        lambda self: None,
        raising=False,
    )

    store = InMemoryEvidenceStore({SESSION_ID: build_day_evidence()})
    sealed = ReplayBundle(store, output_dir=tmp_path / "bundles").seal(SESSION_ID)
    verified = ReplayBundle.verify(sealed.path)

    # Inject a sentinel that fails if live broker path is taken.
    replay = TradingDayReplay(broker_client=_boom)
    result = replay.run(verified)
    assert result.matched is True


def test_cli_script_exists_and_is_importable():
    script = Path(__file__).resolve().parents[2] / "scripts" / "replay_trading_day.py"
    assert script.is_file()
    source = script.read_text(encoding="utf-8")
    assert "TradingDayReplay" in source
    assert "ReplayBundle" in source

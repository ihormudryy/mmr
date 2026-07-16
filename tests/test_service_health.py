"""G0 Task 6: health endpoints, secret redaction, and pycron receipts.

Covers `trader/operations/health.py` (redaction, safe_error, JSON-lines
receipts) and the dashboard's `/healthz` / `/readyz` / `/api/health` routes
(`web/app.py`). No ZMQ, no Docker, no real trader_service — the FastAPI
routes are driven through TestClient with a stubbed SDK, mirroring
`tests/test_web_dashboard.py`'s pattern.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

import web.app as webapp
from trader.operations.health import (
    build_health_payload,
    process_state,
    read_latest_receipts,
    record_receipt,
    redact_secrets,
    safe_error,
)


# ---------------------------------------------------------------------------
# redact_secrets
# ---------------------------------------------------------------------------

def test_redact_secrets_drops_matching_keys_not_just_values():
    raw = {
        'service_hmac_key_file': '/home/trader/.config/mmr/service_hmac.key',
        'dashboard_token': 'super-secret-value',
        'ok_field': 'kept',
    }
    out = redact_secrets(raw)
    encoded = json.dumps(out).lower()
    assert 'service_hmac' not in encoded
    assert 'dashboard_token' not in encoded
    assert out == {'ok_field': 'kept'}


def test_redact_secrets_recurses_into_nested_dicts_and_lists():
    raw = {
        'jobs': [
            {'job': 'db_backup', 'service_hmac_key_file': '/x'},
            {'job': 'data_refresh_us', 'ok': 1},
        ],
        'nested': {'deep': {'MMR_WEB_TOKEN': 'abc', 'kept': True}},
    }
    out = redact_secrets(raw)
    encoded = json.dumps(out).lower()
    assert 'service_hmac' not in encoded
    assert 'web_token' not in encoded
    assert out['jobs'][0] == {'job': 'db_backup'}
    assert out['jobs'][1] == {'job': 'data_refresh_us', 'ok': 1}
    assert out['nested']['deep'] == {'kept': True}


def test_redact_secrets_is_case_insensitive():
    out = redact_secrets({'Service_HMAC_Key_File': 'x', 'DASHBOARD_TOKEN': 'y'})
    assert out == {}


def test_redact_secrets_scrubs_secret_shaped_string_values_too():
    # Defense in depth: even under an innocuous key, a value that mentions
    # a marker (e.g. a stray error message) must not survive verbatim.
    out = redact_secrets({'error': 'field dashboard_token is required'})
    assert out == {'error': '***REDACTED***'}


def test_redact_secrets_leaves_unrelated_data_untouched():
    raw = {'pid': 123, 'uptime_seconds': 1.5, 'nested': {'a': [1, 2, 3]}}
    assert redact_secrets(raw) == raw


# ---------------------------------------------------------------------------
# safe_error
# ---------------------------------------------------------------------------

def test_safe_error_never_includes_the_raw_message():
    exc = ValueError('service_hmac_key_file=/home/trader/.config/mmr/secret.key is invalid')
    result = safe_error(exc)
    assert 'service_hmac_key_file' not in result
    assert '/home/trader' not in result
    assert 'ValueError' in result


# ---------------------------------------------------------------------------
# process_state
# ---------------------------------------------------------------------------

def test_process_state_has_no_dependency_on_other_services():
    state = process_state()
    assert set(state) == {'pid', 'started_at', 'uptime_seconds'}
    assert state['uptime_seconds'] >= 0


# ---------------------------------------------------------------------------
# Pycron one-shot receipts
# ---------------------------------------------------------------------------

def test_record_and_read_latest_receipt_round_trip(tmp_path):
    started = dt.datetime(2026, 7, 15, 20, 30)
    completed = dt.datetime(2026, 7, 15, 20, 31)
    record_receipt('data_refresh_us', started, completed, True, receipts_dir=str(tmp_path))

    latest = read_latest_receipts(receipts_dir=str(tmp_path))
    assert latest == {
        'data_refresh_us': {
            'job': 'data_refresh_us',
            'started_at': started.isoformat(),
            'completed_at': completed.isoformat(),
            'success': True,
            'error': None,
        }
    }


def test_read_latest_receipts_returns_newest_per_job(tmp_path):
    for i in range(3):
        record_receipt(
            'db_backup',
            dt.datetime(2026, 7, 15, 22, i),
            dt.datetime(2026, 7, 15, 22, i, 5),
            success=(i != 1),
            error=None if i != 1 else 'exit code 1',
            receipts_dir=str(tmp_path),
        )
    latest = read_latest_receipts(receipts_dir=str(tmp_path))
    assert latest['db_backup']['started_at'] == dt.datetime(2026, 7, 15, 22, 2).isoformat()
    assert latest['db_backup']['success'] is True


def test_different_jobs_write_to_different_files_never_interleaved(tmp_path):
    record_receipt('data_refresh_us', dt.datetime.now(), dt.datetime.now(), True, receipts_dir=str(tmp_path))
    record_receipt('data_refresh_asx', dt.datetime.now(), dt.datetime.now(), False,
                   error='exit code 2', receipts_dir=str(tmp_path))
    files = sorted(p.name for p in tmp_path.glob('*.jsonl'))
    assert files == ['data_refresh_asx.jsonl', 'data_refresh_us.jsonl']

    latest = read_latest_receipts(receipts_dir=str(tmp_path))
    assert set(latest) == {'data_refresh_us', 'data_refresh_asx'}
    assert latest['data_refresh_asx']['success'] is False
    assert latest['data_refresh_us']['success'] is True


def test_read_latest_receipts_on_missing_directory_returns_empty(tmp_path):
    assert read_latest_receipts(receipts_dir=str(tmp_path / 'does-not-exist')) == {}


def test_read_latest_receipts_degrades_on_corrupt_file(tmp_path):
    bad = tmp_path / 'db_backup.jsonl'
    bad.write_text('{"job": "db_backup", "success": true}\nnot json at all\n')
    latest = read_latest_receipts(receipts_dir=str(tmp_path))
    # The corrupt trailing line is skipped; the last *valid* line wins.
    assert latest['db_backup']['success'] is True


def test_record_receipt_never_persists_a_secret_shaped_error(tmp_path):
    record_receipt(
        'db_backup', dt.datetime.now(), dt.datetime.now(), False,
        error='dashboard_token leaked here', receipts_dir=str(tmp_path),
    )
    raw_bytes = (tmp_path / 'db_backup.jsonl').read_text()
    assert 'dashboard_token' not in raw_bytes.lower()


def test_job_name_is_sandboxed_against_path_traversal(tmp_path):
    record_receipt('../../etc/passwd', dt.datetime.now(), dt.datetime.now(), True,
                    receipts_dir=str(tmp_path))
    # Must land INSIDE tmp_path, never escape it.
    children = list(tmp_path.iterdir())
    assert len(children) == 1
    assert children[0].parent == tmp_path


# ---------------------------------------------------------------------------
# build_health_payload
# ---------------------------------------------------------------------------

def test_build_health_payload_shape(tmp_path):
    payload = build_health_payload(
        dependencies={'trader': {'reachable': True, 'status': {'ib_connected': True}}},
        receipts_dir=str(tmp_path),
    )
    assert set(payload) == {'process', 'dependencies', 'jobs'}
    assert set(payload['process']) == {'pid', 'started_at', 'uptime_seconds'}
    assert payload['dependencies']['trader']['reachable'] is True
    assert payload['jobs'] == {}


# ---------------------------------------------------------------------------
# Brief's verbatim redaction fixture/test
# ---------------------------------------------------------------------------

@pytest.fixture
def health_payload(tmp_path):
    """A deliberately 'buggy producer' payload: a dependency blob carrying
    a secret-shaped KEY, and a job receipt whose error message accidentally
    embeds a secret-shaped VALUE. `build_health_payload` must strip both."""
    record_receipt(
        job='data_refresh_us',
        started_at=dt.datetime(2026, 7, 15, 20, 30),
        completed_at=dt.datetime(2026, 7, 15, 20, 31),
        success=False,
        error='dashboard_token=abc123 leaked into an error string',
        receipts_dir=str(tmp_path),
    )
    return build_health_payload(
        dependencies={
            'trader': {
                'reachable': True,
                'service_hmac_key_file': '/home/trader/.config/mmr/service_hmac.key',
                'status': {'ib_connected': True},
            },
        },
        receipts_dir=str(tmp_path),
    )


def test_health_redacts_secrets(health_payload):
    encoded = json.dumps(health_payload)
    assert "service_hmac" not in encoded.lower()
    assert "dashboard_token" not in encoded.lower()


# ---------------------------------------------------------------------------
# Dashboard HTTP endpoints
# ---------------------------------------------------------------------------

class _StatusStub:
    """Minimal SDK stub — only what `/api/health`'s trader dependency
    fetch touches (`fetch_status` -> `m.status()`)."""

    def __init__(self, status_value=None, raise_exc=None):
        self._status_value = status_value
        self._raise_exc = raise_exc

    def status(self):
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._status_value


@pytest.fixture
def stub_sdk(monkeypatch):
    sdk = _StatusStub(status_value={'ib_connected': True, 'ib_upstream_connected': True})
    monkeypatch.setattr(webapp, '_mmr', sdk)
    monkeypatch.setattr(webapp, '_get_mmr', lambda: sdk)
    monkeypatch.setattr(webapp, '_reset_mmr', lambda: None)
    return sdk


@pytest.fixture
def client(stub_sdk):
    return TestClient(webapp.app)


def test_public_health_has_no_dependency_detail(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert set(client.get("/readyz").json()) == {"ready"}


def test_healthz_and_readyz_require_no_auth_even_when_token_configured(client, monkeypatch):
    monkeypatch.setattr(webapp, '_ACCESS_TOKEN', 'super-secret-dashboard-token')
    assert client.get('/healthz').status_code == 200
    assert client.get('/readyz').status_code == 200


def test_readyz_is_true_while_serving_and_false_after_shutdown(stub_sdk, monkeypatch):
    monkeypatch.setattr(webapp, '_READY', True)
    with TestClient(webapp.app) as ready_client:
        assert ready_client.get('/readyz').json() == {'ready': True}
    # TestClient's context-manager exit fires FastAPI's shutdown event.
    assert webapp._READY is False
    # Restore for any other test relying on module state (defensive; pytest
    # re-imports nothing between tests but this keeps intent explicit).
    monkeypatch.setattr(webapp, '_READY', True)


def test_api_health_requires_auth_when_token_configured(client, monkeypatch):
    monkeypatch.setattr(webapp, '_ACCESS_TOKEN', 'super-secret-dashboard-token')
    resp = client.get('/api/health')
    assert resp.status_code == 401


def test_api_health_reports_process_state_and_reachable_dependency(client):
    resp = client.get('/api/health')
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {'process', 'dependencies', 'jobs'}
    assert body['dependencies']['trader']['reachable'] is True
    assert body['dependencies']['trader']['status']['ib_connected'] is True


def test_api_health_degrades_when_trader_unreachable(monkeypatch):
    sdk = _StatusStub(raise_exc=ConnectionError('trader_service unreachable'))
    monkeypatch.setattr(webapp, '_mmr', sdk)
    monkeypatch.setattr(webapp, '_get_mmr', lambda: sdk)
    monkeypatch.setattr(webapp, '_reset_mmr', lambda: None)
    client = TestClient(webapp.app)

    resp = client.get('/api/health')
    assert resp.status_code == 200
    body = resp.json()
    assert body['dependencies']['trader']['reachable'] is False
    # Never the raw exception string, only the type -- and never a
    # dependency address/path either.
    encoded = json.dumps(body)
    assert 'trader_service unreachable' not in encoded


def test_api_health_includes_scheduled_job_receipts(client, monkeypatch, tmp_path):
    monkeypatch.setattr(webapp, 'build_health_payload',
                         lambda **kw: build_health_payload(receipts_dir=str(tmp_path), **kw))
    record_receipt('db_backup', dt.datetime.now(), dt.datetime.now(), True, receipts_dir=str(tmp_path))

    resp = client.get('/api/health')
    body = resp.json()
    assert body['jobs']['db_backup']['success'] is True


def test_api_health_response_never_carries_secret_shaped_fields(client):
    resp = client.get('/api/health')
    encoded = json.dumps(resp.json()).lower()
    assert 'service_hmac' not in encoded
    assert 'dashboard_token' not in encoded
    assert 'mmr_web_token' not in encoded

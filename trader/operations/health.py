"""Health, secret redaction, and pycron one-shot receipt primitives (G0 Task 6).

Three independent, dependency-light pieces used by the dashboard's health
endpoints (``web/app.py``) and by ``pycron.pycron``'s one-shot job runner:

1. ``redact_secrets`` -- a security property, not a formatting nicety: no
   health payload or receipt may ever surface a ``service_hmac*``/
   ``dashboard_token``-ish key, even if some future field accidentally
   carries one. Matching keys are DROPPED entirely (not just masked)
   because a masked-but-present key (``{"dashboard_token": "***"}``) still
   fails a "the string dashboard_token never appears in the payload" test
   -- the key NAME itself is the thing that must never reach the wire, not
   just its value. See ``tests/test_service_health.py``.
2. ``safe_error`` -- turns an exception into a receipt-safe string. Mirrors
   the convention already established in ``typed_rpc.py``'s
   ``INTERNAL_ERROR`` replies: NEVER ``str(exc)`` (the message could embed
   a file path, hostname, or -- in a misconfigured job -- a secret), only
   the exception's type name. The real detail belongs in server-side logs.
3. Pycron one-shot receipts -- ``record_receipt``/``read_latest_receipts``,
   JSON-lines files under the scheduler's shared state volume (the same
   ``~/.local/share/mmr/logs`` bind mount every one of the five Compose
   services already has -- see ``docker-compose.yml``), one file PER JOB so
   two different jobs finishing at the same instant can never interleave a
   single line (each only ever appends to its own file; POSIX guarantees a
   single short ``write()`` is atomic).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional


# --- Secret redaction -------------------------------------------------------

# Case-insensitive substrings. Deliberately broader than the two the brief's
# tests name explicitly (``service_hmac``, ``dashboard_token``) -- this also
# covers this project's actual current secrets (``service_hmac_key_file``,
# the dashboard's ``MMR_WEB_TOKEN``) and generic secret-shaped keys, so a
# future field doesn't need a redaction-list update to stay safe.
_SECRET_MARKERS = (
    'service_hmac',
    'hmac_key',
    'dashboard_token',
    'web_token',
    'access_token',
    'api_key',
    'password',
    'secret',
    'private_key',
)


def _matches_secret_marker(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def redact_secrets(value: Any) -> Any:
    """Recursively strip secret-shaped keys (and scrub secret-shaped string
    values) from a JSON-able structure.

    Keys are DROPPED, not masked -- see the module docstring for why a
    masked value is not good enough. String values that happen to mention
    a marker (e.g. a stray error message) are replaced outright, as a
    second layer of defense beyond key-based dropping.
    """
    if isinstance(value, dict):
        out: Dict[Any, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _matches_secret_marker(k):
                continue
            out[k] = redact_secrets(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_secrets(v) for v in value]
    if isinstance(value, str) and _matches_secret_marker(value):
        return '***REDACTED***'
    return value


def safe_error(exc: BaseException) -> str:
    """Receipt-safe error string -- the exception's TYPE only, never
    ``str(exc)``. Mirrors ``typed_rpc.py``'s generic ``INTERNAL_ERROR``
    convention: the real message (which could embed a file path, hostname,
    or a misconfigured secret) belongs in server-side logs, not the wire.
    """
    return f'{type(exc).__name__} (see server logs for detail)'


# --- Pycron one-shot receipts ------------------------------------------------

DEFAULT_RECEIPTS_DIR = '~/.local/share/mmr/logs/pycron_receipts'

_UNSAFE_NAME_CHARS_RE = re.compile(r'[^A-Za-z0-9_.-]')


def _receipts_dir(receipts_dir: Optional[str] = None) -> Path:
    raw = receipts_dir or os.environ.get('MMR_RECEIPTS_DIR', DEFAULT_RECEIPTS_DIR)
    return Path(raw).expanduser()


def _receipt_path(job: str, receipts_dir: Optional[str] = None) -> Path:
    # Job names are config-driven (pycron.yaml), never end-user input, but
    # this keeps a stray '/' or '..' from escaping the receipts directory
    # regardless -- cheap, and matches this project's general "sandbox
    # paths derived from config" posture (see strategy_runtime.py's
    # strategies_directory sandboxing).
    safe_name = _UNSAFE_NAME_CHARS_RE.sub('_', job) or 'unknown_job'
    return _receipts_dir(receipts_dir) / f'{safe_name}.jsonl'


def record_receipt(
    job: str,
    started_at: dt.datetime,
    completed_at: dt.datetime,
    success: bool,
    error: Optional[str] = None,
    receipts_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one JSON-lines receipt for a completed pycron one-shot job.

    ``error`` must already be a caller-sanitized string (e.g. ``safe_error(exc)``
    or a small fixed-format message like ``"exit code 1"``) -- this function
    does not know how to turn an arbitrary exception into safe text, it only
    guarantees the final on-disk line never carries a secret-shaped key/value
    (``redact_secrets`` is applied before serialization, defense in depth).
    """
    receipt = redact_secrets({
        'job': job,
        'started_at': started_at.isoformat(),
        'completed_at': completed_at.isoformat(),
        'success': bool(success),
        'error': error,
    })
    path = _receipt_path(job, receipts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a') as f:
        f.write(json.dumps(receipt, sort_keys=True) + '\n')
    return receipt


def read_latest_receipts(receipts_dir: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """Newest receipt per job name.

    Missing/corrupt files degrade to a per-job error entry rather than
    raising -- a health endpoint must never 500 because one job's receipt
    file got truncated mid-write.
    """
    base = _receipts_dir(receipts_dir)
    out: Dict[str, Dict[str, Any]] = {}
    if not base.is_dir():
        return out
    for path in sorted(base.glob('*.jsonl')):
        job = path.stem
        last_valid: Optional[Dict[str, Any]] = None
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        last_valid = json.loads(line)
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            out[job] = {'job': job, 'success': False, 'error': safe_error(exc)}
            continue
        out[job] = redact_secrets(last_valid) if last_valid is not None else {
            'job': job, 'success': False, 'error': 'no valid receipt lines',
        }
    return out


# --- Process / dashboard health ---------------------------------------------

_PROCESS_STARTED_AT = dt.datetime.now(dt.timezone.utc)
_PROCESS_START_MONOTONIC = time.monotonic()


def process_state() -> Dict[str, Any]:
    """Self-attested state for the CURRENT process -- pid, start time,
    uptime. No dependency on any other service; always available."""
    return {
        'pid': os.getpid(),
        'started_at': _PROCESS_STARTED_AT.isoformat(),
        'uptime_seconds': round(time.monotonic() - _PROCESS_START_MONOTONIC, 3),
    }


def build_health_payload(
    *,
    dependencies: Optional[Dict[str, Any]] = None,
    receipts_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the authenticated ``/api/health`` payload: this process's
    own state, caller-supplied dependency health (already degrade-on-failure
    -- e.g. the dashboard's existing trader-status fetcher), and the newest
    receipt per scheduled job. Always passed through ``redact_secrets``
    before return -- defense in depth even though none of these fields are
    expected to carry a secret today.
    """
    payload = {
        'process': process_state(),
        'dependencies': dependencies or {},
        'jobs': read_latest_receipts(receipts_dir),
    }
    return redact_secrets(payload)

"""Tests for the canonical JSON authentication primitives (G0 Task 2).

Covers the brief's two required tests verbatim, plus the security-critical
details called out in the outer task instructions: duplicate-key rejection,
non-finite-number rejection on parse, constant-time signature comparison,
nonce-not-burned-on-bad-signature/skew, clock skew checked in both
directions, nonce TTL expiry, body size limit, and extra="forbid"/missing
field rejection.
"""

import inspect
import json
import threading

import pytest
from pydantic import ValidationError

from trader.messaging.typed_rpc import (
    DEFAULT_CLOCK_SKEW_SECONDS,
    DEFAULT_NONCE_TTL_SECONDS,
    MAX_REQUEST_BYTES,
    AuthenticationError,
    HmacServiceAuthenticator,
    ReplayError,
    ReplayNonceCache,
    RpcProblem,
    TypedRpcRequest,
    TypedRpcResponse,
    _digest,
    canonical_json,
    decode_request,
    signing_bytes,
)


class MutableClock:
    """A settable fake clock for deterministic skew/TTL tests."""

    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


KEY = b"k" * 32
FIXED_NOW = 1_700_000_000.0


# ---------------------------------------------------------------------------
# Brief Step 1 -- verbatim
# ---------------------------------------------------------------------------

def test_signature_binds_method_id_nonce_and_body():
    auth = HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)
    request = auth.sign("approve_proposal", "req-1", "nonce-1", {"proposal_id": 7})
    auth.verify(request)
    changed = request.model_copy(update={"body": {"proposal_id": 8}})
    with pytest.raises(AuthenticationError):
        auth.verify(changed)


def test_nonce_cannot_be_replayed():
    auth = HmacServiceAuthenticator(b"k" * 32, now=lambda: 1_700_000_000.0)
    request = auth.sign("get_status", "req-1", "nonce-1", {})
    auth.verify(request)
    with pytest.raises(ReplayError):
        auth.verify(request)


# ---------------------------------------------------------------------------
# canonical_json / signing_bytes determinism
# ---------------------------------------------------------------------------

def test_canonical_json_sorts_keys_and_is_compact():
    payload = {"b": 1, "a": 2, "nested": {"z": 1, "y": 2}}
    encoded = canonical_json(payload)
    assert encoded == b'{"a":2,"b":1,"nested":{"y":2,"z":1}}'
    assert b" " not in encoded


def test_canonical_json_rejects_non_finite_floats_on_output():
    with pytest.raises(ValueError):
        canonical_json({"x": float("nan")})


def test_signing_bytes_ignores_signature_field():
    auth = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW)
    request = auth.sign("get_status", "req-1", "nonce-1", {"a": 1})
    tampered_signature = request.model_copy(update={"signature": "deadbeef"})
    assert signing_bytes(request) == signing_bytes(tampered_signature)


# ---------------------------------------------------------------------------
# Security detail 1 -- duplicate JSON keys rejected on parse
# ---------------------------------------------------------------------------

def test_duplicate_key_in_raw_json_body_is_rejected():
    auth = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW)
    request = auth.sign("get_status", "req-1", "nonce-1", {"a": 1})
    raw = canonical_json(request.model_dump())
    # Splice in a second, conflicting top-level "method" key.
    tampered = raw[:-1] + b',"method":"cancel_all"}'
    with pytest.raises(AuthenticationError, match="duplicate key"):
        decode_request(tampered)


def test_duplicate_key_nested_inside_body_is_rejected():
    raw = (
        b'{"method":"get_status","request_id":"req-1","timestamp":1700000000.0,'
        b'"nonce":"nonce-1","body":{"a":1,"a":2},"signature":"deadbeef"}'
    )
    with pytest.raises(AuthenticationError, match="duplicate key"):
        decode_request(raw)


# ---------------------------------------------------------------------------
# Security detail 2 -- non-finite numbers rejected on parse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_constant_in_body_is_rejected_on_parse(literal):
    raw = (
        '{"method":"get_status","request_id":"req-1","timestamp":1700000000.0,'
        '"nonce":"nonce-1","body":{"x": %s},"signature":"deadbeef"}' % literal
    ).encode("utf-8")
    with pytest.raises(AuthenticationError, match="non-finite"):
        decode_request(raw)


def test_stdlib_json_loads_accepts_nan_by_default_unlike_decode_request():
    # Documents *why* decode_request needs its own guard: plain json.loads
    # (without our parse_constant hook) happily accepts NaN on input even
    # though canonical_json refuses to emit it.
    parsed = json.loads('{"x": NaN}')
    assert parsed["x"] != parsed["x"]  # NaN is the only value unequal to itself
    with pytest.raises(AuthenticationError):
        decode_request(b'{"x": NaN}')


@pytest.mark.parametrize("literal", ["1e999", "-1e999"])
def test_overflow_float_literal_nested_in_body_is_rejected(literal):
    # An ordinary numeric literal that overflows to +/-inf is routed through
    # json's parse_float, NOT parse_constant -- so it would slip past
    # decode_request and only blow up later inside verify() -> canonical_json
    # as a bare ValueError on the unauthenticated path. The parse_float hook
    # closes that gap. (Regression guard for the Task 2 Critical finding.)
    raw = (
        '{"method":"get_status","request_id":"req-1","timestamp":1700000000.0,'
        '"nonce":"nonce-1","body":{"x": %s},"signature":"deadbeef"}' % literal
    ).encode("utf-8")
    with pytest.raises(AuthenticationError, match="non-finite"):
        decode_request(raw)


@pytest.mark.parametrize("literal", ["1e999", "-1e999"])
def test_overflow_float_literal_at_top_level_timestamp_is_rejected(literal):
    # timestamp is the top-level numeric field; an overflowing literal there
    # is caught by the same parse_float hook before shape validation.
    raw = (
        '{"method":"get_status","request_id":"req-1","timestamp": %s,'
        '"nonce":"nonce-1","body":{},"signature":"deadbeef"}' % literal
    ).encode("utf-8")
    with pytest.raises(AuthenticationError, match="non-finite"):
        decode_request(raw)


def test_overflow_float_never_reaches_verify_as_bare_valueerror():
    # End-to-end: proves the fix keeps the failure inside the
    # AuthenticationError/ReplayError taxonomy rather than surfacing a bare
    # ValueError from verify()'s canonical_json call. Before the parse_float
    # hook, decode_request returned a request whose body carried float('inf')
    # and auth.verify(request) raised ValueError("Out of range float ...").
    auth = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW)
    raw = (
        b'{"method":"get_status","request_id":"req-1","timestamp":1700000000.0,'
        b'"nonce":"nonce-1","body":{"x": 1e999},"signature":"deadbeef"}'
    )
    with pytest.raises((AuthenticationError, ReplayError)):
        auth.verify(decode_request(raw))


# ---------------------------------------------------------------------------
# Security detail 3 -- constant-time signature comparison
# ---------------------------------------------------------------------------

def test_verify_uses_constant_time_compare_digest():
    source = inspect.getsource(HmacServiceAuthenticator.verify)
    assert "hmac.compare_digest" in source
    assert "signature ==" not in source
    assert "== request.signature" not in source


def test_wrong_signature_same_length_is_rejected():
    auth = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW)
    request = auth.sign("get_status", "req-2", "nonce-2", {})
    wrong_same_length = request.model_copy(update={"signature": "0" * len(request.signature)})
    with pytest.raises(AuthenticationError):
        auth.verify(wrong_same_length)


@pytest.mark.parametrize(
    "field, new_value",
    [
        ("method", "cancel_all"),
        ("request_id", "req-tampered"),
        ("nonce", "nonce-tampered"),
        # timestamp bumped by +5s -- still WITHIN the 30s skew window, so it's
        # the SIGNATURE (which covers timestamp) that must reject it, not the
        # skew check. Proves signing_bytes binds timestamp.
        ("timestamp", FIXED_NOW + 5.0),
    ],
)
def test_signature_binds_every_signed_field(field, new_value):
    # Guards against a future edit dropping a field from signing_bytes: mutate
    # each signed field on a validly-signed request (leaving the original
    # signature intact) and assert verification fails.
    auth = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW)
    request = auth.sign("approve_proposal", "req-orig", "nonce-orig", {"proposal_id": 7})
    tampered = request.model_copy(update={field: new_value})
    with pytest.raises(AuthenticationError):
        auth.verify(tampered)


def test_error_messages_do_not_leak_key_or_expected_digest():
    # Secret hygiene: neither the HMAC key bytes nor the expected digest may
    # appear in the exception surfaced to an unauthenticated caller (that
    # would hand an attacker either the secret or an oracle for it).
    auth = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW)
    request = auth.sign("get_status", "req-hygiene", "nonce-hygiene", {"a": 1})
    key_hex = KEY.hex()

    # Signature-mismatch path. verify() internally computes the digest of the
    # SUBMITTED (forged) request -- that is the exploitable value to guard:
    # leaking it is a signing oracle (a valid signature for the forged
    # request). Assert THAT digest is absent, not the original request's.
    forged = request.model_copy(update={"body": {"a": 999}})
    forged_digest = _digest(KEY, forged)
    with pytest.raises(AuthenticationError) as sig_exc:
        auth.verify(forged)
    sig_text = str(sig_exc.value)
    assert forged_digest not in sig_text
    assert key_hex not in sig_text
    assert KEY.decode() not in sig_text

    # Clock-skew path (does not even compute the digest, but assert anyway).
    clock = MutableClock(FIXED_NOW)
    skew_auth = HmacServiceAuthenticator(KEY, now=clock)
    skew_request = skew_auth.sign("get_status", "req-skew", "nonce-skew", {})
    clock.t = FIXED_NOW + DEFAULT_CLOCK_SKEW_SECONDS + 5
    with pytest.raises(AuthenticationError) as skew_exc:
        skew_auth.verify(skew_request)
    skew_text = str(skew_exc.value)
    assert _digest(KEY, skew_request) not in skew_text
    assert key_hex not in skew_text
    assert KEY.decode() not in skew_text


# ---------------------------------------------------------------------------
# Security detail 4 -- nonce claimed last; bad skew/signature don't burn it
# ---------------------------------------------------------------------------

def test_bad_signature_does_not_burn_the_nonce():
    auth = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW)
    request = auth.sign("get_status", "req-3", "nonce-3", {"a": 1})
    forged = request.model_copy(update={"body": {"a": 999}})
    with pytest.raises(AuthenticationError):
        auth.verify(forged)
    # The real request, with its original valid signature, must still verify --
    # if the failed forged attempt had burned "nonce-3" this would raise ReplayError.
    auth.verify(request)


def test_bad_skew_does_not_burn_the_nonce():
    clock = MutableClock(FIXED_NOW)
    auth = HmacServiceAuthenticator(KEY, now=clock)
    request = auth.sign("get_status", "req-4", "nonce-4", {})
    clock.t = FIXED_NOW + DEFAULT_CLOCK_SKEW_SECONDS + 1  # push past the allowed skew
    with pytest.raises(AuthenticationError):
        auth.verify(request)
    clock.t = FIXED_NOW  # back within skew
    auth.verify(request)  # succeeds -- nonce was never claimed by the failed attempt


# ---------------------------------------------------------------------------
# Security detail 5 -- clock skew checked both directions; nonce TTL
# ---------------------------------------------------------------------------

def test_timestamp_too_old_is_rejected():
    clock = MutableClock(FIXED_NOW)
    auth = HmacServiceAuthenticator(KEY, now=clock)
    request = auth.sign("get_status", "req-5", "nonce-5", {})
    clock.t = FIXED_NOW + DEFAULT_CLOCK_SKEW_SECONDS + 1
    with pytest.raises(AuthenticationError):
        auth.verify(request)


def test_timestamp_too_far_in_the_future_is_rejected():
    # Signer's clock is fast relative to the verifier's -- same key, a
    # genuinely valid signature, but the claimed timestamp itself is outside
    # the verifier's allowed window. Proves both directions are checked, not
    # just "too old".
    signer = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW + DEFAULT_CLOCK_SKEW_SECONDS + 1)
    verifier = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW)
    request = signer.sign("get_status", "req-6", "nonce-6", {})
    with pytest.raises(AuthenticationError):
        verifier.verify(request)


def test_timestamp_exactly_at_skew_boundary_is_accepted():
    clock = MutableClock(FIXED_NOW)
    auth = HmacServiceAuthenticator(KEY, now=clock)
    request = auth.sign("get_status", "req-7", "nonce-7", {})
    clock.t = FIXED_NOW + DEFAULT_CLOCK_SKEW_SECONDS  # exactly on the boundary
    auth.verify(request)  # must not raise


def test_nonce_cache_entries_expire_after_ttl():
    clock = MutableClock(0.0)
    cache = ReplayNonceCache(now=clock)
    cache.claim("n-1")
    with pytest.raises(ReplayError):
        cache.claim("n-1")
    clock.t = DEFAULT_NONCE_TTL_SECONDS + 1
    cache.claim("n-1")  # does not raise -- the prior entry expired


def test_no_replay_gap_across_the_full_skew_window():
    # TTL (60s) == 2 x skew (30s) is deliberate: a request signed at the
    # earliest still-acceptable time (T-30) can be replayed at the latest
    # still-acceptable time (T+30). Across that entire 60s span the original
    # nonce must still be remembered, or a valid-looking replay would slip
    # through. Verifies verify() -> claim leaves NO gap.
    clock = MutableClock(0.0)
    auth = HmacServiceAuthenticator(KEY, now=clock)

    # The request's timestamp is fixed at T. A verifier accepts it for the
    # whole window now in [T-30, T+30]. Worst case for a replay gap: the
    # legitimate verify (which claims the nonce) happens at the EARLIEST
    # acceptable moment (now = T-30) and the replay at the LATEST (now = T+30),
    # maximising the elapsed time the nonce must stay remembered.
    clock.t = FIXED_NOW
    request = auth.sign("get_status", "req-gap", "nonce-gap", {})  # timestamp = T

    clock.t = FIXED_NOW - DEFAULT_CLOCK_SKEW_SECONDS  # now = T-30, skew = 30 (OK)
    auth.verify(request)  # accepted, nonce claimed at now = T-30

    # Replay at now = T+30: still exactly within skew of the T timestamp, and
    # exactly TTL (60s) after the claim. With strict ">" eviction, 60 > 60 is
    # False, so the nonce is still remembered -> ReplayError (not a skew
    # rejection, and not a silently-accepted second execution).
    claimed_at = FIXED_NOW - DEFAULT_CLOCK_SKEW_SECONDS
    clock.t = FIXED_NOW + DEFAULT_CLOCK_SKEW_SECONDS
    assert (clock.t - claimed_at) == DEFAULT_NONCE_TTL_SECONDS
    with pytest.raises(ReplayError):
        auth.verify(request)


def test_replay_nonce_cache_claim_is_thread_safe():
    cache = ReplayNonceCache(now=lambda: 0.0)
    successes = []
    lock = threading.Lock()

    def attempt():
        try:
            cache.claim("shared-nonce")
        except ReplayError:
            return
        with lock:
            successes.append(1)

    threads = [threading.Thread(target=attempt) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(successes) == 1


# ---------------------------------------------------------------------------
# Security detail 6 -- body size limit
# ---------------------------------------------------------------------------

def test_oversized_request_is_rejected_before_parsing():
    huge_body = {"padding": "x" * (MAX_REQUEST_BYTES + 1024)}
    raw = canonical_json({
        "method": "get_status",
        "request_id": "req-8",
        "timestamp": FIXED_NOW,
        "nonce": "nonce-8",
        "body": huge_body,
        "signature": "deadbeef",
    })
    assert len(raw) > MAX_REQUEST_BYTES
    with pytest.raises(AuthenticationError, match="exceeds"):
        decode_request(raw)


def test_request_at_the_size_limit_is_not_rejected_for_size():
    # A small envelope must clear the size gate cleanly (it may still fail
    # shape/signature checks for other reasons -- this only proves the size
    # check itself isn't over-eager).
    auth = HmacServiceAuthenticator(KEY, now=lambda: FIXED_NOW)
    request = auth.sign("get_status", "req-9", "nonce-9", {"a": 1})
    raw = canonical_json(request.model_dump())
    assert len(raw) < MAX_REQUEST_BYTES
    decoded = decode_request(raw)
    auth.verify(decoded)


# ---------------------------------------------------------------------------
# Security detail 7 -- extra="forbid", missing required fields rejected
# ---------------------------------------------------------------------------

def test_missing_required_field_is_rejected_on_direct_construction():
    with pytest.raises(ValidationError):
        TypedRpcRequest(
            method="get_status",
            request_id="req-10",
            timestamp=FIXED_NOW,
            nonce="nonce-10",
            # body missing
            signature="deadbeef",
        )


def test_missing_required_field_in_raw_json_is_rejected_via_decode_request():
    raw = (
        b'{"method":"get_status","request_id":"req-11","timestamp":1700000000.0,'
        b'"nonce":"nonce-11","signature":"deadbeef"}'
    )  # no "body"
    with pytest.raises(AuthenticationError):
        decode_request(raw)


def test_unknown_field_is_rejected_on_direct_construction():
    with pytest.raises(ValidationError):
        TypedRpcRequest(
            method="get_status",
            request_id="req-12",
            timestamp=FIXED_NOW,
            nonce="nonce-12",
            body={},
            signature="deadbeef",
            extra_field="not allowed",
        )


def test_unknown_field_in_raw_json_is_rejected_via_decode_request():
    raw = (
        b'{"method":"get_status","request_id":"req-13","timestamp":1700000000.0,'
        b'"nonce":"nonce-13","body":{},"signature":"deadbeef","extra":"nope"}'
    )
    with pytest.raises(AuthenticationError):
        decode_request(raw)


def test_typed_rpc_response_and_rpc_problem_forbid_extra_fields():
    problem = RpcProblem(code="METHOD_NOT_ALLOWED", message="nope")
    response = TypedRpcResponse(request_id="req-14", ok=False, problem=problem)
    assert response.ok is False
    assert response.problem.code == "METHOD_NOT_ALLOWED"
    with pytest.raises(ValidationError):
        RpcProblem(code="X", message="Y", unexpected="z")


# ---------------------------------------------------------------------------
# HMAC key hardening
# ---------------------------------------------------------------------------

def test_short_hmac_key_is_rejected():
    with pytest.raises(ValueError):
        HmacServiceAuthenticator(b"too-short-key")

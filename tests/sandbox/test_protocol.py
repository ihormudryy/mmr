import asyncio

import pytest

from trader.sandbox import protocol as p


def _roundtrip(obj):
    async def run():
        reader, writer = await _pipe()
        p.write_frame(writer, obj)
        await writer.drain()
        got = await p.read_frame(reader)
        writer.close()
        return got

    return asyncio.run(run())


async def _pipe():
    import socket as _s

    a, b = _s.socketpair()
    reader, writer_a = await asyncio.open_connection(sock=a)
    _reader_b, writer = await asyncio.open_connection(sock=b)
    # The unused ends must stay referenced, or garbage collection closes the sockets.
    reader.peer_ends = (writer_a, _reader_b)
    return reader, writer


def test_constants_match_the_index():
    assert p.PROTOCOL_VERSION == 1
    assert p.LIVE_SOCKET == "/run/sandbox-live/sandbox.sock"
    assert p.RESEARCH_SOCKET == "/run/sandbox-research/sandbox.sock"
    assert p.STORE_DIR == "/store"
    assert p.SANDBOX_ERROR == "SANDBOX_ERROR"
    assert p.SANDBOX_BUSY == "SANDBOX_BUSY"
    assert p.SANDBOX_DIGEST_MISMATCH == "SANDBOX_DIGEST_MISMATCH"
    assert p.SANDBOX_BAD_REQUEST == "SANDBOX_BAD_REQUEST"


def test_a_live_session_can_hold_every_conid_a_deployment_binds():
    from trader.automation.ai_deployments import MAX_CONIDS

    assert p.MAX_SESSION_CONIDS == MAX_CONIDS == 20


def test_a_frame_round_trips():
    assert _roundtrip({"v": 1, "type": "PING"}) == {"v": 1, "type": "PING"}


def test_a_frame_over_the_cap_is_refused_on_read():
    import struct

    big = struct.pack(">I", p.MAX_FRAME_BYTES + 10) + b"x" * (p.MAX_FRAME_BYTES + 10)

    async def run():
        reader, writer = await _pipe()
        writer.write(big)  # no drain: the reader stops after the header, so drain would block
        with pytest.raises(p.FrameError):
            await p.read_frame(reader)
        writer.close()

    asyncio.run(run())


def _open_frame(**override):
    frame = {"v": 1, "type": "OPEN", "digest": "sha256:" + "a" * 64, "class": "MyStrat",
             "params": {}, "bar_size": "1 min", "conids": [101]}
    frame.update(override)
    return frame


def test_open_request_parses_and_normalises():
    req = p.parse_request(_open_frame(params={"X": 1, "Y": 2.5}, conids=[101, 202]))
    assert req.type == "OPEN"
    assert req.digest == "sha256:" + "a" * 64
    assert req.class_name == "MyStrat"
    assert req.params == {"X": 1.0, "Y": 2.5}
    assert req.bar_size == "1 min"
    assert req.conids == (101, 202)


def test_open_accepts_a_param_outside_any_declared_range():
    # Ranges are the trader's submit-time check (SP3 decision D6); research neighbours move +-10 %.
    assert p.parse_request(_open_frame(params={"X": -1e6})).params == {"X": -1e6}


def test_open_refuses_a_path_like_digest():
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(_open_frame(digest="sha256:../../etc/passwd"))
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_open_refuses_a_non_identifier_class():
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(_open_frame(**{"class": "a b"}))
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_open_refuses_non_numeric_params():
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(_open_frame(params={"X": "big"}))
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


@pytest.mark.parametrize("conids", [None, [], [0], [-5], [True], ["101"], [101, 101],
                                    list(range(1, 22))])
def test_open_refuses_a_bad_conid_list(conids):
    frame = _open_frame()
    if conids is None:
        del frame["conids"]
    else:
        frame["conids"] = conids
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(frame)
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_probe_request_parses_and_refuses_a_traversal_name():
    ok = p.parse_request({"v": 1, "type": "OPEN_PROBE", "kind": "escape", "name": "format_globals",
                          "conids": [p.PROBE_CONID]})
    assert ok.type == "OPEN_PROBE" and ok.kind == "escape" and ok.name == "format_globals"
    assert ok.conids == (p.PROBE_CONID,)
    with pytest.raises(p.FrameError) as exc:
        p.parse_request({"v": 1, "type": "OPEN_PROBE", "kind": "escape", "name": "../x",
                         "conids": [p.PROBE_CONID]})
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_bar_request_parses_and_refuses_a_bad_time():
    req = p.parse_request({"v": 1, "type": "BAR", "session": "s1", "conid": 101,
                           "t": "2024-01-02T14:30:00+00:00",
                           "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0})
    assert req.session == "s1" and req.conid == 101 and req.ohlcv["close"] == 1.5
    with pytest.raises(p.FrameError):
        p.parse_request({"v": 1, "type": "BAR", "session": "s1", "conid": 101, "t": "not-a-time",
                         "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0})


@pytest.mark.parametrize("conid", [None, 0, True, "101", 1.5])
def test_bar_request_refuses_a_bad_conid(conid):
    frame = {"v": 1, "type": "BAR", "session": "s1", "t": "2024-01-02T14:30:00+00:00",
             "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}
    if conid is not None:
        frame["conid"] = conid
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(frame)
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_a_wrong_version_is_refused():
    with pytest.raises(p.FrameError):
        p.parse_request({"v": 2, "type": "PING"})


def test_validate_signal_checks_range_and_action():
    p.validate_signal({"action": "BUY", "probability": 0.5})
    p.validate_signal({"action": None, "probability": None})
    for bad in ({"action": "HOLD", "probability": 0.5},
                {"action": "BUY", "probability": 1.5},
                {"action": "BUY", "probability": float("nan")}):
        with pytest.raises(p.FrameError):
            p.validate_signal(bad)


@pytest.mark.parametrize("body", [b"\xff\xfe", b"not json", b"[" * 100000, b"[1]"])
def test_a_garbage_body_is_a_frame_error_not_a_crash(body):
    import struct

    async def run():
        reader, writer = await _pipe()
        writer.write(struct.pack(">I", len(body)) + body)
        with pytest.raises(p.FrameError) as exc:
            await p.read_frame(reader)
        assert exc.value.code == p.SANDBOX_BAD_REQUEST
        writer.close()

    asyncio.run(run())


def _bar_with_time(t):
    return {"v": 1, "type": "BAR", "session": "s1", "conid": 101, "t": t,
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0}


def test_a_bar_time_without_an_offset_is_refused():
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(_bar_with_time("2024-01-02T14:30:00"))
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


@pytest.mark.parametrize("text", ["2024-01-02T14:30:00+00:00", "2024-01-02T14:30:00Z"])
def test_a_utc_bar_time_is_accepted(text):
    from datetime import datetime, timezone

    assert p.parse_request(_bar_with_time(text)).t == datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def test_a_non_utc_bar_time_is_converted_to_utc():
    from datetime import datetime, timezone

    got = p.parse_request(_bar_with_time("2024-01-02T16:30:00+02:00")).t
    assert got == datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    assert got.utcoffset().total_seconds() == 0


# --- fix round 1 -----------------------------------------------------------------------------

_REGEX_GOOD = {
    "DIGEST_RE": "sha256:" + "a" * 64,
    "CLASS_RE": "MyStrat",
    "PROBE_NAME_RE": "format_globals",
    "BUILD_ID_RE": "build-1.2",
    "SESSION_RE": "s1f",
}


@pytest.mark.parametrize("name", sorted(_REGEX_GOOD))
def test_every_pattern_is_exact_no_trailing_newline_no_leading_space(name):
    pattern = getattr(p, name)
    good = _REGEX_GOOD[name]
    assert pattern.fullmatch(good) and pattern.match(good)
    for bad in (good + "\n", " " + good, good + " "):
        assert not pattern.fullmatch(bad), bad
        assert not pattern.match(bad), bad


def test_a_trailing_newline_is_refused_in_each_request_field():
    for frame in (_open_frame(digest="sha256:" + "a" * 64 + "\n"), _open_frame(**{"class": "C\n"}),
                  {"v": 1, "type": "CLOSE", "session": "s1\n"}):
        with pytest.raises(p.FrameError) as exc:
            p.parse_request(frame)
        assert exc.value.code == p.SANDBOX_BAD_REQUEST


def _bar(**override):
    frame = {"v": 1, "type": "BAR", "session": "s1", "conid": 101, "t": "2024-01-02T14:30:00+00:00",
             "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0}
    frame.update(override)
    return frame


def _refused(frame):
    with pytest.raises(p.FrameError) as exc:
        p.parse_request(frame)
    assert exc.value.code == p.SANDBOX_BAD_REQUEST


def test_a_number_that_is_not_a_finite_float_is_refused():
    _refused(_bar(close=10**400))
    _refused(_bar(volume=-(10**400)))
    _refused(_open_frame(params={"X": 10**400}))
    _refused(_bar(close=True))
    _refused(_open_frame(params={"X": True}))
    _refused(_bar(close=float("inf")))


def test_a_non_string_probe_kind_or_name_is_a_protocol_error():
    for bad in ([], {}, None, 7):
        _refused({"v": 1, "type": "OPEN_PROBE", "kind": bad, "name": "x", "conids": [p.PROBE_CONID]})
        _refused({"v": 1, "type": "OPEN_PROBE", "kind": "probe", "name": bad, "conids": [p.PROBE_CONID]})


_VALID_FRAMES = [
    {"v": 1, "type": "PING"},
    _open_frame(params={"X": 1.0}),
    {"v": 1, "type": "OPEN_PROBE", "kind": "probe", "name": "x", "conids": [p.PROBE_CONID]},
    _bar(),
    {"v": 1, "type": "CLOSE", "session": "s1"},
]


def test_a_wrongly_typed_field_gives_only_the_protocol_error():
    junk = ([], {}, None, 10**400, [[]], [{}], [None], [10**400], {"X": []}, {"X": None}, {"X": 10**400})
    for frame in _VALID_FRAMES:
        for key in frame:
            for value in junk:
                broken = dict(frame)
                broken[key] = value
                try:
                    p.parse_request(broken)
                except p.FrameError as exc:
                    assert exc.code == p.SANDBOX_BAD_REQUEST
                # any other exception type fails the test


@pytest.mark.parametrize("t", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00",
                               "1969-12-31T23:59:59+00:00", "2100-01-01T00:00:01+00:00"])
def test_a_bar_time_outside_the_sane_range_is_refused(t):
    _refused(_bar(t=t))


def test_the_edges_of_the_bar_time_range_are_accepted():
    p.parse_request(_bar(t="1970-01-01T00:00:00+00:00"))
    p.parse_request(_bar(t="2099-12-31T23:59:59+00:00"))


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 2])
def test_the_version_must_be_the_integer_one(version):
    _refused({"v": version, "type": "PING"})


@pytest.mark.parametrize("conids", [[2], [1, 2], [], [True]])
def test_a_probe_session_holds_exactly_the_probe_conid(conids):
    _refused({"v": 1, "type": "OPEN_PROBE", "kind": "probe", "name": "x", "conids": conids})


def test_a_connection_closed_mid_frame_is_a_frame_error():
    async def run(data):
        reader = asyncio.StreamReader()
        reader.feed_data(data)
        reader.feed_eof()
        with pytest.raises(p.FrameError):
            await p.read_frame(reader)

    asyncio.run(run(b""))
    asyncio.run(run(b"\x00\x00"))
    asyncio.run(run(b"\x00\x00\x00\x09abc"))


def test_encode_frame_refuses_nan_and_an_oversize_frame():
    with pytest.raises(p.FrameError):
        p.encode_frame({"x": float("nan")})
    with pytest.raises(p.FrameError):
        p.encode_frame({"pad": "x" * p.MAX_FRAME_BYTES})
    assert len(p.encode_frame({"pad": "x" * 1000})) > 1000


def test_a_duplicate_json_key_is_refused_on_read():
    import struct

    async def run():
        body = b'{"v":1,"type":"PING","type":"CLOSE"}'
        reader = asyncio.StreamReader()
        reader.feed_data(struct.pack(">I", len(body)) + body)
        with pytest.raises(p.FrameError):
            await p.read_frame(reader)

    asyncio.run(run())


@pytest.mark.parametrize("bar_size", ["1 min", "5 secs", "1 day", "1 month"])
def test_a_known_bar_size_is_accepted(bar_size):
    assert p.parse_request(_open_frame(bar_size=bar_size)).bar_size == bar_size


@pytest.mark.parametrize("bar_size", ["", "7 min", "1 min ", "weekly", "../x", 5, None, []])
def test_an_unknown_bar_size_is_refused(bar_size):
    _refused(_open_frame(bar_size=bar_size))


def test_a_bar_with_a_negative_volume_or_high_below_low_is_refused():
    _refused(_bar(volume=-1.0))
    _refused(_bar(high=0.4, low=0.5))
    p.parse_request(_bar(volume=0.0, high=0.5, low=0.5))


def test_a_conid_above_the_ib_range_is_refused():
    _refused(_bar(conid=p.MAX_CONID + 1))
    _refused(_open_frame(conids=[p.MAX_CONID + 1]))
    assert p.parse_request(_bar(conid=p.MAX_CONID)).conid == p.MAX_CONID


@pytest.mark.parametrize("probability", [10**400, True, "0.5", [], None, float("inf")])
def test_validate_signal_gives_only_the_sandbox_error_for_a_bad_probability(probability):
    with pytest.raises(p.FrameError) as exc:
        p.validate_signal({"action": "BUY", "probability": probability})
    assert exc.value.code == p.SANDBOX_ERROR


def test_decode_frame_body_returns_the_object_and_refuses_duplicates_and_non_objects():
    assert p.decode_frame_body(b'{"a":1}') == {"a": 1}
    for body in (b'{"a":1,"a":2}', b"[1]", b"\xff\xfe", b"nope"):
        with pytest.raises(p.FrameError):
            p.decode_frame_body(body)

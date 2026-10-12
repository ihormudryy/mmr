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
    big = p.encode_frame({"v": 1, "pad": "x" * (p.MAX_FRAME_BYTES + 10)})

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


def test_bar_request_requires_increasing_numbers():
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

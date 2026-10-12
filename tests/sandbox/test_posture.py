from trader.sandbox import posture


def test_read_posture_reports_known_keys(monkeypatch):
    out = posture.read_posture()
    assert set(out) >= {"cap_eff", "seccomp", "no_new_privs", "uid", "interfaces",
                        "forbidden_env", "home_mounts", "root_read_only", "pids_max"}


def test_the_root_mount_options_are_read(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("overlay / overlay ro,relatime,lowerdir=/x 0 0\ntmpfs /tmp tmpfs rw,noexec 0 0\n")
    assert posture.root_read_only(str(mounts)) is True
    mounts.write_text("overlay / overlay rw,relatime 0 0\n")
    assert posture.root_read_only(str(mounts)) is False


def test_the_pids_limit_is_read_from_cgroup_v2_then_v1(tmp_path):
    v2, v1 = tmp_path / "pids.max", tmp_path / "v1-pids.max"
    v1.write_text("64\n")
    assert posture.pids_max((str(v2), str(v1))) == "64"
    assert posture.pids_max((str(tmp_path / "none"),)) == ""


def test_decode_cap_eff_lists_capabilities():
    caps = posture.decode_cap_eff("00000000000000e0")   # bits 5,6,7
    assert set(caps) == {"CAP_KILL", "CAP_SETGID", "CAP_SETUID"}


def test_forbidden_env_names_are_detected(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "x")
    monkeypatch.setenv("TZ", "UTC")
    assert "ALPACA_API_KEY_ID" in posture.forbidden_env_names()
    assert "TZ" not in posture.forbidden_env_names()


# ── fail closed: what cannot be read must never look clean ─────────────────

def test_decode_cap_eff_names_every_set_bit_and_refuses_junk():
    assert "bit0" in posture.decode_cap_eff("00000000000000e1")   # CAP_CHOWN left on
    assert "bit70" in posture.decode_cap_eff("4000000000000000e0")  # beyond 64 bits still shows
    assert posture.decode_cap_eff("not hex") == []
    assert posture.decode_cap_eff("-e0") == []


def test_a_root_without_a_mount_line_or_with_any_rw_layer_is_not_read_only(tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text("tmpfs /tmp tmpfs rw 0 0\n")
    assert posture.root_read_only(str(mounts)) is False
    mounts.write_text("overlay / overlay ro 0 0\noverlay / overlay rw 0 0\n")
    assert posture.root_read_only(str(mounts)) is False
    assert posture.root_read_only(str(tmp_path / "missing")) is False


def test_unreadable_interfaces_and_mounts_never_look_clean(tmp_path):
    assert posture.interfaces(str(tmp_path / "no-net")) == [posture.UNREADABLE]
    assert posture.home_mounts(str(tmp_path / "no-mounts")) == [posture.UNREADABLE]


def test_interfaces_and_home_mounts_are_read(tmp_path):
    net = tmp_path / "net"
    (net / "lo").mkdir(parents=True)
    (net / "eth0").mkdir()
    assert posture.interfaces(str(net)) == ["eth0", "lo"]
    mounts = tmp_path / "mounts"
    mounts.write_text("overlay / overlay ro 0 0\n/dev/sda1 /home/trader/.config ext4 ro 0 0\n"
                      "/dev/sda1 /store ext4 ro 0 0\nproc /proc proc rw 0 0\n")
    assert posture.home_mounts(str(mounts)) == ["/home/trader/.config", "/store"]


def test_forbidden_env_names_ignore_case_and_cover_model_keys(monkeypatch):
    monkeypatch.setenv("alpaca_api_key_id", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("MMR_WEB_TOKEN", "x")
    names = posture.forbidden_env_names()
    assert {"alpaca_api_key_id", "ANTHROPIC_API_KEY", "MMR_WEB_TOKEN"} <= set(names)


def test_an_unreadable_proc_status_reports_unknown_not_zero(monkeypatch):
    monkeypatch.setattr(posture, "_proc_status", lambda: {})
    out = posture.read_posture()
    assert out["cap_eff"] == "" and out["seccomp"] is None and out["no_new_privs"] is None
    monkeypatch.setattr(posture, "_proc_status", lambda: {"Seccomp": "two", "NoNewPrivs": "1"})
    out = posture.read_posture()
    assert out["seccomp"] is None and out["no_new_privs"] == 1


# ── per-interface state for the fallback tunnel devices (ruling 33) ─────────

def _net_dir(tmp_path, devices):
    net = tmp_path / "net"
    for name, (operstate, flags) in devices.items():
        (net / name).mkdir(parents=True)
        if operstate is not None:
            (net / name / "operstate").write_text(operstate + "\n")
        if flags is not None:
            (net / name / "flags").write_text(flags + "\n")
    return net


def test_interface_state_reads_operstate_flags_and_addresses(tmp_path):
    net = _net_dir(tmp_path, {"lo": ("unknown", "0x9"), "tunl0": ("down", "0x80")})
    if_inet6 = tmp_path / "if_inet6"
    if_inet6.write_text("00000000000000000000000000000001 01 80 10 80       lo\n")
    states = posture.interface_states(["lo", "tunl0"], net_dir=str(net), if_inet6_path=str(if_inet6),
                                      ipv4_reader=lambda name: name == "lo")
    assert states["tunl0"] == {"operstate": "down", "flags": 0x80, "ipv4": False, "ipv6": False}
    assert states["lo"] == {"operstate": "unknown", "flags": 0x9, "ipv4": True, "ipv6": True}


def test_unreadable_interface_state_is_unknown_never_clean(tmp_path):
    net = _net_dir(tmp_path, {"tunl0": (None, "junk")})
    states = posture.interface_states(["tunl0"], net_dir=str(net), if_inet6_path=str(tmp_path / "none"),
                                      ipv4_reader=lambda name: None)
    assert states["tunl0"] == {"operstate": None, "flags": None, "ipv4": None, "ipv6": None}


def test_the_ipv4_reader_reports_unknown_for_a_device_it_cannot_ask():
    assert posture.has_ipv4_address("nosuchif0") is None
    assert posture.has_ipv4_address("x" * 16) is None       # too long: never truncated onto another device
    assert posture.has_ipv4_address("") is None


def test_read_posture_reports_interface_state():
    out = posture.read_posture()
    assert isinstance(out["interface_state"], dict)


# ── bytes that are not UTF-8 are unknown, never a crash (Task 6 review) ────

_NOT_UTF8 = b"\xff\xfe overlay / overlay ro 0 0\n"


def test_non_utf8_proc_files_read_as_unknown(tmp_path):
    bad = tmp_path / "bad"
    bad.write_bytes(_NOT_UTF8)
    assert posture._proc_status(str(bad)) == {}
    assert posture.home_mounts(str(bad)) == [posture.UNREADABLE]
    assert posture.root_read_only(str(bad)) is False
    assert posture.pids_max((str(bad),)) == ""
    assert posture.ipv6_devices(str(bad)) is None


def test_non_utf8_interface_files_read_as_unknown(tmp_path):
    net = tmp_path / "net"
    (net / "tunl0").mkdir(parents=True)
    (net / "tunl0" / "operstate").write_bytes(b"\xffdown")
    (net / "tunl0" / "flags").write_bytes(b"\xff0x80")
    states = posture.interface_states(["tunl0"], net_dir=str(net), if_inet6_path=str(net / "tunl0" / "flags"),
                                      ipv4_reader=lambda name: False)
    assert states["tunl0"] == {"operstate": None, "flags": None, "ipv4": False, "ipv6": None}

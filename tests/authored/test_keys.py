"""SP3 Plan 1 Task 1: authored keys and digests (spec section 3)."""
from __future__ import annotations

import hashlib

import pytest

from trader.authored.keys import (
    authored_key, is_authored_key, parse_authored_key, source_digest,
)

SOURCE = b"class X(AuthoredStrategy):\n    pass\n"
HEX = hashlib.sha256(SOURCE).hexdigest()


def test_source_digest_is_prefixed_sha256_hex():
    assert source_digest(SOURCE) == f"sha256:{HEX}"


def test_authored_key_round_trips():
    key = authored_key(f"sha256:{HEX}", "OpeningRangeBreakout")
    assert key == f"authored:sha256:{HEX}:OpeningRangeBreakout"
    assert parse_authored_key(key) == (f"sha256:{HEX}", "OpeningRangeBreakout")
    assert is_authored_key(key)


def test_authored_key_refuses_a_bad_digest():
    with pytest.raises(ValueError):
        authored_key("md5:" + HEX, "X")


def test_authored_key_refuses_a_bad_class_name():
    with pytest.raises(ValueError):
        authored_key(f"sha256:{HEX}", "1Bad")


def test_parse_refuses_a_non_authored_key():
    with pytest.raises(ValueError):
        parse_authored_key("strategies/orb.py:OpeningRangeBreakout")


def test_is_authored_key_is_false_for_a_catalogue_key():
    assert not is_authored_key("strategies/orb.py:OpeningRangeBreakout")
    assert not is_authored_key(f"authored:md5:{HEX}:X")


DIGEST = f"sha256:{HEX}"


@pytest.mark.parametrize("digest", [f"{DIGEST}\n", f" {DIGEST}"])
def test_authored_key_refuses_a_digest_with_stray_whitespace(digest):
    with pytest.raises(ValueError):
        authored_key(digest, "X")


@pytest.mark.parametrize("class_name", ["X\n", " X"])
def test_authored_key_refuses_a_class_name_with_stray_whitespace(class_name):
    with pytest.raises(ValueError):
        authored_key(DIGEST, class_name)


@pytest.mark.parametrize("key", [
    f"authored:{DIGEST}:X\n",
    f" authored:{DIGEST}:X",
    f"authored:{DIGEST}: X",
    f"authored:{DIGEST}\n:X",
])
def test_a_key_with_stray_whitespace_is_not_an_authored_key(key):
    assert not is_authored_key(key)
    with pytest.raises(ValueError):
        parse_authored_key(key)


@pytest.mark.parametrize("digest, class_name", [
    (None, "X"), (DIGEST.encode(), "X"), (DIGEST, None), (DIGEST, b"X"),
])
def test_authored_key_refuses_a_non_string_with_value_error(digest, class_name):
    with pytest.raises(ValueError):
        authored_key(digest, class_name)


@pytest.mark.parametrize("key", [None, f"authored:{DIGEST}:X".encode(), 7])
def test_parse_refuses_a_non_string_with_value_error(key):
    with pytest.raises(ValueError):
        parse_authored_key(key)
    assert not is_authored_key(key)

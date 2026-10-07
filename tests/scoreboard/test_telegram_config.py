import os

import pytest

from tests.scoreboard.telegram_fakes import FAKE_TOKEN, token_file
from trader.scoreboard.telegram_config import TelegramConfigError, load_telegram_config


def test_missing_or_disabled_section_means_off():
    assert load_telegram_config(None) is None and load_telegram_config({"enabled": False}) is None
    assert load_telegram_config({}) is None


def test_disabled_section_ignores_unset_fields():
    assert load_telegram_config({"enabled": False, "chat_id": None, "token_secret_file": ""}) is None


@pytest.mark.parametrize("section", [
    {"enabled": True}, {"enabled": True, "chat_id": None, "token_secret_file": "x"},
    {"enabled": True, "chat_id": 0, "token_secret_file": "x"},
    {"enabled": True, "chat_id": True, "token_secret_file": "x"},
    {"enabled": True, "chat_id": "abc", "token_secret_file": "x"},
    {"enabled": True, "chat_id": 5, "token_secret_file": ""},
    {"enabled": "yes", "chat_id": 5, "token_secret_file": "x"},
    {"enabled": True, "chat_id": 5, "token_secret_file": "x", "chatid": 1},
    {"enabled": False, "chatid": 1},
    ["not", "a", "mapping"],
])
def test_enabled_but_incomplete_or_malformed_fails_loudly(section):
    with pytest.raises(TelegramConfigError):
        load_telegram_config(section)


def _section(path, chat=5):
    return {"enabled": True, "chat_id": chat, "token_secret_file": str(path)}


def test_token_file_must_exist(tmp_path):
    with pytest.raises(TelegramConfigError, match="does not exist"):
        load_telegram_config(_section(tmp_path / "missing"))


def test_token_file_must_not_be_a_symlink(tmp_path):
    link = tmp_path / "link.token"
    link.symlink_to(token_file(tmp_path))
    with pytest.raises(TelegramConfigError, match="symlink"):
        load_telegram_config(_section(link))


def test_token_file_must_be_mode_0600(tmp_path):
    with pytest.raises(TelegramConfigError, match="0600"):
        load_telegram_config(_section(token_file(tmp_path, mode=0o644)))


def test_token_file_must_not_be_empty(tmp_path):
    with pytest.raises(TelegramConfigError, match="empty"):
        load_telegram_config(_section(token_file(tmp_path, content="")))


def test_token_file_must_look_like_a_bot_token(tmp_path):
    with pytest.raises(TelegramConfigError) as exc:
        load_telegram_config(_section(token_file(tmp_path, content="not a token at all")))
    assert "not a token" not in str(exc.value)


def test_a_directory_is_not_a_token_file(tmp_path):
    os.chmod(tmp_path, 0o700)
    with pytest.raises(TelegramConfigError):
        load_telegram_config(_section(tmp_path))


def test_valid_config_reads_the_token_and_strips_whitespace(tmp_path):
    config = load_telegram_config(_section(token_file(tmp_path)))
    assert config.token == FAKE_TOKEN and config.chat_id == "5"


def test_config_repr_and_errors_never_contain_the_token(tmp_path):
    config = load_telegram_config(_section(token_file(tmp_path)))
    assert FAKE_TOKEN not in repr(config) and FAKE_TOKEN not in str(config)
    bad = token_file(tmp_path, content=FAKE_TOKEN + " trailing junk", name="bad.token")
    with pytest.raises(TelegramConfigError) as exc:
        load_telegram_config(_section(bad))
    assert FAKE_TOKEN not in str(exc.value)


@pytest.mark.parametrize("chat,expected", [(123456789, "123456789"), ("-1001234567890", "-1001234567890"),
                                           ("@my_channel", "@my_channel")])
def test_accepts_numeric_and_channel_ids(tmp_path, chat, expected):
    assert load_telegram_config(_section(token_file(tmp_path), chat=chat)).chat_id == expected

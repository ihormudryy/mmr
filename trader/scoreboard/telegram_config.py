"""The ``ai_paper.telegram`` gate (Plan 5 ruling 17).

Off unless ``enabled: true``. Enabled with a bad chat id or token file stops
startup. No error message, repr or log line contains the token.
"""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

KEYS = frozenset({"enabled", "chat_id", "token_secret_file"})
_TOKEN = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")
_NUMERIC_CHAT = re.compile(r"^-?[1-9]\d*$")
_CHANNEL_CHAT = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{4,31}$")


class TelegramConfigError(Exception):
    """The telegram section is invalid; the message names the key, never the token."""


@dataclass(frozen=True)
class TelegramConfig:
    chat_id: str
    token: str = field(repr=False)

    def __str__(self) -> str:
        return f"TelegramConfig(chat_id={self.chat_id!r})"


def _chat_id(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        raise TelegramConfigError("ai_paper.telegram.chat_id must be a numeric chat id or an @channel name")
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or not (_NUMERIC_CHAT.fullmatch(value) or _CHANNEL_CHAT.fullmatch(value)):
        raise TelegramConfigError("ai_paper.telegram.chat_id must be a numeric chat id or an @channel name")
    return value


def _read_token(raw_path: Any) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise TelegramConfigError("ai_paper.telegram.token_secret_file must name a file")
    path = Path(raw_path).expanduser()
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise TelegramConfigError(f"ai_paper.telegram.token_secret_file {path} does not exist") from None
    if stat.S_ISLNK(info.st_mode):
        raise TelegramConfigError(f"ai_paper.telegram.token_secret_file {path} is a symlink; use the file itself")
    if not stat.S_ISREG(info.st_mode):
        raise TelegramConfigError(f"ai_paper.telegram.token_secret_file {path} is not a regular file")
    if info.st_mode & 0o077:
        raise TelegramConfigError(
            f"ai_paper.telegram.token_secret_file {path} must be mode 0600 (owner only), "
            f"is {stat.S_IMODE(info.st_mode):04o}")
    token = path.read_text().strip()
    if not token:
        raise TelegramConfigError(f"ai_paper.telegram.token_secret_file {path} is empty")
    if not _TOKEN.fullmatch(token):
        raise TelegramConfigError(f"ai_paper.telegram.token_secret_file {path} does not hold a bot token "
                                  "(expected <digits>:<secret>)")
    return token


def load_telegram_config(section: Optional[Mapping[str, Any]]) -> Optional[TelegramConfig]:
    """None when off. Unknown keys and a non-bool ``enabled`` fail even when off (a typo is not silence)."""
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise TelegramConfigError("ai_paper.telegram must be a mapping")
    unknown = sorted(set(section) - KEYS)
    if unknown:
        raise TelegramConfigError(f"ai_paper.telegram.{unknown[0]}: unknown key")
    enabled = section.get("enabled", False)
    if type(enabled) is not bool:
        raise TelegramConfigError("ai_paper.telegram.enabled must be true or false")
    if not enabled:
        return None
    return TelegramConfig(chat_id=_chat_id(section.get("chat_id")), token=_read_token(section.get("token_secret_file")))

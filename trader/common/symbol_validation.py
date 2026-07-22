"""Symbol token validation for watchlist / universe adds.

Two layers:
1. **Format** — reject obvious trash before any IB call (empty, punctuation,
   spaces-inside-token after split, absurd length).
2. **Existence** — callers must resolve via IB (``discover_instrument`` /
   ``add_universe_symbols``) and refuse unresolved symbols; this module only
   owns the format gate so garbage never reaches the broker.
"""
from __future__ import annotations

import re

# IB equity tickers: alnum start; alnum / '.' / '-' after (BRK.B, BF-B).
# Numeric listings (HK/JP) allowed. Tokens are already split on whitespace
# by callers, so spaces are not part of a single token here.
_SYMBOL_FORMAT_RE = re.compile(r'^[A-Z0-9][A-Z0-9.\-]{0,31}$')


class SymbolValidationError(ValueError):
    """Raised when one or more symbol tokens fail the format gate."""


def normalize_symbol(raw: str) -> str:
    return (raw or '').strip().upper()


def validate_symbol_format(symbol: str) -> str:
    """Return a normalized symbol or raise ``SymbolValidationError``."""
    sym = normalize_symbol(symbol)
    if not sym:
        raise SymbolValidationError('empty symbol')
    if len(sym) > 32:
        raise SymbolValidationError(f'symbol too long: {sym[:40]!r}')
    if not _SYMBOL_FORMAT_RE.match(sym):
        raise SymbolValidationError(
            f'invalid symbol {sym!r} — use a ticker like AAPL, BRK.B, or 0700')
    if '..' in sym or sym.startswith('.') or sym.endswith('.') \
            or sym.startswith('-') or sym.endswith('-'):
        raise SymbolValidationError(f'invalid symbol {sym!r}')
    return sym


def validate_symbol_list(symbols: list[str]) -> list[str]:
    """Normalize + format-check a list; preserve order, drop exact dupes.

    Raises ``SymbolValidationError`` naming every bad token (fail loudly).
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    bad: list[str] = []
    for raw in symbols:
        try:
            sym = validate_symbol_format(raw)
        except SymbolValidationError as exc:
            bad.append(str(exc))
            continue
        if sym in seen:
            continue
        seen.add(sym)
        cleaned.append(sym)
    if bad:
        raise SymbolValidationError('; '.join(bad))
    if not cleaned:
        raise SymbolValidationError('no symbols given')
    return cleaned

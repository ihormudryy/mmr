"""REST quotes from TwelveData's /quote endpoint (no bid/ask on REST)."""

from typing import Sequence

from trader.data_providers.capabilities import make_quote
from trader.data_providers.errors import ProviderEntitlementError, ProviderError, ProviderRateLimited

CHUNK_SIZE = 120


class TwelveDataQuotes:
    def __init__(self, client):
        self._client = client

    def quotes(self, symbols: Sequence[str]) -> list[dict]:
        results: list[dict] = []
        for start in range(0, len(symbols), CHUNK_SIZE):
            chunk = [s.strip().upper() for s in symbols[start:start + CHUNK_SIZE]]
            payloads = self._payloads_by_symbol(chunk)
            results.extend(_to_quote(symbol, payloads.get(symbol) or {}) for symbol in chunk)
        return results

    def _payloads_by_symbol(self, chunk: list[str]) -> dict:
        raw = self._client.quote(symbol=','.join(chunk)).as_json()
        if isinstance(raw, dict) and raw.get('status') == 'error':
            _raise_if_request_failed(raw, single_symbol=len(chunk) == 1)
            return {chunk[0]: raw}
        # One symbol comes back as a flat dict, several as {SYMBOL: {...}}.
        if isinstance(raw, dict) and 'symbol' in raw and len(chunk) == 1:
            return {chunk[0]: raw}
        return raw if isinstance(raw, dict) else {}


def _raise_if_request_failed(error: dict, single_symbol: bool) -> None:
    """Key, plan and rate-limit errors fail the whole call. A bad single symbol is reported per symbol."""
    message = f"twelvedata /quote failed: {error.get('message', 'unknown error')}"
    code = str(error.get('code', ''))
    if code in ('401', '403'):
        raise ProviderEntitlementError(message)
    if code == '429':
        raise ProviderRateLimited(message)
    if not single_symbol:
        raise ProviderError(message)


def _to_float(value) -> float:
    if value in (None, ''):
        return float('nan')
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('nan')


def _is_error(payload: dict) -> bool:
    return payload.get('status') == 'error' or 'code' in payload


def _to_quote(symbol: str, payload: dict) -> dict:
    if not payload:
        return make_quote(symbol, feed='twelvedata', error=f'twelvedata returned no quote for {symbol}')
    if _is_error(payload):
        message = payload.get('message', 'unknown error')
        return make_quote(symbol, feed='twelvedata', error=f'twelvedata error for {symbol}: {message}')
    return make_quote(
        payload.get('symbol', symbol),
        time=payload.get('datetime') or '',
        last=_to_float(payload.get('close')),
        open=_to_float(payload.get('open')),
        high=_to_float(payload.get('high')),
        low=_to_float(payload.get('low')),
        close=_to_float(payload.get('close')),
        volume=_to_float(payload.get('volume')),
        previous_close=_to_float(payload.get('previous_close')),
        change=_to_float(payload.get('change')),
        change_pct=_to_float(payload.get('percent_change')),
        exchange=payload.get('exchange') or '',
        currency=payload.get('currency') or '',
        name=payload.get('name') or '',
        feed='twelvedata',
    )

# Free Data Providers — Phase 3a: Quotes, Movers, News

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route REST quotes, market movers and news through the provider registry, add Alpaca as the free provider for all three, filter junk out of stock movers, and make Alpaca the default for `movers` and `news`.

**Architecture:** Three new capabilities (`QUOTES`, `MOVERS`, `NEWS`) with small protocols and one shared output shape each. The existing TwelveData/Massive code in `trader/sdk.py` moves into adapters under `trader/data_providers/{twelvedata,massive}/`; Alpaca implementations live in `trader/data_providers/alpaca/`. The SDK asks the registry instead of branching on `source`. A cached Alpaca asset list supplies company names and lets the movers filter drop warrants, rights and units.

**Tech Stack:** Python 3.12, pandas, `requests`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-04-free-data-providers-design.md` (§3, §4.1–4.3, §4.5, §5 "Movers filter"/"Labels", §6, §7). Index: `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`. Phase 1–2 plan (for conventions): `docs/superpowers/plans/2026-10-04-free-data-providers-01-02-history.md`.

## Global Constraints

- Branch `feat/free-data-providers`. One local commit per task. Never push. Every commit message ends with a blank line then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Use the project venv for every command (`.venv/bin/pytest`, `.venv/bin/python`, `.venv/bin/mmr`). Full suite: `.venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py -p no:cacheprovider`. Baseline at the start of 3a: 4111 passed, 5 skipped, 0 failed.
- No network in normal tests. Live tests are gated by env `MMR_LIVE_TESTS=1` **and** the needed keys (see `tests/data_providers/test_live_alpaca.py`).
- Fail loudly: auth, entitlement and rate-limit failures raise a `ProviderError` subclass; never return empty data for them. Unknown symbols are reported per symbol, never guessed.
- Quote dict shape: exactly the keys in `QUOTE_FIELDS`; numeric fields are `float` (NaN when unknown); `error` is `''` when the quote is good.
- Movers frame shape: first columns exactly `MOVER_COLUMNS`, in that order; sorted by `change_pct` descending for gainers, ascending for losers. Extra provider columns may follow.
- News item shape: exactly the keys in `NEWS_FIELDS`; `tickers` and `insights` are lists; `sentiment` is `''` when the provider has none (Alpaca never has sentiment — do not invent any).
- `movers` and `news` do **not** inherit `default_data_source` (CLAUDE.md rule); only `history` and `quotes` do. Per-capability `data_providers:` overrides apply to all.
- Alpaca: market data at `https://data.alpaca.markets`; the asset list at `https://paper-api.alpaca.markets/v2/assets` (paper keys). Quotes use `feed=iex` and are labelled `feed='iex'`. Screener movers accept `top` ≤ 50.
- Movers filter (stocks only): drop rows with `close < min_price` (default 1.0) or unknown price, and rows whose asset name marks a warrant, right or unit (pattern in Task 7). Crypto movers are not filtered.
- CLI handlers for `snapshot`, `snapshot-batch`, `movers` and `news` catch `ProviderError` and print it with `print_status(str(ex), success=False)` — a missing key must show the env var to set, never a traceback (phase 1 precedent: `_handle_data_download`).
- Comments only where the code is not obvious. Plain, intent-revealing names.

## Review Focus

1. **Bad symbols in a batch** (`snapshot-batch AAPL ZZZZQ "BRK B" --source alpaca`): expect AAPL and BRK.B quotes plus a ZZZZQ row whose `error` says Alpaca has no snapshot; single `snapshot ZZZZQ --source alpaca` raises a clear `ValueError`. → Task 3 tests `test_unknown_symbol_gets_error_row`, `test_invalid_symbol_gets_error_row_without_request`, Task 2 test `test_snapshot_raises_on_error_quote`.
2. **Junk-heavy movers** (on 2026-10-02, 29 of Alpaca's top 50 gainers were under $1 and 21 were warrants): expect none of them in the output, fewer rows than `--num` is fine. → Task 7 tests `test_drops_sub_dollar_and_unknown_price`, `test_drops_warrants_rights_units`, `test_keeps_unit_corporation`.
3. **Existing users with `default_data_source: twelvedata`** in their live config: `movers` and `news` must not silently move to TwelveData (Pro+ only → 403). → Task 1 test `test_only_listed_capabilities_inherit_default_data_source`, Task 8 test `test_movers_default_ignores_default_data_source`.
4. **Asset cache unwritable or stale** (read-only Docker filesystem; cache older than 24 h): expect it to still work from memory, and to refresh after 24 h. → Task 7 tests `test_unwritable_cache_still_returns_assets`, `test_stale_cache_is_refreshed`.
5. **Unsupported market for a source** (`movers --market indices --source alpaca`): expect `CapabilityNotSupported` naming `massive`. → Task 8 test `test_unsupported_market_raises`.

## Known findings (from phase 1–2; do not fix here unless a task says so)

- `ProviderRegistry.default_source` raises a bare `KeyError` when a capability has no builtin default — **Task 1 fixes this** because 3a adds capabilities.
- Open minors are listed in `docs/AUDIT_ROADMAP.md` ("Free data providers — open minors").

---

## File Structure

```
trader/data_providers/
├── capabilities.py              # + QUOTES/MOVERS/NEWS, QUOTE_FIELDS, MOVER_COLUMNS, NEWS_FIELDS,
│                                #   make_quote, make_news_item, QuoteProvider, MoversProvider, NewsProvider   (Task 1)
├── registry.py                  # + inherits_global_default; clear error when no default                   (Task 1)
├── builtin.py                   # + specs/defaults for new capabilities, source_choices(), alpaca_asset_directory()  (Tasks 1-8)
├── movers_filter.py             # filter_stock_movers                                                     (Task 7)
├── twelvedata/__init__.py, quotes.py, movers.py                                                           (Tasks 2, 4)
├── massive/__init__.py, movers.py, news.py                                                                (Tasks 4, 5)
└── alpaca/quotes.py, news.py, movers.py, assets.py                                                        (Tasks 3, 6, 7, 8)

tests/data_providers/
├── test_capabilities_3a.py      (Task 1)
├── test_twelvedata_quotes.py    (Task 2 — migrated from tests/test_twelvedata_sdk.py)
├── test_sdk_quotes.py           (Task 2)
├── test_alpaca_quotes.py        (Task 3)
├── test_movers_adapters.py      (Task 4)
├── test_news_adapters.py        (Task 5)
├── test_alpaca_news.py          (Task 6)
├── test_alpaca_assets.py, test_movers_filter.py   (Task 7)
├── test_alpaca_movers.py, test_sdk_movers_news.py (Task 8)
├── fixtures/alpaca_snapshots_aapl.json, alpaca_news_aapl.json, alpaca_movers_stocks.json, alpaca_assets_sample.json
└── test_live_alpaca.py          (extended in Task 9)

Modified: trader/sdk.py (snapshot, snapshot_batch, movers, movers_detail, news, news_detail, + _provider helpers),
trader/mmr_cli.py (snapshot/snapshot-batch/movers/news parsers + _handle_movers/_handle_news),
tests/test_twelvedata_sdk.py (snapshot tests move out), CLAUDE.md, docs/superpowers/plans/…-00-index.md
```

---

### Task 1: Capabilities, shapes, registry inheritance rule, SDK helper

**Files:**
- Modify: `trader/data_providers/capabilities.py`, `trader/data_providers/registry.py`, `trader/data_providers/builtin.py`, `trader/data_providers/__init__.py`, `trader/sdk.py` (add two helper methods near `_massive_client`, ≈L2938)
- Test: `tests/data_providers/test_capabilities_3a.py`

**Interfaces:**
- Consumes: phase 1–2 registry (`ProviderRegistry(config, specs, defaults)`, `from_config`, `get`, `default_source`, `sources_for`).
- Produces:
  - `Capability.QUOTES = 'quotes'`, `Capability.MOVERS = 'movers'`, `Capability.NEWS = 'news'`.
  - `QUOTE_FIELDS`, `MOVER_COLUMNS`, `NEWS_FIELDS` tuples; `make_quote(symbol: str, **fields) -> dict`; `make_news_item(**fields) -> dict`.
  - Protocols `QuoteProvider.quotes(symbols: Sequence[str]) -> list[dict]`; `MoversProvider.markets: frozenset[str]` + `.movers(market: str, direction: str) -> pd.DataFrame`; `NewsProvider.news(ticker: Optional[str], limit: int) -> list[dict]`.
  - `ProviderRegistry(config, specs, defaults, inherits_global_default: Optional[Iterable[Capability]] = None)` — `None` means every capability inherits (keeps phase 1–2 behaviour and tests).
  - `builtin.INHERITS_DEFAULT_DATA_SOURCE = frozenset({Capability.HISTORY, Capability.QUOTES})`; `builtin.source_choices(capability) -> list[str]`.
  - `MMR._provider(capability, source=None)` and `MMR._provider_default(capability) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_capabilities_3a.py
import math

import pandas as pd
import pytest

from trader.data_providers.capabilities import (
    MOVER_COLUMNS, NEWS_FIELDS, QUOTE_FIELDS, Capability, MoversProvider, NewsProvider,
    QuoteProvider, make_news_item, make_quote,
)
from trader.data_providers.errors import CapabilityNotSupported
from trader.data_providers.registry import ProviderRegistry, ProviderSpec


def test_new_capability_values():
    assert [c.value for c in (Capability.QUOTES, Capability.MOVERS, Capability.NEWS)] == ['quotes', 'movers', 'news']


def test_make_quote_fills_every_field():
    quote = make_quote('aapl', last=1.5, feed='iex')
    assert tuple(quote) == QUOTE_FIELDS
    assert quote['symbol'] == 'AAPL'
    assert quote['last'] == 1.5
    assert math.isnan(quote['bid']) and math.isnan(quote['change_pct'])
    assert quote['error'] == '' and quote['feed'] == 'iex' and quote['name'] == ''


def test_make_quote_rejects_unknown_field():
    with pytest.raises(TypeError, match='unknown quote field'):
        make_quote('AAPL', lastt=1.0)


def test_make_news_item_defaults():
    item = make_news_item(title='t', tickers=['AAPL'])
    assert tuple(item) == NEWS_FIELDS
    assert item['sentiment'] == '' and item['insights'] == [] and item['tickers'] == ['AAPL']


def test_mover_columns():
    assert MOVER_COLUMNS == ('ticker', 'name', 'close', 'volume', 'change', 'change_pct', 'provider', 'note')


def test_protocols_are_structural():
    class Q:
        def quotes(self, symbols):
            return []

    class M:
        markets = frozenset({'stocks'})

        def movers(self, market, direction):
            return pd.DataFrame()

    class N:
        def news(self, ticker, limit):
            return []

    assert isinstance(Q(), QuoteProvider) and isinstance(M(), MoversProvider) and isinstance(N(), NewsProvider)


def _registry(inherits, **config):
    specs = [ProviderSpec('a', (), {Capability.HISTORY: lambda c: 'a', Capability.MOVERS: lambda c: 'a'}),
             ProviderSpec('b', (), {Capability.HISTORY: lambda c: 'b', Capability.MOVERS: lambda c: 'b'})]
    return ProviderRegistry(config, specs, {Capability.HISTORY: 'a', Capability.MOVERS: 'a'},
                            inherits_global_default=inherits)


def test_only_listed_capabilities_inherit_default_data_source():
    registry = _registry({Capability.HISTORY}, default_data_source='b')
    assert registry.default_source(Capability.HISTORY) == 'b'
    assert registry.default_source(Capability.MOVERS) == 'a'


def test_data_providers_override_applies_even_without_inheritance():
    registry = _registry({Capability.HISTORY}, data_providers={'movers': 'b'})
    assert registry.default_source(Capability.MOVERS) == 'b'


def test_missing_builtin_default_raises_provider_error():
    registry = ProviderRegistry({}, [ProviderSpec('a', (), {Capability.NEWS: lambda c: 'a'})], {})
    with pytest.raises(CapabilityNotSupported, match='news'):
        registry.default_source(Capability.NEWS)


def test_builtin_inheritance_set_and_choices():
    from trader.data_providers.builtin import INHERITS_DEFAULT_DATA_SOURCE, source_choices
    assert INHERITS_DEFAULT_DATA_SOURCE == frozenset({Capability.HISTORY, Capability.QUOTES})
    assert source_choices(Capability.HISTORY) == ['alpaca', 'massive', 'twelvedata']


def test_sdk_provider_helper_uses_container_config():
    from unittest.mock import MagicMock
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    mmr._container = MagicMock()
    mmr._container.config.return_value = {'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'}
    assert mmr._provider_default(Capability.HISTORY) == 'alpaca'
    assert type(mmr._provider(Capability.HISTORY)).__name__ == 'AlpacaHistoryProvider'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_capabilities_3a.py -v`
Expected: FAIL with `ImportError: cannot import name 'MOVER_COLUMNS'`

- [ ] **Step 3: Implement**

Append to `trader/data_providers/capabilities.py` (add `QUOTES`, `MOVERS`, `NEWS` members to the existing `Capability` enum; add `from typing import Any, Optional, Sequence` to the imports):

```python
class Capability(str, Enum):
    HISTORY = 'history'
    QUOTES = 'quotes'
    MOVERS = 'movers'
    NEWS = 'news'
```

```python
QUOTE_FIELDS: tuple[str, ...] = (
    'symbol', 'time', 'last', 'bid', 'ask', 'bid_size', 'ask_size', 'open', 'high', 'low', 'close',
    'volume', 'previous_close', 'change', 'change_pct', 'exchange', 'currency', 'name', 'feed', 'error',
)
_QUOTE_TEXT_FIELDS = frozenset({'symbol', 'time', 'exchange', 'currency', 'name', 'feed', 'error'})

MOVER_COLUMNS: tuple[str, ...] = (
    'ticker', 'name', 'close', 'volume', 'change', 'change_pct', 'provider', 'note',
)

NEWS_FIELDS: tuple[str, ...] = (
    'id', 'published', 'title', 'summary', 'url', 'author', 'source', 'tickers', 'sentiment', 'insights',
)
_NEWS_LIST_FIELDS = frozenset({'tickers', 'insights'})


def make_quote(symbol: str, **fields: Any) -> dict:
    unknown = set(fields) - set(QUOTE_FIELDS)
    if unknown:
        raise TypeError(f'unknown quote field(s): {sorted(unknown)}')
    quote = {name: ('' if name in _QUOTE_TEXT_FIELDS else float('nan')) for name in QUOTE_FIELDS}
    quote.update(fields)
    quote['symbol'] = symbol.strip().upper()
    return quote


def make_news_item(**fields: Any) -> dict:
    unknown = set(fields) - set(NEWS_FIELDS)
    if unknown:
        raise TypeError(f'unknown news field(s): {sorted(unknown)}')
    item = {name: ([] if name in _NEWS_LIST_FIELDS else '') for name in NEWS_FIELDS}
    item.update(fields)
    return item


@runtime_checkable
class QuoteProvider(Protocol):
    def quotes(self, symbols: Sequence[str]) -> list[dict]:
        """One make_quote() dict per requested symbol, in request order; failures set `error`."""
        ...


@runtime_checkable
class MoversProvider(Protocol):
    markets: frozenset

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        """Frame starting with MOVER_COLUMNS, sorted by change_pct for `direction`."""
        ...


@runtime_checkable
class NewsProvider(Protocol):
    def news(self, ticker: Optional[str], limit: int) -> list[dict]:
        """Newest first, at most `limit` make_news_item() dicts; ticker None means general news."""
        ...
```

In `trader/data_providers/registry.py`:

```python
    def __init__(
        self,
        config: Mapping[str, Any],
        specs: Iterable[ProviderSpec],
        defaults: Mapping[Capability, str],
        inherits_global_default: Optional[Iterable[Capability]] = None,
    ):
        self._config = config
        self._specs = {spec.name: spec for spec in specs}
        self._defaults = dict(defaults)
        self._inherits_global_default = (frozenset(Capability) if inherits_global_default is None
                                         else frozenset(inherits_global_default))

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> 'ProviderRegistry':
        from trader.data_providers.builtin import BUILTIN_DEFAULTS, INHERITS_DEFAULT_DATA_SOURCE, builtin_specs
        return cls(config, builtin_specs(), BUILTIN_DEFAULTS, INHERITS_DEFAULT_DATA_SOURCE)

    def default_source(self, capability: Capability) -> str:
        overrides = self._config.get('data_providers') or {}
        if overrides.get(capability.value):
            return overrides[capability.value]
        global_default = self._config.get('default_data_source')
        if capability in self._inherits_global_default and global_default in self.sources_for(capability):
            return global_default
        if capability not in self._defaults:
            raise CapabilityNotSupported(capability.value, '(no default)', self.sources_for(capability))
        return self._defaults[capability]
```

In `trader/data_providers/builtin.py` add:

```python
INHERITS_DEFAULT_DATA_SOURCE = frozenset({Capability.HISTORY, Capability.QUOTES})


def source_choices(capability: Capability) -> list[str]:
    return sorted(spec.name for spec in builtin_specs() if capability in spec.builders)
```

and rewrite `history_source_choices` as `return source_choices(Capability.HISTORY) + [IB_HISTORY_SOURCE]`.

Export the new names from `trader/data_providers/__init__.py` (`MOVER_COLUMNS`, `NEWS_FIELDS`, `QUOTE_FIELDS`, `MoversProvider`, `NewsProvider`, `QuoteProvider`, `make_news_item`, `make_quote`) and add them to `__all__`.

In `trader/sdk.py`, just above the `_massive_client` property:

```python
    def _provider(self, capability, source: Optional[str] = None):
        from trader.data_providers import ProviderRegistry
        return ProviderRegistry.from_config(self._container.config()).get(capability, source)

    def _provider_default(self, capability) -> str:
        from trader.data_providers import ProviderRegistry
        return ProviderRegistry.from_config(self._container.config()).default_source(capability)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/data_providers -q -p no:cacheprovider`
Expected: all pass (phase 1–2 registry tests still pass because `inherits_global_default=None` keeps the old rule).

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers trader/sdk.py tests/data_providers/test_capabilities_3a.py
git commit -m "feat(providers): add quotes, movers and news capabilities

Only history and quotes inherit default_data_source; movers and news keep
their own default (CLAUDE.md rule). A capability without a default now
raises CapabilityNotSupported instead of KeyError.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: TwelveData quotes adapter; SDK snapshots through the registry

**Files:**
- Create: `trader/data_providers/twelvedata/__init__.py` (empty), `trader/data_providers/twelvedata/quotes.py`
- Modify: `trader/data_providers/builtin.py` (twelvedata spec gets `Capability.QUOTES`; `BUILTIN_DEFAULTS[QUOTES] = 'twelvedata'` for now), `trader/sdk.py` (`snapshot` ≈L2237, `snapshot_batch` ≈L2311), `trader/mmr_cli.py` (snapshot parsers ≈L398–425)
- Move tests: `TestSnapshotTwelveData` and `TestSnapshotBatchTwelveData` from `tests/test_twelvedata_sdk.py` → `tests/data_providers/test_twelvedata_quotes.py`
- Test: `tests/data_providers/test_sdk_quotes.py`

**Interfaces:**
- Consumes: `make_quote`, `Capability.QUOTES`, `MMR._provider`.
- Produces: `TwelveDataQuotes(client)` implementing `QuoteProvider` (`CHUNK_SIZE = 120`); `sdk._quote_to_snapshot(quote) -> dict` and `sdk._quote_to_batch_row(quote) -> dict` (module-level functions); CLI snapshot `--source` choices `['ib'] + source_choices(Capability.QUOTES)`.

- [ ] **Step 1: Write the failing tests**

`tests/data_providers/test_twelvedata_quotes.py` — the five existing TwelveData snapshot tests, re-pointed at the adapter (same stub payloads as in `tests/test_twelvedata_sdk.py:410-503`; copy them, then change only the arrange/act lines as shown):

```python
import math
from unittest.mock import MagicMock

import pytest

from trader.data_providers.twelvedata.quotes import TwelveDataQuotes


class _StubTDPayload:
    def __init__(self, payload):
        self._payload = payload

    def as_json(self):
        return self._payload


AAPL = {'symbol': 'AAPL', 'name': 'Apple Inc', 'exchange': 'NASDAQ', 'currency': 'USD',
        'datetime': '2026-04-17', 'open': '268.0', 'high': '271.0', 'low': '267.5', 'close': '270.19',
        'volume': '41234567', 'previous_close': '270.71', 'change': '-0.51999', 'percent_change': '-0.19209'}
MSFT = dict(AAPL, symbol='MSFT', name='Microsoft', close='420.10')


def _quotes(payload, symbols):
    client = MagicMock()
    client.quote.return_value = _StubTDPayload(payload)
    return TwelveDataQuotes(client).quotes(symbols), client


def test_basic_quote_payload():
    (quote,), _ = _quotes(AAPL, ['AAPL'])
    assert quote['symbol'] == 'AAPL' and quote['last'] == pytest.approx(270.19)
    assert quote['previous_close'] == pytest.approx(270.71)
    assert quote['change'] == pytest.approx(-0.51999)
    assert math.isnan(quote['bid']) and math.isnan(quote['ask'])
    assert quote['exchange'] == 'NASDAQ' and quote['name'] == 'Apple Inc' and quote['feed'] == 'twelvedata'


def test_missing_field_yields_nan():
    (quote,), _ = _quotes({'symbol': 'AAPL'}, ['AAPL'])
    assert math.isnan(quote['last']) and math.isnan(quote['volume'])


def test_batch_uses_comma_join():
    quotes, client = _quotes({'AAPL': AAPL, 'MSFT': MSFT}, ['AAPL', 'MSFT'])
    assert [q['symbol'] for q in quotes] == ['AAPL', 'MSFT']
    assert quotes[1]['last'] == pytest.approx(420.10)
    client.quote.assert_called_once()
    assert client.quote.call_args.kwargs['symbol'] == 'AAPL,MSFT'


def test_chunks_above_120_symbols():
    symbols = [f'S{i}' for i in range(150)]
    _, client = _quotes({}, symbols)
    assert client.quote.call_count == 2
    assert len(client.quote.call_args_list[0].kwargs['symbol'].split(',')) == 120
    assert len(client.quote.call_args_list[1].kwargs['symbol'].split(',')) == 30


def test_missing_symbol_returns_error_row():
    quotes, _ = _quotes({'AAPL': AAPL, 'MISSING': {}}, ['AAPL', 'MISSING'])
    assert quotes[1]['symbol'] == 'MISSING'
    assert math.isnan(quotes[1]['last'])
    assert 'twelvedata returned no quote' in quotes[1]['error']
```

Delete the classes `TestSnapshotTwelveData` and `TestSnapshotBatchTwelveData` from `tests/test_twelvedata_sdk.py` (leave every other class there untouched).

```python
# tests/data_providers/test_sdk_quotes.py
import math
from unittest.mock import MagicMock

import pytest

from trader.data_providers.capabilities import Capability, make_quote
from trader.sdk import MMR


def _mmr_with_quotes(quotes):
    mmr = object.__new__(MMR)
    provider = MagicMock()
    provider.quotes.return_value = quotes
    mmr._provider = MagicMock(return_value=provider)
    return mmr, provider


def test_snapshot_maps_quote_to_legacy_shape():
    mmr, _ = _mmr_with_quotes([make_quote('AAPL', last=2.0, bid=1.9, bid_size=3.0, exchange='NASDAQ',
                                          currency='USD', name='Apple', feed='iex', time='t')])
    snap = mmr.snapshot('AAPL', source='alpaca')
    mmr._provider.assert_called_once_with(Capability.QUOTES, 'alpaca')
    assert snap['symbol'] == 'AAPL' and snap['conId'] == '' and snap['last'] == 2.0
    assert snap['bid'] == 1.9 and snap['bidSize'] == 3.0
    assert math.isnan(snap['lastSize']) and math.isnan(snap['halted'])
    assert snap['feed'] == 'iex' and snap['name'] == 'Apple'


def test_snapshot_raises_on_error_quote():
    mmr, _ = _mmr_with_quotes([make_quote('ZZZZQ', error='alpaca has no snapshot for ZZZZQ')])
    with pytest.raises(ValueError, match='no snapshot for ZZZZQ'):
        mmr.snapshot('ZZZZQ', source='alpaca')


def test_snapshot_batch_keeps_error_rows():
    mmr, _ = _mmr_with_quotes([make_quote('AAPL', last=2.0), make_quote('ZZZZQ', error='nope')])
    rows = mmr.snapshot_batch(['AAPL', 'ZZZZQ'], source='twelvedata')
    assert [r['symbol'] for r in rows] == ['AAPL', 'ZZZZQ']
    assert rows[0]['last'] == 2.0 and rows[0]['error'] == ''
    assert rows[1]['error'] == 'nope' and math.isnan(rows[1]['last'])


def test_cli_snapshot_prints_provider_error(capsys):
    from argparse import Namespace
    from trader.data_providers.errors import ProviderNotConfigured
    from trader.mmr_cli import _handle_snapshot
    mmr = MagicMock()
    mmr.snapshot.side_effect = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')])
    _handle_snapshot(mmr, Namespace(symbol='AAPL', delayed=False, exchange='', currency='', source='alpaca'),
                     'snapshot')
    assert 'ALPACA_API_KEY_ID' in capsys.readouterr().out


def test_cli_snapshot_sources_come_from_registry():
    from trader.mmr_cli import build_parser
    parser = build_parser()
    assert parser.parse_args(['snapshot', 'AAPL', '--source', 'twelvedata']).source == 'twelvedata'
    assert parser.parse_args(['snapshot-batch', 'AAPL', '--source', 'ib']).source == 'ib'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/data_providers/test_twelvedata_quotes.py tests/data_providers/test_sdk_quotes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.twelvedata'`

- [ ] **Step 3: Implement**

```python
# trader/data_providers/twelvedata/quotes.py
"""REST quotes from TwelveData's /quote endpoint (no bid/ask on REST)."""

from typing import Sequence

from trader.data_providers.capabilities import make_quote

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
        # One symbol comes back as a flat dict, several as {SYMBOL: {...}}.
        if isinstance(raw, dict) and 'symbol' in raw and len(chunk) == 1:
            return {chunk[0]: raw}
        return raw if isinstance(raw, dict) else {}


def _to_float(value) -> float:
    if value in (None, ''):
        return float('nan')
    try:
        return float(value)
    except (TypeError, ValueError):
        return float('nan')


def _to_quote(symbol: str, payload: dict) -> dict:
    if not payload:
        return make_quote(symbol, feed='twelvedata', error=f'twelvedata returned no quote for {symbol}')
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
```

`trader/data_providers/builtin.py`: add

```python
def _twelvedata_quotes(config: Mapping[str, Any]):
    from twelvedata import TDClient
    from trader.data_providers.twelvedata.quotes import TwelveDataQuotes
    return TwelveDataQuotes(TDClient(apikey=config['twelvedata_api_key']))
```

register `Capability.QUOTES: _twelvedata_quotes` in the `twelvedata` spec, and set `BUILTIN_DEFAULTS[Capability.QUOTES] = 'twelvedata'` (Task 3 changes it to `'alpaca'`).

`trader/sdk.py` — add two module-level functions (near the top of the module, after imports):

```python
def _quote_to_snapshot(quote: dict) -> dict:
    nan = float('nan')
    return {
        'symbol': quote['symbol'], 'conId': '', 'time': quote['time'],
        'bid': quote['bid'], 'bidSize': quote['bid_size'], 'ask': quote['ask'], 'askSize': quote['ask_size'],
        'last': quote['last'], 'lastSize': nan, 'open': quote['open'], 'high': quote['high'],
        'low': quote['low'], 'close': quote['close'], 'volume': quote['volume'],
        'previous_close': quote['previous_close'], 'change': quote['change'],
        'change_pct': quote['change_pct'], 'halted': nan, 'exchange': quote['exchange'],
        'currency': quote['currency'], 'name': quote['name'], 'feed': quote['feed'],
    }


def _quote_to_batch_row(quote: dict) -> dict:
    keys = ('symbol', 'time', 'bid', 'ask', 'last', 'open', 'high', 'low', 'close', 'volume',
            'previous_close', 'change', 'change_pct', 'exchange', 'currency', 'feed', 'error')
    return {key: quote[key] for key in keys}
```

Replace the `if source == 'twelvedata':` block in `snapshot` with:

```python
        if source != 'ib':
            from trader.data_providers import Capability
            quote = self._provider(Capability.QUOTES, source).quotes([str(symbol)])[0]
            if quote['error']:
                raise ValueError(quote['error'])
            return _quote_to_snapshot(quote)
```

and the `if source == 'twelvedata':` block in `snapshot_batch` with:

```python
        if source != 'ib':
            from trader.data_providers import Capability
            return [_quote_to_batch_row(q) for q in self._provider(Capability.QUOTES, source).quotes(symbols)]
```

Update both docstrings' `source` paragraph to: "'ib' (default) routes via trader_service / IB. Any other value is a registry quotes source (e.g. 'alpaca' — IEX prices, 'twelvedata' — no bid/ask)."

`trader/mmr_cli.py` snapshot and snapshot-batch parsers:

```python
    from trader.data_providers import Capability
    from trader.data_providers.builtin import source_choices
    quote_sources = ['ib'] + source_choices(Capability.QUOTES)
    snap_p.add_argument('--source', choices=quote_sources,
                        default=_src_default(quote_sources, 'ib'),
                        help='Data source (default: ib). Non-IB sources use REST and need no '
                             'trader_service; alpaca = IEX prices, twelvedata = no bid/ask.')
```

(same `choices`/`default` for `snap_batch_p`; put the two imports once near the top of `build_parser` if that is the file's pattern, otherwise just before the snapshot parser).

In `dispatch` (`trader/mmr_cli.py` ≈L2146–2159), move the bodies of the `snapshot`/`snap` and `snapshot-batch` branches into one new function and call it from both branches (`_handle_snapshot(mmr, args, cmd)`):

```python
def _handle_snapshot(mmr: MMR, args: argparse.Namespace, cmd: str):
    from trader.data_providers import ProviderError
    source = getattr(args, 'source', 'ib')
    suffix = f' ({source})' if source != 'ib' else ''
    try:
        if cmd == 'snapshot-batch':
            results = mmr.snapshot_batch(args.symbols, exchange=args.exchange,
                                         currency=args.currency, source=source)
            print(json.dumps({'data': results, 'title': 'Snapshots' + suffix}, default=str))
        else:
            result = mmr.snapshot(args.symbol, delayed=args.delayed, exchange=args.exchange,
                                  currency=args.currency, source=source)
            print_dict(result, title=f'Snapshot: {args.symbol}{suffix}')
    except ProviderError as ex:
        print_status(str(ex), success=False)
```

Deferred to 3b (record in the index): the CLI `snapshot` default still comes from `_src_default` (`MMR_DEFAULT_DATA_SOURCE` / `default_data_source`) and does not read `data_providers.quotes`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/data_providers tests/test_twelvedata_sdk.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers trader/sdk.py trader/mmr_cli.py tests/data_providers tests/test_twelvedata_sdk.py
git commit -m "refactor(providers): route REST snapshots through the quotes registry

Moves the TwelveData /quote code from sdk.py into TwelveDataQuotes. Batch
rows gain 'feed' and 'error'; a missing TwelveData symbol now carries an
error message instead of a bare NaN row.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Alpaca quotes (IEX snapshots)

**Files:**
- Create: `trader/data_providers/alpaca/quotes.py`, `tests/data_providers/fixtures/alpaca_snapshots_aapl.json`
- Modify: `trader/data_providers/builtin.py` (alpaca spec gets `QUOTES`; `BUILTIN_DEFAULTS[QUOTES] = 'alpaca'`)
- Test: `tests/data_providers/test_alpaca_quotes.py`

**Interfaces:**
- Consumes: `AlpacaClient.get_json(path, params)`, `to_alpaca_symbol`, `make_quote`.
- Produces: `AlpacaQuotes(client)` implementing `QuoteProvider`; `SNAPSHOTS_PATH = '/v2/stocks/snapshots'`; `CHUNK_SIZE = 100`.

- [ ] **Step 1: Create the fixture** (real response captured 2026-10-04)

```json
{"AAPL": {"dailyBar": {"c": 333.75, "h": 334.525, "l": 330.66, "n": 18894, "o": 333.16, "t": "2026-10-02T04:00:00Z", "v": 841636, "vw": 332.919606}, "latestQuote": {"ap": 350, "as": 40, "ax": "V", "bp": 316.53, "bs": 40, "bx": "V", "c": ["R"], "t": "2026-10-02T20:00:02.423920349Z", "z": "C"}, "latestTrade": {"c": ["@"], "i": 18885, "p": 333.75, "s": 77, "t": "2026-10-02T19:59:59.099728764Z", "x": "V", "z": "C"}, "minuteBar": {"c": 333.75, "h": 333.8, "l": 333.47, "n": 588, "o": 333.67, "t": "2026-10-02T19:59:00Z", "v": 25355, "vw": 333.651846}, "prevDailyBar": {"c": 330.44, "h": 332.47, "l": 325.81, "n": 19383, "o": 329.55, "t": "2026-10-01T04:00:00Z", "v": 988015, "vw": 329.36679}}}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/data_providers/test_alpaca_quotes.py
import json
import math
from pathlib import Path

import pytest

from trader.data_providers.alpaca.quotes import AlpacaQuotes

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_snapshots_aapl.json').read_text())


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.responses.pop(0)


def test_maps_snapshot_to_quote():
    client = FakeClient([FIXTURE])
    (quote,) = AlpacaQuotes(client).quotes(['aapl'])
    assert client.calls == [('/v2/stocks/snapshots', {'symbols': 'AAPL', 'feed': 'iex'})]
    assert quote['symbol'] == 'AAPL' and quote['error'] == '' and quote['feed'] == 'iex'
    assert quote['last'] == 333.75 and quote['time'] == '2026-10-02T19:59:59.099728764Z'
    assert (quote['bid'], quote['ask'], quote['bid_size'], quote['ask_size']) == (316.53, 350, 40, 40)
    assert (quote['open'], quote['high'], quote['low'], quote['close']) == (333.16, 334.525, 330.66, 333.75)
    assert quote['volume'] == 841636 and quote['previous_close'] == 330.44
    assert quote['change'] == pytest.approx(3.31)
    assert quote['change_pct'] == pytest.approx(3.31 / 330.44 * 100)
    assert quote['currency'] == 'USD'


def test_class_share_symbol_maps_back_to_request():
    client = FakeClient([{'BRK.B': FIXTURE['AAPL']}])
    (quote,) = AlpacaQuotes(client).quotes(['BRK B'])
    assert client.calls[0][1]['symbols'] == 'BRK.B'
    assert quote['symbol'] == 'BRK B' and quote['error'] == ''


def test_unknown_symbol_gets_error_row():
    client = FakeClient([FIXTURE])
    quotes = AlpacaQuotes(client).quotes(['AAPL', 'ZZZZQ'])
    assert [q['symbol'] for q in quotes] == ['AAPL', 'ZZZZQ']
    assert quotes[1]['error'] == 'alpaca has no snapshot for ZZZZQ'
    assert math.isnan(quotes[1]['last'])


def test_invalid_symbol_gets_error_row_without_request():
    client = FakeClient([])
    (quote,) = AlpacaQuotes(client).quotes(['AAPL;DROP'])
    assert client.calls == []
    assert 'not a valid Alpaca stock symbol' in quote['error']


def test_missing_prev_close_leaves_change_nan():
    snapshot = dict(FIXTURE['AAPL'])
    del snapshot['prevDailyBar']
    (quote,) = AlpacaQuotes(FakeClient([{'AAPL': snapshot}])).quotes(['AAPL'])
    assert math.isnan(quote['previous_close']) and math.isnan(quote['change']) and math.isnan(quote['change_pct'])


def test_chunks_of_100():
    symbols = [f'S{i}' for i in range(150)]
    client = FakeClient([{}, {}])
    AlpacaQuotes(client).quotes(symbols)
    assert [len(call[1]['symbols'].split(',')) for call in client.calls] == [100, 50]


def test_registered_as_default_quotes_source():
    from trader.data_providers.capabilities import Capability
    from trader.data_providers.registry import ProviderRegistry
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'})
    assert registry.default_source(Capability.QUOTES) == 'alpaca'
    assert isinstance(registry.get(Capability.QUOTES), AlpacaQuotes)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_alpaca_quotes.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.alpaca.quotes'`

- [ ] **Step 4: Implement**

```python
# trader/data_providers/alpaca/quotes.py
"""Latest prices from Alpaca's free IEX feed (IEX is a few percent of US volume)."""

from typing import Sequence

from trader.data_providers.capabilities import make_quote
from trader.data_providers.symbols import to_alpaca_symbol

SNAPSHOTS_PATH = '/v2/stocks/snapshots'
CHUNK_SIZE = 100


class AlpacaQuotes:
    def __init__(self, client):
        self._client = client

    def quotes(self, symbols: Sequence[str]) -> list[dict]:
        requested: list[tuple[str, str]] = []
        results: dict[str, dict] = {}
        for symbol in symbols:
            try:
                requested.append((symbol, to_alpaca_symbol(symbol)))
            except ValueError as ex:
                results[symbol] = make_quote(symbol, feed='iex', error=str(ex))
        for start in range(0, len(requested), CHUNK_SIZE):
            chunk = requested[start:start + CHUNK_SIZE]
            snapshots = self._client.get_json(
                SNAPSHOTS_PATH, {'symbols': ','.join(alpaca for _, alpaca in chunk), 'feed': 'iex'})
            for symbol, alpaca in chunk:
                results[symbol] = _to_quote(symbol, alpaca, snapshots.get(alpaca))
        return [results[symbol] for symbol in symbols]


def _to_quote(symbol: str, alpaca_symbol: str, snapshot) -> dict:
    if not snapshot:
        return make_quote(symbol, feed='iex', error=f'alpaca has no snapshot for {alpaca_symbol}')
    trade = snapshot.get('latestTrade') or {}
    quote = snapshot.get('latestQuote') or {}
    day = snapshot.get('dailyBar') or {}
    previous = snapshot.get('prevDailyBar') or {}
    nan = float('nan')
    last = float(trade.get('p', nan))
    previous_close = float(previous.get('c', nan))
    change = last - previous_close
    return make_quote(
        symbol,
        time=trade.get('t', ''),
        last=last,
        bid=float(quote.get('bp', nan)), ask=float(quote.get('ap', nan)),
        bid_size=float(quote.get('bs', nan)), ask_size=float(quote.get('as', nan)),
        open=float(day.get('o', nan)), high=float(day.get('h', nan)),
        low=float(day.get('l', nan)), close=float(day.get('c', nan)),
        volume=float(day.get('v', nan)),
        previous_close=previous_close,
        change=change,
        change_pct=change / previous_close * 100 if previous_close else nan,
        currency='USD',
        feed='iex',
    )
```

Note `make_quote` upper-cases its `symbol` argument; `test_class_share_symbol_maps_back_to_request` expects `'BRK B'` (already upper case) back, so this holds.

`trader/data_providers/builtin.py`:

```python
def _alpaca_client(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.client import AlpacaClient
    return AlpacaClient(config['alpaca_api_key_id'], config['alpaca_api_secret_key'])


def _alpaca_quotes(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.quotes import AlpacaQuotes
    return AlpacaQuotes(_alpaca_client(config))
```

Make `_alpaca_history` use `_alpaca_client(config)` too (removes the duplicated client construction). Register `Capability.QUOTES: _alpaca_quotes` in the alpaca spec and set `BUILTIN_DEFAULTS[Capability.QUOTES] = 'alpaca'`.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/data_providers -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add trader/data_providers tests/data_providers
git commit -m "feat(providers): add Alpaca IEX quotes

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Massive and TwelveData movers adapters; SDK movers through the registry

**Files:**
- Create: `trader/data_providers/massive/__init__.py` (empty), `trader/data_providers/massive/movers.py`, `trader/data_providers/twelvedata/movers.py`
- Modify: `trader/data_providers/builtin.py` (massive + twelvedata specs get `MOVERS`; `BUILTIN_DEFAULTS[MOVERS] = 'massive'` — today's behaviour), `trader/sdk.py` (`movers` ≈L3711), `trader/mmr_cli.py` (movers parser ≈L1085–1100, `_handle_movers` ≈L10417)
- Test: `tests/data_providers/test_movers_adapters.py`

**Interfaces:**
- Consumes: `MOVER_COLUMNS`, `Capability.MOVERS`, `MMR._provider`, `MMR._provider_default`.
- Produces: `MassiveMovers(client)` (`markets = frozenset({'stocks', 'crypto', 'indices', 'options', 'futures'})`), `TwelveDataMovers(client)` (`markets = frozenset({'stocks'})`), shared helper `sort_movers(frame, direction) -> pd.DataFrame` in `trader/data_providers/capabilities.py`; `MMR.movers(market='stocks', direction='gainers', source=None) -> pd.DataFrame`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_movers_adapters.py
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trader.data_providers.capabilities import MOVER_COLUMNS, Capability
from trader.data_providers.errors import CapabilityNotSupported
from trader.data_providers.massive.movers import MassiveMovers
from trader.data_providers.twelvedata.movers import TwelveDataMovers


def assert_movers_frame(frame: pd.DataFrame, direction: str) -> None:
    assert tuple(frame.columns[:len(MOVER_COLUMNS)]) == MOVER_COLUMNS
    expected = frame['change_pct'].sort_values(ascending=(direction == 'losers')).tolist()
    assert frame['change_pct'].tolist() == expected


def _snap(ticker, change_pct, close=10.0):
    return SimpleNamespace(ticker=ticker, day=SimpleNamespace(close=close, volume=1000.0),
                           todays_change=1.0, todays_change_percent=change_pct)


def test_massive_movers_shape_and_sort():
    client = MagicMock()
    client.get_snapshot_direction.return_value = [_snap('A', 5.0), _snap('B', 9.0)]
    frame = MassiveMovers(client).movers('stocks', 'gainers')
    client.get_snapshot_direction.assert_called_once_with(market_type='stocks', direction='gainers')
    assert_movers_frame(frame, 'gainers')
    assert frame['ticker'].tolist() == ['B', 'A']
    assert set(frame['provider']) == {'massive'}


def test_massive_movers_tolerates_missing_day():
    client = MagicMock()
    client.get_snapshot_direction.return_value = [SimpleNamespace(ticker='A', day=None, todays_change=None,
                                                                  todays_change_percent=-3.0)]
    frame = MassiveMovers(client).movers('crypto', 'losers')
    assert frame.loc[0, 'ticker'] == 'A' and pd.isna(frame.loc[0, 'close'])


class _Payload:
    def __init__(self, payload):
        self._payload = payload

    def as_json(self):
        return self._payload


def test_twelvedata_movers_shape_and_sort():
    client = MagicMock()
    client.get_market_movers.return_value = _Payload({'values': [
        {'symbol': 'A', 'name': 'Alpha', 'exchange': 'NYSE', 'last': '10', 'volume': '5', 'change': '1',
         'percent_change': '-2'},
        {'symbol': 'B', 'name': 'Beta', 'exchange': 'NYSE', 'last': '20', 'volume': '6', 'change': '2',
         'percent_change': '-7'},
    ]})
    frame = TwelveDataMovers(client).movers('stocks', 'losers')
    assert_movers_frame(frame, 'losers')
    assert frame['ticker'].tolist() == ['B', 'A'] and frame.loc[0, 'name'] == 'Beta'
    assert frame.loc[0, 'close'] == 20.0


def test_twelvedata_rejects_unsupported_market():
    with pytest.raises(CapabilityNotSupported, match='crypto movers'):
        TwelveDataMovers(MagicMock()).movers('crypto', 'gainers')


def test_sdk_movers_uses_registry_default():
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    provider = MagicMock()
    provider.movers.return_value = pd.DataFrame([{'ticker': 'A', 'name': '', 'close': 5.0, 'volume': 1.0,
                                                   'change': 1.0, 'change_pct': 3.0, 'provider': 'x', 'note': ''}])
    mmr._provider = MagicMock(return_value=provider)
    mmr._alpaca_assets = MagicMock(return_value=None)  # used from Task 8 on; harmless before
    frame = mmr.movers(market='stocks', direction='gainers')
    mmr._provider.assert_called_once_with(Capability.MOVERS, None)
    assert frame['ticker'].tolist() == ['A']


def test_cli_movers_prints_provider_error(capsys):
    from argparse import Namespace
    from trader.data_providers.errors import ProviderNotConfigured
    from trader.mmr_cli import _handle_movers
    mmr = MagicMock()
    mmr.movers.side_effect = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')])
    _handle_movers(mmr, Namespace(losers=False, market='stocks', source=None, detail=False, num=20,
                                  min_price=1.0))
    assert 'ALPACA_API_KEY_ID' in capsys.readouterr().out


def test_cli_movers_source_default_is_none():
    from trader.mmr_cli import build_parser
    args = build_parser().parse_args(['movers'])
    assert args.source is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_movers_adapters.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.massive'`

- [ ] **Step 3: Implement**

Add to `trader/data_providers/capabilities.py`:

```python
def sort_movers(frame: pd.DataFrame, direction: str) -> pd.DataFrame:
    """Order a movers frame: MOVER_COLUMNS first, biggest move first for `direction`."""
    for column in MOVER_COLUMNS:
        if column not in frame.columns:
            frame[column] = '' if column in ('ticker', 'name', 'provider', 'note') else float('nan')
    extras = [c for c in frame.columns if c not in MOVER_COLUMNS]
    frame = frame[list(MOVER_COLUMNS) + extras]
    return frame.sort_values('change_pct', ascending=(direction == 'losers'), na_position='last') \
                .reset_index(drop=True)
```

(also export `sort_movers` from `trader/data_providers/__init__.py`).

```python
# trader/data_providers/massive/movers.py
"""Market movers from Massive (Polygon) snapshots."""

import pandas as pd

from trader.data_providers.capabilities import sort_movers


class MassiveMovers:
    markets = frozenset({'stocks', 'crypto', 'indices', 'options', 'futures'})

    def __init__(self, client):
        self._client = client

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        snaps = self._client.get_snapshot_direction(market_type=market, direction=direction)
        rows = [{
            'ticker': snap.ticker or '',
            'close': getattr(snap.day, 'close', None) if snap.day else None,
            'volume': getattr(snap.day, 'volume', None) if snap.day else None,
            'change': snap.todays_change,
            'change_pct': snap.todays_change_percent,
            'provider': 'massive',
        } for snap in snaps]
        frame = pd.DataFrame(rows, columns=['ticker', 'close', 'volume', 'change', 'change_pct', 'provider'])
        return sort_movers(frame.astype({'close': float, 'volume': float, 'change': float, 'change_pct': float}),
                           direction)
```

```python
# trader/data_providers/twelvedata/movers.py
"""Market movers from TwelveData /market_movers (needs a Pro+ plan)."""

import pandas as pd

from trader.data_providers.capabilities import sort_movers
from trader.data_providers.errors import CapabilityNotSupported


class TwelveDataMovers:
    markets = frozenset({'stocks'})

    def __init__(self, client):
        self._client = client

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        if market not in self.markets:
            raise CapabilityNotSupported(f'{market} movers', 'twelvedata', ['massive'])
        payload = self._client.get_market_movers(market=market, direction=direction).as_json()
        entries = payload if isinstance(payload, list) else payload.get('values', [])
        frame = pd.DataFrame([{
            'ticker': e.get('symbol', ''),
            'name': e.get('name', ''),
            'close': e.get('last'),
            'volume': e.get('volume'),
            'change': e.get('change'),
            'change_pct': e.get('percent_change'),
            'provider': 'twelvedata',
            'exchange': e.get('exchange', ''),
        } for e in entries], columns=['ticker', 'name', 'close', 'volume', 'change', 'change_pct',
                                      'provider', 'exchange'])
        for column in ('close', 'volume', 'change', 'change_pct'):
            frame[column] = pd.to_numeric(frame[column], errors='coerce')
        return sort_movers(frame, direction)
```

`trader/data_providers/builtin.py`:

```python
def _massive_movers(config: Mapping[str, Any]):
    from massive import RESTClient
    from trader.data_providers.massive.movers import MassiveMovers
    return MassiveMovers(RESTClient(api_key=config['massive_api_key']))


def _twelvedata_movers(config: Mapping[str, Any]):
    from twelvedata import TDClient
    from trader.data_providers.twelvedata.movers import TwelveDataMovers
    return TwelveDataMovers(TDClient(apikey=config['twelvedata_api_key']))
```

Register them, and set `BUILTIN_DEFAULTS[Capability.MOVERS] = 'massive'`.

`trader/sdk.py` — replace the whole body of `movers` (both branches) with:

```python
    def movers(
        self,
        market: str = 'stocks',
        direction: str = 'gainers',
        source: Optional[str] = None,
    ) -> pd.DataFrame:
        """Top movers for `market` ('stocks', 'crypto', 'indices', ...) from a registry movers source.

        `source=None` uses the movers default (`data_providers.movers`, else the builtin default);
        movers never inherit `default_data_source`.
        """
        from trader.data_providers import Capability
        return self._provider(Capability.MOVERS, source).movers(market, direction)
```

In `movers_detail`, change the signature default to `source: Optional[str] = None` and add at the top of its body `source = source or self._provider_default(Capability.MOVERS)` (with the local import). Leave both existing branches unchanged; the TwelveData branch keeps calling `self.movers(..., source='twelvedata')`.

`trader/mmr_cli.py` movers parser:

```python
    movers_p.add_argument('--source', choices=source_choices(Capability.MOVERS), default=None,
                          help='Data source (default: data_providers.movers, else the builtin default). '
                               'twelvedata needs a Pro+ plan for market movers.')
```

In `_handle_movers`: `source = getattr(args, 'source', None)`; title suffix becomes `f' — {source}' if source else ''`; keep the TwelveData credit notice under `if source == 'twelvedata':`. Wrap everything after those assignments in `try: … except ProviderError as ex: print_status(str(ex), success=False)` (import `ProviderError` from `trader.data_providers` inside the function).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/data_providers tests/test_twelvedata_sdk.py -q -p no:cacheprovider`
Expected: all pass (`TestMoversDetailTwelveData` stubs `m.movers`, so it is unaffected).

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers trader/sdk.py trader/mmr_cli.py tests/data_providers
git commit -m "refactor(providers): route movers through the registry

Massive and TwelveData movers move from sdk.py into adapters with one
sorted frame shape. Default stays massive in this commit.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Massive news adapters (polygon, benzinga); SDK news through the registry

**Files:**
- Create: `trader/data_providers/massive/news.py`
- Modify: `trader/data_providers/builtin.py` (new specs `polygon`, `benzinga` with the Massive key and `NEWS`; `BUILTIN_DEFAULTS[NEWS] = 'polygon'` — today's behaviour), `trader/sdk.py` (`news` ≈L4229, `news_detail` ≈L4283), `trader/mmr_cli.py` (news parser ≈L897–910, `_handle_news` ≈L10175)
- Test: `tests/data_providers/test_news_adapters.py`

**Interfaces:**
- Consumes: `make_news_item`, `Capability.NEWS`, `MMR._provider`.
- Produces: `MassiveTickerNews(client)` (source name `polygon`, carries Massive sentiment insights), `MassiveBenzingaNews(client)` (source name `benzinga`); `MMR.news(ticker=None, limit=10, source=None) -> pd.DataFrame` with columns `published, title, tickers, author, url, summary` plus `sentiment` when any item has one; `MMR.news_detail(ticker=None, limit=5, source=None) -> list[dict]` with keys `title, published, author, tickers, url, summary, insights`.

Output change (intentional, documented in Task 9): the Benzinga `teaser` and Polygon `description` fields are both called `summary` now.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_news_adapters.py
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd

from trader.data_providers.capabilities import NEWS_FIELDS, Capability, make_news_item
from trader.data_providers.massive.news import MassiveBenzingaNews, MassiveTickerNews


def _polygon_article():
    return SimpleNamespace(id='p1', published_utc='2026-10-03T12:00:00Z', title='T', description='D',
                           article_url='u', author='A', tickers=['AAPL'],
                           insights=[SimpleNamespace(ticker='AAPL', sentiment='positive',
                                                     sentiment_reasoning='because')])


def test_polygon_news_item():
    client = MagicMock()
    client.list_ticker_news.return_value = iter([_polygon_article(), _polygon_article()])
    items = MassiveTickerNews(client).news('AAPL', 1)
    client.list_ticker_news.assert_called_once_with(ticker='AAPL', limit=1)
    assert len(items) == 1 and tuple(items[0]) == NEWS_FIELDS
    item = items[0]
    assert item['published'] == '2026-10-03T12:00:00' and item['summary'] == 'D' and item['source'] == 'polygon'
    assert item['sentiment'] == 'positive'
    assert item['insights'] == [{'ticker': 'AAPL', 'sentiment': 'positive', 'reasoning': 'because'}]


def test_benzinga_news_item():
    client = MagicMock()
    client.list_benzinga_news.return_value = iter([SimpleNamespace(
        benzinga_id=7, published='2026-10-03T12:00:00Z', title='T', teaser='Z', url='u', author='A',
        tickers=['MSFT'])])
    (item,) = MassiveBenzingaNews(client).news(None, 5)
    client.list_benzinga_news.assert_called_once_with(tickers=None, limit=5)
    assert item['summary'] == 'Z' and item['source'] == 'benzinga' and item['sentiment'] == ''


def _mmr(items):
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    provider = MagicMock()
    provider.news.return_value = items
    mmr._provider = MagicMock(return_value=provider)
    return mmr


def test_sdk_news_frame_without_sentiment_column():
    mmr = _mmr([make_news_item(published='p', title='t', tickers=['A', 'B'], author='x', url='u', summary='s')])
    frame = mmr.news('A', limit=3)
    mmr._provider.assert_called_once_with(Capability.NEWS, None)
    assert list(frame.columns) == ['published', 'title', 'tickers', 'author', 'url', 'summary']
    assert frame.loc[0, 'tickers'] == 'A, B'


def test_sdk_news_frame_with_sentiment_column():
    mmr = _mmr([make_news_item(title='t', sentiment='negative')])
    assert 'sentiment' in mmr.news(source='polygon').columns


def test_sdk_news_detail_shape():
    mmr = _mmr([make_news_item(title='t', tickers=['A'], summary='s', insights=[{'ticker': 'A'}])])
    (article,) = mmr.news_detail('A')
    assert set(article) == {'title', 'published', 'author', 'tickers', 'url', 'summary', 'insights'}
    assert article['tickers'] == ['A'] and article['insights'] == [{'ticker': 'A'}]


def test_cli_news_prints_provider_error(capsys):
    from argparse import Namespace
    from trader.data_providers.errors import ProviderNotConfigured
    from trader.mmr_cli import _handle_news
    mmr = MagicMock()
    mmr.news.side_effect = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')])
    _handle_news(mmr, Namespace(ticker='AAPL', detail=False, limit=3, source=None))
    assert 'ALPACA_API_KEY_ID' in capsys.readouterr().out


def test_cli_news_sources_come_from_registry():
    from trader.mmr_cli import build_parser
    parser = build_parser()
    assert parser.parse_args(['news']).source is None
    assert parser.parse_args(['news', 'AAPL', '--source', 'benzinga']).source == 'benzinga'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_news_adapters.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.massive.news'`

- [ ] **Step 3: Implement**

```python
# trader/data_providers/massive/news.py
"""News from Massive: its own feed (with per-ticker sentiment) and Benzinga."""

from itertools import islice
from typing import Optional

from trader.data_providers.capabilities import make_news_item


class MassiveTickerNews:
    def __init__(self, client):
        self._client = client

    def news(self, ticker: Optional[str], limit: int) -> list[dict]:
        articles = islice(self._client.list_ticker_news(ticker=ticker, limit=limit), limit)
        return [_ticker_news_item(a) for a in articles]


class MassiveBenzingaNews:
    def __init__(self, client):
        self._client = client

    def news(self, ticker: Optional[str], limit: int) -> list[dict]:
        articles = islice(self._client.list_benzinga_news(tickers=ticker, limit=limit), limit)
        return [make_news_item(
            id=str(getattr(a, 'benzinga_id', '') or ''),
            published=(a.published or '')[:19],
            title=a.title or '',
            summary=a.teaser or '',
            url=a.url or '',
            author=a.author or '',
            source='benzinga',
            tickers=list(a.tickers or []),
        ) for a in articles]


def _ticker_news_item(article) -> dict:
    insights = [{'ticker': i.ticker, 'sentiment': i.sentiment, 'reasoning': i.sentiment_reasoning}
                for i in (article.insights or [])]
    return make_news_item(
        id=str(getattr(article, 'id', '') or ''),
        published=(article.published_utc or '')[:19],
        title=article.title or '',
        summary=article.description or '',
        url=article.article_url or '',
        author=article.author or '',
        source='polygon',
        tickers=list(article.tickers or []),
        sentiment=', '.join(i['sentiment'] for i in insights if i['sentiment']),
        insights=insights,
    )
```

`trader/data_providers/builtin.py`:

```python
def _massive_rest_client(config: Mapping[str, Any]):
    from massive import RESTClient
    return RESTClient(api_key=config['massive_api_key'])


def _polygon_news(config: Mapping[str, Any]):
    from trader.data_providers.massive.news import MassiveTickerNews
    return MassiveTickerNews(_massive_rest_client(config))


def _benzinga_news(config: Mapping[str, Any]):
    from trader.data_providers.massive.news import MassiveBenzingaNews
    return MassiveBenzingaNews(_massive_rest_client(config))
```

Make `_massive_movers` use `_massive_rest_client(config)`. Add specs:

```python
        ProviderSpec('polygon', (('massive_api_key', 'MASSIVE_API_KEY'),), {Capability.NEWS: _polygon_news}),
        ProviderSpec('benzinga', (('massive_api_key', 'MASSIVE_API_KEY'),), {Capability.NEWS: _benzinga_news}),
```

and `BUILTIN_DEFAULTS[Capability.NEWS] = 'polygon'`.

`trader/sdk.py` — replace the bodies of `news` and `news_detail`:

```python
    def news(self, ticker: Optional[str] = None, limit: int = 10,
             source: Optional[str] = None) -> pd.DataFrame:
        """News headlines from a registry news source ('alpaca', 'polygon', 'benzinga')."""
        from trader.data_providers import Capability
        items = self._provider(Capability.NEWS, source).news(ticker, limit)
        frame = pd.DataFrame([{
            'published': i['published'], 'title': i['title'], 'tickers': ', '.join(i['tickers']),
            'author': i['author'], 'url': i['url'], 'summary': i['summary'], 'sentiment': i['sentiment'],
        } for i in items], columns=['published', 'title', 'tickers', 'author', 'url', 'summary', 'sentiment'])
        if not frame['sentiment'].astype(bool).any():
            frame = frame.drop(columns='sentiment')
        return frame

    def news_detail(self, ticker: Optional[str] = None, limit: int = 5,
                    source: Optional[str] = None) -> List[dict]:
        """News with summaries and (where the provider has it) per-ticker sentiment insights."""
        from trader.data_providers import Capability
        keys = ('title', 'published', 'author', 'tickers', 'url', 'summary', 'insights')
        return [{key: item[key] for key in keys}
                for item in self._provider(Capability.NEWS, source).news(ticker, limit)]
```

`trader/mmr_cli.py`:
- news parser: help `'News headlines (default source: see data_providers.news)'`; epilog line `'  news AAPL --source benzinga   # Massive Benzinga feed'`; argument:

```python
    news_p.add_argument('--source', default=None, choices=source_choices(Capability.NEWS),
                        help='News source (default: data_providers.news, else the builtin default)')
```

- `_handle_news`: add `except ProviderError as ex: print_status(str(ex), success=False)` next to the existing `except ValueError` (import `ProviderError` from `trader.data_providers` inside the function); in detail mode use `desc = a.get('summary', '')`; in table mode truncate `df['summary'] = df['summary'].str[:60]` when the column exists (replace the old `teaser` lines); docstring `"""Fetch news headlines from a registry news source."""`.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/data_providers -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers trader/sdk.py trader/mmr_cli.py tests/data_providers
git commit -m "refactor(providers): route news through the registry

Massive's own feed and Benzinga become the 'polygon' and 'benzinga' news
sources. Teaser/description are both 'summary' now.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Alpaca news

**Files:**
- Create: `trader/data_providers/alpaca/news.py`, `tests/data_providers/fixtures/alpaca_news_aapl.json`
- Modify: `trader/data_providers/builtin.py` (alpaca spec gets `NEWS`; default stays `polygon` until Task 8)
- Test: `tests/data_providers/test_alpaca_news.py`

**Interfaces:**
- Consumes: `AlpacaClient.get_json`, `make_news_item`, `to_alpaca_symbol`.
- Produces: `AlpacaNews(client)` implementing `NewsProvider`; `NEWS_PATH = '/v1beta1/news'`; `MAX_LIMIT = 50`.

- [ ] **Step 1: Create the fixture** (real response captured 2026-10-04, images/content blanked)

```json
{"news": [{"author": "Mohd Haider", "content": "", "created_at": "2026-10-04T11:00:23Z", "headline": "Apple, Meta And Jim Cramer Set the Tone for AI, iPhone and Vision Pro Moves: This Week in Apple", "id": 62152341, "images": [], "source": "benzinga", "summary": "Apple dominated technology headlines this week as AI privacy, iPhone demand, wearables and corporate restructuring drew investor attention.", "symbols": ["AAPL", "META"], "updated_at": "2026-10-04T11:00:23Z", "url": "https://www.benzinga.com/markets/tech/26/10/62152341/apple-weekly-tech-headlines-ai-iphone"}], "next_page_token": null}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/data_providers/test_alpaca_news.py
import json
from pathlib import Path

import pytest

from trader.data_providers.alpaca.news import AlpacaNews
from trader.data_providers.capabilities import NEWS_FIELDS

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_news_aapl.json').read_text())


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.response


def test_maps_article_to_news_item():
    client = FakeClient(FIXTURE)
    (item,) = AlpacaNews(client).news('aapl', 3)
    assert client.calls == [('/v1beta1/news', {'symbols': 'AAPL', 'limit': 3, 'sort': 'desc'})]
    assert tuple(item) == NEWS_FIELDS
    assert item['id'] == '62152341' and item['published'] == '2026-10-04T11:00:23'
    assert item['title'].startswith('Apple, Meta') and item['summary'].startswith('Apple dominated')
    assert item['tickers'] == ['AAPL', 'META'] and item['source'] == 'alpaca/benzinga'
    assert item['sentiment'] == '' and item['insights'] == []


def test_general_news_has_no_symbols_param():
    client = FakeClient({'news': []})
    assert AlpacaNews(client).news(None, 10) == []
    assert 'symbols' not in client.calls[0][1]


def test_limit_is_capped_at_50():
    client = FakeClient({'news': []})
    AlpacaNews(client).news('AAPL', 500)
    assert client.calls[0][1]['limit'] == 50


def test_invalid_ticker_raises():
    with pytest.raises(ValueError):
        AlpacaNews(FakeClient({'news': []})).news('AAPL;DROP', 3)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_alpaca_news.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement**

```python
# trader/data_providers/alpaca/news.py
"""News headlines from Alpaca (Benzinga content). Alpaca has no sentiment — none is invented."""

from typing import Optional

from trader.data_providers.capabilities import make_news_item
from trader.data_providers.symbols import to_alpaca_symbol

NEWS_PATH = '/v1beta1/news'
MAX_LIMIT = 50


class AlpacaNews:
    def __init__(self, client):
        self._client = client

    def news(self, ticker: Optional[str], limit: int) -> list[dict]:
        params = {'limit': min(limit, MAX_LIMIT), 'sort': 'desc'}
        if ticker:
            params = {'symbols': to_alpaca_symbol(ticker), **params}
        articles = self._client.get_json(NEWS_PATH, params).get('news') or []
        return [make_news_item(
            id=str(a.get('id', '')),
            published=(a.get('created_at') or '')[:19],
            title=a.get('headline') or '',
            summary=a.get('summary') or '',
            url=a.get('url') or '',
            author=a.get('author') or '',
            source=f"alpaca/{a.get('source') or 'unknown'}",
            tickers=list(a.get('symbols') or []),
        ) for a in articles[:limit]]
```

`trader/data_providers/builtin.py`: `_alpaca_news(config)` returning `AlpacaNews(_alpaca_client(config))`; register `Capability.NEWS` in the alpaca spec.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/data_providers -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add trader/data_providers tests/data_providers
git commit -m "feat(providers): add Alpaca news

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Alpaca asset directory and the stock movers filter

**Files:**
- Create: `trader/data_providers/alpaca/assets.py`, `trader/data_providers/movers_filter.py`, `tests/data_providers/fixtures/alpaca_assets_sample.json`
- Modify: `trader/data_providers/builtin.py` (`alpaca_asset_directory(config)`)
- Test: `tests/data_providers/test_alpaca_assets.py`, `tests/data_providers/test_movers_filter.py`

**Interfaces:**
- Consumes: `AlpacaClient(key_id, secret_key, base_url=...)`, `AlpacaClient.get_json`.
- Produces:
  - `ALPACA_PAPER_TRADING_URL = 'https://paper-api.alpaca.markets'`, `ASSETS_PATH = '/v2/assets'`, `DEFAULT_CACHE_PATH = Path('~/.local/share/mmr/cache/alpaca_assets.json').expanduser()`, `CACHE_MAX_AGE = dt.timedelta(hours=24)`.
  - `AlpacaAssetDirectory(client, cache_path=DEFAULT_CACHE_PATH, now=_utc_now)` with `.load() -> AlpacaAssetDirectory` (fetches or reads the cache now, so errors surface at a known point; returns self), `.name(symbol) -> str`, `.exchange(symbol) -> str`, `.knows(symbol) -> bool`, `.is_derivative_unit(symbol) -> bool` (warrant/right/unit).
  - `filter_stock_movers(frame, min_price: float, assets: Optional[AlpacaAssetDirectory]) -> pd.DataFrame`.
  - `builtin.alpaca_asset_directory(config) -> Optional[AlpacaAssetDirectory]` (None when Alpaca keys are missing).

- [ ] **Step 1: Create the fixture** (real asset entries captured 2026-10-04)

```json
[{"symbol": "AAPL", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "class": "us_equity", "tradable": true, "status": "active"},
 {"symbol": "BRK.B", "name": "BERKSHIRE HATHAWAY Class B", "exchange": "NYSE", "class": "us_equity", "tradable": true, "status": "active"},
 {"symbol": "HPAIW", "name": "Helport AI Limited Warrants", "exchange": "NASDAQ", "class": "us_equity", "tradable": true, "status": "active"},
 {"symbol": "GLLRF", "name": "GLOBAL LTS ACQUISITION CORP Rights", "exchange": "OTC", "class": "us_equity", "tradable": false, "status": "active"},
 {"symbol": "KVAUF", "name": "Keen Vision Acquisition Corporation Units", "exchange": "OTC", "class": "us_equity", "tradable": false, "status": "active"},
 {"symbol": "BBAI.WS", "name": "BigBear.ai Holdings, Inc. Redeemable warrants, each full warrant exercisable for one share of common stock at an exercise price of $11.50 per share", "exchange": "NYSE", "class": "us_equity", "tradable": true, "status": "active"},
 {"symbol": "UNTC", "name": "Unit Corporation Common Stock", "exchange": "OTC", "class": "us_equity", "tradable": true, "status": "active"},
 {"symbol": "PRIAF", "name": "PRIOR1TY INTELLIGENCE GROUP PLC Ordinary Shares (United Kingdom)", "exchange": "OTC", "class": "us_equity", "tradable": false, "status": "active"}]
```

(`BBAI.WS` and `UNTC` are illustrative names for pattern coverage — `UNTC` checks that a company literally named "Unit Corporation" is kept.)

- [ ] **Step 2: Write the failing tests**

```python
# tests/data_providers/test_alpaca_assets.py
import datetime as dt
import json
import os
from pathlib import Path

from trader.data_providers.alpaca.assets import AlpacaAssetDirectory

SAMPLE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_assets_sample.json').read_text())
NOW = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self):
        self.calls = 0

    def get_json(self, path, params):
        self.calls += 1
        assert path == '/v2/assets' and params == {'status': 'active', 'asset_class': 'us_equity'}
        return SAMPLE


def test_fetches_once_and_answers_lookups(tmp_path):
    client = FakeClient()
    assets = AlpacaAssetDirectory(client, cache_path=tmp_path / 'a.json', now=lambda: NOW)
    assert assets.name('AAPL') == 'Apple Inc. Common Stock' and assets.exchange('BRK.B') == 'NYSE'
    assert assets.knows('aapl') and not assets.knows('ZZZZQ')
    assert assets.name('ZZZZQ') == ''
    assert client.calls == 1


def test_load_fetches_eagerly_and_returns_self(tmp_path):
    client = FakeClient()
    assets = AlpacaAssetDirectory(client, cache_path=tmp_path / 'a.json', now=lambda: NOW)
    assert assets.load() is assets and client.calls == 1
    assets.name('AAPL')
    assert client.calls == 1


def test_derivative_units_are_detected():
    assets = AlpacaAssetDirectory(FakeClient(), cache_path=None, now=lambda: NOW)
    flagged = {s['symbol'] for s in SAMPLE if assets.is_derivative_unit(s['symbol'])}
    assert flagged == {'HPAIW', 'GLLRF', 'KVAUF', 'BBAI.WS'}


def test_fresh_cache_is_reused(tmp_path):
    cache = tmp_path / 'a.json'
    AlpacaAssetDirectory(FakeClient(), cache_path=cache, now=lambda: NOW).name('AAPL')
    second = FakeClient()
    assert AlpacaAssetDirectory(second, cache_path=cache, now=lambda: NOW + dt.timedelta(hours=23)).name('AAPL')
    assert second.calls == 0


def test_stale_cache_is_refreshed(tmp_path):
    cache = tmp_path / 'a.json'
    AlpacaAssetDirectory(FakeClient(), cache_path=cache, now=lambda: NOW).name('AAPL')
    later = FakeClient()
    AlpacaAssetDirectory(later, cache_path=cache, now=lambda: NOW + dt.timedelta(hours=25)).name('AAPL')
    assert later.calls == 1


def test_unwritable_cache_still_returns_assets(tmp_path):
    read_only = tmp_path / 'ro'
    read_only.mkdir()
    os.chmod(read_only, 0o500)
    try:
        assets = AlpacaAssetDirectory(FakeClient(), cache_path=read_only / 'sub' / 'a.json', now=lambda: NOW)
        assert assets.name('AAPL') == 'Apple Inc. Common Stock'
    finally:
        os.chmod(read_only, 0o700)


def test_corrupt_cache_is_refetched(tmp_path):
    cache = tmp_path / 'a.json'
    cache.write_text('{not json')
    client = FakeClient()
    assert AlpacaAssetDirectory(client, cache_path=cache, now=lambda: NOW).knows('AAPL')
    assert client.calls == 1
```

```python
# tests/data_providers/test_movers_filter.py
import datetime as dt
import json
from pathlib import Path

import pandas as pd

from trader.data_providers.alpaca.assets import AlpacaAssetDirectory
from trader.data_providers.movers_filter import filter_stock_movers

SAMPLE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_assets_sample.json').read_text())


class _Client:
    def get_json(self, path, params):
        return SAMPLE


ASSETS = AlpacaAssetDirectory(_Client(), cache_path=None,
                              now=lambda: dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc))


def _frame(rows):
    return pd.DataFrame([{'ticker': t, 'name': '', 'close': c, 'volume': float('nan'), 'change': 1.0,
                          'change_pct': p, 'provider': 'alpaca', 'note': ''} for t, c, p in rows])


def test_drops_sub_dollar_and_unknown_price():
    out = filter_stock_movers(_frame([('AAPL', 300.0, 5.0), ('PENNY', 0.5, 90.0), ('NOPRICE', float('nan'), 80.0)]),
                              min_price=1.0, assets=None)
    assert out['ticker'].tolist() == ['AAPL']


def test_drops_warrants_rights_units():
    out = filter_stock_movers(_frame([('HPAIW', 3.0, 99.0), ('GLLRF', 2.0, 50.0), ('KVAUF', 10.0, 40.0),
                                      ('AAPL', 300.0, 5.0)]), min_price=1.0, assets=ASSETS)
    assert out['ticker'].tolist() == ['AAPL']


def test_keeps_unit_corporation():
    out = filter_stock_movers(_frame([('UNTC', 30.0, 7.0)]), min_price=1.0, assets=ASSETS)
    assert out['ticker'].tolist() == ['UNTC']


def test_fills_names_from_assets():
    out = filter_stock_movers(_frame([('AAPL', 300.0, 5.0)]), min_price=1.0, assets=ASSETS)
    assert out.loc[0, 'name'] == 'Apple Inc. Common Stock'


def test_without_assets_notes_that_instrument_filter_is_off():
    out = filter_stock_movers(_frame([('AAPL', 300.0, 5.0)]), min_price=1.0, assets=None)
    assert 'warrant filter off' in out.loc[0, 'note']


def test_min_price_zero_keeps_pennies():
    out = filter_stock_movers(_frame([('PENNY', 0.5, 90.0)]), min_price=0.0, assets=None)
    assert out['ticker'].tolist() == ['PENNY']
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/data_providers/test_alpaca_assets.py tests/data_providers/test_movers_filter.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 4: Implement**

```python
# trader/data_providers/alpaca/assets.py
"""Alpaca's list of US equities: names, exchanges, and which symbols are warrants/rights/units.

The list is ~14k symbols (6.5 MB), so it is cached on disk for a day. A cache
that cannot be written (read-only container) is not an error.
"""

import datetime as dt
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Optional

ALPACA_PAPER_TRADING_URL = 'https://paper-api.alpaca.markets'
ASSETS_PATH = '/v2/assets'
DEFAULT_CACHE_PATH = Path('~/.local/share/mmr/cache/alpaca_assets.json').expanduser()
CACHE_MAX_AGE = dt.timedelta(hours=24)

# Security-type word at the end of the name, or followed by ", each" / "," (SPAC unit wording).
# "Unit Corporation Common Stock" does not match.
_DERIVATIVE_UNIT = re.compile(r'\b(warrants?|rights?|units?)\b\s*(,|$)', re.IGNORECASE)

logger = logging.getLogger(__name__)


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class AlpacaAssetDirectory:
    def __init__(self, client, cache_path: Optional[Path] = DEFAULT_CACHE_PATH,
                 now: Callable[[], dt.datetime] = _utc_now):
        self._client = client
        self._cache_path = cache_path
        self._now = now
        self._assets: Optional[dict[str, dict]] = None

    def load(self) -> 'AlpacaAssetDirectory':
        if self._assets is None:
            self._assets = self._read_cache() or self._fetch()
        return self

    def name(self, symbol: str) -> str:
        return self._lookup(symbol).get('name', '')

    def exchange(self, symbol: str) -> str:
        return self._lookup(symbol).get('exchange', '')

    def knows(self, symbol: str) -> bool:
        return bool(self._lookup(symbol))

    def is_derivative_unit(self, symbol: str) -> bool:
        return bool(_DERIVATIVE_UNIT.search(self.name(symbol)))

    def _lookup(self, symbol: str) -> dict:
        return self.load()._assets.get(symbol.strip().upper(), {})

    def _read_cache(self) -> Optional[dict[str, dict]]:
        if self._cache_path is None or not self._cache_path.exists():
            return None
        try:
            cached = json.loads(self._cache_path.read_text())
            fetched_at = dt.datetime.fromisoformat(cached['fetched_at'])
            if self._now() - fetched_at > CACHE_MAX_AGE:
                return None
            return cached['assets']
        except (ValueError, KeyError, TypeError) as ex:
            logger.warning('ignoring unreadable alpaca asset cache %s: %s', self._cache_path, ex)
            return None

    def _fetch(self) -> dict[str, dict]:
        raw = self._client.get_json(ASSETS_PATH, {'status': 'active', 'asset_class': 'us_equity'})
        assets = {a['symbol'].upper(): {'name': a.get('name') or '', 'exchange': a.get('exchange') or ''}
                  for a in raw if a.get('symbol')}
        self._write_cache(assets)
        return assets

    def _write_cache(self, assets: dict[str, dict]) -> None:
        if self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({'fetched_at': self._now().isoformat(), 'assets': assets})
            with tempfile.NamedTemporaryFile('w', dir=self._cache_path.parent, delete=False) as tmp:
                tmp.write(payload)
            os.replace(tmp.name, self._cache_path)
        except OSError as ex:
            logger.warning('could not write alpaca asset cache %s: %s', self._cache_path, ex)
```

```python
# trader/data_providers/movers_filter.py
"""Removes noise from stock movers: sub-$1 names and warrants/rights/units."""

from typing import Optional

import pandas as pd

INSTRUMENT_FILTER_OFF_NOTE = 'warrant filter off: Alpaca not configured'


def filter_stock_movers(frame: pd.DataFrame, min_price: float, assets) -> pd.DataFrame:
    keep = frame['close'].notna() & (frame['close'] >= min_price)
    if assets is not None:
        keep &= ~frame['ticker'].map(assets.is_derivative_unit)
    filtered = frame[keep].copy()
    if assets is None:
        filtered['note'] = _append_note(filtered['note'], INSTRUMENT_FILTER_OFF_NOTE)
    else:
        missing_name = filtered['name'].fillna('') == ''
        filtered.loc[missing_name, 'name'] = filtered.loc[missing_name, 'ticker'].map(assets.name)
    return filtered.reset_index(drop=True)


def _append_note(notes: pd.Series, extra: str) -> pd.Series:
    return notes.fillna('').map(lambda note: f'{note}; {extra}' if note else extra)
```

Note: if `test_unwritable_cache_still_returns_assets` cannot make the directory unwritable (e.g. tests run as root), it still passes because the fetch result is returned regardless; that is the behaviour under test.

`trader/data_providers/builtin.py`:

```python
def alpaca_asset_directory(config: Mapping[str, Any]):
    """The cached Alpaca asset list, or None when Alpaca keys are not configured."""
    if not all(str(config.get(key) or '').strip() for key in ('alpaca_api_key_id', 'alpaca_api_secret_key')):
        return None
    from trader.data_providers.alpaca.assets import ALPACA_PAPER_TRADING_URL, AlpacaAssetDirectory
    from trader.data_providers.alpaca.client import AlpacaClient
    client = AlpacaClient(config['alpaca_api_key_id'], config['alpaca_api_secret_key'],
                          base_url=ALPACA_PAPER_TRADING_URL)
    return AlpacaAssetDirectory(client)
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/data_providers -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add trader/data_providers tests/data_providers
git commit -m "feat(providers): add cached Alpaca asset list and stock movers filter

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Alpaca movers, filter wiring, default switch, movers detail

**Files:**
- Create: `trader/data_providers/alpaca/movers.py`, `tests/data_providers/fixtures/alpaca_movers_stocks.json`
- Modify: `trader/data_providers/builtin.py` (alpaca spec gets `MOVERS`; `BUILTIN_DEFAULTS[MOVERS] = 'alpaca'`, `BUILTIN_DEFAULTS[NEWS] = 'alpaca'`), `trader/sdk.py` (`movers`, `movers_detail`, new `_movers_detail_from_capabilities`), `trader/mmr_cli.py` (movers `--min-price`, `_handle_movers`)
- Test: `tests/data_providers/test_alpaca_movers.py`, `tests/data_providers/test_sdk_movers_news.py`

**Interfaces:**
- Consumes: Tasks 1, 4, 6, 7.
- Produces: `AlpacaMovers(client)` (`markets = frozenset({'stocks', 'crypto'})`, `MAX_TOP = 50`); `MMR.movers(market='stocks', direction='gainers', source=None, min_price=1.0)`; `MMR.movers_detail(market, direction, num=20, source=None, min_price=1.0)`; `MMR._alpaca_assets()` (loaded directory or None); `MMR._movers_asset_directory()` (same, but logs and returns None on `ProviderError`/`requests.RequestException`). `sdk.py` already defines `logger = logging.getLogger(__name__)` (L36); use it.

Ruling recorded in this plan: `movers_detail` keeps its existing Massive and TwelveData branches in 3a and gains a capability-based path for every other source. Phase 4 (ratios in the registry) replaces all three with the capability path. Cost if wrong: one temporary dispatch in one method.

- [ ] **Step 1: Create the fixture** (trimmed real response, 2026-10-02 close)

```json
{"gainers": [{"change": 0.0221, "percent_change": 221, "price": 0.0321, "symbol": "HPAIW"}, {"change": 2.32, "percent_change": 198.29, "price": 3.49, "symbol": "AMOD"}, {"change": 0.0217, "percent_change": 146.62, "price": 0.0365, "symbol": "CYCUW"}, {"change": 3.82, "percent_change": 104.37, "price": 7.48, "symbol": "SDEV"}, {"change": 12.92, "percent_change": 100.54, "price": 25.77, "symbol": "MN"}], "losers": [{"change": -0.0062, "percent_change": -62.63, "price": 0.0037, "symbol": "ASTLW"}, {"change": -0.0273, "percent_change": -52.1, "price": 0.0251, "symbol": "SBFMW"}], "market_type": "stocks", "last_updated": "2026-10-02T23:59:00.151438156Z"}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/data_providers/test_alpaca_movers.py
import json
from pathlib import Path

import pytest

from trader.data_providers.alpaca.movers import AlpacaMovers
from trader.data_providers.capabilities import MOVER_COLUMNS
from trader.data_providers.errors import CapabilityNotSupported

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_movers_stocks.json').read_text())


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.response


def test_stock_gainers_frame():
    client = FakeClient(FIXTURE)
    frame = AlpacaMovers(client).movers('stocks', 'gainers')
    assert client.calls == [('/v1beta1/screener/stocks/movers', {'top': 50})]
    assert tuple(frame.columns[:len(MOVER_COLUMNS)]) == MOVER_COLUMNS
    assert frame['ticker'].tolist() == ['HPAIW', 'AMOD', 'CYCUW', 'SDEV', 'MN']
    assert frame.loc[1, 'close'] == 3.49 and frame.loc[1, 'change_pct'] == 198.29
    assert frame['volume'].isna().all() and set(frame['provider']) == {'alpaca'}
    assert frame.loc[0, 'note'] == 'as of 2026-10-02T23:59:00Z'


def test_losers_sorted_ascending():
    frame = AlpacaMovers(FakeClient(FIXTURE)).movers('stocks', 'losers')
    assert frame['ticker'].tolist() == ['ASTLW', 'SBFMW']


def test_crypto_path():
    client = FakeClient({'gainers': [], 'losers': [], 'last_updated': ''})
    AlpacaMovers(client).movers('crypto', 'gainers')
    assert client.calls[0][0] == '/v1beta1/screener/crypto/movers'


def test_unsupported_market_raises():
    with pytest.raises(CapabilityNotSupported, match='indices movers') as info:
        AlpacaMovers(FakeClient(FIXTURE)).movers('indices', 'gainers')
    assert info.value.supported == ['massive']
```

```python
# tests/data_providers/test_sdk_movers_news.py
from unittest.mock import MagicMock

import pandas as pd

from trader.data_providers.capabilities import Capability, make_news_item
from trader.data_providers.registry import ProviderRegistry


def _frame():
    return pd.DataFrame([
        {'ticker': 'HPAIW', 'name': '', 'close': 3.0, 'volume': float('nan'), 'change': 1.0, 'change_pct': 99.0,
         'provider': 'alpaca', 'note': ''},
        {'ticker': 'PENNY', 'name': '', 'close': 0.2, 'volume': float('nan'), 'change': 0.1, 'change_pct': 50.0,
         'provider': 'alpaca', 'note': ''},
        {'ticker': 'AAPL', 'name': '', 'close': 300.0, 'volume': float('nan'), 'change': 3.0, 'change_pct': 1.0,
         'provider': 'alpaca', 'note': ''},
    ])


def _mmr(frame, news_items=(), assets=None):
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    movers_provider, news_provider = MagicMock(), MagicMock()
    movers_provider.movers.return_value = frame
    news_provider.news.return_value = list(news_items)
    mmr._provider = MagicMock(side_effect=lambda cap, source=None:
                              movers_provider if cap == Capability.MOVERS else news_provider)
    mmr._provider_default = MagicMock(return_value='alpaca')
    mmr._alpaca_assets = MagicMock(return_value=assets)
    return mmr


class _Assets:
    def load(self):
        return self

    def is_derivative_unit(self, symbol):
        return symbol.endswith('W')

    def name(self, symbol):
        return {'AAPL': 'Apple Inc.'}.get(symbol, '')

    def exchange(self, symbol):
        return 'NASDAQ'


def test_stock_movers_are_filtered():
    out = _mmr(_frame(), assets=_Assets()).movers('stocks', 'gainers')
    assert out['ticker'].tolist() == ['AAPL'] and out.loc[0, 'name'] == 'Apple Inc.'


def test_asset_list_failure_only_turns_instrument_filter_off():
    from trader.data_providers.errors import ProviderEntitlementError
    mmr = _mmr(_frame())
    mmr._alpaca_assets = MagicMock(side_effect=ProviderEntitlementError('alpaca rejected the API key'))
    out = mmr.movers('stocks', 'gainers', source='massive')
    assert out['ticker'].tolist() == ['HPAIW', 'AAPL']
    assert all('warrant filter off' in note for note in out['note'])


def test_crypto_movers_are_not_filtered():
    out = _mmr(_frame(), assets=_Assets()).movers('crypto', 'gainers')
    assert len(out) == 3


def test_movers_default_ignores_default_data_source():
    registry = ProviderRegistry.from_config({'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.MOVERS) == 'alpaca'
    assert registry.default_source(Capability.NEWS) == 'alpaca'


def test_movers_detail_from_capabilities():
    news = [make_news_item(title='Apple up', sentiment='')]
    detail = _mmr(_frame(), news_items=news, assets=_Assets()).movers_detail('stocks', 'gainers', num=5)
    assert [d['ticker'] for d in detail] == ['AAPL']
    row = detail[0]
    assert row['details'] == {'name': 'Apple Inc.', 'exchange': 'NASDAQ', 'description': ''}
    assert row['news'] == {'headline': 'Apple up', 'sentiment': ''}
    assert row['ratios'] == {} and row['close'] == 300.0 and row['open'] is None


def test_cli_min_price_flag():
    from trader.mmr_cli import build_parser
    assert build_parser().parse_args(['movers', '--min-price', '0']).min_price == 0.0
    assert build_parser().parse_args(['movers']).min_price == 1.0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/data_providers/test_alpaca_movers.py tests/data_providers/test_sdk_movers_news.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.alpaca.movers'`

- [ ] **Step 4: Implement**

```python
# trader/data_providers/alpaca/movers.py
"""Top gainers/losers from Alpaca's screener (stocks and crypto; at most 50 each way)."""

import pandas as pd

from trader.data_providers.capabilities import sort_movers
from trader.data_providers.errors import CapabilityNotSupported

MAX_TOP = 50


class AlpacaMovers:
    markets = frozenset({'stocks', 'crypto'})

    def __init__(self, client):
        self._client = client

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        if market not in self.markets:
            raise CapabilityNotSupported(f'{market} movers', 'alpaca', ['massive'])
        payload = self._client.get_json(f'/v1beta1/screener/{market}/movers', {'top': MAX_TOP})
        as_of = (payload.get('last_updated') or '')[:19]
        note = f'as of {as_of}Z' if as_of else ''
        frame = pd.DataFrame([{
            'ticker': e.get('symbol', ''),
            'close': float(e.get('price', float('nan'))),
            'volume': float('nan'),
            'change': float(e.get('change', float('nan'))),
            'change_pct': float(e.get('percent_change', float('nan'))),
            'provider': 'alpaca',
            'note': note,
        } for e in payload.get(direction) or []],
            columns=['ticker', 'close', 'volume', 'change', 'change_pct', 'provider', 'note'])
        return sort_movers(frame, direction)
```

`trader/data_providers/builtin.py`: `_alpaca_movers(config)` → `AlpacaMovers(_alpaca_client(config))`; register `Capability.MOVERS` in the alpaca spec; set `BUILTIN_DEFAULTS[Capability.MOVERS] = 'alpaca'` and `BUILTIN_DEFAULTS[Capability.NEWS] = 'alpaca'`.

`trader/sdk.py`:

```python
    def _alpaca_assets(self):
        from trader.data_providers.builtin import alpaca_asset_directory
        directory = alpaca_asset_directory(self._container.config())
        return directory.load() if directory else None

    def _movers_asset_directory(self):
        """The asset list for movers enrichment, or None — a failure here never fails movers."""
        import requests
        from trader.data_providers import ProviderError
        try:
            return self._alpaca_assets()
        except (ProviderError, requests.RequestException) as ex:
            logger.warning('alpaca asset list unavailable, movers warrant filter off: %s', ex)
            return None

    def movers(
        self,
        market: str = 'stocks',
        direction: str = 'gainers',
        source: Optional[str] = None,
        min_price: float = 1.0,
    ) -> pd.DataFrame:
        """Top movers for `market` from a registry movers source.

        Stock movers drop names under `min_price` and, when Alpaca is configured, warrants,
        rights and units. `source=None` uses `data_providers.movers`, else the builtin default;
        movers never inherit `default_data_source`.
        """
        from trader.data_providers import Capability
        from trader.data_providers.movers_filter import filter_stock_movers
        frame = self._provider(Capability.MOVERS, source).movers(market, direction)
        if market == 'stocks':
            frame = filter_stock_movers(frame, min_price, self._movers_asset_directory())
        return frame
```

`movers_detail`: signature `(self, market='stocks', direction='gainers', num=20, source=None, min_price=1.0)`; after resolving `source = source or self._provider_default(Capability.MOVERS)`, keep the existing `twelvedata` branch, wrap the existing Massive code in `if source == 'massive':`, and end with `return self._movers_detail_from_capabilities(market, direction, num, source, min_price)`:

```python
    def _movers_detail_from_capabilities(self, market, direction, num, source, min_price) -> list[dict]:
        from concurrent.futures import ThreadPoolExecutor
        from trader.data_providers import Capability
        frame = self.movers(market=market, direction=direction, source=source, min_price=min_price).head(num)
        assets = self._movers_asset_directory() if market == 'stocks' else None
        news_provider = self._provider(Capability.NEWS)

        def latest_headline(ticker: str) -> dict:
            try:
                items = news_provider.news(ticker, 1)
            except Exception:
                return {}
            return {'headline': items[0]['title'], 'sentiment': items[0]['sentiment']} if items else {}

        tickers = frame['ticker'].tolist()
        with ThreadPoolExecutor(max_workers=5) as pool:
            headlines = dict(zip(tickers, pool.map(latest_headline, tickers)))
        return [{
            'ticker': row.ticker,
            'open': None,
            'close': row.close,
            'volume': None if pd.isna(row.volume) else row.volume,
            'change': row.change,
            'change_pct': row.change_pct,
            'details': {'name': row.name or '', 'exchange': assets.exchange(row.ticker) if assets else '',
                        'description': ''},
            'ratios': {},
            'news': headlines.get(row.ticker, {}),
        } for row in frame.itertuples(index=False)]
```

Watch out: `row.name` on a pandas `itertuples` namedtuple is the column `name`, which is fine here because `index=False`. A headline failure for one ticker must not fail the whole detail view — that is why `latest_headline` catches and returns `{}`; this is enrichment, the movers themselves still fail loudly.

`trader/mmr_cli.py`: add to the movers parser

```python
    movers_p.add_argument('--min-price', type=float, default=1.0,
                          help='Drop stock movers below this price (default: 1.0; 0 keeps all)')
```

and pass `min_price=args.min_price` to both `mmr.movers(...)` and `mmr.movers_detail(...)` in `_handle_movers`. Change the parser help to `'Top market movers (default: Alpaca; stocks drop sub-$1 names, warrants, rights, units)'`. Remove the "(card view; Massive only)" wording from `--detail` help.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/data_providers tests/test_twelvedata_sdk.py -q -p no:cacheprovider`, then the full suite once.
Expected: all pass; full suite 0 failed. Expect to update tests that assert the old **default** movers/news source (`grep -rn "source='massive'\|source='polygon'\|default='massive'\|default='polygon'" tests/`) — change only default-source assertions, never Massive provider behaviour. List every changed test in the report.

- [ ] **Step 6: Commit**

```bash
git add trader/data_providers trader/sdk.py trader/mmr_cli.py tests
git commit -m "feat(providers): make Alpaca the default movers and news source

Stock movers drop sub-\$1 names and (with Alpaca configured) warrants,
rights and units; --min-price adjusts the price floor. movers --detail
works on Alpaca with names and headlines; ratios arrive in phase 4.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Live checks, weekday movers check, docs

**Files:**
- Modify: `tests/data_providers/test_live_alpaca.py` (add live tests), `CLAUDE.md`, `docs/OPERATIONAL_STATE.md`, `config_defaults/trader.yaml` (comment above `default_data_source`), `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md` (mark 3a done)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Add live tests** (same gating as the existing file — reuse its `pytestmark` and `_provider` pattern)

```python
def _registry():
    return ProviderRegistry.from_config({
        'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
        'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY'],
    })


def test_live_quotes():
    aapl, unknown = _registry().get(Capability.QUOTES, 'alpaca').quotes(['AAPL', 'ZZZZQ'])
    assert aapl['error'] == '' and aapl['last'] > 0 and aapl['feed'] == 'iex'
    assert 'no snapshot' in unknown['error']


def test_live_news():
    items = _registry().get(Capability.NEWS, 'alpaca').news('AAPL', 3)
    assert 1 <= len(items) <= 3 and all(item['title'] for item in items)


def test_live_movers_are_clean():
    from trader.data_providers.builtin import alpaca_asset_directory
    from trader.data_providers.movers_filter import filter_stock_movers
    config = {'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
              'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY']}
    assets = alpaca_asset_directory(config)
    raw = _registry().get(Capability.MOVERS, 'alpaca').movers('stocks', 'gainers')
    clean = filter_stock_movers(raw, 1.0, assets)
    assert len(raw) > 0
    assert (clean['close'] >= 1.0).all()
    assert not clean['ticker'].map(assets.is_derivative_unit).any()
    assert assets.knows('AAPL') and assets.name('AAPL')


def test_live_crypto_movers():
    assert len(_registry().get(Capability.MOVERS, 'alpaca').movers('crypto', 'gainers')) > 0
```

(Add `from trader.data_providers.capabilities import Capability` and `ProviderRegistry` imports if the file lacks them.)

Run (keys from `.env`, never printed):

```bash
set -a; eval "$(grep -E '^ALPACA_API_(KEY_ID|SECRET_KEY)=' .env)"; set +a
MMR_LIVE_TESTS=1 .venv/bin/pytest tests/data_providers/test_live_alpaca.py -v -m live
```

Expected: all live tests pass. Default run still skips them: `.venv/bin/pytest tests/data_providers/test_live_alpaca.py -q` → all skipped. The asset test writes `~/.local/share/mmr/cache/alpaca_assets.json`; that is expected and safe.

- [ ] **Step 2: Weekday intraday movers check**

The spec requires confirming that Basic-plan movers are intraday, not end-of-day only. Run `TZ=America/New_York date`. If it is Monday–Friday between 10:00 and 15:30 ET, run:

```bash
.venv/bin/python - <<'EOF'
import os, datetime as dt
from trader.data_providers.alpaca.client import AlpacaClient
client = AlpacaClient(os.environ['ALPACA_API_KEY_ID'], os.environ['ALPACA_API_SECRET_KEY'])
updated = client.get_json('/v1beta1/screener/stocks/movers', {'top': 1})['last_updated']
age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(updated[:19] + '+00:00')
print('last_updated', updated, 'age minutes', round(age.total_seconds() / 60, 1))
EOF
```

Record the result. Intraday means age under 30 minutes. Otherwise record "not run (outside US market hours)" and add an operator TODO line to `docs/OPERATIONAL_STATE.md`: "Confirm Alpaca movers `last_updated` is intraday on a weekday (spec 3a check)". If it IS run and shows end-of-day only, note it in CLAUDE.md next to the movers command ("Alpaca free movers update end-of-day") — do not change code.

- [ ] **Step 3: Docs**

- `CLAUDE.md`:
  - Design principle "Massive first, IB fallback": state that Alpaca (free) is now the default for history, movers, news and REST quotes; Massive and TwelveData stay opt-in via `--source`; `ideas` still defaults to Massive until phase 3b.
  - CLI command list: `snapshot AAPL --source alpaca`, `snapshot-batch AAPL MSFT --source alpaca`, `movers` (default Alpaca, `--min-price`, `--source massive` for indices/options/futures), `news AAPL` (default Alpaca, `--source polygon|benzinga` for Massive), `news AAPL --detail` (sentiment only from `--source polygon`).
  - Configuration `default_data_source` bullet: it now affects `data download` and REST quote commands (`snapshot`, `snapshot-batch`); `movers` and `news` never inherit it — use `data_providers.movers` / `data_providers.news`. (Fixes the phase 2 wording "alpaca only affects data download", which is no longer true.)
  - Note the output change: news `teaser`/`description` → `summary`; batch snapshot rows have `feed` and `error`.
  - Note that stock `movers` often returns fewer rows than `--num` after filtering (on 2026-10-02, 16 of Alpaca's 50 top gainers survived), and that `movers --detail` on Alpaca shows names and headlines but no ratios, market cap or description until phase 4 (`--source massive` keeps them).
- `docs/OPERATIONAL_STATE.md` — new operator step under the Alpaca section: "From phase 3a, `mmr movers` and `mmr news` default to Alpaca **without any config edit** (they ignore `default_data_source`). The CLI needs the Alpaca keys: export `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`, or put them in the live `~/.config/mmr/trader.yaml` (empty env values no longer blank YAML keys). To keep Massive: set `data_providers: {movers: massive, news: polygon}` in the live `trader.yaml`."
- `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md` — add the same sentence to "Operator steps", and add to the 3b row: "CLI `snapshot` default should honour `data_providers.quotes` (deferred from 3a)".
- `config_defaults/trader.yaml` comment above `default_data_source`: same rule as the CLAUDE.md bullet; extend the commented `data_providers:` example with `#   movers: alpaca` and `#   news: alpaca`.
- `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`: mark 3a `done` (plus the two edits above).

- [ ] **Step 4: Verify**

Run: `grep -rn "def _handle_movers\|source == 'twelvedata'" trader/sdk.py | head` — the only remaining `source == 'twelvedata'` branches in `sdk.py` should be in methods phases 3b–8 own (financials, forex, scan_ideas, movers_detail's temporary branch). Then the full suite: `.venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py -p no:cacheprovider` → 0 failed.

- [ ] **Step 5: Commit**

```bash
git add tests/data_providers/test_live_alpaca.py CLAUDE.md docs config_defaults/trader.yaml
git commit -m "docs: document Alpaca quotes, movers and news

<weekday movers check result here>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

After this task, write the phase 3b plan (`…-03b-scanner-merge.md`) against the code as it now exists.

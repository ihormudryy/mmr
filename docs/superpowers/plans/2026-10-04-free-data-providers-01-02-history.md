# Free Data Providers — Phases 1–2: Registry + Alpaca History

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route all REST history downloads through one provider registry (phase 1, no behaviour change), then add Alpaca as the free default history provider (phase 2).

**Architecture:** New package `trader/data_providers/` holds a `Capability` enum, a `HistoryProvider` protocol, typed errors and a `ProviderRegistry` that maps `(capability, source)` to a provider built from the config dict. The existing `MassiveHistoryWorker` and `TwelveDataHistoryWorker` already satisfy the protocol and are registered in place. `data_service.py` and `mmr_cli.py:_handle_data_download` ask the registry instead of branching on `source`. Phase 2 adds `trader/data_providers/alpaca/` (REST client, timeframe map, completed-session rule, history provider) and switches defaults.

**Tech Stack:** Python 3.12, pandas, `requests` (already a dependency), `exchange-calendars` (already a dependency), pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-free-data-providers-design.md` (read §3 verified facts, §4.1–4.3, §5, §7, §8). Index: `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`.

## Global Constraints

- Branch `feat/free-data-providers`. One local commit per task. Never push.
- Commit subject style used in this repo: `feat(providers): …`, `refactor(providers): …`, `chore: …`. Last line of every commit message, after a blank line: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Use the project venv for every command (`.venv/bin/pytest`, `.venv/bin/python`, `.venv/bin/mmr`); the system Python has no pandas. Full suite: `.venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`. Pre-existing import errors in `test_aiorx.py`, `test_aiozmq_simple.py`, `test_disposable.py`, `test_mmr_client.py`, `test_mmr_server.py`, `test_perf2.py`, `test_performance.py` are known and ignored.
- No network in normal tests. Live tests are gated by env `MMR_LIVE_TESTS=1` **and** the needed keys.
- History frame contract (unchanged from today): tz-aware `DatetimeIndex` named `date`, sorted ascending; columns `open, high, low, close, volume, average, bar_count, bar_size, what_to_show`; `bar_size` is `str(BarSize)`; `what_to_show` is `int(WhatToShow.TRADES)`.
- History date semantics (match `MassiveHistoryWorker`): `start_date`/`end_date` are **inclusive whole days** — only their `.date()` matters.
- Fail loudly: never return empty data for an auth, entitlement or rate-limit failure; raise a `ProviderError` subclass with a message that names the provider and what to do.
- Alpaca requests: `feed=sip`, `adjustment=split`. Never fill gaps from IEX.
- Completed-session rule: Alpaca returns bars only up to the end (20:00 ET) of the last NYSE session for which `now >= 20:16 ET` on that session date.
- IB history stays on its existing separate code path (contract-based, async, persistent loop). It is offered as the CLI source `ib`, not as a registry provider.
- Comments only where the code is not obvious. Plain, intent-revealing names.

## Review Focus

1. **Mid-session download** (user runs `mmr data download AAPL --bar-size "1 min" --days 5 --source alpaca` at 11:00 ET on a weekday): expect today's bars to be *absent*, a log line saying the end was cut, and tomorrow's nightly refresh to fill today completely. → Task 9 test `test_mid_session_end_is_cut_to_previous_session`. UX note (not fixed here): when today is the only missing range, the CLI prints the yellow "No data returned for AAPL", which reads like a failure.
2. **Holiday / weekend runs** (download on Saturday, or on 2026-11-26 Thanksgiving): expect the last completed *trading* session, not a calendar day. → Task 8 tests `test_weekend_uses_friday`, `test_holiday_uses_previous_session`.
3. **Multi-page responses** (365 days of 1-min bars ≈ 37 pages of 10,000): expect all pages fetched in order with no duplicates. → Task 9 test `test_follows_next_page_token`.
4. **Bad or missing keys** (empty `alpaca_api_key_id`, or Alpaca returns 401/403): expect a `ProviderNotConfigured` / `ProviderEntitlementError` naming `ALPACA_API_KEY_ID` or the Alpaca message, never an empty frame counted as "no data". → Task 4 test `test_missing_key_names_env_var`, Task 7 tests `test_403_raises_entitlement_error_with_message`, `test_401_raises_entitlement_error`.
5. **Class shares and odd tickers** (IB-style `BRK B`, lower-case `aapl`, or junk like `AAPL;DROP`): expect `BRK.B`, `AAPL`, and a `ValueError` respectively — never a guessed symbol. → Task 9 tests `test_symbol_mapping`.

## Known findings (do not fix in these phases; report to the user)

- `TwelveDataHistoryWorker.get_history` returns an empty frame for intraday bars when `start_date == end_date` (its chunk loop is `while chunk_start < end_date`). `TickData.missing()` returns single-day ranges as `start == end`, so `data download --source twelvedata` can silently skip one-day gaps. Alpaca and Massive use whole-day inclusive semantics and are not affected. Phase 1 keeps TwelveData unchanged (no behaviour change). **User decision 2026-10-04: leave it** (TwelveData becomes opt-in); document it in Task 12's CLAUDE.md/docs update.

---

## File Structure

```
trader/data_providers/
├── __init__.py              # re-exports Capability, HistoryProvider, ProviderRegistry, errors   (Task 1-3)
├── capabilities.py          # Capability enum, HISTORY_COLUMNS, HistoryProvider protocol           (Task 1)
├── errors.py                # ProviderError + 4 subclasses                                          (Task 1)
├── registry.py              # ProviderSpec, ProviderRegistry                                        (Task 2)
├── builtin.py               # builtin_specs(), BUILTIN_DEFAULTS, history_source_choices()           (Task 3, 10)
├── rate_limit.py            # RateLimiter, call_with_retry                                          (Task 6)
├── symbols.py               # to_alpaca_symbol                                                      (Task 9)
└── alpaca/
    ├── __init__.py
    ├── client.py            # AlpacaClient: auth, rate limit, retry, error mapping, pagination      (Task 7)
    ├── timeframes.py        # to_alpaca_timeframe                                                   (Task 8)
    ├── sessions.py          # last_completed_session_end                                            (Task 8)
    └── history.py           # AlpacaHistoryProvider                                                 (Task 9)

tests/data_providers/
├── __init__.py
├── fixtures/alpaca_bars_aapl_1min_2023-01-03.json   (Task 9)
├── test_errors_and_capabilities.py                  (Task 1)
├── test_registry.py                                 (Task 2)
├── test_builtin.py                                  (Task 3, 10)
├── test_history_contract.py                         (Task 3, 9)
├── test_data_service_registry.py                    (Task 4)
├── test_cli_history_worker.py                       (Task 5, 10)
├── test_rate_limit.py                               (Task 6)
├── test_alpaca_client.py                            (Task 7)
├── test_alpaca_timeframes_sessions.py               (Task 8)
├── test_alpaca_history.py                           (Task 9)
└── test_live_alpaca.py                              (Task 11)

Modified:
trader/data_service.py, trader/messaging/data_service_api.py, trader/sdk.py (pull_history),
trader/mmr_cli.py (_handle_data_download, parser, _auto_source_for_universe, _load_default_data_source),
trader/config.py, trader/container.py, config_defaults/trader.yaml, config_defaults/data_refresh.yaml,
docker-compose.yml, docker.sh, start_mmr.sh, docs/OPERATIONAL_STATE.md

Deleted (Task 5):
trader/listeners/polygon_listener.py, trader/listeners/polygon_reactive.py,
trader/batch/polygon_batch.py, trader/batch/polygon_queuer.py
```

---

# Phase 1 — Registry foundation (no behaviour change)

### Task 1: Errors and capability protocol

**Files:**
- Create: `trader/data_providers/__init__.py`, `trader/data_providers/errors.py`, `trader/data_providers/capabilities.py`
- Create: `tests/data_providers/__init__.py` (empty), `tests/data_providers/test_errors_and_capabilities.py`

**Interfaces:**
- Produces: `ProviderError(Exception)`; `ProviderNotConfigured(provider: str, missing: Sequence[tuple[str, str]])` where each tuple is `(config_key, env_var)`; `CapabilityNotSupported(capability: str, source: str, supported: Sequence[str])`; `ProviderEntitlementError(ProviderError)`; `ProviderRateLimited(ProviderError)`. `Capability(str, Enum)` with member `HISTORY = 'history'`. `HISTORY_COLUMNS: tuple[str, ...]`. `HistoryProvider` (runtime-checkable Protocol) with `get_history(ticker, bar_size, start_date, end_date, timezone='US/Eastern') -> pd.DataFrame`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_errors_and_capabilities.py
import datetime as dt

import pandas as pd

from trader.data_providers.capabilities import Capability, HISTORY_COLUMNS, HistoryProvider
from trader.data_providers.errors import (
    CapabilityNotSupported,
    ProviderEntitlementError,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
)


def test_not_configured_names_every_key_and_env_var():
    err = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID'),
                                           ('alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY')])
    message = str(err)
    assert 'alpaca is not configured' in message
    for name in ('alpaca_api_key_id', 'ALPACA_API_KEY_ID', 'alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY'):
        assert name in message
    assert err.provider == 'alpaca'


def test_capability_not_supported_lists_alternatives():
    err = CapabilityNotSupported('history', 'finnhub', ['alpaca', 'massive'])
    assert "'finnhub' does not support history" in str(err)
    assert 'alpaca, massive' in str(err)


def test_all_errors_share_base_class():
    for cls in (ProviderNotConfigured, CapabilityNotSupported, ProviderEntitlementError, ProviderRateLimited):
        assert issubclass(cls, ProviderError)


def test_capability_values_are_stable_strings():
    assert Capability.HISTORY.value == 'history'
    assert Capability('history') is Capability.HISTORY


def test_history_columns_match_existing_contract():
    assert HISTORY_COLUMNS == ('open', 'high', 'low', 'close', 'volume',
                               'average', 'bar_count', 'bar_size', 'what_to_show')


def test_history_provider_protocol_is_structural():
    class Fake:
        def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
            return pd.DataFrame()

    assert isinstance(Fake(), HistoryProvider)
    assert not isinstance(object(), HistoryProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_errors_and_capabilities.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers'`

- [ ] **Step 3: Write minimal implementation**

```python
# trader/data_providers/errors.py
"""Typed failures for market-data providers. Callers catch ProviderError."""

from typing import Sequence


class ProviderError(Exception):
    pass


class ProviderNotConfigured(ProviderError):
    def __init__(self, provider: str, missing: Sequence[tuple[str, str]]):
        self.provider = provider
        self.missing = list(missing)
        settings = ', '.join(f'{key} (env {env})' for key, env in self.missing)
        super().__init__(f'{provider} is not configured: set {settings} in trader.yaml or the environment')


class CapabilityNotSupported(ProviderError):
    def __init__(self, capability: str, source: str, supported: Sequence[str]):
        self.capability = capability
        self.source = source
        self.supported = list(supported)
        super().__init__(
            f"source '{source}' does not support {capability}; "
            f"use one of: {', '.join(self.supported) or 'none configured'}"
        )


class ProviderEntitlementError(ProviderError):
    """The provider refused the request for plan, licence or key reasons."""


class ProviderRateLimited(ProviderError):
    """The provider's rate limit or quota is exhausted; retry later."""
```

```python
# trader/data_providers/capabilities.py
"""What a market-data provider can do, as structural interfaces."""

import datetime as dt
from enum import Enum
from typing import Protocol, runtime_checkable

import pandas as pd

from trader.objects import BarSize


class Capability(str, Enum):
    HISTORY = 'history'


HISTORY_COLUMNS: tuple[str, ...] = (
    'open', 'high', 'low', 'close', 'volume', 'average', 'bar_count', 'bar_size', 'what_to_show',
)


@runtime_checkable
class HistoryProvider(Protocol):
    def get_history(
        self,
        ticker: str,
        bar_size: BarSize,
        start_date: dt.datetime,
        end_date: dt.datetime,
        timezone: str = 'US/Eastern',
    ) -> pd.DataFrame:
        """Bars for whole days start_date..end_date inclusive, indexed by tz-aware `date`."""
        ...
```

```python
# trader/data_providers/__init__.py
from trader.data_providers.capabilities import Capability, HISTORY_COLUMNS, HistoryProvider
from trader.data_providers.errors import (
    CapabilityNotSupported,
    ProviderEntitlementError,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
)

__all__ = [
    'Capability', 'HISTORY_COLUMNS', 'HistoryProvider',
    'CapabilityNotSupported', 'ProviderEntitlementError', 'ProviderError',
    'ProviderNotConfigured', 'ProviderRateLimited',
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/data_providers/test_errors_and_capabilities.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers tests/data_providers
git commit -m "feat(providers): add provider errors and history capability protocol

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Provider registry

**Files:**
- Create: `trader/data_providers/registry.py`
- Modify: `trader/data_providers/__init__.py` (export `ProviderRegistry`, `ProviderSpec`)
- Test: `tests/data_providers/test_registry.py`

**Interfaces:**
- Consumes: Task 1 errors and `Capability`.
- Produces:
  - `ProviderSpec(name: str, required_keys: tuple[tuple[str, str], ...], builders: Mapping[Capability, Callable[[Mapping[str, Any]], object]])` — frozen dataclass.
  - `ProviderRegistry(config: Mapping[str, Any], specs: Iterable[ProviderSpec], defaults: Mapping[Capability, str])`
  - `.sources_for(capability) -> list[str]` (sorted, all registered sources that have a builder for it, configured or not)
  - `.default_source(capability) -> str` — precedence: `config['data_providers'][capability.value]` → `config['default_data_source']` if it supports the capability → `defaults[capability]`.
  - `.get(capability, source: str | None = None) -> object` — builds a **new** provider each call (providers are cheap; this keeps thread use safe, matching today's per-call worker construction).
  - Raises `CapabilityNotSupported` for unknown source or missing builder; `ProviderNotConfigured` when any required key is empty/whitespace/missing.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_registry.py
import pytest

from trader.data_providers.capabilities import Capability
from trader.data_providers.errors import CapabilityNotSupported, ProviderNotConfigured
from trader.data_providers.registry import ProviderRegistry, ProviderSpec


class FakeHistory:
    def __init__(self, key):
        self.key = key

    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        raise NotImplementedError


def _specs():
    return [
        ProviderSpec('alpha', (('alpha_key', 'ALPHA_KEY'),),
                     {Capability.HISTORY: lambda cfg: FakeHistory(cfg['alpha_key'])}),
        ProviderSpec('beta', (('beta_key', 'BETA_KEY'),),
                     {Capability.HISTORY: lambda cfg: FakeHistory(cfg['beta_key'])}),
        ProviderSpec('nohistory', (), {}),
    ]


def _registry(**config):
    return ProviderRegistry(config, _specs(), {Capability.HISTORY: 'alpha'})


def test_sources_for_lists_only_providers_with_a_builder():
    assert _registry().sources_for(Capability.HISTORY) == ['alpha', 'beta']


def test_get_builds_named_source_with_config():
    provider = _registry(beta_key='b-123').get(Capability.HISTORY, 'beta')
    assert isinstance(provider, FakeHistory)
    assert provider.key == 'b-123'


def test_get_without_source_uses_builtin_default():
    assert _registry(alpha_key='a').get(Capability.HISTORY).key == 'a'


def test_default_data_source_overrides_builtin_default_when_supported():
    registry = _registry(alpha_key='a', beta_key='b', default_data_source='beta')
    assert registry.default_source(Capability.HISTORY) == 'beta'


def test_default_data_source_ignored_when_it_lacks_the_capability():
    registry = _registry(default_data_source='nohistory')
    assert registry.default_source(Capability.HISTORY) == 'alpha'


def test_data_providers_mapping_wins_over_everything():
    registry = _registry(default_data_source='alpha', data_providers={'history': 'beta'})
    assert registry.default_source(Capability.HISTORY) == 'beta'


def test_unknown_source_raises_with_alternatives():
    with pytest.raises(CapabilityNotSupported) as info:
        _registry().get(Capability.HISTORY, 'gamma')
    assert info.value.supported == ['alpha', 'beta']


def test_source_without_capability_raises():
    with pytest.raises(CapabilityNotSupported):
        _registry().get(Capability.HISTORY, 'nohistory')


@pytest.mark.parametrize('value', ['', '   ', None])
def test_missing_or_blank_key_raises_not_configured(value):
    with pytest.raises(ProviderNotConfigured) as info:
        _registry(alpha_key=value).get(Capability.HISTORY, 'alpha')
    assert 'ALPHA_KEY' in str(info.value)


def test_each_get_returns_a_new_instance():
    registry = _registry(alpha_key='a')
    assert registry.get(Capability.HISTORY) is not registry.get(Capability.HISTORY)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# trader/data_providers/registry.py
"""Maps (capability, source) to a provider instance built from config."""

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from trader.data_providers.capabilities import Capability
from trader.data_providers.errors import CapabilityNotSupported, ProviderNotConfigured

ProviderBuilder = Callable[[Mapping[str, Any]], object]


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    required_keys: tuple[tuple[str, str], ...]
    builders: Mapping[Capability, ProviderBuilder]


class ProviderRegistry:
    def __init__(
        self,
        config: Mapping[str, Any],
        specs: Iterable[ProviderSpec],
        defaults: Mapping[Capability, str],
    ):
        self._config = config
        self._specs = {spec.name: spec for spec in specs}
        self._defaults = dict(defaults)

    def sources_for(self, capability: Capability) -> list[str]:
        return sorted(name for name, spec in self._specs.items() if capability in spec.builders)

    def default_source(self, capability: Capability) -> str:
        overrides = self._config.get('data_providers') or {}
        if overrides.get(capability.value):
            return overrides[capability.value]
        global_default = self._config.get('default_data_source')
        if global_default in self.sources_for(capability):
            return global_default
        return self._defaults[capability]

    def get(self, capability: Capability, source: Optional[str] = None) -> object:
        name = source or self.default_source(capability)
        spec = self._specs.get(name)
        if spec is None or capability not in spec.builders:
            raise CapabilityNotSupported(capability.value, name, self.sources_for(capability))
        self._require_keys(spec)
        return spec.builders[capability](self._config)

    def _require_keys(self, spec: ProviderSpec) -> None:
        missing = [(key, env) for key, env in spec.required_keys
                   if not str(self._config.get(key) or '').strip()]
        if missing:
            raise ProviderNotConfigured(spec.name, missing)
```

Add to `trader/data_providers/__init__.py`:

```python
from trader.data_providers.registry import ProviderRegistry, ProviderSpec
```

and append `'ProviderRegistry', 'ProviderSpec'` to `__all__`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/data_providers/test_registry.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers tests/data_providers/test_registry.py
git commit -m "feat(providers): add provider registry with config-driven defaults

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Register Massive and TwelveData history + shared contract test

**Files:**
- Create: `trader/data_providers/builtin.py`
- Modify: `trader/data_providers/registry.py` (add `ProviderRegistry.from_config`)
- Test: `tests/data_providers/test_builtin.py`, `tests/data_providers/test_history_contract.py`

**Interfaces:**
- Consumes: `ProviderSpec`, `ProviderRegistry`, `Capability`.
- Produces:
  - `builtin_specs() -> list[ProviderSpec]` (phase 1: `massive`, `twelvedata`)
  - `BUILTIN_DEFAULTS: dict[Capability, str]` (phase 1: `{Capability.HISTORY: 'twelvedata'}` — today's shipped default)
  - `history_source_choices() -> list[str]` = registry history sources + `['ib']`
  - `ProviderRegistry.from_config(config) -> ProviderRegistry` using `builtin_specs()` and `BUILTIN_DEFAULTS`
  - `assert_history_frame(df)` helper inside `tests/data_providers/test_history_contract.py` (reused by Task 9)

- [ ] **Step 1: Write the failing tests**

```python
# tests/data_providers/test_builtin.py
from trader.data_providers.builtin import BUILTIN_DEFAULTS, builtin_specs, history_source_choices
from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderRegistry
from trader.listeners.massive_history import MassiveHistoryWorker
from trader.listeners.twelvedata_history import TwelveDataHistoryWorker


def test_history_sources_include_rest_providers_and_ib():
    choices = history_source_choices()
    assert {'massive', 'twelvedata', 'ib'} <= set(choices)
    assert choices[-1] == 'ib'


def test_every_builtin_default_points_at_a_registered_source():
    names = {spec.name for spec in builtin_specs()}
    assert set(BUILTIN_DEFAULTS.values()) <= names


def test_from_config_builds_existing_workers():
    registry = ProviderRegistry.from_config({'massive_api_key': 'm', 'twelvedata_api_key': 't'})
    assert isinstance(registry.get(Capability.HISTORY, 'massive'), MassiveHistoryWorker)
    assert isinstance(registry.get(Capability.HISTORY, 'twelvedata'), TwelveDataHistoryWorker)
```

```python
# tests/data_providers/test_history_contract.py
"""Every HistoryProvider must return the same frame shape."""

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from trader.data_providers.capabilities import HISTORY_COLUMNS
from trader.objects import BarSize, WhatToShow


def assert_history_frame(df: pd.DataFrame, bar_size: BarSize) -> None:
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.name == 'date'
    assert df.index.tz is not None
    assert df.index.is_monotonic_increasing
    assert not df.index.has_duplicates
    assert tuple(df.columns) == HISTORY_COLUMNS
    assert set(df['bar_size']) == {str(bar_size)}
    assert set(df['what_to_show']) == {int(WhatToShow.TRADES)}


def test_massive_history_frame_matches_contract():
    from trader.listeners.massive_history import MassiveHistoryWorker
    agg = SimpleNamespace(timestamp=1717250400000, open=1.0, high=2.0, low=0.5,
                          close=1.5, volume=100.0, vwap=1.2, transactions=7)
    with patch('trader.listeners.massive_history.RESTClient') as client_cls:
        client_cls.return_value.list_aggs.return_value = [agg]
        worker = MassiveHistoryWorker(massive_api_key='k')
        df = worker.get_history('AAPL', BarSize.Mins1, dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 1))
    assert_history_frame(df, BarSize.Mins1)


def test_twelvedata_history_frame_matches_contract():
    from trader.listeners.twelvedata_history import TwelveDataHistoryWorker
    raw = pd.DataFrame(
        {'open': [1.0], 'high': [2.0], 'low': [0.5], 'close': [1.5], 'volume': [100]},
        index=pd.DatetimeIndex([pd.Timestamp('2024-06-03 09:30')], name='datetime'),
    )
    with patch('trader.listeners.twelvedata_history.TDClient') as client_cls:
        client_cls.return_value.time_series.return_value.as_pandas.return_value = raw
        worker = TwelveDataHistoryWorker(twelvedata_api_key='k')
        # Two-day window: TwelveDataHistoryWorker's intraday chunk loop returns
        # nothing when start == end (pre-existing quirk, see Known findings).
        df = worker.get_history('AAPL', BarSize.Mins1, dt.datetime(2024, 6, 3), dt.datetime(2024, 6, 4))
    assert_history_frame(df, BarSize.Mins1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/data_providers/test_builtin.py tests/data_providers/test_history_contract.py -v`
Expected: `test_builtin.py` FAILS with `ModuleNotFoundError: No module named 'trader.data_providers.builtin'`. The two contract tests PASS already (verified 2026-10-04 — they pin today's behaviour). If a contract test FAILS, **stop**: an existing worker breaks the contract; read its `get_history` and report the difference before changing anything.

- [ ] **Step 3: Write minimal implementation**

```python
# trader/data_providers/builtin.py
"""The providers MMR ships with, and which one each capability uses by default."""

from typing import Any, Mapping

from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderSpec


def _massive_history(config: Mapping[str, Any]):
    from trader.listeners.massive_history import MassiveHistoryWorker
    return MassiveHistoryWorker(massive_api_key=config['massive_api_key'])


def _twelvedata_history(config: Mapping[str, Any]):
    from trader.listeners.twelvedata_history import TwelveDataHistoryWorker
    return TwelveDataHistoryWorker(twelvedata_api_key=config['twelvedata_api_key'])


def builtin_specs() -> list[ProviderSpec]:
    return [
        ProviderSpec('massive', (('massive_api_key', 'MASSIVE_API_KEY'),),
                     {Capability.HISTORY: _massive_history}),
        ProviderSpec('twelvedata', (('twelvedata_api_key', 'TWELVEDATA_API_KEY'),),
                     {Capability.HISTORY: _twelvedata_history}),
    ]


BUILTIN_DEFAULTS: dict[Capability, str] = {
    Capability.HISTORY: 'twelvedata',
}

# IB history is contract-based and async, so it keeps its own code path and is
# offered only as a CLI source name, not as a registry provider.
IB_HISTORY_SOURCE = 'ib'


def history_source_choices() -> list[str]:
    rest_sources = sorted(spec.name for spec in builtin_specs() if Capability.HISTORY in spec.builders)
    return rest_sources + [IB_HISTORY_SOURCE]
```

Add to `ProviderRegistry` in `trader/data_providers/registry.py`:

```python
    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> 'ProviderRegistry':
        from trader.data_providers.builtin import BUILTIN_DEFAULTS, builtin_specs
        return cls(config, builtin_specs(), BUILTIN_DEFAULTS)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/data_providers -v`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers tests/data_providers
git commit -m "feat(providers): register Massive and TwelveData history behind the registry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Data service downloads through the registry

**Files:**
- Modify: `trader/data_service.py` — replace `_download_massive_one` (≈L103–141) and `_download_twelvedata_one` (≈L141–180) with one `_download_rest_one`; replace the duplicated bodies of `pull_massive` (≈L239) and `pull_twelvedata` (≈L315) with one `pull_history`; keep `pull_massive`/`pull_twelvedata` as one-line aliases; drop the now-unused `MassiveHistoryWorker`/`TwelveDataHistoryWorker` imports.
- Modify: `trader/messaging/data_service_api.py` — add `pull_history` rpcmethod.
- Modify: `trader/sdk.py` (≈L2555–2590) — add `MMR.pull_history(source, …)`.
- Test: `tests/data_providers/test_data_service_registry.py`

**Interfaces:**
- Consumes: `ProviderRegistry.from_config`, `Capability.HISTORY`, `ProviderError`.
- Produces:
  - `DataService.pull_history(source: str, symbols=None, universe=None, bar_size='1 day', prev_days=30, max_concurrent=5) -> dict` with today's result shape `{'enqueued', 'completed', 'failed', 'errors'}`.
  - `DataService._provider_config() -> dict` — the provider keys the service was constructed with (phase 2 adds Alpaca keys here).
  - `DataServiceApi.pull_history(source, symbols, universe, bar_size, prev_days, max_concurrent)`.
  - `MMR.pull_history(source, symbols=None, universe=None, bar_size='1 day', prev_days=30) -> dict`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_data_service_registry.py
import asyncio
import datetime as dt
from unittest.mock import patch

import pandas as pd
import pytest

from types import SimpleNamespace

from trader.data_service import DataService


def _security(symbol='AAPL'):
    # Only the attributes pull_history touches; SecurityDefinition has ~25 required fields.
    return SimpleNamespace(symbol=symbol, exchange='SMART', conId=265598,
                           primaryExchange='NASDAQ', timeZoneId='US/Eastern')


class RecordingProvider:
    calls = []

    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        RecordingProvider.calls.append(ticker)
        return pd.DataFrame()


def test_pull_history_reports_missing_key_without_downloading(tmp_duckdb_path):
    service = DataService(duckdb_path=tmp_duckdb_path)
    result = asyncio.run(service.pull_history('massive', symbols=['AAPL']))
    assert result['enqueued'] == 0
    assert 'MASSIVE_API_KEY' in result['errors'][0]


def test_pull_history_reports_unknown_source(tmp_duckdb_path):
    service = DataService(duckdb_path=tmp_duckdb_path)
    result = asyncio.run(service.pull_history('nope', symbols=['AAPL']))
    assert result['enqueued'] == 0
    assert "does not support history" in result['errors'][0]


def test_pull_history_routes_to_registry_provider(tmp_duckdb_path):
    RecordingProvider.calls = []
    service = DataService(massive_api_key='k', duckdb_path=tmp_duckdb_path)
    with patch.object(service, '_resolve_symbols', return_value=[_security()]), \
         patch('trader.data_providers.builtin._massive_history', return_value=RecordingProvider()):
        result = asyncio.run(service.pull_history('massive', symbols=['AAPL'], prev_days=3))
    assert RecordingProvider.calls and set(RecordingProvider.calls) == {'AAPL'}
    assert result['failed'] == 0


def test_legacy_aliases_delegate_to_pull_history(tmp_duckdb_path):
    service = DataService(duckdb_path=tmp_duckdb_path)
    with patch.object(service, 'pull_history', return_value={'ok': 1}) as pull:
        asyncio.run(service.pull_massive(symbols=['A']))
        asyncio.run(service.pull_twelvedata(symbols=['B']))
    assert [c.args[0] for c in pull.call_args_list] == ['massive', 'twelvedata']
```

Note: `tmp_duckdb_path` is an existing fixture in `tests/conftest.py`. `patch.object(service, 'pull_history', return_value=...)` on an `async def` is fine because `patch.object` auto-detects coroutine functions and creates an `AsyncMock`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_data_service_registry.py -v`
Expected: FAIL with `AttributeError: 'DataService' object has no attribute 'pull_history'`

- [ ] **Step 3: Write minimal implementation**

In `trader/data_service.py`:

1. Remove imports `from trader.listeners.massive_history import MassiveHistoryWorker` and `from trader.listeners.twelvedata_history import TwelveDataHistoryWorker`. Add:

```python
from trader.data_providers import Capability, ProviderError, ProviderRegistry
```

2. Add to `DataService`:

```python
    def _provider_config(self) -> dict:
        return {
            'massive_api_key': self.massive_api_key,
            'twelvedata_api_key': self.twelvedata_api_key,
        }
```

3. Replace both `_download_massive_one` and `_download_twelvedata_one` with:

```python
    async def _download_rest_one(
        self,
        sem: asyncio.Semaphore,
        source: str,
        provider,
        security: SecurityDefinition,
        date_range: DateRange,
        bar_size: BarSize,
        tick_data: TickData,
    ) -> dict:
        async with sem:
            self._running_count += 1
            try:
                logging.info('downloading {} {} from {} to {}'.format(
                    source, security.symbol, pdt(date_range.start), pdt(date_range.end)
                ))
                df = await asyncio.to_thread(
                    provider.get_history,
                    ticker=security.symbol,
                    bar_size=bar_size,
                    start_date=dateify(date_range.start, timezone=security.timeZoneId, make_sod=True),
                    end_date=dateify(date_range.end, timezone=security.timeZoneId, make_eod=True),
                    timezone=security.timeZoneId if security.timeZoneId else 'US/Eastern',
                )
                if len(df) > 0:
                    tick_data.write(security, df)
                    logging.debug('wrote {} rows for {}'.format(len(df), security.symbol))
                self._completed_count += 1
                return {'symbol': security.symbol, 'rows': len(df), 'ok': True}
            except Exception as ex:
                self._failed_count += 1
                logging.error('{} download failed for {}: {}'.format(source, security.symbol, ex))
                return {'symbol': security.symbol, 'error': str(ex), 'ok': False}
            finally:
                self._running_count -= 1
```

4. Replace the full bodies of `pull_massive` and `pull_twelvedata` with one `pull_history` (the body is today's `pull_massive` body with the key check replaced by a registry lookup and the task factory replaced) plus two aliases:

```python
    async def pull_history(
        self,
        source: str,
        symbols: Optional[list[str]] = None,
        universe: Optional[str] = None,
        bar_size: str = '1 day',
        prev_days: int = 30,
        max_concurrent: int = 5,
    ) -> dict:
        """Find missing date ranges, download them from `source`, write to DuckDB.

        Returns {'enqueued': N, 'completed': N, 'failed': N, 'errors': [...]}
        """
        try:
            provider = ProviderRegistry.from_config(self._provider_config()).get(Capability.HISTORY, source)
        except ProviderError as ex:
            return {'enqueued': 0, 'completed': 0, 'failed': 0, 'errors': [str(ex)]}

        securities = self._resolve_symbols(symbols, universe)
        if not securities:
            return {'enqueued': 0, 'completed': 0, 'failed': 0,
                    'errors': ['no securities resolved']}

        bs = BarSize.parse_str(bar_size)
        tick_data = TickStorage(self.history_duckdb_path).get_tickdata(bar_size=bs)

        start_date = dateify(dt.datetime.now() - dt.timedelta(days=prev_days + 1), make_sod=True)
        end_date = dateify(dt.datetime.now() - dt.timedelta(days=1), make_eod=True)

        sem = asyncio.Semaphore(max_concurrent)
        tasks = []

        for security in securities:
            tz_start = timezoneify(start_date, timezone=security.timeZoneId)
            tz_end = timezoneify(end_date, timezone=security.timeZoneId)

            exchange_calendar = _try_get_exchange_calendar(security)

            try:
                if exchange_calendar:
                    date_ranges = tick_data.missing(
                        security,
                        exchange_calendar,
                        date_range=DateRange(start=tz_start, end=tz_end),
                    )
                else:
                    date_ranges = [DateRange(start=tz_start, end=tz_end)]
            except Exception as ex:
                logging.warning('missing() failed for {}: {}, downloading full range'.format(
                    security.symbol, ex))
                date_ranges = [DateRange(start=tz_start, end=tz_end)]

            for dr in date_ranges:
                tasks.append(self._download_rest_one(sem, source, provider, security, dr, bs, tick_data))

        enqueued = len(tasks)
        if enqueued == 0:
            return {'enqueued': 0, 'completed': 0, 'failed': 0, 'errors': []}

        logging.info('enqueued {} {} download tasks'.format(enqueued, source))
        results = await asyncio.gather(*tasks, return_exceptions=True)

        errors = []
        completed = 0
        failed = 0
        for r in results:
            if isinstance(r, Exception):
                failed += 1
                errors.append(str(r))
            elif isinstance(r, dict) and r.get('ok'):
                completed += 1
            else:
                failed += 1
                if isinstance(r, dict):
                    errors.append(r.get('error', 'unknown error'))

        return {'enqueued': enqueued, 'completed': completed, 'failed': failed, 'errors': errors}

    async def pull_massive(self, symbols=None, universe=None, bar_size='1 day',
                           prev_days=30, max_concurrent=5) -> dict:
        return await self.pull_history('massive', symbols, universe, bar_size, prev_days, max_concurrent)

    async def pull_twelvedata(self, symbols=None, universe=None, bar_size='1 day',
                              prev_days=30, max_concurrent=5) -> dict:
        return await self.pull_history('twelvedata', symbols, universe, bar_size, prev_days, max_concurrent)
```

Before replacing, diff today's `pull_massive` and `pull_twelvedata` bodies (`sed -n 239,315p` vs `sed -n 315,395p trader/data_service.py`). If they differ in anything other than the key check, worker class and log strings, keep that difference in `pull_history` and note it in the commit body.

Update the module docstring line `1. Direct (from CLI): instantiate and call pull_massive()/pull_ib()` to `… call pull_history()/pull_ib()`.

5. In `trader/messaging/data_service_api.py` add:

```python
    @rpcmethod
    async def pull_history(
        self,
        source: str,
        symbols: Optional[list[str]] = None,
        universe: Optional[str] = None,
        bar_size: str = '1 day',
        prev_days: int = 30,
        max_concurrent: int = 5,
    ) -> dict:
        return await self.service.pull_history(source, symbols, universe, bar_size, prev_days, max_concurrent)
```

6. In `trader/sdk.py`, next to `pull_massive` (≈L2555), add (copy the call style of the existing `pull_massive` body exactly — it uses `self._data_rpc.rpc(return_type=dict)`):

```python
    def pull_history(
        self,
        source: str,
        symbols: Optional[List[str]] = None,
        universe: Optional[str] = None,
        bar_size: str = '1 day',
        prev_days: int = 30,
    ) -> dict:
        """Download historical data from any registry history source via the data_service."""
        return consume(
            self._data_rpc.rpc(return_type=dict).pull_history(
                source=source, symbols=symbols, universe=universe, bar_size=bar_size, prev_days=prev_days,
            )
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/data_providers tests/test_massive_history.py tests/test_twelvedata_history.py tests/test_history_split.py -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add trader/data_service.py trader/messaging/data_service_api.py trader/sdk.py tests/data_providers/test_data_service_registry.py
git commit -m "refactor(providers): route data_service REST history through the registry

pull_massive/pull_twelvedata stay as aliases of pull_history so existing
RPC clients and skill helpers keep working.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: CLI `data download` through the registry; delete dead polygon modules

**Files:**
- Modify: `trader/mmr_cli.py` — `_handle_data_download` (≈L8500–8560: key-check block and worker construction), data download parser (≈L1858–1864).
- Delete: `trader/listeners/polygon_listener.py`, `trader/listeners/polygon_reactive.py`, `trader/batch/polygon_batch.py`, `trader/batch/polygon_queuer.py`
- Test: `tests/data_providers/test_cli_history_worker.py`

**Interfaces:**
- Consumes: `ProviderRegistry.from_config`, `history_source_choices`, `ProviderError`.
- Produces: `_rest_history_worker(source: str, cfg: Mapping) -> HistoryProvider` in `mmr_cli.py` (raises `ProviderError`).

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_cli_history_worker.py
import pytest

from trader.data_providers.errors import CapabilityNotSupported, ProviderNotConfigured
from trader.listeners.massive_history import MassiveHistoryWorker


def test_rest_history_worker_builds_from_config():
    from trader.mmr_cli import _rest_history_worker
    assert isinstance(_rest_history_worker('massive', {'massive_api_key': 'k'}), MassiveHistoryWorker)


def test_rest_history_worker_missing_key_names_env_var():
    from trader.mmr_cli import _rest_history_worker
    with pytest.raises(ProviderNotConfigured, match='TWELVEDATA_API_KEY'):
        _rest_history_worker('twelvedata', {'twelvedata_api_key': ''})


def test_rest_history_worker_rejects_unknown_source():
    from trader.mmr_cli import _rest_history_worker
    with pytest.raises(CapabilityNotSupported):
        _rest_history_worker('yahoo', {})


def test_download_parser_offers_registry_sources():
    from trader.data_providers.builtin import history_source_choices
    from trader.mmr_cli import build_parser
    parser = build_parser()
    args = parser.parse_args(['data', 'download', 'AAPL', '--source', 'ib'])
    assert args.source == 'ib'
    for source in history_source_choices():
        assert parser.parse_args(['data', 'download', 'AAPL', '--source', source]).source == source


def test_dead_polygon_modules_are_gone():
    import importlib.util
    for name in ('trader.listeners.polygon_listener', 'trader.listeners.polygon_reactive',
                 'trader.batch.polygon_batch', 'trader.batch.polygon_queuer'):
        assert importlib.util.find_spec(name) is None, name
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_cli_history_worker.py -v`
Expected: FAIL with `ImportError: cannot import name '_rest_history_worker'` and the polygon test failing.

- [ ] **Step 3: Write minimal implementation**

1. Add near `_handle_data_download` in `trader/mmr_cli.py`:

```python
def _rest_history_worker(source: str, cfg):
    from trader.data_providers import Capability, ProviderRegistry
    return ProviderRegistry.from_config(cfg).get(Capability.HISTORY, source)
```

2. In `_handle_data_download`, replace the whole `api_key = ''` / `if source == 'twelvedata': … elif source == 'massive': … elif source == 'ib': pass else: … return` block with:

```python
    from trader.data_providers import ProviderError

    worker = None
    if source != 'ib':
        try:
            worker = _rest_history_worker(source, cfg)
        except ProviderError as ex:
            print_status(str(ex), success=False)
            return
```

and replace the later worker-construction block

```python
    if source == 'twelvedata':
        from trader.listeners.twelvedata_history import TwelveDataHistoryWorker
        worker = TwelveDataHistoryWorker(twelvedata_api_key=api_key)
    elif source == 'massive':
        from trader.listeners.massive_history import MassiveHistoryWorker
        worker = MassiveHistoryWorker(massive_api_key=api_key)
    else:  # source == 'ib'
```

with

```python
    if source == 'ib':
```

keeping the IB body unchanged (de-indent nothing; it was the `else` body, now the `if` body). Check with `grep -n "api_key" trader/mmr_cli.py | sed -n '1,200p'` that no reference to the removed local `api_key` remains inside `_handle_data_download`.

3. In the data download parser:

```python
    from trader.data_providers.builtin import history_source_choices
    data_dl_p.add_argument(
        '--source',
        choices=history_source_choices(),
        default=_src_default(history_source_choices(), 'massive'),
        help='Data source (default: from default_data_source in trader.yaml). Use `ib` for '
             'international exchanges (ASX, SEHK, TSE, EU) — requires trader_service / IB Gateway.',
    )
```

(The fallback stays `'massive'` in phase 1 — no behaviour change. Phase 2 changes it.)

4. Delete the dead modules:

```bash
git rm trader/listeners/polygon_listener.py trader/listeners/polygon_reactive.py \
       trader/batch/polygon_batch.py trader/batch/polygon_queuer.py
grep -rn "polygon_listener\|polygon_reactive\|polygon_batch\|polygon_queuer\|PolygonQueuer\|PolygonListener" \
     --include='*.py' --include='*.toml' --include='*.yaml' --include='*.sh' . 
```

Expected: the grep prints nothing. If it prints anything, remove that reference only if it is itself dead code; otherwise stop and report.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/data_providers -q && .venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`
Expected: data_providers all pass; full suite shows no new failures compared with a baseline run on the parent commit. (Record the baseline once, before Task 1, on the untouched branch: `.venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py | tail -15` and keep the failure list.)

- [ ] **Step 5: Manual check (no behaviour change)**

Run: `.venv/bin/mmr data download AAPL --bar-size "1 day" --days 5 --source massive` with no Massive key configured.
Expected: red status `massive is not configured: set massive_api_key (env MASSIVE_API_KEY) in trader.yaml or the environment`.

- [ ] **Step 6: Commit**

```bash
git add -A trader/mmr_cli.py tests/data_providers/test_cli_history_worker.py
git commit -m "refactor(providers): route CLI data download through the registry; drop dead polygon modules

The four polygon_* modules imported polygon/arctic/ib_insync, none of which
are dependencies, and nothing imported them.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

**Phase 1 is done here.** Run the full suite once more and compare with the baseline before starting phase 2.

---

# Phase 2 — Alpaca history (free default)

### Task 6: Rate limiter and 429 retry

**Files:**
- Create: `trader/data_providers/rate_limit.py`
- Test: `tests/data_providers/test_rate_limit.py`

**Interfaces:**
- Produces:
  - `RateLimiter(calls: int, period: float, clock=time.monotonic, sleep=time.sleep)` with `.acquire() -> None` (thread-safe sliding window).
  - `call_with_retry(send: Callable[[], requests.Response], *, provider: str, max_tries: int = 3, base_delay: float = 1.0, sleep=time.sleep) -> requests.Response` — retries only HTTP 429; honours a numeric `Retry-After` header; otherwise waits `base_delay * 2**(attempt-1)`; after the last try raises `ProviderRateLimited`.

Spec deviation note: spec §7 says "`backoff` package". A 15-line loop with injectable `sleep` is simpler to test and read; behaviour is the same (exponential, max 3 tries). Update spec §7 wording in Task 12.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_rate_limit.py
import pytest

from trader.data_providers.errors import ProviderRateLimited
from trader.data_providers.rate_limit import RateLimiter, call_with_retry


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


def test_limiter_allows_burst_up_to_limit_without_sleeping():
    fake = FakeClock()
    limiter = RateLimiter(3, 60.0, clock=fake.clock, sleep=fake.sleep)
    for _ in range(3):
        limiter.acquire()
    assert fake.sleeps == []


def test_limiter_waits_for_oldest_call_to_leave_window():
    fake = FakeClock()
    limiter = RateLimiter(2, 60.0, clock=fake.clock, sleep=fake.sleep)
    limiter.acquire()
    fake.now = 10.0
    limiter.acquire()
    limiter.acquire()
    assert fake.sleeps == [50.0]


def test_retry_returns_first_non_429_response():
    responses = iter([FakeResponse(429), FakeResponse(200)])
    fake = FakeClock()
    result = call_with_retry(lambda: next(responses), provider='alpaca', sleep=fake.sleep)
    assert result.status_code == 200
    assert fake.sleeps == [1.0]


def test_retry_honours_retry_after_header():
    responses = iter([FakeResponse(429, {'Retry-After': '7'}), FakeResponse(200)])
    fake = FakeClock()
    call_with_retry(lambda: next(responses), provider='alpaca', sleep=fake.sleep)
    assert fake.sleeps == [7.0]


def test_retry_gives_up_with_rate_limited_error():
    fake = FakeClock()
    with pytest.raises(ProviderRateLimited, match='alpaca'):
        call_with_retry(lambda: FakeResponse(429), provider='alpaca', max_tries=3, sleep=fake.sleep)
    assert fake.sleeps == [1.0, 2.0]


def test_non_429_errors_are_not_retried():
    calls = []

    def send():
        calls.append(1)
        return FakeResponse(500)

    assert call_with_retry(send, provider='alpaca', sleep=lambda s: None).status_code == 500
    assert len(calls) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_rate_limit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.rate_limit'`

- [ ] **Step 3: Write minimal implementation**

```python
# trader/data_providers/rate_limit.py
"""Client-side pacing so we stay under a provider's published limits."""

import threading
import time
from collections import deque
from typing import Callable

from trader.data_providers.errors import ProviderRateLimited

HTTP_TOO_MANY_REQUESTS = 429


class RateLimiter:
    """At most `calls` acquisitions in any `period`-second window, across threads."""

    def __init__(self, calls: int, period: float, clock=time.monotonic, sleep=time.sleep):
        self._calls = calls
        self._period = period
        self._clock = clock
        self._sleep = sleep
        self._recent: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = self._clock()
                while self._recent and now - self._recent[0] >= self._period:
                    self._recent.popleft()
                if len(self._recent) < self._calls:
                    self._recent.append(now)
                    return
                wait = self._period - (now - self._recent[0])
            self._sleep(wait)


def call_with_retry(send: Callable, *, provider: str, max_tries: int = 3,
                    base_delay: float = 1.0, sleep=time.sleep):
    for attempt in range(1, max_tries + 1):
        response = send()
        if response.status_code != HTTP_TOO_MANY_REQUESTS:
            return response
        if attempt == max_tries:
            break
        sleep(_retry_delay(response, base_delay, attempt))
    raise ProviderRateLimited(f'{provider} rate limit hit after {max_tries} tries; retry later')


def _retry_delay(response, base_delay: float, attempt: int) -> float:
    retry_after = response.headers.get('Retry-After', '')
    if retry_after.isdigit():
        return float(retry_after)
    return base_delay * 2 ** (attempt - 1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/data_providers/test_rate_limit.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers/rate_limit.py tests/data_providers/test_rate_limit.py
git commit -m "feat(providers): add shared rate limiter and 429 retry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Alpaca REST client

**Files:**
- Create: `trader/data_providers/alpaca/__init__.py` (empty), `trader/data_providers/alpaca/client.py`
- Test: `tests/data_providers/test_alpaca_client.py`

**Interfaces:**
- Consumes: `RateLimiter`, `call_with_retry`, `ProviderEntitlementError`, `ProviderError`.
- Produces:
  - `ALPACA_DATA_URL = 'https://data.alpaca.markets'`
  - `AlpacaClient(key_id: str, secret_key: str, session: requests.Session | None = None, limiter: RateLimiter | None = None, base_url: str = ALPACA_DATA_URL)`
  - `.get_json(path: str, params: Mapping[str, Any]) -> dict`
  - `.paginate(path: str, params: Mapping[str, Any]) -> Iterator[dict]` — yields each page's JSON, following `next_page_token` via the `page_token` param until it is empty.
  - Module-level `ALPACA_LIMITER = RateLimiter(200, 60.0)` shared by every client in the process (spec §7).
  - Error mapping: 401 → `ProviderEntitlementError('alpaca rejected the API key (HTTP 401); check ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY')`; 403 → `ProviderEntitlementError(f'alpaca refused {path}: {message}')` where `message` is the JSON `message` field; other ≥400 → `ProviderError(f'alpaca {path} failed: HTTP {code} {message}')`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_alpaca_client.py
import pytest

from trader.data_providers.alpaca.client import AlpacaClient
from trader.data_providers.errors import ProviderEntitlementError, ProviderError


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({'url': url, 'params': dict(params or {}), 'headers': headers, 'timeout': timeout})
        return self._responses.pop(0)


class NoWaitLimiter:
    def acquire(self):
        pass


def _client(*responses):
    session = FakeSession(responses)
    return AlpacaClient('kid', 'secret', session=session, limiter=NoWaitLimiter()), session


def test_sends_auth_headers_and_timeout():
    client, session = _client(FakeResponse(200, {'ok': True}))
    assert client.get_json('/v2/x', {'a': 1}) == {'ok': True}
    sent = session.requests[0]
    assert sent['url'] == 'https://data.alpaca.markets/v2/x'
    assert sent['headers'] == {'APCA-API-KEY-ID': 'kid', 'APCA-API-SECRET-KEY': 'secret'}
    assert sent['params'] == {'a': 1}
    assert sent['timeout'] == 30


def test_403_raises_entitlement_error_with_message():
    client, _ = _client(FakeResponse(403, {'message': 'subscription does not permit querying recent SIP data'}))
    with pytest.raises(ProviderEntitlementError, match='recent SIP data'):
        client.get_json('/v2/stocks/bars', {})


def test_401_raises_entitlement_error():
    client, _ = _client(FakeResponse(401, {'message': 'unauthorized'}))
    with pytest.raises(ProviderEntitlementError, match='ALPACA_API_KEY_ID'):
        client.get_json('/v2/stocks/bars', {})


def test_other_http_errors_raise_provider_error():
    client, _ = _client(FakeResponse(400, {'message': 'invalid timeframe: 1Sec'}))
    with pytest.raises(ProviderError, match='HTTP 400 invalid timeframe'):
        client.get_json('/v2/stocks/bars', {})


def test_paginate_follows_next_page_token():
    client, session = _client(
        FakeResponse(200, {'bars': {'AAPL': [1]}, 'next_page_token': 'p2'}),
        FakeResponse(200, {'bars': {'AAPL': [2]}, 'next_page_token': None}),
    )
    pages = list(client.paginate('/v2/stocks/bars', {'symbols': 'AAPL'}))
    assert [p['bars']['AAPL'] for p in pages] == [[1], [2]]
    assert 'page_token' not in session.requests[0]['params']
    assert session.requests[1]['params']['page_token'] == 'p2'
    assert session.requests[1]['params']['symbols'] == 'AAPL'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_alpaca_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.alpaca'`

- [ ] **Step 3: Write minimal implementation**

```python
# trader/data_providers/alpaca/client.py
"""Thin authenticated client for Alpaca's market-data REST API."""

from typing import Any, Iterator, Mapping, Optional

import requests

from trader.data_providers.errors import ProviderEntitlementError, ProviderError
from trader.data_providers.rate_limit import RateLimiter, call_with_retry

ALPACA_DATA_URL = 'https://data.alpaca.markets'
REQUEST_TIMEOUT_SECS = 30

# Basic plan: 200 REST calls per minute, shared by every client in this process.
ALPACA_LIMITER = RateLimiter(200, 60.0)


class AlpacaClient:
    """One requests.Session per client. data_service shares one client across its
    download threads; requests.Session is used that way widely but is not formally
    thread-safe, so keep per-call state out of this class."""

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        session: Optional[requests.Session] = None,
        limiter: Optional[RateLimiter] = None,
        base_url: str = ALPACA_DATA_URL,
    ):
        self._headers = {'APCA-API-KEY-ID': key_id, 'APCA-API-SECRET-KEY': secret_key}
        self._session = session or requests.Session()
        self._limiter = limiter or ALPACA_LIMITER
        self._base_url = base_url

    def get_json(self, path: str, params: Mapping[str, Any]) -> dict:
        self._limiter.acquire()
        response = call_with_retry(
            lambda: self._session.get(self._base_url + path, params=dict(params),
                                      headers=self._headers, timeout=REQUEST_TIMEOUT_SECS),
            provider='alpaca',
        )
        if response.status_code >= 400:
            raise _error_for(path, response)
        return response.json()

    def paginate(self, path: str, params: Mapping[str, Any]) -> Iterator[dict]:
        page_params = dict(params)
        while True:
            page = self.get_json(path, page_params)
            yield page
            token = page.get('next_page_token')
            if not token:
                return
            page_params['page_token'] = token


def _error_for(path: str, response) -> ProviderError:
    try:
        message = response.json().get('message', '')
    except ValueError:
        message = ''
    if response.status_code == 401:
        return ProviderEntitlementError(
            'alpaca rejected the API key (HTTP 401); check ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY'
        )
    if response.status_code == 403:
        return ProviderEntitlementError(f'alpaca refused {path}: {message}')
    return ProviderError(f'alpaca {path} failed: HTTP {response.status_code} {message}')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/data_providers/test_alpaca_client.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers/alpaca tests/data_providers/test_alpaca_client.py
git commit -m "feat(providers): add Alpaca market-data REST client

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Alpaca timeframes and the completed-session rule

**Files:**
- Create: `trader/data_providers/alpaca/timeframes.py`, `trader/data_providers/alpaca/sessions.py`
- Test: `tests/data_providers/test_alpaca_timeframes_sessions.py`

**Interfaces:**
- Produces:
  - `to_alpaca_timeframe(bar_size: BarSize) -> str` — raises `ValueError('unsupported BarSize for Alpaca: … (no seconds bars; use --source ib or massive)')` for seconds bars.
  - `SESSION_COMPLETE_ET = dt.time(20, 16)`, `SESSION_END_ET = dt.time(20, 0)`
  - `last_completed_session_end(now: dt.datetime, calendar=None) -> dt.datetime` — tz-aware US/Eastern datetime at 20:00 of the latest XNYS session that is complete at `now` (`now` must be tz-aware; naive raises `ValueError`).

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_alpaca_timeframes_sessions.py
import datetime as dt

import pytest
import pytz

from trader.data_providers.alpaca.sessions import last_completed_session_end
from trader.data_providers.alpaca.timeframes import to_alpaca_timeframe
from trader.objects import BarSize

ET = pytz.timezone('US/Eastern')


def _et(*args):
    return ET.localize(dt.datetime(*args))


@pytest.mark.parametrize('bar_size, expected', [
    (BarSize.Mins1, '1Min'), (BarSize.Mins2, '2Min'), (BarSize.Mins3, '3Min'),
    (BarSize.Mins5, '5Min'), (BarSize.Mins10, '10Min'), (BarSize.Mins15, '15Min'),
    (BarSize.Mins20, '20Min'), (BarSize.Mins30, '30Min'), (BarSize.Hours1, '1Hour'),
    (BarSize.Hours2, '2Hour'), (BarSize.Hours3, '3Hour'), (BarSize.Hours4, '4Hour'),
    (BarSize.Hours8, '8Hour'), (BarSize.Days1, '1Day'), (BarSize.Weeks1, '1Week'),
    (BarSize.Months1, '1Month'),
])
def test_timeframe_mapping(bar_size, expected):
    assert to_alpaca_timeframe(bar_size) == expected


@pytest.mark.parametrize('bar_size', [BarSize.Secs1, BarSize.Secs5, BarSize.Secs10,
                                      BarSize.Secs15, BarSize.Secs30])
def test_seconds_bars_are_rejected_with_alternative(bar_size):
    with pytest.raises(ValueError, match='use --source ib or massive'):
        to_alpaca_timeframe(bar_size)


def test_mid_session_returns_previous_session():
    # Thursday 2026-10-01 11:00 ET -> Wednesday 2026-09-30 20:00 ET
    assert last_completed_session_end(_et(2026, 10, 1, 11, 0)) == _et(2026, 9, 30, 20, 0)


def test_just_before_cutoff_returns_previous_session():
    assert last_completed_session_end(_et(2026, 10, 1, 20, 15)) == _et(2026, 9, 30, 20, 0)


def test_after_cutoff_returns_today():
    assert last_completed_session_end(_et(2026, 10, 1, 20, 16)) == _et(2026, 10, 1, 20, 0)


def test_weekend_uses_friday():
    # Sunday 2026-10-04 -> Friday 2026-10-02
    assert last_completed_session_end(_et(2026, 10, 4, 9, 0)) == _et(2026, 10, 2, 20, 0)


def test_holiday_uses_previous_session():
    # Thanksgiving Thursday 2026-11-26 is closed -> Wednesday 2026-11-25
    assert last_completed_session_end(_et(2026, 11, 26, 22, 0)) == _et(2026, 11, 25, 20, 0)


def test_utc_input_is_converted():
    now_utc = dt.datetime(2026, 10, 2, 0, 30, tzinfo=dt.timezone.utc)  # 2026-10-01 20:30 ET
    assert last_completed_session_end(now_utc) == _et(2026, 10, 1, 20, 0)


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match='timezone-aware'):
        last_completed_session_end(dt.datetime(2026, 10, 1, 12, 0))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/data_providers/test_alpaca_timeframes_sessions.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# trader/data_providers/alpaca/timeframes.py
from trader.objects import BarSize

_TIMEFRAMES = {
    BarSize.Mins1: '1Min', BarSize.Mins2: '2Min', BarSize.Mins3: '3Min',
    BarSize.Mins5: '5Min', BarSize.Mins10: '10Min', BarSize.Mins15: '15Min',
    BarSize.Mins20: '20Min', BarSize.Mins30: '30Min',
    BarSize.Hours1: '1Hour', BarSize.Hours2: '2Hour', BarSize.Hours3: '3Hour',
    BarSize.Hours4: '4Hour', BarSize.Hours8: '8Hour',
    BarSize.Days1: '1Day', BarSize.Weeks1: '1Week', BarSize.Months1: '1Month',
}


def to_alpaca_timeframe(bar_size: BarSize) -> str:
    if bar_size not in _TIMEFRAMES:
        raise ValueError(
            f'unsupported BarSize for Alpaca: {bar_size} (no seconds bars; use --source ib or massive)'
        )
    return _TIMEFRAMES[bar_size]
```

```python
# trader/data_providers/alpaca/sessions.py
"""Which NYSE session is fully available on Alpaca's free (Basic) plan.

Basic blocks SIP data newer than ~15 minutes, and post-market runs to
20:00 ET. Writing a half-finished session would make TickData.missing()
treat the day as present and never backfill it, so only completed sessions
are fetched.
"""

import datetime as dt

import exchange_calendars
import pandas as pd
import pytz

ET = pytz.timezone('US/Eastern')
SESSION_END_ET = dt.time(20, 0)
SESSION_COMPLETE_ET = dt.time(20, 16)
_LOOKBACK_DAYS = 14


def last_completed_session_end(now: dt.datetime, calendar=None) -> dt.datetime:
    if now.tzinfo is None:
        raise ValueError('now must be timezone-aware')
    calendar = calendar or exchange_calendars.get_calendar('XNYS')
    now_et = now.astimezone(ET)
    today = now_et.date()
    sessions = calendar.sessions_in_range(
        pd.Timestamp(today - dt.timedelta(days=_LOOKBACK_DAYS)), pd.Timestamp(today)
    )
    for session in reversed(sessions):
        day = session.date()
        if day < today or now_et.time() >= SESSION_COMPLETE_ET:
            return ET.localize(dt.datetime.combine(day, SESSION_END_ET))
    raise ValueError(f'no NYSE session in the {_LOOKBACK_DAYS} days before {today}')
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/data_providers/test_alpaca_timeframes_sessions.py -v`
Expected: 28 passed. If `test_holiday_uses_previous_session` fails, print `exchange_calendars.get_calendar('XNYS').sessions_in_range('2026-11-20','2026-11-30')` and confirm the calendar's 2026 holidays before changing code.

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers/alpaca tests/data_providers/test_alpaca_timeframes_sessions.py
git commit -m "feat(providers): add Alpaca timeframe map and completed-session rule

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Alpaca history provider

**Files:**
- Create: `trader/data_providers/symbols.py`, `trader/data_providers/alpaca/history.py`
- Create: `tests/data_providers/fixtures/alpaca_bars_aapl_1min_2023-01-03.json`
- Modify: `tests/data_providers/test_history_contract.py` (add Alpaca case)
- Test: `tests/data_providers/test_alpaca_history.py`

**Interfaces:**
- Consumes: `AlpacaClient`, `to_alpaca_timeframe`, `last_completed_session_end`, `HISTORY_COLUMNS`.
- Produces:
  - `to_alpaca_symbol(symbol: str) -> str` — strips, upper-cases, maps one inner space to `.` (`'BRK B'` → `'BRK.B'`); raises `ValueError` for empty or anything outside `[A-Z0-9.]`.
  - `AlpacaHistoryProvider(client: AlpacaClient, now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc))` implementing `HistoryProvider`. Requests `GET /v2/stocks/bars` with `symbols`, `timeframe`, `start`, `end` (RFC 3339 UTC), `feed=sip`, `adjustment=split`, `limit=10000`, `sort=asc`.

- [ ] **Step 1: Create the fixture** (real response captured 2026-10-04, trimmed to 3 bars)

```json
{"bars":{"AAPL":[
 {"c":129.76,"h":130.6999,"l":129.76,"n":23332,"o":130.25,"t":"2023-01-03T14:30:00Z","v":2844202,"vw":130.225278},
 {"c":129.65,"h":129.87,"l":129.46,"n":10712,"o":129.7682,"t":"2023-01-03T14:31:00Z","v":539908,"vw":129.669286},
 {"c":129.46,"h":129.84,"l":129.46,"n":9580,"o":129.64,"t":"2023-01-03T14:32:00Z","v":428709,"vw":129.673211}
]},"next_page_token":null}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/data_providers/test_alpaca_history.py
import datetime as dt
import json
from pathlib import Path

import pytest
import pytz

from trader.data_providers.alpaca.history import AlpacaHistoryProvider
from trader.data_providers.symbols import to_alpaca_symbol
from trader.objects import BarSize, WhatToShow

FIXTURE = Path(__file__).parent / 'fixtures' / 'alpaca_bars_aapl_1min_2023-01-03.json'
ET = pytz.timezone('US/Eastern')
A_LATER_SUNDAY = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.params = None

    def paginate(self, path, params):
        self.path, self.params = path, dict(params)
        yield from self.pages


def _provider(pages, now=A_LATER_SUNDAY):
    client = FakeClient(pages)
    return AlpacaHistoryProvider(client, now=lambda: now), client


def test_converts_bars_to_history_frame():
    provider, client = _provider([json.loads(FIXTURE.read_text())])
    df = provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3))
    assert len(df) == 3
    first = df.iloc[0]
    assert df.index[0] == ET.localize(dt.datetime(2023, 1, 3, 9, 30))
    assert (first.open, first.high, first.low, first.close) == (130.25, 130.6999, 129.76, 129.76)
    assert first.volume == 2844202
    assert first.average == 130.225278
    assert first.bar_count == 23332
    assert first.bar_size == '1 min'
    assert first.what_to_show == int(WhatToShow.TRADES)


def test_request_params_use_sip_split_adjustment_and_whole_days():
    provider, client = _provider([{'bars': {}}])
    provider.get_history('AAPL', BarSize.Days1, dt.datetime(2023, 1, 3, 15, 0), dt.datetime(2023, 1, 5, 1, 0))
    assert client.path == '/v2/stocks/bars'
    assert client.params == {
        'symbols': 'AAPL', 'timeframe': '1Day', 'feed': 'sip', 'adjustment': 'split',
        'limit': 10000, 'sort': 'asc',
        'start': '2023-01-03T05:00:00Z',   # 00:00 ET
        'end': '2023-01-06T04:59:59Z',     # 23:59:59 ET on 2023-01-05
    }


def test_follows_next_page_token():
    page1 = {'bars': {'AAPL': [{'t': '2023-01-03T14:30:00Z', 'o': 1, 'h': 1, 'l': 1, 'c': 1, 'v': 1, 'vw': 1, 'n': 1}]}}
    page2 = {'bars': {'AAPL': [{'t': '2023-01-03T14:31:00Z', 'o': 2, 'h': 2, 'l': 2, 'c': 2, 'v': 2, 'vw': 2, 'n': 2}]}}
    provider, _ = _provider([page1, page2])
    df = provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3))
    assert list(df.close) == [1, 2]


def test_mid_session_end_is_cut_to_previous_session():
    from unittest.mock import patch
    thursday_11am = ET.localize(dt.datetime(2026, 10, 1, 11, 0))
    provider, client = _provider([{'bars': {}}], now=thursday_11am)
    with patch('trader.data_providers.alpaca.history.logging') as log:
        provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2026, 9, 28), dt.datetime(2026, 10, 1))
    assert client.params['end'] == '2026-10-01T00:00:00Z'   # Wed 2026-09-30 20:00 ET
    assert any('end cut' in call.args[0] for call in log.info.call_args_list)


def test_window_entirely_after_last_completed_session_returns_empty_without_request():
    thursday_11am = ET.localize(dt.datetime(2026, 10, 1, 11, 0))
    provider, client = _provider([{'bars': {}}], now=thursday_11am)
    df = provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2026, 10, 1), dt.datetime(2026, 10, 1))
    assert df.empty
    assert client.params is None


def test_no_bars_returns_empty_frame():
    provider, _ = _provider([{'bars': {}}])
    assert provider.get_history('AAPL', BarSize.Days1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3)).empty


def test_output_timezone_follows_argument():
    provider, _ = _provider([json.loads(FIXTURE.read_text())])
    df = provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3),
                              timezone='UTC')
    assert str(df.index.tz) == 'UTC'
    assert df.index[0].hour == 14


@pytest.mark.parametrize('raw, expected', [('AAPL', 'AAPL'), (' aapl ', 'AAPL'), ('BRK B', 'BRK.B'),
                                           ('BRK.B', 'BRK.B')])
def test_symbol_mapping(raw, expected):
    assert to_alpaca_symbol(raw) == expected


@pytest.mark.parametrize('raw', ['', '  ', 'AAPL;DROP', 'BRK  B', 'A/B'])
def test_symbol_mapping_rejects_junk(raw):
    with pytest.raises(ValueError):
        to_alpaca_symbol(raw)
```

Add to `tests/data_providers/test_history_contract.py`:

```python
def test_alpaca_history_frame_matches_contract():
    import json
    from pathlib import Path
    from trader.data_providers.alpaca.history import AlpacaHistoryProvider

    page = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_bars_aapl_1min_2023-01-03.json').read_text())

    class OnePage:
        def paginate(self, path, params):
            yield page

    provider = AlpacaHistoryProvider(OnePage(), now=lambda: dt.datetime(2026, 10, 4, tzinfo=dt.timezone.utc))
    df = provider.get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3))
    assert_history_frame(df, BarSize.Mins1)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/data_providers/test_alpaca_history.py tests/data_providers/test_history_contract.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.alpaca.history'`

- [ ] **Step 4: Write minimal implementation**

```python
# trader/data_providers/symbols.py
"""Exact, per-provider ticker spelling. Never guesses."""

import re

_ALPACA_SYMBOL = re.compile(r'^[A-Z0-9]+(\.[A-Z0-9]+)?$')


def to_alpaca_symbol(symbol: str) -> str:
    """IB writes class shares as 'BRK B'; Alpaca expects 'BRK.B'."""
    candidate = symbol.strip().upper().replace(' ', '.')
    if not _ALPACA_SYMBOL.match(candidate):
        raise ValueError(f'not a valid Alpaca stock symbol: {symbol!r}')
    return candidate
```

```python
# trader/data_providers/alpaca/history.py
"""Historical bars from Alpaca (SIP feed, split-adjusted, completed sessions only)."""

import datetime as dt
from typing import Callable

import pandas as pd
import pytz

from trader.common.logging_helper import setup_logging
from trader.data_providers.alpaca.sessions import ET, last_completed_session_end
from trader.data_providers.alpaca.timeframes import to_alpaca_timeframe
from trader.data_providers.capabilities import HISTORY_COLUMNS
from trader.data_providers.symbols import to_alpaca_symbol
from trader.objects import BarSize, WhatToShow

logging = setup_logging(module_name='alpaca_history')

BARS_PATH = '/v2/stocks/bars'
PAGE_LIMIT = 10000


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class AlpacaHistoryProvider:
    def __init__(self, client, now: Callable[[], dt.datetime] = _utc_now):
        self._client = client
        self._now = now

    def get_history(
        self,
        ticker: str,
        bar_size: BarSize,
        start_date: dt.datetime,
        end_date: dt.datetime,
        timezone: str = 'US/Eastern',
    ) -> pd.DataFrame:
        symbol = to_alpaca_symbol(ticker)
        start = ET.localize(dt.datetime.combine(start_date.date() if isinstance(start_date, dt.datetime)
                                                else start_date, dt.time(0, 0)))
        requested_end = ET.localize(dt.datetime.combine(end_date.date() if isinstance(end_date, dt.datetime)
                                                        else end_date, dt.time(23, 59, 59)))
        end = min(requested_end, last_completed_session_end(self._now()))
        if end < requested_end:
            logging.info('alpaca {}: end cut from {} to {} (only completed sessions are fetched)'.format(
                symbol, requested_end.isoformat(), end.isoformat()))
        if end <= start:
            return pd.DataFrame()

        params = {
            'symbols': symbol,
            'timeframe': to_alpaca_timeframe(bar_size),
            'start': _rfc3339_utc(start),
            'end': _rfc3339_utc(end),
            'feed': 'sip',
            'adjustment': 'split',
            'limit': PAGE_LIMIT,
            'sort': 'asc',
        }
        bars = [bar for page in self._client.paginate(BARS_PATH, params)
                for bar in (page.get('bars') or {}).get(symbol, [])]
        if not bars:
            logging.info('alpaca {}: no bars from {} to {}'.format(symbol, params['start'], params['end']))
            return pd.DataFrame()
        return _to_frame(bars, bar_size, timezone)


def _rfc3339_utc(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _to_frame(bars: list[dict], bar_size: BarSize, timezone: str) -> pd.DataFrame:
    frame = pd.DataFrame({
        'date': pd.to_datetime([bar['t'] for bar in bars], utc=True).tz_convert(pytz.timezone(timezone)),
        'open': [bar['o'] for bar in bars],
        'high': [bar['h'] for bar in bars],
        'low': [bar['l'] for bar in bars],
        'close': [bar['c'] for bar in bars],
        'volume': [bar['v'] for bar in bars],
        'average': [bar.get('vw') for bar in bars],
        'bar_count': [bar.get('n') for bar in bars],
        'bar_size': str(bar_size),
        'what_to_show': int(WhatToShow.TRADES),
    }).set_index('date')
    frame = frame[~frame.index.duplicated(keep='last')].sort_index()
    return frame[list(HISTORY_COLUMNS)]
```

Note on the `start`/`end` normalisation: callers pass either `dt.datetime` (data service, CLI) or `dt.date` (ranges from `TickData.missing()`); both reduce to whole ET days, matching `MassiveHistoryWorker`. A `dt.datetime` is also a `dt.date`, so the `isinstance(…, dt.datetime)` check must come first, as written.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/data_providers -v`
Expected: all passed

- [ ] **Step 6: Commit**

```bash
git add trader/data_providers tests/data_providers
git commit -m "feat(providers): add Alpaca history provider (SIP, split-adjusted, completed sessions)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Wire Alpaca into config, registry defaults, CLI, refresh and Docker

**Files:**
- Modify: `trader/config.py` (add `AlpacaConfig`, field on `MMRConfig`, `_FLAT_KEY_MAP` entries, env fallback next to the Massive one at ≈L295)
- Modify: `trader/container.py` (≈L111–114: two flat keys)
- Modify: `trader/data_providers/builtin.py` (alpaca spec; `BUILTIN_DEFAULTS[HISTORY] = 'alpaca'`)
- Modify: `trader/data_service.py` (`__init__` params `alpaca_api_key_id`, `alpaca_api_secret_key`; `_provider_config`)
- Modify: `trader/mmr_cli.py` — `_load_default_data_source` fallback `'massive'` → `'alpaca'` (≈L290); data download parser fallback → `'alpaca'`; `_US_EXCHANGES_FOR_TD` → `_US_EXCHANGES`; `_auto_source_for_universe` returns `'alpaca'` (≈L9192); `history` subcommand: add `alpaca` sub-parser calling `mmr.pull_history('alpaca', …)` (≈L826 parser, ≈L9565 handler)
- Modify: `config_defaults/trader.yaml` (keys + `default_data_source: alpaca` + comment), `config_defaults/data_refresh.yaml` (US jobs `source: alpaca`, comments)
- Modify: `docker-compose.yml` (≈L74), `docker.sh` (≈L380), `start_mmr.sh` (≈L162 and the key prompt ≈L492–534)
- Test: extend `tests/data_providers/test_builtin.py`, `tests/data_providers/test_cli_history_worker.py`; add a config test to `tests/data_providers/test_builtin.py`

**Interfaces:**
- Consumes: everything above.
- Produces: config keys `alpaca_api_key_id`, `alpaca_api_secret_key` (env `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`); registry source `alpaca`; CLI `mmr history alpaca --symbol/--universe …`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/data_providers/test_builtin.py`:

```python
def test_alpaca_is_the_default_history_source():
    from trader.data_providers.alpaca.history import AlpacaHistoryProvider
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'})
    assert registry.default_source(Capability.HISTORY) == 'alpaca'
    assert isinstance(registry.get(Capability.HISTORY), AlpacaHistoryProvider)


def test_data_providers_override_beats_default_data_source():
    registry = ProviderRegistry.from_config({'data_providers': {'history': 'massive'},
                                             'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.HISTORY) == 'massive'


def test_download_parser_default_is_none_so_registry_decides():
    from trader.mmr_cli import build_parser
    assert build_parser().parse_args(['data', 'download', 'AAPL']).source is None


def test_alpaca_missing_secret_names_env_var():
    import pytest
    from trader.data_providers.errors import ProviderNotConfigured
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k'})
    with pytest.raises(ProviderNotConfigured, match='ALPACA_API_SECRET_KEY'):
        registry.get(Capability.HISTORY, 'alpaca')


def test_alpaca_keys_load_from_env(monkeypatch, tmp_path):
    from trader.config import MMRConfig
    cfg = tmp_path / 'trader.yaml'
    cfg.write_text('duckdb_path: data/mmr.duckdb\n')
    monkeypatch.setenv('ALPACA_API_KEY_ID', 'env-id')
    monkeypatch.setenv('ALPACA_API_SECRET_KEY', 'env-secret')
    config = MMRConfig.from_yaml(str(cfg))
    assert config.alpaca.api_key_id == 'env-id'
    assert config.alpaca.secret_key == 'env-secret'


def test_alpaca_yaml_keys_win_over_env(monkeypatch, tmp_path):
    from trader.config import MMRConfig
    cfg = tmp_path / 'trader.yaml'
    cfg.write_text("alpaca_api_key_id: yaml-id\nalpaca_api_secret_key: yaml-secret\n")
    monkeypatch.setenv('ALPACA_API_KEY_ID', 'env-id')
    config = MMRConfig.from_yaml(str(cfg))
    assert config.alpaca.api_key_id == 'yaml-id'
```

(Check first how the existing Massive env fallback behaves when the env var is set *and* YAML has a value — `trader/config.py` ≈L295 only falls back when the config value is empty. Match that: YAML wins. If `test_alpaca_yaml_keys_win_over_env` contradicts how flat keys are actually applied — e.g. `_FLAT_KEY_MAP` lets the env var override — follow the existing Massive behaviour and change the test to match, noting it in the commit body.)

Append to `tests/data_providers/test_cli_history_worker.py`:

```python
def test_us_universe_auto_source_is_alpaca():
    from types import SimpleNamespace
    from trader.mmr_cli import _auto_source_for_universe
    us = [SimpleNamespace(exchange='SMART', primaryExchange='NASDAQ')] * 3
    asx = [SimpleNamespace(exchange='ASX', primaryExchange='ASX')] * 3
    assert _auto_source_for_universe(us) == 'alpaca'
    assert _auto_source_for_universe(asx) == 'ib'
    assert _auto_source_for_universe([]) == 'ib'


def test_history_alpaca_subcommand_parses():
    from trader.mmr_cli import build_parser
    args = build_parser().parse_args(['history', 'alpaca', '--symbol', 'AAPL'])
    assert args.symbol == 'AAPL'


def test_data_refresh_template_uses_alpaca_for_us_jobs():
    from pathlib import Path
    import yaml
    jobs = yaml.safe_load(Path('config_defaults/data_refresh.yaml').read_text())['jobs']
    assert jobs['us_top20_daily']['source'] == 'alpaca'
    assert jobs['us_top20_1min']['source'] == 'alpaca'
```

(Check the `history` parser's `dest` for the sub-action name with `grep -n "hist_sub = " trader/mmr_cli.py` so the handler test matches.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/data_providers/test_builtin.py tests/data_providers/test_cli_history_worker.py -v`
Expected: the new tests FAIL (`CapabilityNotSupported: source 'alpaca'…`, `AttributeError: 'MMRConfig' object has no attribute 'alpaca'`, `'twelvedata' != 'alpaca'`).

- [ ] **Step 3: Implement**

`trader/data_providers/builtin.py` — add:

```python
def _alpaca_history(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.client import AlpacaClient
    from trader.data_providers.alpaca.history import AlpacaHistoryProvider
    client = AlpacaClient(config['alpaca_api_key_id'], config['alpaca_api_secret_key'])
    return AlpacaHistoryProvider(client)
```

add to `builtin_specs()` (first in the list):

```python
        ProviderSpec('alpaca', (('alpaca_api_key_id', 'ALPACA_API_KEY_ID'),
                                ('alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY')),
                     {Capability.HISTORY: _alpaca_history}),
```

and set `BUILTIN_DEFAULTS = {Capability.HISTORY: 'alpaca'}`.

`trader/config.py`:

```python
@dataclass
class AlpacaConfig:
    api_key_id: str = ''
    secret_key: str = ''
```

`MMRConfig` field `alpaca: AlpacaConfig = field(default_factory=AlpacaConfig)`; `_FLAT_KEY_MAP` entries `'alpaca_api_key_id': ('alpaca', 'api_key_id')`, `'alpaca_api_secret_key': ('alpaca', 'secret_key')`; next to the Massive env fallback:

```python
        if not config.alpaca.api_key_id:
            config.alpaca.api_key_id = os.getenv('ALPACA_API_KEY_ID', '')
        if not config.alpaca.secret_key:
            config.alpaca.secret_key = os.getenv('ALPACA_API_SECRET_KEY', '')
```

`trader/container.py` after the `twelvedata_api_key` line:

```python
        self.configuration['alpaca_api_key_id'] = self.mmr_config.alpaca.api_key_id
        self.configuration['alpaca_api_secret_key'] = self.mmr_config.alpaca.secret_key
```

`trader/data_service.py` — add constructor params `alpaca_api_key_id: str = ''`, `alpaca_api_secret_key: str = ''` (store as attributes) and include both in `_provider_config()`.

`trader/mmr_cli.py`:
- `_load_default_data_source`: final `return 'massive'` → `return 'alpaca'` (still used by other commands' `--source`).
- data download parser: `default=None` (help text: "default: the registry's history default — `data_providers.history`, else `default_data_source`, else alpaca"). In `_handle_data_download` replace `source = getattr(args, 'source', 'massive')` with:

```python
    from trader.data_providers import Capability, ProviderRegistry
    source = getattr(args, 'source', None) or ProviderRegistry.from_config(cfg).default_source(Capability.HISTORY)
```

  so `data_providers.history` and `default_data_source` are honoured through one mechanism. `_handle_data_refresh` already passes an explicit `source`, so it is unaffected.
- Expected behaviour, do not "fix": a US universe job with no `source:` and no Alpaca keys now fails loudly naming `ALPACA_API_KEY_ID` (spec §6 — no silent fallback to another provider).
- rename `_US_EXCHANGES_FOR_TD` → `_US_EXCHANGES` (update its comment: "US-exchange codes that route to Alpaca by default") and in `_auto_source_for_universe` return `'alpaca'` instead of `'twelvedata'`; update its docstring.
- `history` subcommand: add a parser `hist_alpaca_p = hist_sub.add_parser('alpaca', help='Download history from Alpaca (free)')` with the same four arguments as `hist_td_p`; in the handler add a branch `elif action == 'alpaca': result = mmr.pull_history('alpaca', symbols=symbols, universe=universe, bar_size=args.bar_size, prev_days=args.prev_days)` with a `console.print('[dim]Pulling Alpaca history via data_service...[/dim]')`.

`config_defaults/trader.yaml` — after the TwelveData block:

```yaml
# Alpaca market data (free Basic plan with a paper account). Default history
# source for US stocks. Env vars ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY
# are used when these are empty.
alpaca_api_key_id: ''
alpaca_api_secret_key: ''
```

and change `default_data_source: twelvedata` → `default_data_source: alpaca`; update the comment above it to list `alpaca` and drop the "`ideas` and `movers` always default to massive" note only if it is still true after phase 3 (leave it for now — phase 3 owns that).

`config_defaults/data_refresh.yaml` — US jobs `source: alpaca`; update the `source` field comment: `'alpaca', 'twelvedata', 'massive', or 'ib'. Optional — when omitted, US exchanges → 'alpaca' (free, SIP), all others → 'ib'.`; update the US jobs heading comment: `# ---- US (Alpaca, free) — runs after US after-hours close (20:30 ET) ----` and replace the TwelveData `prepost` note with: `# Alpaca SIP 1-min bars include pre- and post-market (04:00–20:00 ET). Only completed sessions are fetched (see trader/data_providers/alpaca/sessions.py).`

`docker-compose.yml` under `x-mmr-common-env` after `TWELVEDATA_API_KEY`:

```yaml
  ALPACA_API_KEY_ID: ${ALPACA_API_KEY_ID:-}
  ALPACA_API_SECRET_KEY: ${ALPACA_API_SECRET_KEY:-}
```

`docker.sh` after the TwelveData status line:

```bash
    _api_key_status_d "Alpaca"          "alpaca_api_key_id"  "ALPACA_API_KEY_ID"  "$yaml_file"
```

`start_mmr.sh` after the TwelveData status line:

```bash
    api_key_status "Alpaca"           "alpaca_api_key_id"   "ALPACA_API_KEY_ID"   "$yaml_file"
```

and in the setup wizard key prompt (≈L492–534) add an Alpaca prompt mirroring the Massive one (two values: key id, secret). Read that block first and copy its exact pattern.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/data_providers -q && .venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`
Expected: data_providers all pass; no new failures in the full suite versus baseline. Expect to update tests that assert the old defaults — find them with `grep -rn "'twelvedata'" tests/ | grep -i "default\|auto_source"`; change only assertions about the *default* or *auto-picked* source, never tests of the TwelveData provider itself.

- [ ] **Step 5: Commit**

```bash
git add -A trader config_defaults docker-compose.yml docker.sh start_mmr.sh tests/data_providers
git commit -m "feat(providers): make Alpaca the default history source

Adds alpaca_api_key_id / alpaca_api_secret_key (env fallbacks), registers
Alpaca history, switches default_data_source and US refresh jobs to alpaca,
and adds 'mmr history alpaca'.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Live verification — adjustment, end-to-end download, smoke test

**Files:**
- Create: `tests/data_providers/test_live_alpaca.py`
- Modify: `pyproject.toml` (`markers`: add `"live: calls real provider APIs; needs MMR_LIVE_TESTS=1 and keys"`)

**Interfaces:**
- Consumes: `ProviderRegistry.from_config`, `AlpacaHistoryProvider`.

- [ ] **Step 1: Write the live test**

```python
# tests/data_providers/test_live_alpaca.py
"""Real Alpaca calls. Run with: MMR_LIVE_TESTS=1 ALPACA_API_KEY_ID=... ALPACA_API_SECRET_KEY=... pytest -m live"""

import datetime as dt
import os

import pytest

from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderRegistry
from trader.objects import BarSize

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv('MMR_LIVE_TESTS') != '1'
        or not os.getenv('ALPACA_API_KEY_ID') or not os.getenv('ALPACA_API_SECRET_KEY'),
        reason='live test: set MMR_LIVE_TESTS=1 and Alpaca keys',
    ),
]


def _provider():
    return ProviderRegistry.from_config({
        'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
        'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY'],
    }).get(Capability.HISTORY, 'alpaca')


def test_one_day_of_sip_minute_bars_with_extended_hours():
    df = _provider().get_history('AAPL', BarSize.Mins1, dt.datetime(2023, 1, 3), dt.datetime(2023, 1, 3))
    assert len(df) > 700                      # 04:00–20:00 ET session, ~960 bars
    assert df.index.min().hour == 4 and df.index.max().hour == 19


def test_nvda_split_is_adjusted_with_no_jump():
    # NVDA 10-for-1 split effective 2024-06-10. Split-adjusted closes stay ~$120.
    df = _provider().get_history('NVDA', BarSize.Days1, dt.datetime(2024, 6, 3), dt.datetime(2024, 6, 14))
    closes = df['close']
    assert closes.max() / closes.min() < 1.2
    assert closes.max() < 200
```

- [ ] **Step 2: Run the live test**

Run (reads keys from the repo `.env` without printing them):

```bash
set -a; eval "$(grep -E '^ALPACA_API_(KEY_ID|SECRET_KEY)=' .env)"; set +a
MMR_LIVE_TESTS=1 .venv/bin/pytest tests/data_providers/test_live_alpaca.py -v -m live
```

Expected: 2 passed. Also confirm the normal suite skips it: `pytest tests/data_providers/test_live_alpaca.py -q` → `2 skipped`.

- [ ] **Step 3: Verify what the stored data is (adjustment decision gate)**

1. TwelveData: open https://twelvedata.com/docs#time-series and record the documented default of the `adjust` parameter. The worker (`trader/listeners/twelvedata_history.py:168`) passes none.
2. Massive: open https://massive.com/docs/stocks/get_v2_aggs_ticker__stocksticker__range__multiplier___timespan___from___to and record what `adjusted=true` (the client default) adjusts for.
3. Run: `mmr --json data query NVDA --bar-size "1 day" --days 900 | python3 -c "import json,sys; rows=json.load(sys.stdin)['data']; print([r for r in rows if r.get('date','').startswith('2024-06-0')][:5])"` — if stored NVDA closes around 2024-06-07 are ~$1,200, stored data is **not** split-adjusted; if ~$120, it is.

Decision rule: if both providers document split-only adjustment and step 3 shows ~$120 (or there is no stored NVDA), keep `adjustment=split`. If either documents dividend adjustment, or step 3 shows ~$1,200, **stop and ask the user** before continuing — mixing adjustment bases corrupts backtests.

Record the three findings in the commit body.

- [ ] **Step 4: End-to-end CLI check**

Redirect storage away from the user's live DuckDB with a temp copy of the config, then **prove** the redirect took before downloading anything. Setting `DUCKDB_PATH` / `HISTORY_DUCKDB_PATH` does **not** redirect the CLI (verified), so do not rely on it.

```bash
set -a; eval "$(grep -E '^ALPACA_API_(KEY_ID|SECRET_KEY)=' .env)"; set +a
CHECK_DIR="$(mktemp -d)"
cp config_defaults/trader.yaml "$CHECK_DIR/trader.yaml"
# set duckdb_path and history_duckdb_path in the copy to "$CHECK_DIR/check.duckdb" and "$CHECK_DIR/check_history.duckdb"
export TRADER_CONFIG="$CHECK_DIR/trader.yaml"
.venv/bin/mmr --json data summary
```

**Hard gate:** the summary must be empty **and** you must print the resolved `duckdb_path` / `history_duckdb_path` and confirm both are the temp paths. An empty summary alone can be a false pass when the live DB is also empty. If either path is not a temp path, **stop, do not download**.

Only then:

```bash
.venv/bin/mmr data download AAPL --bar-size "1 day" --days 30 --source alpaca
.venv/bin/mmr data download AAPL --bar-size "1 day" --days 30 --source alpaca   # second run
.venv/bin/mmr --json data query AAPL --bar-size "1 day" --days 30 | head -c 600
```

Expected: first run writes ~20 rows; second run prints "already up to date"; the query shows split-adjusted daily bars dated at 00:00 ET with no row for today if run before 20:16 ET. Never write test rows into the user's live DuckDB.

- [ ] **Step 5: Commit**

```bash
git add tests/data_providers/test_live_alpaca.py pyproject.toml
git commit -m "test(providers): add gated live Alpaca history checks

<paste the three adjustment findings from Step 3 here>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 12: Docs for phases 1–2

**Files:**
- Modify: `CLAUDE.md` — "Massive first, IB fallback" principle, Architecture diagram's `data_service` line (add `AlpacaHistoryProvider` via registry), "Configuration" (`default_data_source` now `alpaca`, new keys), "Data refresh loop" (US → alpaca), "Command Latency Reference" note that Alpaca history needs no paid plan.
- Modify: `docs/OPERATIONAL_STATE.md` — operator steps (edit live `~/.config/mmr/trader.yaml` + `data_refresh.yaml`; optional backup + forced US refetch; until then, stored US history is a TD/Massive→Alpaca splice).
- Modify: `docs/superpowers/specs/2026-10-04-free-data-providers-design.md` §7 — replace "(`backoff` package, already a dependency)" with "(simple loop, honours `Retry-After`)"; §4.2 — replace "Providers are built lazily on first use, and cached." with "A new provider is built per `get()`; adapters may keep their own HTTP session."; §10 — note the slicing rule from the plan index; §8 — "`alpaca-py`" becomes "`alpaca-py` (streaming only, phase 7); REST uses `requests`".
- Modify: `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md` — mark phases 1 and 2 done.

- [ ] **Step 1: Make the edits above.** Keep CLAUDE.md wording short; do not rewrite unrelated sections. TwelveData/Massive remain documented as opt-in sources.

- [ ] **Step 2: Verify nothing references removed names**

Run: `grep -rn "_US_EXCHANGES_FOR_TD\|_download_massive_one\|_download_twelvedata_one" --include='*.py' --include='*.md' .`
Expected: no output.

- [ ] **Step 3: Full suite**

Run: `.venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py`
Expected: no new failures versus baseline.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs
git commit -m "docs: document Alpaca as free default history source

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

After this task, write the phase 3a plan (`…-03a-quotes-movers-news.md`) against the code as it now exists.

# Free Data Providers — Phase 6: Forex and Computed Movers

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every `forex` command through the provider registry, add Frankfurter (ECB daily reference rates, free, no key) as the default for `forex convert` and `forex snapshot-all`, compute FX movers from Frankfurter (`computed_fx`), and make `movers --market indices` default to ETF proxies ranked from Alpaca IEX quotes (`etf_proxy`). `forex snapshot` / `forex quote` keep IB as the default.

**Architecture:** One new data capability, `FOREX` (spec §4.1 `ForexProvider`: `rate`, `rates`, `convert`), with one shared rate dict, one rates frame and one conversion dict. Two routing capabilities, `MOVERS_FOREX` and `MOVERS_INDICES`, reuse the existing `MoversProvider` protocol and frame; they exist because the registry keeps one default per capability and movers defaults differ per market. The existing Massive and TwelveData forex code in `trader/sdk.py` moves into adapters (`trader/data_providers/{massive,twelvedata}/forex.py`). Frankfurter lives in `trader/data_providers/frankfurter.py`; the two computed movers sources live in `trader/data_providers/computed_movers.py`. IB stays outside the registry for forex, exactly like IB history.

**Tech Stack:** Python 3.12, pandas, `requests`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-04-free-data-providers-design.md` (§3 Frankfurter, §4.1 `ForexProvider`, §4.3 rows "Movers indices", "Movers forex", "FX snapshot / quote", "FX convert", "FX snapshot-all", §5 "Labels", §6, §7). Index: `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`. Format and conventions: `docs/superpowers/plans/2026-10-04-free-data-providers-03a-quotes-movers-news.md`.

## Global Constraints

- Lane worktree `/Users/mudryy/private/mmr/.worktrees/fdp-6-forex`, branch `feat/fdp-6-forex`. Run every command from the worktree root. One local commit per task (`git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex ...`). Never push. Every commit message ends with a blank line then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Use the main venv, from the worktree root: `/Users/mudryy/private/mmr/.venv/bin/pytest`, `/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli ...`. Never use `.venv/bin/mmr` (it runs the main checkout's code).
- No network in normal tests. Live tests are gated by env `MMR_LIVE_TESTS=1` (plus Alpaca keys for Alpaca tests), like `tests/data_providers/test_live_alpaca.py`.
- Fail loudly: bad user input (bad currency code, bad pair, a currency ECB does not publish, non-positive amount) raises `ValueError`; provider failures (HTTP errors, network errors, empty or short data) raise a `ProviderError` subclass. Never return empty data or a fake 0 % change for them.
- Shapes (Task 1): forex rate dict = exactly the keys in `FX_RATE_FIELDS`; conversion dict = exactly `FX_CONVERSION_FIELDS`; rates frame starts with `FX_RATES_COLUMNS`, sorted by `change_pct` descending; movers frames start with `MOVER_COLUMNS` and go through `sort_movers`. Unknown numbers are `NaN`, unknown text is `''` (same rule as `make_quote`).
- Labels (spec §5): every Frankfurter value carries `as_of` (the ECB date) and `note == ECB_NOTE` (`'ECB daily reference rate, not a live quote'`); every computed FX mover row has `provider='computed_fx'` and a note naming both ECB dates; every index ETF row has `provider='etf_proxy'`, a `name` ending in `(ETF proxy)` and a note starting `ETF proxy for <index>; IEX prices`.
- Frankfurter: `GET https://api.frankfurter.dev/v2/rates` with `providers=ECB` on **every** call (the unpinned v2 rate blends ~90 central banks and stamps it with today's date). No key. Client-side limiter 5 requests/second (spec §7). 429 → `call_with_retry` → `ProviderRateLimited`.
- Defaults: no forex or movers capability inherits `default_data_source` or `MMR_DEFAULT_DATA_SOURCE` (only `history` does — `INHERITS_DEFAULT_DATA_SOURCE` stays `{HISTORY}`). `data_providers.forex`, `data_providers.movers_forex`, `data_providers.movers_indices` overrides apply.
- The stock REST guards (`_reject_exchange_hints_for_rest_source`, `_reject_conids_for_rest_source`) and `Capability.QUOTES` are not touched. Forex never goes through `snapshot()` or `Capability.QUOTES`.
- IB forex path (IDEALPRO `CASH` contract via `_resolve_contract` + typed `get_snapshot`) keeps its code; only the pair parsing in front of it becomes strict. IB Gateway is down, so the IB path is covered by mocked unit tests and an operator TODO, not by live tests.
- CLI `_handle_forex` catches `ValueError` and `ProviderError` and prints them with `print_status(str(ex), success=False)` — a missing key shows the env var to set, never a traceback.
- Comments only where the code is not obvious. Plain, intent-revealing names.

## Review Focus

1. **Bad pair or currency input** (`forex snapshot EUR`, `forex snapshot EURUSDX`, `forex convert EUR EURO 10`, `forex snapshot USDUSD`): expect a `ValueError` before any provider or IB call — no more silent "EUR means EUR/USD"; a valid pair on a REST source must never reach the stock `Capability.QUOTES` path, and the IB path must resolve exactly `('EUR', sec_type='CASH', exchange='IDEALPRO', currency='USD')`. → Task 1 `test_parse_forex_pair_is_strict`, Task 3 `test_bad_pair_raises_before_any_call`, `test_forex_snapshot_rest_source_uses_forex_capability_not_stock_quotes`, `test_forex_snapshot_ib_resolves_exact_idealpro_cash_contract`, Task 4 `test_invalid_currency_raises_before_request`.
2. **A real ISO currency that ECB does not publish** (`forex convert USD COP 100 --source frankfurter`): Frankfurter answers 200 and silently drops COP; expect `ValueError` naming COP. Unknown code (XXX) → Frankfurter 422 → `ValueError`; empty response → `ProviderError`. → Task 4 `test_currency_ecb_does_not_publish_raises`, `test_client_maps_bad_input_to_value_error`, `test_empty_response_is_provider_error`.
3. **Weekends, holidays and partial dates** (run on Sunday 2026-10-04; a date where one currency is missing; only one ECB date in the window): expect `as_of='2026-10-02'` (Friday), incomplete dates skipped, and `ProviderError` instead of a fake 0 % change when fewer than two complete dates exist. → Task 4 `test_rate_uses_two_latest_complete_ecb_dates`, `test_date_missing_a_currency_is_skipped`, `test_fewer_than_two_dates_raises`, Task 5 `test_single_date_raises`.
4. **Existing users with `default_data_source: twelvedata`** (or `MMR_DEFAULT_DATA_SOURCE=twelvedata`): today `forex snapshot/quote/convert` silently inherit it. Expect snapshot/quote → `ib`, convert/snapshot-all → registry default (Frankfurter), movers → `computed_fx`; only `data_providers.forex` changes them. → Task 3 `test_forex_defaults_ignore_default_data_source`, `test_forex_quote_default_honours_data_providers_forex`, Task 4 `test_registry_frankfurter_needs_no_key_and_is_forex_default`, Task 5 `test_registry_default_is_computed_fx_and_needs_no_key`.
5. **Index movers that are not indices, and partial ETF failures** (`movers --market indices`; one ETF has no IEX snapshot; all fail; `--source alpaca` for indices): every row says it is an ETF proxy; a failed ETF keeps a labelled row with NaN change; all failing raises; wrong source names `etf_proxy, massive`; the stock price/warrant filter never runs on indices. → Task 6 `test_every_row_says_etf_proxy`, `test_failed_etf_quote_keeps_labelled_row`, `test_all_quotes_failing_raises`, `test_wrong_source_for_indices_names_supported`, `test_indices_route_to_index_movers_capability`.

## Decisions

- Decision: Frankfurter v2 `/v2/rates` with `providers=ECB` pinned, not v1 — v1 answers with a `Deprecation` header and a `successor-version` link to v2; unpinned v2 blends ~90 central banks and dated a Sunday blend 2026-10-04 (0.88595 vs ECB 0.89087) — cost if wrong: one module (`frankfurter.py`) changes URL/parsing.
- Decision: cross rates computed via EUR (ECB's native base) and rounded to 6 significant digits — ECB publishes every rate against EUR with 5 digits, so EUR/X pairs stay exact and float noise disappears — cost if wrong: crosses differ from other sites in the 6th digit.
- Decision: daily change = the two latest complete ECB dates in a 14-day window; fewer than two → `ProviderError` — never a fake 0 % change — cost if wrong: an error during an ECB outage longer than ~10 business days.
- Decision: Frankfurter 404/422 and "ECB does not publish this currency" → `ValueError`; other HTTP errors, network errors, non-list payloads, empty data → `ProviderError` — Frankfurter silently drops valid non-ECB codes (probe: COP) — cost if wrong: none (both are caught by the CLI).
- Decision: one `Capability.FOREX` (spec §4.1 `rate/rates/convert`) plus routing capabilities `MOVERS_FOREX` and `MOVERS_INDICES` — the registry keeps one default per capability and movers defaults differ per market; no inline `if market == ...` source branches — cost if wrong: config keys `movers_forex` / `movers_indices` renamed later.
- Decision: `forex snapshot` / `forex quote` default `ib`, kept outside the registry (`IB_FOREX_SOURCE`, like `IB_HISTORY_SOURCE`); `data_providers.forex` overrides it; every forex command ignores `default_data_source` / `MMR_DEFAULT_DATA_SOURCE` — brief + CLAUDE.md rule; today they inherit it — cost if wrong: users who relied on `default_data_source: twelvedata` for forex must add `data_providers: {forex: twelvedata}`.
- Decision: for REST sources, `forex snapshot` and `forex quote` both call `rate()`; TwelveData uses `/quote` (not `/exchange_rate`), Massive uses the ticker snapshot (not `get_last_forex_quote`) — one shape, fewer endpoints — cost if wrong: `forex quote --source massive` loses the quote exchange id.
- Decision: Massive/TwelveData forex output keys follow the shared shapes (`pair`, `as_of`, `source`, `note`, NaN when unknown); TwelveData `is_market_open` moves into `note` — same precedent as 3a snapshots — cost if wrong: scripts reading `ticker` / `timestamp` / `is_market_open` need updating.
- Decision: Massive forex bid/ask come from `last_quote.bid_price` / `ask_price` — the old code read non-existent `bid`/`ask`/`P` attributes (massive `LastQuote` has `bid_price`/`ask_price`) and always returned None — cost if wrong: none.
- Decision: strict pair parsing for every source including IB: exactly `EURUSD`, `EUR/USD` or `C:EURUSD`, two different 3-letter codes; `EUR` alone no longer means `EUR/USD` — precision principle ("never close enough") — cost if wrong: users typing one currency get an error and must type the pair.
- Decision: `forex snapshot-all` = rates of every pair against `--base` (default USD) with an optional list of quote currencies; Massive filters its all-pairs snapshot to that base — spec row "daily rates for all pairs vs base" — cost if wrong: a Massive user who wants every pair runs it once per base.
- Decision: FX majors list = EURUSD, USDJPY, GBPUSD, USDCHF, AUDUSD, USDCAD, NZDUSD + EURGBP, EURJPY, GBPJPY — fixed and explicit (`MAJOR_FX_PAIRS`) — cost if wrong: edit one tuple.
- Decision: computed FX movers and ETF-proxy movers show the whole fixed list ranked (gainers descending, losers ascending), not only positive or negative rows — small fixed lists; hiding rows hides information — cost if wrong: "gainers" can show negative rows on a down day (the sign is in `change_pct`).
- Decision: ETF list = SPY, QQQ, DIA, IWM + the 11 Select Sector SPDRs (XLK … XLC) mapped to their indices; `ticker` = ETF, `name` = `<index> (ETF proxy)`, `volume` NaN (IEX volume is a few percent of the market) — cost if wrong: edit `INDEX_PROXY_ETFS`.
- Decision: one failed ETF quote keeps its row with NaN change and the Alpaca error in `note`; all failed → `ProviderError` — one delisted ETF must not kill the command, and the gap stays visible — cost if wrong: a partial table on bad days.
- Decision: `forex_convert` amount must be finite and > 0 (`ValueError`) — never trust input — cost if wrong: no negative conversions.
- Decision: skill helpers (`skills/mmr-skill/scripts/mmr_helpers.py`: `forex_convert` forces `--source massive`, `forex_movers` has no source) and skill docs are not changed here — the index assigns skills docs to phase 9 — cost if wrong: skill users keep Massive for convert until phase 9.
- Decision: IB forex path not live-verified (gateway down); its code is unchanged apart from strict parsing, covered by mocked tests, plus an operator TODO — cost if wrong: an IB problem shows up later, on the default `forex snapshot`.

## Known findings (do not fix here unless a task says so)

- Phase 5 (options) is planned in a parallel lane and edits the same files (`capabilities.py`, `builtin.py`, `__init__.py`, `sdk.py`, `mmr_cli.py`). Expect simple merge conflicts (both append enum members, specs and exports); keep both sides.
- Dashboard (phase 9) still calls, unchanged by this phase: `web/command_center/routes_research.py` `/forex/snapshot`, `/forex/quote` (`source: Literal["massive","ib","twelvedata"] = "massive"`), `/forex/movers`, `/forex/snapshot-all`, `/forex/convert`, and `/movers` with `market="indices"` — all through `trader/tools/massive_research.py` (`MassiveResearch.forex_snapshot/forex_quote/forex_movers/forex_snapshot_all/forex_convert/movers`, Massive client directly, plus the TwelveData forex snapshot/quote branches). They do not call the SDK methods changed here, so they keep working as before.
- `skills/mmr-skill/SKILL.md` and `references/DATA.md` still say forex movers / snapshot-all are Massive-only (phase 9).

---

## File Structure

```
trader/data_providers/
├── capabilities.py      # + FOREX, MOVERS_FOREX, MOVERS_INDICES; FX_RATE_FIELDS, FX_RATES_COLUMNS,
│                        #   FX_CONVERSION_FIELDS, make_fx_rate, make_fx_conversion, sort_fx_rates,
│                        #   ForexProvider, movers_capability                                  (Task 1)
├── symbols.py           # + parse_currency, parse_forex_codes, parse_forex_pair                 (Task 1)
├── __init__.py          # exports                                                              (Task 1)
├── massive/forex.py     # MassiveForex                                                         (Task 2)
├── massive/movers.py    # markets + 'forex'                                                    (Task 2)
├── twelvedata/forex.py  # TwelveDataForex                                                      (Task 2)
├── builtin.py           # forex/movers specs, IB_FOREX_SOURCE, forex_quote_source_choices,
│                        #   _ALPACA_KEYS, defaults                                             (Tasks 2, 4, 5, 6)
├── frankfurter.py       # FrankfurterClient, FrankfurterForex, DailyRates                      (Task 4)
├── computed_movers.py   # MAJOR_FX_PAIRS, ComputedFxMovers (Task 5); INDEX_PROXY_ETFS, EtfProxyMovers (Task 6)
└── alpaca/movers.py     # indices error names etf_proxy, massive                               (Task 6)

tests/data_providers/
├── test_capabilities_6.py                                   (Task 1)
├── test_forex_adapters.py, test_twelvedata_forex.py         (Task 2; TD tests migrated from tests/test_twelvedata_sdk.py)
├── test_sdk_forex.py, test_cli_forex.py                     (Task 3)
├── test_frankfurter.py                                      (Task 4)
├── test_computed_fx_movers.py                               (Task 5)
├── test_etf_proxy_movers.py, test_sdk_index_movers.py       (Task 6)
├── test_alpaca_movers.py                                    (Task 6: one assertion migrated)
├── fixtures/frankfurter_ecb_majors.json                     (Task 4)
├── fixtures/alpaca_snapshots_index_etfs.json                (Task 6)
├── test_live_frankfurter.py                                 (Task 7)
└── test_live_alpaca.py                                      (Task 7: extended)

Modified: trader/sdk.py (forex_snapshot, forex_quote, forex_convert, forex_snapshot_all, forex_movers,
movers, movers_detail; _parse_forex_pair removed), trader/mmr_cli.py (forex parsers, _handle_forex,
_forex_quote_source_default, _forex_needs_trader, main() local-command rule, movers parser, _handle_movers),
tests/test_twelvedata_sdk.py (three forex test classes move out), CLAUDE.md, config_defaults/trader.yaml,
docs/OPERATIONAL_STATE.md, docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md
```

---

### Task 1: Forex capabilities, shapes, strict pair parsing, movers routing helper

**Files:**
- Modify: `trader/data_providers/capabilities.py`, `trader/data_providers/symbols.py`, `trader/data_providers/__init__.py`
- Test: `tests/data_providers/test_capabilities_6.py`

**Interfaces:**
- Consumes: existing `Capability`, `MOVER_COLUMNS`, `sort_movers` (unchanged).
- Produces:
  - `Capability.FOREX = 'forex'`, `Capability.MOVERS_FOREX = 'movers_forex'`, `Capability.MOVERS_INDICES = 'movers_indices'`.
  - `FX_RATE_FIELDS`, `FX_RATES_COLUMNS`, `FX_CONVERSION_FIELDS` tuples.
  - `make_fx_rate(base: str, quote: str, **fields) -> dict`; `make_fx_conversion(base: str, quote: str, amount: float, **fields) -> dict`; `sort_fx_rates(frame: pd.DataFrame) -> pd.DataFrame`.
  - Protocol `ForexProvider`: `rate(base: str, quote: str) -> dict`, `rates(base: str, symbols: Optional[Sequence[str]]) -> pd.DataFrame`, `convert(base: str, quote: str, amount: float) -> dict`.
  - `movers_capability(market: str) -> Capability`.
  - `symbols.parse_currency(code: str) -> str`; `symbols.parse_forex_codes(base: str, quote: str) -> tuple[str, str]`; `symbols.parse_forex_pair(pair: str) -> tuple[str, str]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_capabilities_6.py
import math

import pandas as pd
import pytest

from trader.data_providers.capabilities import (
    FX_CONVERSION_FIELDS, FX_RATE_FIELDS, FX_RATES_COLUMNS, Capability, ForexProvider,
    make_fx_conversion, make_fx_rate, movers_capability, sort_fx_rates,
)
from trader.data_providers.symbols import parse_currency, parse_forex_codes, parse_forex_pair


def test_new_capability_values():
    assert [c.value for c in (Capability.FOREX, Capability.MOVERS_FOREX, Capability.MOVERS_INDICES)] == \
        ['forex', 'movers_forex', 'movers_indices']


def test_make_fx_rate_fills_every_field():
    rate = make_fx_rate('eur', 'usd', last=1.1225, source='frankfurter')
    assert tuple(rate) == FX_RATE_FIELDS
    assert rate['pair'] == 'EUR/USD' and rate['base'] == 'EUR' and rate['quote'] == 'USD'
    assert rate['last'] == 1.1225 and math.isnan(rate['bid']) and math.isnan(rate['change_pct'])
    assert rate['as_of'] == '' and rate['note'] == '' and rate['source'] == 'frankfurter'


def test_make_fx_rate_rejects_unknown_and_identity_fields():
    with pytest.raises(TypeError, match='unknown fx rate field'):
        make_fx_rate('EUR', 'USD', rate=1.0)
    with pytest.raises(TypeError, match='unknown fx rate field'):
        make_fx_rate('EUR', 'USD', pair='GBP/USD')


def test_make_fx_conversion():
    out = make_fx_conversion('eur', 'usd', 1000, converted=1122.5, rate=1.1225)
    assert tuple(out) == FX_CONVERSION_FIELDS
    assert out['from'] == 'EUR' and out['to'] == 'USD' and out['amount'] == 1000.0
    assert out['converted'] == 1122.5 and math.isnan(out['bid']) and out['source'] == ''
    with pytest.raises(TypeError, match='unknown fx conversion field'):
        make_fx_conversion('EUR', 'USD', 1, timestamp=1)


def test_sort_fx_rates_orders_columns_and_rows():
    frame = pd.DataFrame([{'pair': 'USD/JPY', 'change_pct': -0.2, 'open': 1.0},
                          {'pair': 'USD/CAD', 'change_pct': 0.5, 'open': 2.0}])
    out = sort_fx_rates(frame)
    assert tuple(out.columns[:len(FX_RATES_COLUMNS)]) == FX_RATES_COLUMNS
    assert list(out.columns[len(FX_RATES_COLUMNS):]) == ['open']
    assert out['pair'].tolist() == ['USD/CAD', 'USD/JPY']
    assert out.loc[0, 'source'] == '' and math.isnan(out.loc[0, 'last'])


def test_movers_capability_per_market():
    assert movers_capability('stocks') is Capability.MOVERS
    assert movers_capability('crypto') is Capability.MOVERS
    assert movers_capability('options') is Capability.MOVERS
    assert movers_capability('indices') is Capability.MOVERS_INDICES
    assert movers_capability('forex') is Capability.MOVERS_FOREX


def test_forex_protocol_is_structural():
    class Fx:
        def rate(self, base, quote):
            return {}

        def rates(self, base, symbols):
            return pd.DataFrame()

        def convert(self, base, quote, amount):
            return {}

    assert isinstance(Fx(), ForexProvider)


@pytest.mark.parametrize('text, expected', [
    ('EURUSD', ('EUR', 'USD')), ('eur/usd', ('EUR', 'USD')), ('C:GBPJPY', ('GBP', 'JPY')), (' usdcad ', ('USD', 'CAD')),
])
def test_parse_forex_pair_accepts_exact_spellings(text, expected):
    assert parse_forex_pair(text) == expected


@pytest.mark.parametrize('text', ['EUR', 'EURUSDX', 'EUR//USD', 'EU/RUSD', 'EUR1SD', 'USDUSD', '€URUSD', '', 'X:EURUSD'])
def test_parse_forex_pair_is_strict(text):
    with pytest.raises(ValueError):
        parse_forex_pair(text)


def test_parse_currency_and_codes():
    assert parse_currency(' eur ') == 'EUR'
    for bad in ('EURO', 'E1R', '', 'EU'):
        with pytest.raises(ValueError, match='3-letter currency code'):
            parse_currency(bad)
    assert parse_forex_codes('eur', 'jpy') == ('EUR', 'JPY')
    with pytest.raises(ValueError, match='same currency'):
        parse_forex_codes('USD', 'usd')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_capabilities_6.py -v -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'FX_CONVERSION_FIELDS'`

- [ ] **Step 3: Implement**

In `trader/data_providers/capabilities.py`, add three members to the `Capability` enum (keep the existing ones):

```python
class Capability(str, Enum):
    HISTORY = 'history'
    QUOTES = 'quotes'
    MOVERS = 'movers'
    NEWS = 'news'
    FOREX = 'forex'
    MOVERS_FOREX = 'movers_forex'
    MOVERS_INDICES = 'movers_indices'
```

Append at the end of the file:

```python
FX_RATE_FIELDS: tuple[str, ...] = (
    'pair', 'base', 'quote', 'last', 'bid', 'ask', 'open', 'high', 'low', 'close',
    'previous_close', 'change', 'change_pct', 'volume', 'as_of', 'source', 'note',
)
FX_RATES_COLUMNS: tuple[str, ...] = (
    'pair', 'last', 'previous_close', 'change', 'change_pct', 'as_of', 'source', 'note',
)
FX_CONVERSION_FIELDS: tuple[str, ...] = (
    'from', 'to', 'amount', 'converted', 'rate', 'bid', 'ask', 'as_of', 'source', 'note',
)
_FX_TEXT_FIELDS = frozenset({'pair', 'base', 'quote', 'from', 'to', 'as_of', 'source', 'note'})
_FX_RATE_IDENTITY = frozenset({'pair', 'base', 'quote'})
_FX_CONVERSION_IDENTITY = frozenset({'from', 'to', 'amount'})


def _fx_record(names: tuple[str, ...]) -> dict:
    return {name: ('' if name in _FX_TEXT_FIELDS else float('nan')) for name in names}


def make_fx_rate(base: str, quote: str, **fields: Any) -> dict:
    unknown = set(fields) - (set(FX_RATE_FIELDS) - _FX_RATE_IDENTITY)
    if unknown:
        raise TypeError(f'unknown fx rate field(s): {sorted(unknown)}')
    record = _fx_record(FX_RATE_FIELDS)
    record.update(fields)
    base, quote = base.strip().upper(), quote.strip().upper()
    record.update(pair=f'{base}/{quote}', base=base, quote=quote)
    return record


def make_fx_conversion(base: str, quote: str, amount: float, **fields: Any) -> dict:
    unknown = set(fields) - (set(FX_CONVERSION_FIELDS) - _FX_CONVERSION_IDENTITY)
    if unknown:
        raise TypeError(f'unknown fx conversion field(s): {sorted(unknown)}')
    record = _fx_record(FX_CONVERSION_FIELDS)
    record.update(fields)
    record.update({'from': base.strip().upper(), 'to': quote.strip().upper(), 'amount': float(amount)})
    return record


def sort_fx_rates(frame: pd.DataFrame) -> pd.DataFrame:
    """Order a forex rates frame: FX_RATES_COLUMNS first, biggest gain first."""
    for column in FX_RATES_COLUMNS:
        if column not in frame.columns:
            frame[column] = '' if column in _FX_TEXT_FIELDS else float('nan')
    extras = [c for c in frame.columns if c not in FX_RATES_COLUMNS]
    frame = frame[list(FX_RATES_COLUMNS) + extras]
    return frame.sort_values('change_pct', ascending=False, na_position='last').reset_index(drop=True)


@runtime_checkable
class ForexProvider(Protocol):
    def rate(self, base: str, quote: str) -> dict:
        """One make_fx_rate() dict for base/quote (codes already validated and upper case)."""
        ...

    def rates(self, base: str, symbols: Optional[Sequence[str]]) -> pd.DataFrame:
        """Frame starting with FX_RATES_COLUMNS: base against each symbol (None = every symbol the source has)."""
        ...

    def convert(self, base: str, quote: str, amount: float) -> dict:
        """One make_fx_conversion() dict."""
        ...


# The registry keeps one default per capability, and movers defaults differ per market.
_MOVERS_CAPABILITY_BY_MARKET = {'forex': Capability.MOVERS_FOREX, 'indices': Capability.MOVERS_INDICES}


def movers_capability(market: str) -> Capability:
    return _MOVERS_CAPABILITY_BY_MARKET.get(market, Capability.MOVERS)
```

In `trader/data_providers/symbols.py`, append:

```python
_CURRENCY = re.compile(r'^[A-Z]{3}$')


def parse_currency(code: str) -> str:
    candidate = str(code).strip().upper()
    if not _CURRENCY.match(candidate):
        raise ValueError(f'not a 3-letter currency code: {code!r}')
    return candidate


def parse_forex_codes(base: str, quote: str) -> tuple[str, str]:
    base, quote = parse_currency(base), parse_currency(quote)
    if base == quote:
        raise ValueError(f'base and quote are the same currency: {base}')
    return base, quote


def parse_forex_pair(pair: str) -> tuple[str, str]:
    """'EURUSD', 'EUR/USD' or 'C:EURUSD' -> ('EUR', 'USD'). Anything else is an error, never a guess."""
    text = str(pair).strip().upper().removeprefix('C:')
    if len(text) == 7 and text[3] == '/':
        text = text[:3] + text[4:]
    if len(text) != 6:
        raise ValueError(f'not a currency pair: {pair!r}; use EURUSD, EUR/USD or C:EURUSD')
    return parse_forex_codes(text[:3], text[3:])
```

In `trader/data_providers/__init__.py` import and add to `__all__`: `FX_CONVERSION_FIELDS`, `FX_RATE_FIELDS`, `FX_RATES_COLUMNS`, `ForexProvider`, `make_fx_conversion`, `make_fx_rate`, `movers_capability`, `sort_fx_rates`.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers -q -p no:cacheprovider`
Expected: all pass (new enum members have no builders yet; nothing routes to them).

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex add trader/data_providers tests/data_providers/test_capabilities_6.py
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex commit -m "feat(providers): add forex capability, shapes and strict pair parsing

MOVERS_FOREX and MOVERS_INDICES are routing keys so forex and index
movers can have their own default sources.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Massive and TwelveData forex adapters in the registry

**Files:**
- Create: `trader/data_providers/massive/forex.py`, `trader/data_providers/twelvedata/forex.py`
- Modify: `trader/data_providers/massive/movers.py` (`markets` + `'forex'`), `trader/data_providers/builtin.py`
- Move tests: `TestForexSnapshotTwelveData`, `TestForexQuoteTwelveData`, `TestForexConvertTwelveData` (≈L367–410) out of `tests/test_twelvedata_sdk.py` → `tests/data_providers/test_twelvedata_forex.py` (keep `_StubTDPayload` and `_bind_only_mmr` in the old file; other classes use them). `TestForexQuoteTwelveData` is replaced, not ported: the quote path now uses `/quote` (Decision 7).
- Test: `tests/data_providers/test_forex_adapters.py`

**Interfaces:**
- Consumes: Task 1 shapes and `Capability.FOREX`, `Capability.MOVERS_FOREX`.
- Produces: `MassiveForex(client)` and `TwelveDataForex(client)` implementing `ForexProvider`; `MassiveMovers.markets` includes `'forex'`; `builtin.IB_FOREX_SOURCE = 'ib'`; `builtin.forex_quote_source_choices() -> list[str]` (`['ib'] + source_choices(Capability.FOREX)`); `BUILTIN_DEFAULTS[FOREX] = 'massive'` and `BUILTIN_DEFAULTS[MOVERS_FOREX] = 'massive'` (today's behaviour; switched in Tasks 4 and 5).

- [ ] **Step 1: Write the failing tests**

```python
# tests/data_providers/test_forex_adapters.py
import math
from types import SimpleNamespace
from unittest.mock import MagicMock

from trader.data_providers.capabilities import FX_CONVERSION_FIELDS, FX_RATE_FIELDS, FX_RATES_COLUMNS, Capability
from trader.data_providers.massive.forex import MassiveForex
from trader.data_providers.massive.movers import MassiveMovers
from trader.data_providers.registry import ProviderRegistry


def _snap(ticker, change_pct, close=1.1):
    return SimpleNamespace(
        ticker=ticker,
        day=SimpleNamespace(open=1.0, high=1.2, low=0.9, close=close, volume=50.0),
        prev_day=SimpleNamespace(close=1.05),
        last_quote=SimpleNamespace(bid_price=1.0999, ask_price=1.1001),
        todays_change=0.05, todays_change_percent=change_pct, updated=1759500000000)


def test_rate_maps_snapshot_including_bid_ask():
    client = MagicMock()
    client.get_snapshot_ticker.return_value = _snap('C:EURUSD', 4.7)
    rate = MassiveForex(client).rate('EUR', 'USD')
    client.get_snapshot_ticker.assert_called_once_with(market_type='forex', ticker='C:EURUSD')
    assert tuple(rate) == FX_RATE_FIELDS
    assert rate['pair'] == 'EUR/USD' and rate['bid'] == 1.0999 and rate['ask'] == 1.1001
    assert rate['close'] == 1.1 and rate['previous_close'] == 1.05 and rate['change_pct'] == 4.7
    assert rate['volume'] == 50.0 and rate['source'] == 'massive' and rate['as_of'] == '1759500000000'
    assert math.isnan(rate['last'])


def test_rate_tolerates_missing_parts():
    client = MagicMock()
    client.get_snapshot_ticker.return_value = SimpleNamespace(
        ticker='C:EURUSD', day=None, prev_day=None, last_quote=None, todays_change=None,
        todays_change_percent=None, updated=None)
    rate = MassiveForex(client).rate('EUR', 'USD')
    assert math.isnan(rate['close']) and math.isnan(rate['bid']) and rate['as_of'] == ''


def test_rates_filters_to_base_and_sorts():
    client = MagicMock()
    client.get_snapshot_all.return_value = [_snap('C:USDJPY', -0.2), _snap('C:EURUSD', 0.9), _snap('C:USDCAD', 0.4)]
    frame = MassiveForex(client).rates('USD', None)
    client.get_snapshot_all.assert_called_once_with(market_type='forex', tickers=None)
    assert tuple(frame.columns[:len(FX_RATES_COLUMNS)]) == FX_RATES_COLUMNS
    assert frame['pair'].tolist() == ['USD/CAD', 'USD/JPY']
    assert frame.loc[0, 'last'] == 1.1 and frame.loc[0, 'open'] == 1.0 and set(frame['source']) == {'massive'}


def test_rates_with_symbols_requests_exact_tickers():
    client = MagicMock()
    client.get_snapshot_all.return_value = []
    frame = MassiveForex(client).rates('USD', ['EUR', 'JPY'])
    client.get_snapshot_all.assert_called_once_with(market_type='forex', tickers=['C:USDEUR', 'C:USDJPY'])
    assert frame.empty and tuple(frame.columns[:len(FX_RATES_COLUMNS)]) == FX_RATES_COLUMNS


def test_convert():
    client = MagicMock()
    client.get_real_time_currency_conversion.return_value = SimpleNamespace(
        from_='EUR', to='USD', initial_amount=100.0, converted=112.25,
        last=SimpleNamespace(bid=1.1224, ask=1.1226, exchange=48, timestamp=1759500000000))
    out = MassiveForex(client).convert('EUR', 'USD', 100.0)
    client.get_real_time_currency_conversion.assert_called_once_with('EUR', 'USD', amount=100.0)
    assert tuple(out) == FX_CONVERSION_FIELDS
    assert out['converted'] == 112.25 and out['bid'] == 1.1224 and out['ask'] == 1.1226
    assert out['as_of'] == '1759500000000' and out['source'] == 'massive' and math.isnan(out['rate'])


def test_massive_movers_supports_forex():
    client = MagicMock()
    client.get_snapshot_direction.return_value = [_snap('C:EURUSD', 0.9)]
    frame = MassiveMovers(client).movers('forex', 'gainers')
    client.get_snapshot_direction.assert_called_once_with(market_type='forex', direction='gainers')
    assert frame.loc[0, 'ticker'] == 'C:EURUSD'


def test_registry_builds_forex_adapters():
    from trader.data_providers.builtin import forex_quote_source_choices, source_choices
    from trader.data_providers.twelvedata.forex import TwelveDataForex
    registry = ProviderRegistry.from_config({'massive_api_key': 'm', 'twelvedata_api_key': 't'})
    assert isinstance(registry.get(Capability.FOREX, 'massive'), MassiveForex)
    assert isinstance(registry.get(Capability.FOREX, 'twelvedata'), TwelveDataForex)
    assert isinstance(registry.get(Capability.MOVERS_FOREX, 'massive'), MassiveMovers)
    choices = forex_quote_source_choices()
    assert choices[0] == 'ib' and {'massive', 'twelvedata'} <= set(choices)
    assert 'massive' in source_choices(Capability.MOVERS_FOREX)
```

```python
# tests/data_providers/test_twelvedata_forex.py
import math
from unittest.mock import MagicMock

import pytest

from trader.data_providers.capabilities import FX_CONVERSION_FIELDS, FX_RATE_FIELDS
from trader.data_providers.errors import CapabilityNotSupported, ProviderError
from trader.data_providers.twelvedata.forex import TwelveDataForex


class _StubTDPayload:
    def __init__(self, payload):
        self._payload = payload

    def as_json(self):
        return self._payload


QUOTE = {
    'symbol': 'EUR/USD', 'open': '1.08200', 'high': '1.08500', 'low': '1.08000', 'close': '1.08300',
    'volume': '0', 'previous_close': '1.08100', 'change': '0.00200', 'percent_change': '0.18500',
    'datetime': '2026-04-29', 'timestamp': 1777000000, 'is_market_open': True,
}


def test_rate_from_quote_payload():
    client = MagicMock()
    client.quote.return_value = _StubTDPayload(QUOTE)
    out = TwelveDataForex(client).rate('EUR', 'USD')
    client.quote.assert_called_once_with(symbol='EUR/USD')
    assert tuple(out) == FX_RATE_FIELDS
    assert out['pair'] == 'EUR/USD'
    assert out['close'] == pytest.approx(1.083) and out['last'] == pytest.approx(1.083)
    assert out['previous_close'] == pytest.approx(1.081)
    assert out['change_pct'] == pytest.approx(0.185)
    assert out['as_of'] == '2026-04-29' and out['source'] == 'twelvedata' and out['note'] == 'market open'
    assert math.isnan(out['bid']) and math.isnan(out['ask'])


def test_rate_market_closed_note_and_blank_numbers():
    client = MagicMock()
    client.quote.return_value = _StubTDPayload(dict(QUOTE, is_market_open=False, volume=''))
    out = TwelveDataForex(client).rate('EUR', 'USD')
    assert out['note'] == 'market closed' and math.isnan(out['volume'])


def test_error_payload_raises():
    client = MagicMock()
    client.quote.return_value = _StubTDPayload({'code': 400, 'message': '**symbol** not found', 'status': 'error'})
    with pytest.raises(ProviderError, match='not found'):
        TwelveDataForex(client).rate('EUR', 'XYZ')


def test_convert():
    client = MagicMock()
    client.currency_conversion.return_value = _StubTDPayload({
        'symbol': 'EUR/USD', 'rate': 1.1678, 'amount': 116.78, 'timestamp': 1777000000,
    })
    out = TwelveDataForex(client).convert('EUR', 'USD', 100.0)
    client.currency_conversion.assert_called_once_with(symbol='EUR/USD', amount=100.0)
    assert tuple(out) == FX_CONVERSION_FIELDS
    assert out['from'] == 'EUR' and out['to'] == 'USD' and out['amount'] == 100.0
    assert out['converted'] == pytest.approx(116.78) and out['rate'] == pytest.approx(1.1678)
    assert out['as_of'] == '1777000000' and out['source'] == 'twelvedata'


def test_rates_for_every_pair_not_supported():
    with pytest.raises(CapabilityNotSupported) as info:
        TwelveDataForex(MagicMock()).rates('USD', None)
    assert info.value.supported == ['frankfurter', 'massive']
```

Then delete `TestForexSnapshotTwelveData`, `TestForexQuoteTwelveData` and `TestForexConvertTwelveData` from `tests/test_twelvedata_sdk.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_forex_adapters.py tests/data_providers/test_twelvedata_forex.py -v -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.massive.forex'`

- [ ] **Step 3: Implement**

```python
# trader/data_providers/massive/forex.py
"""Forex from Massive (Polygon): ticker snapshots and real-time conversion."""

from typing import Optional, Sequence

import pandas as pd

from trader.data_providers.capabilities import make_fx_conversion, make_fx_rate, sort_fx_rates

_RATES_FRAME_COLUMNS = ['pair', 'last', 'previous_close', 'change', 'change_pct', 'as_of', 'source', 'note',
                        'open', 'high', 'low', 'volume']


class MassiveForex:
    def __init__(self, client):
        self._client = client

    def rate(self, base: str, quote: str) -> dict:
        snap = self._client.get_snapshot_ticker(market_type='forex', ticker=f'C:{base}{quote}')
        return make_fx_rate(
            base, quote,
            bid=_number(snap.last_quote, 'bid_price'), ask=_number(snap.last_quote, 'ask_price'),
            open=_number(snap.day, 'open'), high=_number(snap.day, 'high'), low=_number(snap.day, 'low'),
            close=_number(snap.day, 'close'), volume=_number(snap.day, 'volume'),
            previous_close=_number(snap.prev_day, 'close'),
            change=_number(snap, 'todays_change'), change_pct=_number(snap, 'todays_change_percent'),
            as_of=_text(snap.updated), source='massive',
        )

    def rates(self, base: str, symbols: Optional[Sequence[str]]) -> pd.DataFrame:
        tickers = [f'C:{base}{symbol}' for symbol in symbols] if symbols else None
        prefix = f'C:{base}'
        rows = [{
            'pair': f'{base}/{snap.ticker[len(prefix):]}',
            'last': _number(snap.day, 'close'),
            'previous_close': _number(snap.prev_day, 'close'),
            'change': _number(snap, 'todays_change'),
            'change_pct': _number(snap, 'todays_change_percent'),
            'as_of': _text(snap.updated),
            'source': 'massive',
            'note': '',
            'open': _number(snap.day, 'open'),
            'high': _number(snap.day, 'high'),
            'low': _number(snap.day, 'low'),
            'volume': _number(snap.day, 'volume'),
        } for snap in self._client.get_snapshot_all(market_type='forex', tickers=tickers)
            if (snap.ticker or '').startswith(prefix)]
        return sort_fx_rates(pd.DataFrame(rows, columns=_RATES_FRAME_COLUMNS))

    def convert(self, base: str, quote: str, amount: float) -> dict:
        result = self._client.get_real_time_currency_conversion(base, quote, amount=amount)
        return make_fx_conversion(
            base, quote, amount,
            converted=_number(result, 'converted'),
            bid=_number(result.last, 'bid'), ask=_number(result.last, 'ask'),
            as_of=_text(getattr(result.last, 'timestamp', None)), source='massive',
        )


def _number(obj, attr: str) -> float:
    value = getattr(obj, attr, None) if obj is not None else None
    return float('nan') if value is None else float(value)


def _text(value) -> str:
    return '' if value is None else str(value)
```

```python
# trader/data_providers/twelvedata/forex.py
"""Forex from TwelveData REST: /quote for a pair, /currency_conversion for amounts. REST has no bid/ask."""

from typing import Optional, Sequence

import pandas as pd

from trader.data_providers.capabilities import make_fx_conversion, make_fx_rate
from trader.data_providers.errors import CapabilityNotSupported, ProviderError


class TwelveDataForex:
    def __init__(self, client):
        self._client = client

    def rate(self, base: str, quote: str) -> dict:
        payload = _checked(self._client.quote(symbol=f'{base}/{quote}').as_json())
        note = ''
        if 'is_market_open' in payload:
            note = 'market open' if payload['is_market_open'] else 'market closed'
        return make_fx_rate(
            base, quote,
            last=_number(payload, 'close'),
            open=_number(payload, 'open'), high=_number(payload, 'high'), low=_number(payload, 'low'),
            close=_number(payload, 'close'), previous_close=_number(payload, 'previous_close'),
            change=_number(payload, 'change'), change_pct=_number(payload, 'percent_change'),
            volume=_number(payload, 'volume'),
            as_of=str(payload.get('datetime') or ''), source='twelvedata', note=note,
        )

    def rates(self, base: str, symbols: Optional[Sequence[str]]) -> pd.DataFrame:
        raise CapabilityNotSupported('forex rates for every pair', 'twelvedata', ['frankfurter', 'massive'])

    def convert(self, base: str, quote: str, amount: float) -> dict:
        payload = _checked(self._client.currency_conversion(symbol=f'{base}/{quote}', amount=amount).as_json())
        return make_fx_conversion(
            base, quote, amount,
            converted=_number(payload, 'amount'), rate=_number(payload, 'rate'),
            as_of=str(payload.get('timestamp') or ''), source='twelvedata',
        )


def _checked(payload: dict) -> dict:
    if payload.get('status') == 'error':
        raise ProviderError(f"twelvedata forex request failed: {payload.get('message', '')}")
    return payload


def _number(payload: dict, key: str) -> float:
    try:
        return float(payload.get(key))
    except (TypeError, ValueError):
        return float('nan')
```

(`frankfurter` in the TwelveData error arrives in Task 4; the message is correct from then on.)

`trader/data_providers/massive/movers.py`: `markets = frozenset({'stocks', 'crypto', 'indices', 'options', 'futures', 'forex'})`.

`trader/data_providers/builtin.py`:

```python
def _massive_forex(config: Mapping[str, Any]):
    from trader.data_providers.massive.forex import MassiveForex
    return MassiveForex(_massive_rest_client(config))


def _twelvedata_forex(config: Mapping[str, Any]):
    from twelvedata import TDClient
    from trader.data_providers.twelvedata.forex import TwelveDataForex
    return TwelveDataForex(TDClient(apikey=config['twelvedata_api_key']))
```

Add `Capability.FOREX: _massive_forex` and `Capability.MOVERS_FOREX: _massive_movers` to the `massive` spec, `Capability.FOREX: _twelvedata_forex` to the `twelvedata` spec, and to `BUILTIN_DEFAULTS`:

```python
    Capability.FOREX: 'massive',
    Capability.MOVERS_FOREX: 'massive',
```

Below `IB_HISTORY_SOURCE`:

```python
# IB forex is contract-based (IDEALPRO CASH via trader_service), so like IB history it is a
# CLI source name for `forex snapshot` / `forex quote`, not a registry provider.
IB_FOREX_SOURCE = 'ib'


def forex_quote_source_choices() -> list[str]:
    return [IB_FOREX_SOURCE] + source_choices(Capability.FOREX)
```

(`forex_quote_source_choices` must sit below `source_choices`.)

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers tests/test_twelvedata_sdk.py -q -p no:cacheprovider`
Expected: all pass (the SDK still uses its own forex code until Task 3).

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex add trader/data_providers tests/data_providers tests/test_twelvedata_sdk.py
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex commit -m "refactor(providers): wrap Massive and TwelveData forex as adapters

Massive forex bid/ask now read bid_price/ask_price; the old code read
attributes the Massive model does not have and always returned None.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: SDK and CLI forex commands through the registry

**Files:**
- Modify: `trader/sdk.py` (Forex section ≈L3479–3675: delete `_parse_forex_pair`, replace `forex_snapshot`, `forex_quote`, `forex_snapshot_all`, `forex_movers`; move `forex_convert` from ≈L4170 into this section and replace it), `trader/mmr_cli.py` (forex parsers ≈L1078–1117, `_handle_forex` ≈L10412, `main()` forex block ≈L11807–11812, new helpers next to `_snapshot_source_default` ≈L306)
- Test: `tests/data_providers/test_sdk_forex.py`, `tests/data_providers/test_cli_forex.py`

**Interfaces:**
- Consumes: Task 1 (`parse_forex_pair`, `parse_forex_codes`, `parse_currency`, `Capability.FOREX`, `Capability.MOVERS_FOREX`), Task 2 (`IB_FOREX_SOURCE`, `forex_quote_source_choices`, adapters), `MMR._provider`, `MMR._provider_default`.
- Produces:
  - `MMR.forex_snapshot(pair: str, source: str = 'ib') -> dict` (IB dict as today; registry source → `make_fx_rate` dict).
  - `MMR.forex_quote(from_currency: str, to_currency: str, source: str = 'ib') -> dict` (same rule).
  - `MMR.forex_convert(from_currency: str, to_currency: str, amount: float, source: Optional[str] = None) -> dict`.
  - `MMR.forex_snapshot_all(base: str = 'USD', symbols: Optional[List[str]] = None, source: Optional[str] = None) -> pd.DataFrame`.
  - `MMR.forex_movers(direction: str = 'gainers', source: Optional[str] = None) -> pd.DataFrame`.
  - CLI: `_forex_quote_source_default(choices) -> str`, `_forex_needs_trader(args) -> bool`; `forex snapshot-all [SYMBOL ...] [--base USD] [--source ...]`; `forex movers --source`; `forex convert --source` default `None`.

Output change (documented in Task 7): registry forex sources return the shared shapes; `forex snapshot-all` takes `--base` and quote currencies instead of Massive tickers.

- [ ] **Step 1: Write the failing tests**

```python
# tests/data_providers/test_sdk_forex.py
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trader.data_providers.capabilities import Capability, make_fx_conversion, make_fx_rate
from trader.sdk import MMR


def _mmr(provider=None):
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock(return_value=provider or MagicMock())
    return mmr


def test_forex_snapshot_rest_source_uses_forex_capability_not_stock_quotes():
    provider = MagicMock()
    provider.rate.return_value = make_fx_rate('EUR', 'USD', last=1.1)
    mmr = _mmr(provider)
    out = mmr.forex_snapshot('eur/usd', source='massive')
    mmr._provider.assert_called_once_with(Capability.FOREX, 'massive')
    provider.rate.assert_called_once_with('EUR', 'USD')
    assert out['pair'] == 'EUR/USD' and out['last'] == 1.1


def test_forex_quote_rest_source():
    provider = MagicMock()
    provider.rate.return_value = make_fx_rate('EUR', 'JPY', last=176.99)
    mmr = _mmr(provider)
    assert mmr.forex_quote('eur', 'jpy', source='twelvedata')['last'] == 176.99
    mmr._provider.assert_called_once_with(Capability.FOREX, 'twelvedata')
    provider.rate.assert_called_once_with('EUR', 'JPY')


def _ib_mmr():
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock()
    mmr._resolve_contract = MagicMock(return_value=SimpleNamespace(conId=12087792))
    # _typed_query is a read-only property over these two clients (built lazily when both are None).
    mmr._typed_query_client = MagicMock()
    mmr._typed_command_client = MagicMock()
    mmr._typed_query_client.call.return_value = {'snapshot': {'bid': 1.1, 'ask': 1.2, 'last': 1.15}}
    return mmr


def test_forex_snapshot_ib_resolves_exact_idealpro_cash_contract():
    mmr = _ib_mmr()
    out = mmr.forex_snapshot('C:EURUSD')
    mmr._resolve_contract.assert_called_once_with('EUR', sec_type='CASH', exchange='IDEALPRO', currency='USD')
    mmr._typed_query_client.call.assert_called_once_with(
        'get_snapshot', {'instrument_id': 12087792, 'delayed': False}, dict)
    mmr._provider.assert_not_called()
    assert out['pair'] == 'EUR/USD' and out['bid'] == 1.1 and out['ask'] == 1.2


def test_forex_quote_ib_resolves_exact_contract():
    mmr = _ib_mmr()
    out = mmr.forex_quote('eur', 'usd')
    mmr._resolve_contract.assert_called_once_with('EUR', sec_type='CASH', exchange='IDEALPRO', currency='USD')
    assert out == {'pair': 'EUR/USD', 'bid': 1.1, 'ask': 1.2, 'last': 1.15, 'time': None}


@pytest.mark.parametrize('pair', ['EUR', 'EURUSDX', 'USDUSD'])
def test_bad_pair_raises_before_any_call(pair):
    mmr = _ib_mmr()
    with pytest.raises(ValueError):
        mmr.forex_snapshot(pair, source='massive')
    with pytest.raises(ValueError):
        mmr.forex_snapshot(pair)
    with pytest.raises(ValueError):
        mmr.forex_quote(pair[:3], pair[3:] or 'EU')
    mmr._provider.assert_not_called()
    mmr._resolve_contract.assert_not_called()


def test_forex_convert_default_source_and_validation():
    provider = MagicMock()
    provider.convert.return_value = make_fx_conversion('EUR', 'USD', 100, converted=112.25)
    mmr = _mmr(provider)
    assert mmr.forex_convert('eur', 'usd', 100)['converted'] == 112.25
    mmr._provider.assert_called_once_with(Capability.FOREX, None)
    provider.convert.assert_called_once_with('EUR', 'USD', 100.0)
    for bad in (0, -5, float('nan'), float('inf')):
        with pytest.raises(ValueError, match='positive'):
            mmr.forex_convert('EUR', 'USD', bad)
    with pytest.raises(ValueError, match='3-letter'):
        mmr.forex_convert('EUR', 'EURO', 10)


def test_forex_snapshot_all_passes_base_and_symbols():
    provider = MagicMock()
    provider.rates.return_value = pd.DataFrame()
    mmr = _mmr(provider)
    mmr.forex_snapshot_all(base='usd', symbols=['eur', 'jpy'], source='massive')
    mmr._provider.assert_called_once_with(Capability.FOREX, 'massive')
    provider.rates.assert_called_once_with('USD', ['EUR', 'JPY'])
    mmr.forex_snapshot_all()
    provider.rates.assert_called_with('USD', None)
    with pytest.raises(ValueError, match='same currency'):
        mmr.forex_snapshot_all(base='USD', symbols=['USD'])


def test_forex_movers_routes_through_movers_forex():
    provider = MagicMock()
    provider.movers.return_value = pd.DataFrame({'ticker': ['EURUSD']})
    mmr = _mmr(provider)
    mmr.forex_movers('losers', source='massive')
    mmr._provider.assert_called_once_with(Capability.MOVERS_FOREX, 'massive')
    provider.movers.assert_called_once_with('forex', 'losers')
```

```python
# tests/data_providers/test_cli_forex.py
from argparse import Namespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trader.data_providers.builtin import BUILTIN_DEFAULTS
from trader.data_providers.capabilities import Capability
from trader.data_providers.errors import ProviderNotConfigured
from trader.data_providers.registry import ProviderRegistry


@pytest.fixture
def trader_config(tmp_path, monkeypatch):
    monkeypatch.delenv('MMR_DEFAULT_DATA_SOURCE', raising=False)

    def write(yaml_text: str):
        path = tmp_path / 'trader.yaml'
        path.write_text(yaml_text)
        monkeypatch.setenv('TRADER_CONFIG', str(path))

    return write


def test_forex_defaults_ignore_default_data_source(trader_config, monkeypatch):
    from trader.mmr_cli import build_parser
    trader_config('default_data_source: twelvedata\n')
    monkeypatch.setenv('MMR_DEFAULT_DATA_SOURCE', 'twelvedata')
    parser = build_parser()
    assert parser.parse_args(['forex', 'snapshot', 'EURUSD']).source == 'ib'
    assert parser.parse_args(['forex', 'quote', 'EUR', 'USD']).source == 'ib'
    assert parser.parse_args(['forex', 'convert', 'EUR', 'USD', '100']).source is None
    assert parser.parse_args(['forex', 'snapshot-all']).source is None
    assert parser.parse_args(['forex', 'movers']).source is None
    registry = ProviderRegistry.from_config({'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.FOREX) == BUILTIN_DEFAULTS[Capability.FOREX]
    assert registry.default_source(Capability.MOVERS_FOREX) == BUILTIN_DEFAULTS[Capability.MOVERS_FOREX]


def test_forex_quote_default_honours_data_providers_forex(trader_config):
    from trader.mmr_cli import build_parser
    trader_config('data_providers: {forex: twelvedata}\n')
    parser = build_parser()
    assert parser.parse_args(['forex', 'snapshot', 'EURUSD']).source == 'twelvedata'
    assert parser.parse_args(['forex', 'quote', 'EUR', 'USD']).source == 'twelvedata'


def test_forex_quote_default_ignores_unknown_override(trader_config):
    from trader.mmr_cli import _forex_quote_source_default
    trader_config('data_providers: {forex: alpaca}\n')
    assert _forex_quote_source_default(['ib', 'massive', 'twelvedata']) == 'ib'


def test_snapshot_all_arguments(trader_config):
    from trader.mmr_cli import build_parser
    trader_config('')
    args = build_parser().parse_args(['forex', 'snapshot-all', 'EUR', 'JPY', '--base', 'GBP'])
    assert args.symbols == ['EUR', 'JPY'] and args.base == 'GBP'
    assert build_parser().parse_args(['forex', 'snapshot-all']).base == 'USD'


@pytest.mark.parametrize('argv, needs_trader', [
    (['forex', 'snapshot', 'EURUSD'], True),
    (['forex', 'quote', 'EUR', 'USD'], True),
    (['fx', 'snapshot', 'EURUSD'], True),
    (['forex', 'snapshot', 'EURUSD', '--source', 'massive'], False),
    (['forex', 'quote', 'EUR', 'USD', '--source', 'twelvedata'], False),
    (['forex', 'convert', 'EUR', 'USD', '100'], False),
    (['forex', 'snapshot-all'], False),
    (['forex', 'movers'], False),
])
def test_forex_needs_trader_only_for_ib(trader_config, argv, needs_trader):
    from trader.mmr_cli import _forex_needs_trader, build_parser
    trader_config('')
    assert _forex_needs_trader(build_parser().parse_args(argv)) is needs_trader


def test_handle_forex_prints_provider_error(capsys):
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr._provider_default.return_value = 'massive'
    mmr.forex_convert.side_effect = ProviderNotConfigured('massive', [('massive_api_key', 'MASSIVE_API_KEY')])
    _handle_forex(mmr, Namespace(fx_action='convert', from_currency='EUR', to_currency='USD', amount=100.0,
                                 source=None))
    assert 'MASSIVE_API_KEY' in capsys.readouterr().out


def test_handle_forex_prints_value_error(capsys):
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr.forex_snapshot.side_effect = ValueError("not a currency pair: 'EUR'")
    _handle_forex(mmr, Namespace(fx_action='snapshot', pair='EUR', source='ib'))
    assert 'not a currency pair' in capsys.readouterr().out


def test_handle_forex_snapshot_all_passes_base_and_symbols():
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr._provider_default.return_value = 'massive'
    mmr.forex_snapshot_all.return_value = pd.DataFrame()
    _handle_forex(mmr, Namespace(fx_action='snapshot-all', base='usd', symbols=['eur'], source=None))
    mmr._provider_default.assert_called_once_with(Capability.FOREX)
    mmr.forex_snapshot_all.assert_called_once_with(base='USD', symbols=['EUR'], source='massive')


def test_handle_forex_movers_resolves_movers_forex_default():
    from trader.mmr_cli import _handle_forex
    mmr = MagicMock()
    mmr._provider_default.return_value = 'massive'
    mmr.forex_movers.return_value = pd.DataFrame()
    _handle_forex(mmr, Namespace(fx_action='movers', losers=True, source=None))
    mmr._provider_default.assert_called_once_with(Capability.MOVERS_FOREX)
    mmr.forex_movers.assert_called_once_with(direction='losers', source='massive')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_sdk_forex.py tests/data_providers/test_cli_forex.py -v -p no:cacheprovider`
Expected: FAIL — e.g. `test_forex_snapshot_rest_source_uses_forex_capability_not_stock_quotes` with `AttributeError: 'MMR' object has no attribute '_massive_rest_client'` and `ImportError: cannot import name '_forex_quote_source_default'`.

- [ ] **Step 3: Implement**

`trader/sdk.py` — in the Forex section, delete `_parse_forex_pair` and replace `forex_snapshot`, `forex_quote`, `forex_snapshot_all` and `forex_movers` with the methods below; delete the old `forex_convert` (≈L4170, just above the "News (provider registry)" header) and put the new one here:

```python
    def forex_snapshot(self, pair: str, source: str = 'ib') -> dict:
        """Snapshot for a currency pair ('EURUSD', 'EUR/USD' or 'C:EURUSD').

        source 'ib' (default) resolves the IDEALPRO CASH contract via trader_service. Any other
        value is a registry forex source: 'frankfurter' (ECB daily reference rate, not live),
        'massive', 'twelvedata' (no bid/ask on REST).
        """
        from trader.data_providers.symbols import parse_forex_pair
        base, quote_ccy = parse_forex_pair(pair)
        if source != 'ib':
            from trader.data_providers import Capability
            return self._provider(Capability.FOREX, source).rate(base, quote_ccy)
        contract = self._resolve_contract(base, sec_type='CASH', exchange='IDEALPRO', currency=quote_ccy)
        response = self._typed_query.call(
            'get_snapshot',
            {'instrument_id': int(contract.conId), 'delayed': False},
            dict,
        )
        s = response.get('snapshot') or {}
        return {
            'pair': f'{base}/{quote_ccy}',
            'bid': s.get('bid'),
            'bidSize': s.get('bid_size'),
            'ask': s.get('ask'),
            'askSize': s.get('ask_size'),
            'last': s.get('last'),
            'open': s.get('open'),
            'high': s.get('high'),
            'low': s.get('low'),
            'close': s.get('close'),
            'time': s.get('time'),
        }

    def forex_quote(self, from_currency: str, to_currency: str, source: str = 'ib') -> dict:
        """Last quote for a currency pair. 'ib' (default) gives IB bid/ask; registry sources give
        the same shared rate dict as :meth:`forex_snapshot`."""
        from trader.data_providers.symbols import parse_forex_codes
        base, quote_ccy = parse_forex_codes(from_currency, to_currency)
        if source != 'ib':
            from trader.data_providers import Capability
            return self._provider(Capability.FOREX, source).rate(base, quote_ccy)
        contract = self._resolve_contract(base, sec_type='CASH', exchange='IDEALPRO', currency=quote_ccy)
        response = self._typed_query.call(
            'get_snapshot',
            {'instrument_id': int(contract.conId), 'delayed': False},
            dict,
        )
        s = response.get('snapshot') or {}
        return {
            'pair': f'{base}/{quote_ccy}',
            'bid': s.get('bid'),
            'ask': s.get('ask'),
            'last': s.get('last'),
            'time': s.get('time'),
        }

    def forex_convert(self, from_currency: str, to_currency: str, amount: float,
                      source: Optional[str] = None) -> dict:
        """Convert `amount` with a registry forex source (default: `data_providers.forex`, else builtin)."""
        import math
        from trader.data_providers import Capability
        from trader.data_providers.symbols import parse_forex_codes
        base, quote_ccy = parse_forex_codes(from_currency, to_currency)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError(f'amount must be a positive number, got {amount!r}')
        return self._provider(Capability.FOREX, source).convert(base, quote_ccy, float(amount))

    def forex_snapshot_all(self, base: str = 'USD', symbols: Optional[List[str]] = None,
                           source: Optional[str] = None) -> pd.DataFrame:
        """Rates of `base` against each of `symbols` (None = every currency the source has)."""
        from trader.data_providers import Capability
        from trader.data_providers.symbols import parse_currency, parse_forex_codes
        base = parse_currency(base)
        quotes = [parse_forex_codes(base, symbol)[1] for symbol in symbols] if symbols else None
        return self._provider(Capability.FOREX, source).rates(base, quotes)

    def forex_movers(self, direction: str = 'gainers', source: Optional[str] = None) -> pd.DataFrame:
        """Forex movers from a registry source (default: `data_providers.movers_forex`, else builtin)."""
        from trader.data_providers import Capability
        return self._provider(Capability.MOVERS_FOREX, source).movers('forex', direction)
```

`trader/mmr_cli.py`:

1. Below `_snapshot_source_default`:

```python
def _forex_quote_source_default(choices) -> str:
    """Default `--source` for `forex snapshot` / `forex quote`: data_providers.forex, else IB.

    default_data_source and MMR_DEFAULT_DATA_SOURCE are history settings and are ignored here.
    """
    from trader.data_providers.builtin import IB_FOREX_SOURCE
    explicit = _data_provider_overrides(_read_trader_config()).get('forex')
    return explicit if explicit in choices else IB_FOREX_SOURCE


def _forex_needs_trader(args: argparse.Namespace) -> bool:
    """Only IB-routed forex snapshot/quote talk to trader_service; registry forex sources are local."""
    from trader.data_providers.builtin import IB_FOREX_SOURCE
    return (getattr(args, 'fx_action', None) in ('snapshot', 'snap', 'quote')
            and getattr(args, 'source', None) == IB_FOREX_SOURCE)
```

2. In `build_parser`, extend the existing local import (≈L438) to `from trader.data_providers.builtin import forex_quote_source_choices, source_choices`, then replace the whole `# forex` parser block with:

```python
    # forex
    forex_quote_sources = forex_quote_source_choices()
    fx_p = sub.add_parser('forex', aliases=['fx'], help='Forex rates, conversion and movers',
                           epilog='Examples:\n'
                                  '  forex snapshot EURUSD                 # via IB (default)\n'
                                  '  forex snapshot EURUSD --source massive\n'
                                  '  forex quote EUR USD                   # via IB (default)\n'
                                  '  forex snapshot-all EUR JPY --base USD\n'
                                  '  forex movers --losers\n'
                                  '  forex convert EUR USD 1000',
                           formatter_class=fmt)
    fx_sub = fx_p.add_subparsers(dest='fx_action')

    fx_snap_p = fx_sub.add_parser('snapshot', aliases=['snap'], help='Forex pair snapshot')
    fx_snap_p.add_argument('pair', help='Currency pair: EURUSD, EUR/USD or C:EURUSD')
    fx_snap_p.add_argument('--source', choices=forex_quote_sources,
                           default=_forex_quote_source_default(forex_quote_sources),
                           help='Data source (default: data_providers.forex, else ib)')

    fx_all_p = fx_sub.add_parser('snapshot-all', help='Rates of --base against every (or the given) currency')
    fx_all_p.add_argument('symbols', nargs='*', help='Quote currencies, e.g. EUR JPY (default: all)')
    fx_all_p.add_argument('--base', default='USD', help='Base currency (default: USD)')
    fx_all_p.add_argument('--source', choices=source_choices(Capability.FOREX), default=None,
                          help='Data source (default: data_providers.forex, else the builtin default)')

    fx_movers_p = fx_sub.add_parser('movers', help='Top forex movers')
    fx_movers_p.add_argument('--losers', action='store_true', default=False, help='Show losers instead of gainers')
    fx_movers_p.add_argument('--source', choices=source_choices(Capability.MOVERS_FOREX), default=None,
                             help='Data source (default: data_providers.movers_forex, else the builtin default)')

    fx_quote_p = fx_sub.add_parser('quote', help='Last forex quote')
    fx_quote_p.add_argument('from_currency', help='From currency (e.g. EUR)')
    fx_quote_p.add_argument('to_currency', help='To currency (e.g. USD)')
    fx_quote_p.add_argument('--source', choices=forex_quote_sources,
                            default=_forex_quote_source_default(forex_quote_sources),
                            help='Data source (default: data_providers.forex, else ib)')

    fx_convert_p = fx_sub.add_parser('convert', help='Currency conversion')
    fx_convert_p.add_argument('from_currency', help='From currency (e.g. EUR)')
    fx_convert_p.add_argument('to_currency', help='To currency (e.g. USD)')
    fx_convert_p.add_argument('amount', type=float, help='Amount to convert')
    fx_convert_p.add_argument('--source', choices=source_choices(Capability.FOREX), default=None,
                              help='Data source (default: data_providers.forex, else the builtin default)')
```

3. Replace `_handle_forex` with:

```python
def _handle_forex(mmr: MMR, args: argparse.Namespace):
    """Forex commands. snapshot/quote default to IB; the others use a registry source."""
    import logging as _logging
    _logging.getLogger('urllib3').setLevel(_logging.WARNING)
    from trader.data_providers import ProviderError

    action = getattr(args, 'fx_action', None)
    if not action:
        console.print('[yellow]Usage: forex snapshot|snapshot-all|movers|quote|convert[/yellow]')
        return
    try:
        _run_forex_action(mmr, args, action)
    except (ValueError, ProviderError) as ex:
        print_status(str(ex), success=False)


def _run_forex_action(mmr: MMR, args: argparse.Namespace, action: str):
    from trader.data_providers import Capability
    if action in ('snapshot', 'snap'):
        pair = args.pair.upper()
        print_dict(mmr.forex_snapshot(pair, source=args.source), title=f'Forex Snapshot: {pair} ({args.source})')
    elif action == 'quote':
        base, quote = args.from_currency.upper(), args.to_currency.upper()
        print_dict(mmr.forex_quote(base, quote, source=args.source),
                   title=f'Forex Quote: {base}/{quote} ({args.source})')
    elif action == 'convert':
        source = args.source or mmr._provider_default(Capability.FOREX)
        base, quote = args.from_currency.upper(), args.to_currency.upper()
        print_dict(mmr.forex_convert(base, quote, args.amount, source=source),
                   title=f'Convert: {args.amount} {base} → {quote} ({source})')
    elif action == 'snapshot-all':
        source = args.source or mmr._provider_default(Capability.FOREX)
        base = args.base.upper()
        symbols = [symbol.upper() for symbol in args.symbols] or None
        print_df(mmr.forex_snapshot_all(base=base, symbols=symbols, source=source),
                 title=f'Forex Rates vs {base} ({source})')
    elif action == 'movers':
        source = args.source or mmr._provider_default(Capability.MOVERS_FOREX)
        direction = 'losers' if args.losers else 'gainers'
        print_df(mmr.forex_movers(direction=direction, source=source),
                 title=f'Forex Movers ({direction}) — {source}')
    else:
        console.print(f'[yellow]Unknown forex action: {action}[/yellow]')
```

4. In `main()`, replace the block that starts `# Forex commands routed via twelvedata (or massive REST) don't need IB.` (5 lines, `if cmd == 'forex': ...`) with:

```python
        # Only IB-routed forex snapshot/quote need trader_service.
        if cmd in ('forex', 'fx') and not _forex_needs_trader(args):
            is_local = True
```

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers tests/test_twelvedata_sdk.py -q -p no:cacheprovider`
Expected: all pass. Then `grep -n "_parse_forex_pair\|_src_default(\['ib', 'massive', 'twelvedata'\]" trader/` → no matches.

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex add trader/sdk.py trader/mmr_cli.py tests/data_providers
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex commit -m "refactor(forex): route forex commands through the provider registry

Forex no longer inherits default_data_source: snapshot/quote default to
IB (data_providers.forex overrides), the rest use the registry default.
Pair parsing is strict; 'EUR' alone no longer means EUR/USD.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Frankfurter (ECB daily rates) — rate, rates, convert; default for convert and snapshot-all

**Files:**
- Create: `trader/data_providers/frankfurter.py`, `tests/data_providers/fixtures/frankfurter_ecb_majors.json`
- Modify: `trader/data_providers/builtin.py` (new `frankfurter` spec; `BUILTIN_DEFAULTS[FOREX] = 'frankfurter'`)
- Test: `tests/data_providers/test_frankfurter.py`

**Interfaces:**
- Consumes: Task 1 shapes and parsers; `RateLimiter`, `call_with_retry`; `ProviderError`.
- Produces: `FrankfurterClient(session=None, limiter=None, base_url=FRANKFURTER_URL)` with `.get_json(path, params)`; `DailyRates(previous_date, latest_date, previous, latest)` with `.pair(base, quote) -> tuple[float, float]` (previous, latest); `FrankfurterForex(client, today=dt.date.today)` implementing `ForexProvider` plus `.daily_rates(currencies: Optional[Iterable[str]] = None) -> DailyRates`; constants `FRANKFURTER_URL`, `RATES_PATH = '/v2/rates'`, `ECB_NOTE`, `LOOKBACK_DAYS = 14`; builder `builtin._frankfurter_forex(config)`.

- [ ] **Step 1: Create the fixture** (real `GET /v2/rates?base=EUR&quotes=AUD,CAD,CHF,GBP,JPY,NZD,USD&providers=ECB&from=2026-09-20&to=2026-10-04`, captured 2026-10-04, trimmed to the last three dates)

```json
[{"date": "2026-09-30", "base": "EUR", "quote": "AUD", "rate": 1.6297}, {"date": "2026-09-30", "base": "EUR", "quote": "CAD", "rate": 1.6105}, {"date": "2026-09-30", "base": "EUR", "quote": "CHF", "rate": 0.9478}, {"date": "2026-09-30", "base": "EUR", "quote": "GBP", "rate": 0.85463}, {"date": "2026-09-30", "base": "EUR", "quote": "JPY", "rate": 178.27}, {"date": "2026-09-30", "base": "EUR", "quote": "NZD", "rate": 2.0115}, {"date": "2026-09-30", "base": "EUR", "quote": "USD", "rate": 1.1355}, {"date": "2026-10-01", "base": "EUR", "quote": "AUD", "rate": 1.6255}, {"date": "2026-10-01", "base": "EUR", "quote": "CAD", "rate": 1.6095}, {"date": "2026-10-01", "base": "EUR", "quote": "CHF", "rate": 0.9437}, {"date": "2026-10-01", "base": "EUR", "quote": "GBP", "rate": 0.85373}, {"date": "2026-10-01", "base": "EUR", "quote": "JPY", "rate": 178.49}, {"date": "2026-10-01", "base": "EUR", "quote": "NZD", "rate": 2.0121}, {"date": "2026-10-01", "base": "EUR", "quote": "USD", "rate": 1.1298}, {"date": "2026-10-02", "base": "EUR", "quote": "AUD", "rate": 1.6176}, {"date": "2026-10-02", "base": "EUR", "quote": "CAD", "rate": 1.5984}, {"date": "2026-10-02", "base": "EUR", "quote": "CHF", "rate": 0.9279}, {"date": "2026-10-02", "base": "EUR", "quote": "GBP", "rate": 0.85033}, {"date": "2026-10-02", "base": "EUR", "quote": "JPY", "rate": 176.99}, {"date": "2026-10-02", "base": "EUR", "quote": "NZD", "rate": 2.0002}, {"date": "2026-10-02", "base": "EUR", "quote": "USD", "rate": 1.1225}]
```

Probe facts this task relies on (2026-10-04): unknown code `XXX` → HTTP 422 `{"status":422,"message":"invalid currency: XXX"}`; a valid code ECB does not publish (`COP`) with `providers=ECB` → HTTP 200 with that code silently missing; an unknown provider → HTTP 200 `[]`; base=EUR "all currencies" includes an `EUR→EUR 1.0` row; the time series returns business days only (2026-10-03/04 absent). Cross-check: v1 `latest?base=EUR&symbols=USD&amount=1000` gave `1122.5`.

- [ ] **Step 2: Write the failing test**

```python
# tests/data_providers/test_frankfurter.py
import datetime as dt
import json
import math
from pathlib import Path

import pytest
import requests

from trader.data_providers.capabilities import FX_CONVERSION_FIELDS, FX_RATE_FIELDS, FX_RATES_COLUMNS, Capability
from trader.data_providers.errors import ProviderError, ProviderRateLimited
from trader.data_providers.frankfurter import ECB_NOTE, FrankfurterClient, FrankfurterForex
from trader.data_providers.registry import ProviderRegistry

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'frankfurter_ecb_majors.json').read_text())
SUNDAY = dt.date(2026, 10, 4)


class FakeResponse:
    def __init__(self, status_code, payload, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.requests.append({'url': url, 'params': dict(params or {}), 'timeout': timeout})
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class NoWaitLimiter:
    def acquire(self):
        pass


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.rows


def _client(*responses):
    session = FakeSession(responses)
    return FrankfurterClient(session=session, limiter=NoWaitLimiter()), session


def _forex(rows=FIXTURE):
    client = FakeClient(rows)
    return FrankfurterForex(client, today=lambda: SUNDAY), client


def test_client_sends_params_and_timeout():
    client, session = _client(FakeResponse(200, []))
    assert client.get_json('/v2/rates', {'base': 'EUR'}) == []
    assert session.requests == [{'url': 'https://api.frankfurter.dev/v2/rates', 'params': {'base': 'EUR'},
                                 'timeout': 30}]


@pytest.mark.parametrize('status', [404, 422])
def test_client_maps_bad_input_to_value_error(status):
    client, _ = _client(FakeResponse(status, {'status': status, 'message': 'invalid currency: XXX'}))
    with pytest.raises(ValueError, match='invalid currency: XXX'):
        client.get_json('/v2/rates', {})


def test_client_other_http_error_is_provider_error():
    client, _ = _client(FakeResponse(500, {'message': 'boom'}))
    with pytest.raises(ProviderError, match='HTTP 500 boom'):
        client.get_json('/v2/rates', {})


def test_client_network_error_is_provider_error():
    client, _ = _client(requests.ConnectionError('down'))
    with pytest.raises(ProviderError, match='request failed'):
        client.get_json('/v2/rates', {})


def test_client_429_retries_then_rate_limited():
    limited = FakeResponse(429, {'message': 'slow down'}, headers={'Retry-After': '0'})
    client, session = _client(limited, limited, limited)
    with pytest.raises(ProviderRateLimited):
        client.get_json('/v2/rates', {})
    assert len(session.requests) == 3


def test_rate_uses_two_latest_complete_ecb_dates():
    forex, client = _forex()
    rate = forex.rate('EUR', 'USD')
    assert client.calls == [('/v2/rates', {'base': 'EUR', 'quotes': 'USD', 'providers': 'ECB',
                                           'from': '2026-09-20', 'to': '2026-10-04'})]
    assert tuple(rate) == FX_RATE_FIELDS
    assert rate['last'] == 1.1225 and rate['close'] == 1.1225 and rate['previous_close'] == 1.1298
    assert rate['change'] == pytest.approx(-0.0073)
    assert rate['change_pct'] == pytest.approx(-0.6461, abs=1e-4)
    assert rate['as_of'] == '2026-10-02' and rate['source'] == 'frankfurter' and rate['note'] == ECB_NOTE
    assert math.isnan(rate['bid']) and math.isnan(rate['ask'])


def test_cross_rate_via_eur():
    forex, client = _forex()
    rate = forex.rate('usd', 'jpy')
    assert client.calls[0][1]['quotes'] == 'JPY,USD'
    assert rate['pair'] == 'USD/JPY'
    assert rate['last'] == pytest.approx(157.675) and rate['previous_close'] == pytest.approx(157.984)


def test_date_missing_a_currency_is_skipped():
    rows = [r for r in FIXTURE if not (r['date'] == '2026-10-02' and r['quote'] == 'JPY')]
    rate = _forex(rows)[0].rate('USD', 'JPY')
    assert rate['as_of'] == '2026-10-01'
    assert rate['last'] == pytest.approx(157.984)
    assert rate['previous_close'] == pytest.approx(178.27 / 1.1355, rel=1e-5)


def test_fewer_than_two_dates_raises():
    rows = [r for r in FIXTURE if r['date'] == '2026-10-02']
    with pytest.raises(ProviderError, match='need 2'):
        _forex(rows)[0].rate('EUR', 'USD')


def test_currency_ecb_does_not_publish_raises():
    with pytest.raises(ValueError, match='ECB publishes no daily rate for COP'):
        _forex()[0].rate('USD', 'COP')


def test_empty_response_is_provider_error():
    with pytest.raises(ProviderError, match='no ECB rates'):
        _forex([])[0].rate('EUR', 'USD')


def test_rate_against_other_base_is_provider_error():
    rows = [dict(r, base='USD') for r in FIXTURE]
    with pytest.raises(ProviderError, match="'USD'"):
        _forex(rows)[0].rate('EUR', 'JPY')


def test_invalid_currency_raises_before_request():
    forex, client = _forex()
    for base, quote in (('EURO', 'USD'), ('EUR', 'EUR'), ('', 'USD')):
        with pytest.raises(ValueError):
            forex.rate(base, quote)
    assert client.calls == []


def test_convert_matches_frankfurter_amount_math():
    out = _forex()[0].convert('EUR', 'USD', 1000.0)
    assert tuple(out) == FX_CONVERSION_FIELDS
    assert out['converted'] == pytest.approx(1122.5) and out['rate'] == 1.1225
    assert out['as_of'] == '2026-10-02' and out['source'] == 'frankfurter' and out['note'] == ECB_NOTE
    assert math.isnan(out['bid'])


def test_rates_for_symbols():
    frame = _forex()[0].rates('USD', ['JPY', 'CAD'])
    assert tuple(frame.columns[:len(FX_RATES_COLUMNS)]) == FX_RATES_COLUMNS
    assert frame['pair'].tolist() == ['USD/CAD', 'USD/JPY']
    assert set(frame['as_of']) == {'2026-10-02'} and set(frame['source']) == {'frankfurter'}
    assert set(frame['note']) == {ECB_NOTE}


def test_rates_without_symbols_covers_every_ecb_currency():
    rows = FIXTURE + [{'date': d, 'base': 'EUR', 'quote': 'EUR', 'rate': 1.0} for d in ('2026-10-01', '2026-10-02')]
    forex, client = _forex(rows)
    frame = forex.rates('USD', None)
    assert 'quotes' not in client.calls[0][1]
    assert sorted(frame['pair']) == ['USD/AUD', 'USD/CAD', 'USD/CHF', 'USD/EUR', 'USD/GBP', 'USD/JPY', 'USD/NZD']


def test_rates_base_not_published_raises():
    with pytest.raises(ValueError, match='COP'):
        _forex()[0].rates('COP', None)


def test_registry_frankfurter_needs_no_key_and_is_forex_default():
    from trader.data_providers.builtin import forex_quote_source_choices
    registry = ProviderRegistry.from_config({'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.FOREX) == 'frankfurter'
    assert isinstance(registry.get(Capability.FOREX), FrankfurterForex)
    assert forex_quote_source_choices() == ['ib', 'frankfurter', 'massive', 'twelvedata']
```

- [ ] **Step 3: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_frankfurter.py -v -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.frankfurter'`

- [ ] **Step 4: Implement**

```python
# trader/data_providers/frankfurter.py
"""ECB daily reference rates from Frankfurter (free, no key).

One fixing per TARGET business day (about 16:00 CET) — not live quotes. Every call
pins providers=ECB: Frankfurter's unpinned v2 rate blends ~90 central banks and
stamps the blend with today's date, which would mislabel the data.
"""

import datetime as dt
from typing import Any, Callable, Iterable, Mapping, NamedTuple, Optional, Sequence

import pandas as pd
import requests

from trader.data_providers.capabilities import make_fx_conversion, make_fx_rate, sort_fx_rates
from trader.data_providers.errors import ProviderError
from trader.data_providers.rate_limit import RateLimiter, call_with_retry
from trader.data_providers.symbols import parse_currency, parse_forex_codes

FRANKFURTER_URL = 'https://api.frankfurter.dev'
RATES_PATH = '/v2/rates'
ECB_PROVIDER = 'ECB'
PIVOT_CURRENCY = 'EUR'
LOOKBACK_DAYS = 14
REQUEST_TIMEOUT_SECS = 30
SOURCE = 'frankfurter'
ECB_NOTE = 'ECB daily reference rate, not a live quote'

# Spec §7: polite pacing, shared by every client in this process.
FRANKFURTER_LIMITER = RateLimiter(5, 1.0)

_RATES_FRAME_COLUMNS = ['pair', 'last', 'previous_close', 'change', 'change_pct', 'as_of', 'source', 'note']


class FrankfurterClient:
    def __init__(self, session: Optional[requests.Session] = None, limiter: Optional[RateLimiter] = None,
                 base_url: str = FRANKFURTER_URL):
        self._session = session or requests.Session()
        self._limiter = limiter or FRANKFURTER_LIMITER
        self._base_url = base_url

    def get_json(self, path: str, params: Mapping[str, Any]) -> Any:
        try:
            response = call_with_retry(lambda: self._send(path, params), provider=SOURCE)
        except requests.RequestException as ex:
            raise ProviderError(f'frankfurter {path} request failed: {ex}') from ex
        if response.status_code in (404, 422):
            raise ValueError(f'frankfurter rejected the request: {_message(response)}')
        if response.status_code >= 400:
            raise ProviderError(f'frankfurter {path} failed: HTTP {response.status_code} {_message(response)}')
        return response.json()

    def _send(self, path: str, params: Mapping[str, Any]):
        self._limiter.acquire()
        return self._session.get(self._base_url + path, params=dict(params), timeout=REQUEST_TIMEOUT_SECS)


def _message(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ''
    return payload.get('message', '') if isinstance(payload, dict) else ''


class DailyRates(NamedTuple):
    """ECB rates in units per 1 EUR (EUR itself = 1.0) on the two latest complete dates."""
    previous_date: str
    latest_date: str
    previous: dict
    latest: dict

    def pair(self, base: str, quote: str) -> tuple[float, float]:
        """(previous, latest) price of one `base` in `quote`."""
        return _cross(self.previous, base, quote), _cross(self.latest, base, quote)


def _cross(rates: Mapping[str, float], base: str, quote: str) -> float:
    # ECB publishes 5 significant digits; 6 keeps all of them and drops float noise from the division.
    return float(f'{rates[quote] / rates[base]:.6g}')


def _pct(previous: float, latest: float) -> float:
    return (latest - previous) / previous * 100


class FrankfurterForex:
    def __init__(self, client, today: Callable[[], dt.date] = dt.date.today):
        self._client = client
        self._today = today

    def rate(self, base: str, quote: str) -> dict:
        base, quote = parse_forex_codes(base, quote)
        rates = self.daily_rates({base, quote})
        previous, latest = rates.pair(base, quote)
        return make_fx_rate(base, quote, last=latest, close=latest, previous_close=previous,
                            change=latest - previous, change_pct=_pct(previous, latest),
                            as_of=rates.latest_date, source=SOURCE, note=ECB_NOTE)

    def rates(self, base: str, symbols: Optional[Sequence[str]]) -> pd.DataFrame:
        base = parse_currency(base)
        if symbols:
            pairs = [parse_forex_codes(base, symbol) for symbol in symbols]
            rates = self.daily_rates({currency for pair in pairs for currency in pair})
        else:
            rates = self.daily_rates()
            if base not in rates.latest or base not in rates.previous:
                raise ValueError(f'ECB publishes no daily rate for {base} (frankfurter)')
            published = sorted(set(rates.previous) & set(rates.latest))
            pairs = [(base, quote) for quote in published if quote != base]
        rows = []
        for pair_base, quote in pairs:
            previous, latest = rates.pair(pair_base, quote)
            rows.append({'pair': f'{pair_base}/{quote}', 'last': latest, 'previous_close': previous,
                         'change': latest - previous, 'change_pct': _pct(previous, latest),
                         'as_of': rates.latest_date, 'source': SOURCE, 'note': ECB_NOTE})
        return sort_fx_rates(pd.DataFrame(rows, columns=_RATES_FRAME_COLUMNS))

    def convert(self, base: str, quote: str, amount: float) -> dict:
        base, quote = parse_forex_codes(base, quote)
        rates = self.daily_rates({base, quote})
        rate = rates.pair(base, quote)[1]
        return make_fx_conversion(base, quote, amount, converted=amount * rate, rate=rate,
                                  as_of=rates.latest_date, source=SOURCE, note=ECB_NOTE)

    def daily_rates(self, currencies: Optional[Iterable[str]] = None) -> DailyRates:
        """ECB rates on the two latest dates that have every requested currency (None = all)."""
        wanted = None if currencies is None else sorted({parse_currency(c) for c in currencies} - {PIVOT_CURRENCY})
        end = self._today()
        start = end - dt.timedelta(days=LOOKBACK_DAYS)
        params = {'base': PIVOT_CURRENCY, 'providers': ECB_PROVIDER, 'from': start.isoformat(), 'to': end.isoformat()}
        if wanted:
            params['quotes'] = ','.join(wanted)
        by_date = _rates_by_date(self._client.get_json(RATES_PATH, params))
        if not by_date:
            raise ProviderError(f'frankfurter returned no ECB rates between {start} and {end}')
        if wanted:
            published = set().union(*by_date.values())
            missing = [currency for currency in wanted if currency not in published]
            if missing:
                raise ValueError(f"ECB publishes no daily rate for {', '.join(missing)} (frankfurter)")
            dates = [day for day in sorted(by_date) if set(wanted) <= set(by_date[day])]
        else:
            dates = sorted(by_date)
        if len(dates) < 2:
            raise ProviderError(f'frankfurter returned {len(dates)} complete ECB rate date(s) '
                                f'between {start} and {end}; need 2')
        previous_date, latest_date = dates[-2], dates[-1]
        return DailyRates(previous_date, latest_date,
                          {PIVOT_CURRENCY: 1.0, **by_date[previous_date]},
                          {PIVOT_CURRENCY: 1.0, **by_date[latest_date]})


def _rates_by_date(rows: Any) -> dict[str, dict[str, float]]:
    if not isinstance(rows, list):
        raise ProviderError(f'frankfurter returned an unexpected payload: {str(rows)[:200]}')
    by_date: dict[str, dict[str, float]] = {}
    for row in rows:
        if row.get('base') != PIVOT_CURRENCY:
            raise ProviderError(f"frankfurter returned a rate against {row.get('base')!r}, expected {PIVOT_CURRENCY}")
        if row['quote'] != PIVOT_CURRENCY:
            by_date.setdefault(row['date'], {})[row['quote']] = float(row['rate'])
    return by_date
```

`trader/data_providers/builtin.py`:

```python
def _frankfurter_forex(config: Mapping[str, Any]):
    from trader.data_providers.frankfurter import FrankfurterClient, FrankfurterForex
    return FrankfurterForex(FrankfurterClient())
```

Add `ProviderSpec('frankfurter', (), {Capability.FOREX: _frankfurter_forex}),` to `builtin_specs()` and set `BUILTIN_DEFAULTS[Capability.FOREX] = 'frankfurter'`.

- [ ] **Step 5: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers tests/test_twelvedata_sdk.py -q -p no:cacheprovider`
Expected: all pass. `test_cli_forex.py::test_forex_defaults_ignore_default_data_source` now checks `frankfurter` through `BUILTIN_DEFAULTS`.

- [ ] **Step 6: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex add trader/data_providers tests/data_providers
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex commit -m "feat(providers): add Frankfurter ECB daily rates as the forex default

forex convert and snapshot-all now default to free ECB reference rates,
labelled with their ECB date. Every call pins providers=ECB; the
unpinned v2 rate is a blend of ~90 central banks.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Computed FX movers (`computed_fx`) as the forex movers default

**Files:**
- Create: `trader/data_providers/computed_movers.py`
- Modify: `trader/data_providers/builtin.py` (new `computed_fx` spec; `BUILTIN_DEFAULTS[MOVERS_FOREX] = 'computed_fx'`), `trader/mmr_cli.py` (final forex help/epilog text)
- Test: `tests/data_providers/test_computed_fx_movers.py`

**Interfaces:**
- Consumes: `FrankfurterForex.daily_rates`, `DailyRates.pair`, `_frankfurter_forex` (Task 4); `sort_movers`, `MOVER_COLUMNS`.
- Produces: `MAJOR_FX_PAIRS: tuple[tuple[str, str], ...]`; `ComputedFxMovers(forex)` implementing `MoversProvider` (`markets = frozenset({'forex'})`); `_MOVER_FRAME_COLUMNS` (module list, reused in Task 6); builder `builtin._computed_fx_movers(config)`.

- [ ] **Step 1: Write the failing test** (expected values are hand-computed from the Task 4 fixture: e.g. USDJPY = 176.99/1.1225 = 157.675 vs 178.49/1.1298 = 157.984 → −0.1956 %)

```python
# tests/data_providers/test_computed_fx_movers.py
import datetime as dt
import json
import math
from pathlib import Path

import pytest

from trader.data_providers.capabilities import MOVER_COLUMNS, Capability
from trader.data_providers.computed_movers import MAJOR_FX_PAIRS, ComputedFxMovers
from trader.data_providers.errors import CapabilityNotSupported, ProviderError
from trader.data_providers.frankfurter import FrankfurterForex
from trader.data_providers.registry import ProviderRegistry

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'frankfurter_ecb_majors.json').read_text())


class FakeClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.rows


def _movers(rows=FIXTURE):
    client = FakeClient(rows)
    return ComputedFxMovers(FrankfurterForex(client, today=lambda: dt.date(2026, 10, 4))), client


def test_major_pairs_list():
    assert [base + quote for base, quote in MAJOR_FX_PAIRS] == [
        'EURUSD', 'USDJPY', 'GBPUSD', 'USDCHF', 'AUDUSD', 'USDCAD', 'NZDUSD', 'EURGBP', 'EURJPY', 'GBPJPY']


def test_gainers_frame_in_one_request():
    movers, client = _movers()
    frame = movers.movers('forex', 'gainers')
    assert len(client.calls) == 1
    assert client.calls[0][1]['quotes'] == 'AUD,CAD,CHF,GBP,JPY,NZD,USD'
    assert tuple(frame.columns[:len(MOVER_COLUMNS)]) == MOVER_COLUMNS
    assert frame['ticker'].tolist() == ['USDCAD', 'NZDUSD', 'AUDUSD', 'USDJPY', 'GBPUSD',
                                        'EURGBP', 'GBPJPY', 'EURUSD', 'EURJPY', 'USDCHF']
    usdjpy = frame.set_index('ticker').loc['USDJPY']
    assert usdjpy['name'] == 'USD/JPY' and usdjpy['close'] == pytest.approx(157.675)
    assert usdjpy['change_pct'] == pytest.approx(-0.1956, abs=1e-4)
    assert math.isnan(usdjpy['volume'])
    assert set(frame['provider']) == {'computed_fx'}
    assert set(frame['note']) == {'ECB daily rates 2026-10-02 vs 2026-10-01'}


def test_losers_put_biggest_drop_first():
    frame = _movers()[0].movers('forex', 'losers')
    assert frame['ticker'].iloc[0] == 'USDCHF'
    assert frame['change_pct'].iloc[0] == pytest.approx(-1.0349, abs=1e-4)


def test_other_market_rejected():
    with pytest.raises(CapabilityNotSupported, match='stocks movers'):
        _movers()[0].movers('stocks', 'gainers')


def test_single_date_raises():
    rows = [r for r in FIXTURE if r['date'] == '2026-10-02']
    with pytest.raises(ProviderError, match='need 2'):
        _movers(rows)[0].movers('forex', 'gainers')


def test_registry_default_is_computed_fx_and_needs_no_key():
    from trader.data_providers.builtin import source_choices
    registry = ProviderRegistry.from_config({'default_data_source': 'massive'})
    assert registry.default_source(Capability.MOVERS_FOREX) == 'computed_fx'
    assert isinstance(registry.get(Capability.MOVERS_FOREX), ComputedFxMovers)
    assert source_choices(Capability.MOVERS_FOREX) == ['computed_fx', 'massive']


def test_cli_accepts_computed_fx_source():
    from trader.mmr_cli import build_parser
    assert build_parser().parse_args(['forex', 'movers', '--source', 'computed_fx']).source == 'computed_fx'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_computed_fx_movers.py -v -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.computed_movers'`

- [ ] **Step 3: Implement**

```python
# trader/data_providers/computed_movers.py
"""Movers computed locally where no free provider ranks them."""

import pandas as pd

from trader.data_providers.capabilities import sort_movers
from trader.data_providers.errors import CapabilityNotSupported

MAJOR_FX_PAIRS: tuple[tuple[str, str], ...] = (
    ('EUR', 'USD'), ('USD', 'JPY'), ('GBP', 'USD'), ('USD', 'CHF'), ('AUD', 'USD'), ('USD', 'CAD'), ('NZD', 'USD'),
    ('EUR', 'GBP'), ('EUR', 'JPY'), ('GBP', 'JPY'),
)

_MOVER_FRAME_COLUMNS = ['ticker', 'name', 'close', 'volume', 'change', 'change_pct', 'provider', 'note']


class ComputedFxMovers:
    """Day-over-day change of the FX majors between the two latest ECB fixings (daily, not live)."""

    markets = frozenset({'forex'})

    def __init__(self, forex):
        self._forex = forex

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        if market not in self.markets:
            raise CapabilityNotSupported(f'{market} movers', 'computed_fx', ['massive'])
        rates = self._forex.daily_rates({currency for pair in MAJOR_FX_PAIRS for currency in pair})
        note = f'ECB daily rates {rates.latest_date} vs {rates.previous_date}'
        rows = []
        for base, quote in MAJOR_FX_PAIRS:
            previous, latest = rates.pair(base, quote)
            rows.append({'ticker': f'{base}{quote}', 'name': f'{base}/{quote}', 'close': latest,
                         'volume': float('nan'), 'change': latest - previous,
                         'change_pct': (latest - previous) / previous * 100,
                         'provider': 'computed_fx', 'note': note})
        return sort_movers(pd.DataFrame(rows, columns=_MOVER_FRAME_COLUMNS), direction)
```

`trader/data_providers/builtin.py`:

```python
def _computed_fx_movers(config: Mapping[str, Any]):
    from trader.data_providers.computed_movers import ComputedFxMovers
    return ComputedFxMovers(_frankfurter_forex(config))
```

Add `ProviderSpec('computed_fx', (), {Capability.MOVERS_FOREX: _computed_fx_movers}),` and set `BUILTIN_DEFAULTS[Capability.MOVERS_FOREX] = 'computed_fx'`.

`trader/mmr_cli.py` — final forex parser text (all sources now exist). Replace the `fx_p = sub.add_parser('forex', ...)` call with:

```python
    fx_p = sub.add_parser('forex', aliases=['fx'],
                           help='Forex: snapshot/quote via IB; free ECB daily rates for convert, snapshot-all, movers',
                           epilog='Examples:\n'
                                  '  forex snapshot EURUSD                       # via IB (default)\n'
                                  '  forex snapshot EURUSD --source frankfurter  # ECB daily reference rate, not live\n'
                                  '  forex snapshot EURUSD --source massive\n'
                                  '  forex quote EUR USD                         # IB bid/ask (default)\n'
                                  '  forex snapshot-all                          # ECB daily rates, every currency vs USD\n'
                                  '  forex snapshot-all EUR JPY --base GBP\n'
                                  '  forex movers                                # 10 FX majors/crosses, ECB day-over-day\n'
                                  '  forex movers --losers --source massive\n'
                                  '  forex convert EUR USD 1000                  # ECB daily rate (frankfurter)',
                           formatter_class=fmt)
```

and change the two `fx_snap_p` / `fx_quote_p` `--source` help strings to `'Data source (default: data_providers.forex, else ib). frankfurter = ECB daily reference rate, not live'`.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex add trader/data_providers trader/mmr_cli.py tests/data_providers
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex commit -m "feat(providers): compute forex movers from ECB daily rates

forex movers ranks 10 major pairs and crosses by the change between the
two latest ECB fixings (computed_fx). Massive stays opt-in.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: ETF-proxy index movers (`etf_proxy`) as the indices default

**Files:**
- Create: `tests/data_providers/fixtures/alpaca_snapshots_index_etfs.json`
- Modify: `trader/data_providers/computed_movers.py` (add `INDEX_PROXY_ETFS`, `EtfProxyMovers`), `trader/data_providers/builtin.py` (`_ALPACA_KEYS`, `etf_proxy` spec, massive `MOVERS_INDICES`, default), `trader/data_providers/alpaca/movers.py` (indices error names `etf_proxy`, `massive`), `trader/sdk.py` (`movers`, `movers_detail`), `trader/mmr_cli.py` (movers parser, `_handle_movers`)
- Migrate: `tests/data_providers/test_alpaca_movers.py::test_unsupported_market_raises` — change `assert info.value.supported == ['massive']` to `assert info.value.supported == ['etf_proxy', 'massive']`.
- Test: `tests/data_providers/test_etf_proxy_movers.py`, `tests/data_providers/test_sdk_index_movers.py`

**Interfaces:**
- Consumes: `AlpacaQuotes` via `builtin._alpaca_quotes` (existing), `make_quote`, `sort_movers`, `_MOVER_FRAME_COLUMNS` (Task 5), `movers_capability` (Task 1).
- Produces: `INDEX_PROXY_ETFS: tuple[tuple[str, str], ...]` (ETF → index); `EtfProxyMovers(quotes)` implementing `MoversProvider` (`markets = frozenset({'indices'})`); `builtin._ALPACA_KEYS`; `BUILTIN_DEFAULTS[MOVERS_INDICES] = 'etf_proxy'`; `MMR.movers` routes by `movers_capability(market)`.

- [ ] **Step 1: Create the fixture** (real `GET /v2/stocks/snapshots?symbols=SPY,QQQ,...&feed=iex`, Friday 2026-10-02 close, captured 2026-10-04; trimmed to SPY, QQQ, XLRE, `minuteBar` removed — the other 12 ETFs are deliberately absent so the "no snapshot" path is exercised; note XLRE's `latestQuote.ap: 0`, which is why the last-trade price is used)

```json
{"SPY": {"dailyBar": {"c": 769.65, "h": 772.65, "l": 767.16, "n": 25113, "o": 770.68, "t": "2026-10-02T04:00:00Z", "v": 1498708, "vw": 769.820149}, "latestQuote": {"ap": 769.78, "as": 1480, "ax": "V", "bp": 769.64, "bs": 1480, "bx": "V", "c": ["R"], "t": "2026-10-02T20:00:08.603511705Z", "z": "B"}, "latestTrade": {"c": [" ", "T"], "i": 52983997229884, "p": 769.72, "s": 40, "t": "2026-10-02T20:20:08.09286524Z", "x": "V", "z": "B"}, "prevDailyBar": {"c": 764.1, "h": 765.63, "l": 758.815, "n": 21934, "o": 764.34, "t": "2026-10-01T04:00:00Z", "v": 1414181, "vw": 762.589018}}, "QQQ": {"dailyBar": {"c": 749.49, "h": 754.51, "l": 747.63, "n": 7925, "o": 751.31, "t": "2026-10-02T04:00:00Z", "v": 467408, "vw": 750.390178}, "latestQuote": {"ap": 749.1, "as": 280, "ax": "V", "bp": 749.01, "bs": 280, "bx": "V", "c": ["R"], "t": "2026-10-02T20:50:51.555295116Z", "z": "C"}, "latestTrade": {"c": ["@", "T"], "i": 7925, "p": 749.2, "s": 85, "t": "2026-10-02T20:54:33.372147692Z", "x": "V", "z": "C"}, "prevDailyBar": {"c": 742.02, "h": 744.65, "l": 736.25, "n": 10042, "o": 742.5, "t": "2026-10-01T04:00:00Z", "v": 490226, "vw": 740.719642}}, "XLRE": {"dailyBar": {"c": 40.8, "h": 41.29, "l": 40.755, "n": 2080, "o": 40.91, "t": "2026-10-02T04:00:00Z", "v": 515321, "vw": 40.924352}, "latestQuote": {"ap": 0, "as": 0, "ax": " ", "bp": 39.6, "bs": 100, "bx": "V", "c": ["R"], "t": "2026-10-02T20:00:02.22108925Z", "z": "B"}, "latestTrade": {"c": [" "], "i": 52983996441907, "p": 40.8, "s": 102, "t": "2026-10-02T19:59:54.100697618Z", "x": "V", "z": "B"}, "prevDailyBar": {"c": 40.7, "h": 40.85, "l": 40.41, "n": 2687, "o": 40.785, "t": "2026-10-01T04:00:00Z", "v": 847116, "vw": 40.632207}}}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/data_providers/test_etf_proxy_movers.py
import json
import math
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from trader.data_providers.alpaca.quotes import AlpacaQuotes
from trader.data_providers.capabilities import MOVER_COLUMNS, Capability, make_quote
from trader.data_providers.computed_movers import INDEX_PROXY_ETFS, EtfProxyMovers
from trader.data_providers.errors import CapabilityNotSupported, ProviderError, ProviderNotConfigured
from trader.data_providers.registry import ProviderRegistry

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'alpaca_snapshots_index_etfs.json').read_text())
ALPACA = {'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'}


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.response


def _frame(direction='gainers'):
    client = FakeClient(FIXTURE)
    return EtfProxyMovers(AlpacaQuotes(client)).movers('indices', direction), client


def test_etf_list_is_explicit():
    assert dict(INDEX_PROXY_ETFS) == {
        'SPY': 'S&P 500', 'QQQ': 'Nasdaq-100', 'DIA': 'Dow Jones Industrial Average', 'IWM': 'Russell 2000',
        'XLK': 'S&P 500 Technology sector', 'XLF': 'S&P 500 Financials sector', 'XLE': 'S&P 500 Energy sector',
        'XLV': 'S&P 500 Health Care sector', 'XLI': 'S&P 500 Industrials sector',
        'XLY': 'S&P 500 Consumer Discretionary sector', 'XLP': 'S&P 500 Consumer Staples sector',
        'XLU': 'S&P 500 Utilities sector', 'XLB': 'S&P 500 Materials sector',
        'XLRE': 'S&P 500 Real Estate sector', 'XLC': 'S&P 500 Communication Services sector',
    }


def test_ranks_etfs_from_one_alpaca_iex_request():
    frame, client = _frame()
    assert len(client.calls) == 1
    path, params = client.calls[0]
    assert path == '/v2/stocks/snapshots' and params['feed'] == 'iex'
    assert params['symbols'].split(',') == [etf for etf, _ in INDEX_PROXY_ETFS]
    assert tuple(frame.columns[:len(MOVER_COLUMNS)]) == MOVER_COLUMNS
    assert frame['ticker'].tolist()[:3] == ['QQQ', 'SPY', 'XLRE']
    assert len(frame) == len(INDEX_PROXY_ETFS)


def test_every_row_says_etf_proxy():
    frame, _ = _frame()
    assert set(frame['provider']) == {'etf_proxy'}
    assert frame['note'].str.startswith('ETF proxy for ').all()
    assert frame['name'].str.endswith('(ETF proxy)').all()
    qqq = frame.set_index('ticker').loc['QQQ']
    assert qqq['name'] == 'Nasdaq-100 (ETF proxy)'
    assert qqq['note'] == 'ETF proxy for Nasdaq-100; IEX prices as of 2026-10-02T20:54:33Z'
    assert qqq['close'] == 749.2 and qqq['change_pct'] == pytest.approx(0.9676, abs=1e-4)
    assert math.isnan(qqq['volume'])


def test_failed_etf_quote_keeps_labelled_row():
    frame, _ = _frame()
    dia = frame.set_index('ticker').loc['DIA']
    assert math.isnan(dia['change_pct']) and math.isnan(dia['close'])
    assert dia['note'] == 'ETF proxy for Dow Jones Industrial Average; IEX prices; alpaca has no snapshot for DIA'


def test_losers_put_biggest_drop_first():
    quotes = MagicMock()
    quotes.quotes.return_value = [
        make_quote(etf, last=1.0, change=0.0, change_pct=float(i), time='2026-10-02T20:00:00Z', feed='iex')
        for i, (etf, _) in enumerate(INDEX_PROXY_ETFS)]
    frame = EtfProxyMovers(quotes).movers('indices', 'losers')
    assert frame['ticker'].iloc[0] == 'SPY' and frame['ticker'].iloc[-1] == 'XLC'


def test_all_quotes_failing_raises():
    quotes = MagicMock()
    quotes.quotes.return_value = [make_quote(etf, feed='iex', error=f'alpaca has no snapshot for {etf}')
                                  for etf, _ in INDEX_PROXY_ETFS]
    with pytest.raises(ProviderError, match='no ETF quote'):
        EtfProxyMovers(quotes).movers('indices', 'gainers')


def test_rejects_other_markets():
    with pytest.raises(CapabilityNotSupported, match='stocks movers'):
        EtfProxyMovers(MagicMock()).movers('stocks', 'gainers')


def test_registry_index_movers_default_and_sources():
    registry = ProviderRegistry.from_config(dict(ALPACA, massive_api_key='m'))
    assert registry.default_source(Capability.MOVERS_INDICES) == 'etf_proxy'
    assert isinstance(registry.get(Capability.MOVERS_INDICES), EtfProxyMovers)
    assert type(registry.get(Capability.MOVERS_INDICES, 'massive')).__name__ == 'MassiveMovers'


def test_etf_proxy_without_alpaca_keys_names_env_var():
    with pytest.raises(ProviderNotConfigured, match='ALPACA_API_KEY_ID'):
        ProviderRegistry.from_config({}).get(Capability.MOVERS_INDICES)


def test_wrong_source_for_indices_names_supported():
    with pytest.raises(CapabilityNotSupported) as info:
        ProviderRegistry.from_config(ALPACA).get(Capability.MOVERS_INDICES, 'alpaca')
    assert info.value.supported == ['etf_proxy', 'massive']
```

```python
# tests/data_providers/test_sdk_index_movers.py
from argparse import Namespace
from unittest.mock import MagicMock

import pandas as pd

from trader.data_providers.capabilities import Capability

NAN = float('nan')


def _index_frame():
    return pd.DataFrame([{'ticker': 'SPY', 'name': 'S&P 500 (ETF proxy)', 'close': 0.5, 'volume': NAN,
                          'change': 0.0, 'change_pct': 1.0, 'provider': 'etf_proxy',
                          'note': 'ETF proxy for S&P 500; IEX prices'}])


def _mmr():
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    movers_provider, news_provider = MagicMock(), MagicMock()
    movers_provider.movers.return_value = _index_frame()
    news_provider.news.return_value = []
    mmr._provider = MagicMock(side_effect=lambda cap, source=None:
                              news_provider if cap == Capability.NEWS else movers_provider)
    mmr._provider_default = MagicMock(return_value='etf_proxy')
    mmr._alpaca_assets = MagicMock(return_value=None)
    return mmr, movers_provider


def test_indices_route_to_index_movers_capability():
    mmr, provider = _mmr()
    out = mmr.movers(market='indices', direction='gainers')
    mmr._provider.assert_called_once_with(Capability.MOVERS_INDICES, None)
    provider.movers.assert_called_once_with('indices', 'gainers')
    assert out['ticker'].tolist() == ['SPY']   # close 0.5 < min_price: the stock filter must not run
    mmr._alpaca_assets.assert_not_called()


def test_stocks_still_route_to_movers():
    mmr, provider = _mmr()
    provider.movers.return_value = _index_frame().assign(close=50.0)
    mmr.movers(market='stocks', direction='gainers', source='alpaca')
    mmr._provider.assert_called_once_with(Capability.MOVERS, 'alpaca')


def test_movers_detail_default_uses_market_capability():
    mmr, _ = _mmr()
    detail = mmr.movers_detail(market='indices', direction='gainers', num=5)
    mmr._provider_default.assert_called_once_with(Capability.MOVERS_INDICES)
    assert detail[0]['ticker'] == 'SPY' and detail[0]['details']['name'] == 'S&P 500 (ETF proxy)'


def test_cli_movers_sources_and_default(capsys):
    from trader.mmr_cli import _handle_movers, build_parser
    parser = build_parser()
    assert parser.parse_args(['movers', '--market', 'indices', '--source', 'etf_proxy']).source == 'etf_proxy'
    assert parser.parse_args(['movers', '--source', 'alpaca']).source == 'alpaca'
    mmr = MagicMock()
    mmr._provider_default.return_value = 'etf_proxy'
    mmr.movers.return_value = _index_frame()
    _handle_movers(mmr, Namespace(losers=False, market='indices', source=None, detail=False, num=20,
                                  min_price=1.0))
    mmr._provider_default.assert_called_once_with(Capability.MOVERS_INDICES)
    mmr.movers.assert_called_once_with(market='indices', direction='gainers', source='etf_proxy', min_price=1.0)
    assert 'etf_proxy' in capsys.readouterr().out
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_etf_proxy_movers.py tests/data_providers/test_sdk_index_movers.py -v -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'INDEX_PROXY_ETFS'`

- [ ] **Step 4: Implement**

Append to `trader/data_providers/computed_movers.py` (add `from trader.data_providers.errors import CapabilityNotSupported, ProviderError` — replace the existing errors import):

```python
INDEX_PROXY_ETFS: tuple[tuple[str, str], ...] = (
    ('SPY', 'S&P 500'),
    ('QQQ', 'Nasdaq-100'),
    ('DIA', 'Dow Jones Industrial Average'),
    ('IWM', 'Russell 2000'),
    ('XLK', 'S&P 500 Technology sector'),
    ('XLF', 'S&P 500 Financials sector'),
    ('XLE', 'S&P 500 Energy sector'),
    ('XLV', 'S&P 500 Health Care sector'),
    ('XLI', 'S&P 500 Industrials sector'),
    ('XLY', 'S&P 500 Consumer Discretionary sector'),
    ('XLP', 'S&P 500 Consumer Staples sector'),
    ('XLU', 'S&P 500 Utilities sector'),
    ('XLB', 'S&P 500 Materials sector'),
    ('XLRE', 'S&P 500 Real Estate sector'),
    ('XLC', 'S&P 500 Communication Services sector'),
)


class EtfProxyMovers:
    """Index movers approximated by index-tracking ETFs. Rows are ETFs, never the indices themselves."""

    markets = frozenset({'indices'})

    def __init__(self, quotes):
        self._quotes = quotes

    def movers(self, market: str, direction: str) -> pd.DataFrame:
        if market not in self.markets:
            raise CapabilityNotSupported(f'{market} movers', 'etf_proxy', ['massive'])
        quotes = self._quotes.quotes([etf for etf, _ in INDEX_PROXY_ETFS])
        if all(quote['error'] for quote in quotes):
            raise ProviderError(f"etf_proxy: no ETF quote available ({quotes[0]['error']})")
        rows = [_proxy_row(etf, index, quote) for (etf, index), quote in zip(INDEX_PROXY_ETFS, quotes)]
        return sort_movers(pd.DataFrame(rows, columns=_MOVER_FRAME_COLUMNS), direction)


def _proxy_row(etf: str, index: str, quote: dict) -> dict:
    note = f'ETF proxy for {index}; IEX prices'
    if quote['error']:
        note = f"{note}; {quote['error']}"
    elif quote['time']:
        note = f"{note} as of {quote['time'][:19]}Z"
    # IEX volume is a few percent of the market, so it is not shown as volume.
    return {'ticker': etf, 'name': f'{index} (ETF proxy)', 'close': quote['last'], 'volume': float('nan'),
            'change': quote['change'], 'change_pct': quote['change_pct'], 'provider': 'etf_proxy', 'note': note}
```

`trader/data_providers/builtin.py`:

```python
_ALPACA_KEYS = (('alpaca_api_key_id', 'ALPACA_API_KEY_ID'), ('alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY'))


def _etf_proxy_movers(config: Mapping[str, Any]):
    from trader.data_providers.computed_movers import EtfProxyMovers
    return EtfProxyMovers(_alpaca_quotes(config))
```

Use `_ALPACA_KEYS` as the `required_keys` of the existing `alpaca` spec, add `Capability.MOVERS_INDICES: _massive_movers` to the `massive` spec, add `ProviderSpec('etf_proxy', _ALPACA_KEYS, {Capability.MOVERS_INDICES: _etf_proxy_movers}),` and set `BUILTIN_DEFAULTS[Capability.MOVERS_INDICES] = 'etf_proxy'`.

`trader/data_providers/alpaca/movers.py` — replace the `raise` line:

```python
_OTHER_SOURCES_BY_MARKET = {'indices': ['etf_proxy', 'massive']}
```

(module level, below `MAX_TOP`), and in `movers`:

```python
            raise CapabilityNotSupported(f'{market} movers', 'alpaca', _OTHER_SOURCES_BY_MARKET.get(market, ['massive']))
```

`trader/sdk.py`:
- In `movers`, replace `from trader.data_providers import Capability` with `from trader.data_providers import movers_capability` and the provider line with `frame = self._provider(movers_capability(market), source).movers(market, direction)`. New docstring first paragraph: `"""Top movers for `market` from a registry movers source (indices: ETF proxies by default, forex: computed from ECB rates).`; keep the rest, drop the sentence "Forex has its own command (see ``forex_movers``)."
- In `movers_detail`, add `movers_capability` to the existing `from trader.data_providers import Capability` line and change `source = source or self._provider_default(Capability.MOVERS)` to `source = source or self._provider_default(movers_capability(market))`.

`trader/mmr_cli.py`:
- movers parser: change `--market` help to `'Market type (default: stocks). indices = ETF proxies (SPY, QQQ, DIA, IWM, sector SPDRs; Alpaca IEX prices) unless --source massive'`, and replace the `--source` argument with:

```python
    movers_sources = sorted(set(source_choices(Capability.MOVERS)) | set(source_choices(Capability.MOVERS_INDICES)))
    movers_p.add_argument('--source', choices=movers_sources, default=None,
                          help='Data source (default: data_providers.movers, or data_providers.movers_indices for '
                               'indices; else alpaca for stocks/crypto and etf_proxy for indices). '
                               'twelvedata needs a Pro+ plan for market movers.')
```

- `_handle_movers`: `from trader.data_providers import ProviderError, movers_capability` and `source = getattr(args, 'source', None) or mmr._provider_default(movers_capability(market))`.

- [ ] **Step 5: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers tests/test_twelvedata_sdk.py -q -p no:cacheprovider`
Expected: all pass, including the migrated `test_alpaca_movers.py::test_unsupported_market_raises`.

- [ ] **Step 6: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex add trader tests/data_providers
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex commit -m "feat(providers): rank index ETFs as the default index movers

movers --market indices now ranks SPY, QQQ, DIA, IWM and the sector
SPDRs from Alpaca IEX quotes. Every row says it is an ETF proxy;
--source massive keeps the real indices.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Live checks, real CLI run, docs

**Files:**
- Create: `tests/data_providers/test_live_frankfurter.py`
- Modify: `tests/data_providers/test_live_alpaca.py`, `CLAUDE.md`, `config_defaults/trader.yaml`, `docs/OPERATIONAL_STATE.md`, `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Add live tests**

```python
# tests/data_providers/test_live_frankfurter.py
"""Real Frankfurter calls (no key). Run with: MMR_LIVE_TESTS=1 pytest -m live tests/data_providers/test_live_frankfurter.py"""

import datetime as dt
import os

import pytest

from trader.data_providers.capabilities import Capability
from trader.data_providers.frankfurter import ECB_NOTE
from trader.data_providers.registry import ProviderRegistry

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.getenv('MMR_LIVE_TESTS') != '1', reason='live test: set MMR_LIVE_TESTS=1'),
]


def _forex():
    return ProviderRegistry.from_config({}).get(Capability.FOREX, 'frankfurter')


def test_live_eurusd_is_an_ecb_business_day_fixing():
    rate = _forex().rate('EUR', 'USD')
    as_of = dt.date.fromisoformat(rate['as_of'])
    assert 0.5 < rate['last'] < 2.0 and rate['note'] == ECB_NOTE
    assert as_of <= dt.date.today() and as_of.weekday() < 5    # a weekend date would mean blended, not ECB


def test_live_unpublished_currency_is_loud():
    with pytest.raises(ValueError, match='COP'):
        _forex().rate('USD', 'COP')


def test_live_convert_and_all_rates():
    out = _forex().convert('EUR', 'USD', 1000.0)
    assert out['converted'] == pytest.approx(1000 * out['rate'])
    frame = _forex().rates('USD', None)
    assert len(frame) >= 25 and 'USD/EUR' in set(frame['pair'])


def test_live_computed_fx_movers():
    frame = ProviderRegistry.from_config({}).get(Capability.MOVERS_FOREX).movers('forex', 'gainers')
    assert len(frame) == 10 and frame['change_pct'].notna().all()
```

Append to `tests/data_providers/test_live_alpaca.py`:

```python
def test_live_etf_proxy_index_movers():
    registry = ProviderRegistry.from_config({
        'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
        'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY'],
    })
    frame = registry.get(Capability.MOVERS_INDICES, 'etf_proxy').movers('indices', 'gainers')
    assert len(frame) == 15
    assert frame['note'].str.startswith('ETF proxy for ').all()
    assert frame['change_pct'].notna().sum() >= 12
```

Run (keys from the main checkout's `.env`, never printed):

```bash
set -a; eval "$(grep -E '^ALPACA_API_(KEY_ID|SECRET_KEY)=' /Users/mudryy/private/mmr/.env)"; set +a
MMR_LIVE_TESTS=1 /Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_live_frankfurter.py tests/data_providers/test_live_alpaca.py -v -m live -p no:cacheprovider
```

Expected: all live tests pass. Default run still skips them: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_live_frankfurter.py -q -p no:cacheprovider` → all skipped.

- [ ] **Step 2: Real CLI run** (same shell, keys loaded; record the output summary in the commit body)

```bash
/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli forex convert EUR USD 1000
/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli forex snapshot EURUSD --source frankfurter
/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli forex quote EUR JPY --source frankfurter
/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli forex snapshot-all JPY GBP CHF
/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli forex movers
/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli movers --market indices
/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli forex convert USD COP 100
/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli forex snapshot EUR --source frankfurter
```

Expected: the first six print tables labelled with the ECB date / `ETF proxy for …`; the last two print a red error (`ECB publishes no daily rate for COP`, `not a currency pair`) and no traceback. Do **not** run `forex snapshot EURUSD` without `--source` (IB Gateway is down) — that is the operator TODO below.

- [ ] **Step 3: Docs**

- `CLAUDE.md`:
  - Design principle "Free providers first, IB fallback": add a bullet after **Quotes** — "**Forex and index movers.** `forex snapshot` / `forex quote` default to IB; `forex convert`, `forex snapshot-all` and `forex movers` default to free ECB daily reference rates from Frankfurter (`computed_fx` for movers), labelled with the ECB date — not live quotes. `movers --market indices` defaults to ETF proxies (`etf_proxy`, Alpaca IEX prices), labelled as proxies. Massive/TwelveData stay opt-in via `--source`." In the **Inheritance** bullet add "forex commands" to the list that never inherits `default_data_source`, and `data_providers.forex` / `movers_forex` / `movers_indices` to the override list.
  - CLI command list: replace the line `movers --market indices --source massive   # Indices/options/futures need Massive` with

    ```
    movers --market indices          # ETF proxies (SPY, QQQ, DIA, IWM, sector SPDRs) from Alpaca IEX prices — not the indices
    movers --market indices --source massive   # Real indices (paid); options/futures still need Massive
    ```

    and replace the eight `forex ...` lines with

    ```
    forex snapshot EURUSD                        # IB (default; needs trader_service)
    forex snapshot EURUSD --source frankfurter   # ECB daily reference rate (free, not live)
    forex snapshot EURUSD --source massive       # Massive snapshot (paid)
    forex quote EUR USD                          # IB bid/ask (default)
    forex quote EUR USD --source frankfurter     # Same ECB daily rate as snapshot
    forex snapshot-all                           # ECB daily rates, every currency vs USD (Frankfurter)
    forex snapshot-all JPY GBP --base EUR        # Chosen currencies vs EUR
    forex movers                                 # 10 FX majors/crosses ranked by ECB day-over-day change (computed_fx)
    forex movers --losers --source massive       # Massive forex movers (paid)
    forex convert EUR USD 1000                   # ECB daily rate (Frankfurter); --source massive|twelvedata
    ```
  - Configuration `default_data_source` bullet: change "default `--source` for history download, watch, financials, fx where that choice is valid (watch, financials and fx read it through `_src_default`)" to "… for history download, watch, financials where that choice is valid (watch and financials read it through `_src_default`)", and add "**Forex commands never inherit it**: `forex snapshot`/`quote` default to IB, the rest to Frankfurter / `computed_fx`; override with `data_providers.forex` / `data_providers.movers_forex`; `data_providers.movers_indices` for index movers."
  - "Command Service Requirements": add `forex convert|snapshot-all|movers` and `forex snapshot|quote --source frankfurter|massive|twelvedata` to "No service needed", and `forex snapshot|quote` (IB source) to "Requires trader typed RPC".
  - Note the output changes: forex `--source massive|twelvedata` results now use the shared keys (`pair`, `as_of`, `source`, `note`; TwelveData market state is in `note`); `forex quote --source massive|twelvedata` returns the same dict as `forex snapshot`; `forex snapshot-all` takes `--base` + quote currencies; pairs must be exact (`EURUSD`, `EUR/USD`, `C:EURUSD`) — `EUR` alone is an error; unknown numbers in forex `--json` dicts are `NaN` (was `null`), same convention as 3a snapshots.
- `config_defaults/trader.yaml`: in the comment above `default_data_source`, remove `fx` from "(history download, snapshot, watch, financials, fx)" and add the line `# Forex commands never inherit it (see data_providers.forex below).`; extend the commented `data_providers:` example with

  ```yaml
  #   forex: frankfurter          # forex convert/snapshot-all; also snapshot/quote (unset = IB)
  #   movers_forex: computed_fx
  #   movers_indices: etf_proxy
  ```
- `docs/OPERATIONAL_STATE.md` — new section "Forex and index movers (phase 6)": "`forex convert`, `forex snapshot-all` and `forex movers` default to free ECB daily reference rates (Frankfurter, no key) — one fixing per business day about 16:00 CET, labelled with its date, not live. `movers --market indices` defaults to ETF proxies from Alpaca IEX prices (needs the Alpaca keys). `forex snapshot` / `forex quote` still default to IB. Users who relied on `default_data_source: twelvedata` for forex: set `data_providers: {forex: twelvedata}` in the live `~/.config/mmr/trader.yaml`." plus `- TODO (operator): when IB Gateway is up, verify mmr forex snapshot EURUSD and mmr forex quote EUR USD (IB IDEALPRO CASH path; not live-verified in phase 6).`
- `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`: mark row 6 `done`; add an "After phase 6" operator step with the same two sentences (defaults + twelvedata override) and the IB TODO.

- [ ] **Step 4: Verify**

Run: `grep -n "_massive_client\|_twelvedata_client" trader/sdk.py | grep -in forex` → no matches (no forex code left on the SDK clients). Then the full suite once, when no other lane is running its suite (tests bind fixed ports): `/Users/mudryy/private/mmr/.venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py -p no:cacheprovider` → 0 failed. If a test outside `tests/data_providers` asserts old forex output keys or the old `forex snapshot-all` signature, change only that assertion and list it in the report.

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex add tests/data_providers CLAUDE.md config_defaults/trader.yaml docs
git -C /Users/mudryy/private/mmr/.worktrees/fdp-6-forex commit -m "docs: document free forex rates and ETF-proxy index movers

<live test result and CLI run summary here>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

After this task, write the phase 7 plan (`…-07-streaming.md`) against the code as it now exists.

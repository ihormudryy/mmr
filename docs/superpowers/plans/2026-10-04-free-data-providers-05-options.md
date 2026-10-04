# Free Data Providers — Phase 5: Options

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move options market data (expirations, chain, single-contract snapshot, implied-distribution inputs) behind a new `OPTIONS` capability, wrap the existing Massive code as an adapter, and make Alpaca's free indicative feed the default. Option orders (`options buy` / `options sell`) stay on IB.

**Architecture:** One strict option-symbol module (`trader/data_providers/option_symbols.py`) parses and builds OCC symbols in both spellings (`O:AAPL261120C00250000` for Massive, `AAPL261120C00250000` for Alpaca) and raises on anything else. One row shape (`OPTION_FIELDS`, `make_option_row`) carries every chain and contract record, with `provider` and `feed` labels and NaN for anything the provider did not send. `MassiveOptions` and `AlpacaOptions` implement `OptionsProvider`; the SDK asks the registry. The implied-distribution math in `trader/tools/chain.py` is kept; a new feeder builds its inputs from capability rows and refuses NaN/zero IV instead of fitting zeros.

**Tech Stack:** Python 3.12, pandas, numpy, `requests` (via the existing `AlpacaClient`), the existing `massive` SDK, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-04-free-data-providers-design.md` (§3 Alpaca options facts, §4.1 `OptionsProvider`, §4.2 registry, §4.3 row "Options: `alpaca` (indicative); opt-in `massive`", §5 "Labels", §6 errors, §7 rate limits). Index: `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`. Format/conventions: `docs/superpowers/plans/2026-10-04-free-data-providers-03a-quotes-movers-news.md`.

## Global Constraints

- Lane worktree `/Users/mudryy/private/mmr/.worktrees/fdp-5-options`, branch `feat/fdp-5-options`. Run every command from that directory. One local commit per task. Never push. Every commit message ends with a blank line then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Use the main venv with paths that work from the worktree root: `/Users/mudryy/private/mmr/.venv/bin/pytest`, `/Users/mudryy/private/mmr/.venv/bin/python`. Never run `.venv/bin/mmr` (it runs the main checkout's code); use `/Users/mudryy/private/mmr/.venv/bin/python -m trader.mmr_cli ...`.
- Lanes 3b and 6 run in parallel and edit the same shared files (`trader/data_providers/capabilities.py` enum, `builtin.py` specs/defaults, `__init__.py` exports, `trader/sdk.py`, `trader/mmr_cli.py`). Append new enum members, specs, exports and functions; do not reorder or reformat neighbouring code, so merges stay trivial.
- Per task, run only the test files the task names (other lanes bind the same fixed ports). The full suite runs once, in Task 10, and only when the orchestrator confirms no other lane is running tests: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py -p no:cacheprovider`.
- No network in normal tests. Live tests are gated by env `MMR_LIVE_TESTS=1` **and** the needed keys (pattern: `tests/data_providers/test_live_alpaca.py`). Keys live in `/Users/mudryy/private/mmr/.env` (the worktree has no `.env`); never print them.
- Fail loudly: auth, entitlement, rate-limit and unknown-underlying failures raise a `ProviderError` subclass; malformed user input raises `ValueError`. Never return empty data for a failure, never guess a contract.
- Option row shape: exactly the keys in `OPTION_FIELDS`, in order. `ticker`, `type`, `strike`, `expiration` always come from the parsed `OptionSymbol`, never from separate provider fields. Numbers the provider did not send are `float('nan')` — never `0.0`, never computed (no derived `break_even`, no estimated greeks). `iv` is in percent (today's contract). Every row has `provider` (`alpaca` / `massive`) and `feed` (`indicative` / `opra`).
- `OPTIONS` does **not** inherit `default_data_source` (only `HISTORY` does). Default `alpaca`; `data_providers.options` overrides; `--source` overrides both.
- Alpaca: market data at `https://data.alpaca.markets` (`feed=indicative`; `feed=opra` is 403 on Basic); contract lists at `https://paper-api.alpaca.markets/v2/options/contracts` with the same paper keys as the asset list. Both clients share `ALPACA_LIMITER` (200/min) — conservative, on purpose.
- CLI `options` handlers catch `ProviderError` and print it with `print_status(str(ex), success=False)`; `ValueError` reaches the dispatcher's existing `Error: ...` handler. Read `--source` with `getattr(args, 'source', None)` (an existing test builds a `Namespace` without it).
- Do not change `web/`, `trader/tools/massive_research.py` or `trader/tools/options_data.py`'s `chain_records` / `contract_snapshot` (dashboard, phase 9).
- Comments only where the code is not obvious. Plain, intent-revealing names.

## Review Focus

1. **Option symbols in either spelling, or malformed** (`options snapshot O:AAPL261120C00250000`, `aapl261120c00250000`, `O:AAPL`, `AAPL261131C00250000`, padded `AAPL  261120C00250000`, a float strike like `2.01`): expect an exact `OptionSymbol` or a `ValueError` naming the shape; never a near-miss contract. → Task 1 tests `test_parses_both_provider_forms`, `test_rejects_malformed_symbols`, `test_build_rounds_float_strike_exactly`; Task 7 test `test_snapshot_accepts_both_forms`.
2. **Illiquid contracts on the indicative feed** (on 2026-11-20 AAPL, 55 of 168 contracts had no greeks/IV; some had only a quote): expect NaN in every missing column, `feed='indicative'` on every row, and `—` in the CLI table. → Task 4 tests `test_missing_greeks_iv_trade_and_bar_are_nan`, `test_every_row_is_labelled_indicative`; Task 8 test `test_chain_table_renders_nan_as_dash`.
3. **Implied distribution over sparse IV**: rows with NaN/zero IV are excluded and counted, not fitted as 0; too few usable strikes or a same-day expiration raise. → Task 6 tests `test_nan_and_zero_iv_rows_are_excluded_and_counted`, `test_nan_rows_do_not_change_the_distribution`, `test_too_few_usable_strikes_raises`, `test_same_day_expiration_raises`.
4. **Expirations on the order path** (`options buy AAPL -e 3m` with no Alpaca keys; `-e foobar`; `-e 2026-3-20`): expect the missing env var named, or a `ValueError`, and no order call; exact dates never touch a data provider. → Task 8 tests `test_buy_relative_expiry_without_keys_is_loud`, `test_resolve_rejects_garbage_without_lookup`; existing `tests/test_options.py::TestRelativeExpiration::test_resolve_exact_date_passthrough`.
5. **Unknown underlying, unentitled source, inherited config** (`options expirations ZZZZQ`, `options chain ZZZZQ -e 2026-11-20` — Alpaca's snapshot endpoint returns `{}` for it; `--source massive` on a plan without options; live config `default_data_source: massive`): expect a loud provider error, an entitlement error naming `--source alpaca`, and options staying on Alpaca. → Task 4 test `test_unknown_underlying_fails_before_snapshots`; Task 3 test `test_not_authorized_maps_to_entitlement_error`; Task 2 test `test_options_never_inherit_default_data_source`.

## Decisions

- Decision: the canonical `ticker` column is the bare OCC body (`AAPL261120C00250000`) for every provider; `options snapshot` accepts both spellings — one shape across providers, and it is Alpaca's native form — cost if wrong: scripts that match `O:` in `--source massive` chain JSON must strip/add the prefix.
- Decision: missing numbers are NaN, never 0.0, including on the Massive adapter (today's Massive path fills 0.0) — wrong data is worse than no data — cost if wrong: consumers that compared to 0 now see NaN/null.
- Decision: Alpaca `underlying_price` comes from one IEX stock snapshot per chain/contract call (NaN when IEX has no trade) — the indicative option snapshots carry no underlying price and `implied` needs one — cost if wrong: a stale IEX last trade off-centres the implied distribution for thin names.
- Decision: Alpaca `break_even` stays NaN — Massive sends it, Alpaca does not, and computing it would be inventing a provider field — cost if wrong: an empty column on the default path.
- Decision: the Alpaca chain makes one contracts-API call (same filters) before the snapshots, for `open_interest` and because it fails with HTTP 422 for an unknown underlying where the snapshot endpoint silently returns `{}` — cost if wrong: one extra request per chain.
- Decision: Alpaca `volume` is the daily-bar volume only when that bar is from the latest quote's session; an older daily bar means 0.0 (no trades since); no bar or no quote means NaN — `dailyBar` is "last day with trades", not today — cost if wrong: if the quote session lags, real volume shows as 0.
- Decision: an Alpaca contract that exists (contracts API 200) but has no snapshot returns a row with NaN quotes, not an error — the contract is real, the market data is absent, and the row says so — cost if wrong: a user sees an all-NaN row instead of an error.
- Decision: `implied` drops calls with NaN or ≤ 0 IV and needs at least `MIN_IMPLIED_STRIKES = 8` usable strikes, else `ValueError` with the counts; the result reports `strikes_used`, `strikes_excluded`, `provider`, `feed` — a degree-5 fit through ≤ 6 points interpolates noise, and today zeros are fitted silently — cost if wrong: thin expirations that "worked" on zeros now raise.
- Decision: implied time to expiry `T` = calendar days / 365 (today: calendar days / 255, which mixes conventions and makes T ~43% too long, widening the distribution ~20%); same-day or past expirations raise — IV from both providers is annualised on a calendar year — cost if wrong: distributions change vs before; revert is one constant.
- Decision: `implied` returns plain Python lists for `x`, `market_implied`, `constant` — today `x` is a numpy array that `--json` prints as a string — cost if wrong: none known.
- Decision: `options buy` / `options sell` stay on IB (`_resolve_option_contract` uses IB `resolve_contract`, no Massive); only a relative `-e` (`3m`) asks the OPTIONS capability (`--source` on buy/sell selects it); exact dates never call a provider and must be strict `YYYY-MM-DD` — today garbage like `foobar` or `2026-3-20` is passed to IB as-is — cost if wrong: an input IB might have tolerated is now refused.
- Decision: option-symbol mapping lives in a new `trader/data_providers/option_symbols.py`, not `symbols.py` — options and stock spellings are different grammars, and lane 6 may edit `symbols.py` — cost if wrong: two symbol modules.
- Decision: building an OCC strike rounds `strike * 1000` and refuses anything not exactly on a 1/1000 grid (the old `int(strike * 1000)` turned `2.01` into `00002009`, a different contract) — cost if wrong: none known.
- Decision: the dashboard path stays as is until phase 9, except that `chain.get_option_dates` and `chain.implied_constant` keep their signatures but route through `MassiveOptions` (so dashboard implied also gets the NaN-exclusion and T fixes); `options_data.chain_records/contract_snapshot` are untouched — cost if wrong: two Massive chain normalisers coexist until phase 9.
- Decision: Massive `BadResponse` containing `NOT_AUTHORIZED` maps to `ProviderEntitlementError` whose text keeps `NOT_AUTHORIZED` (the dashboard's `is_data_entitlement_error` matches on it) and names `--source alpaca` — cost if wrong: none known.
- Decision: Massive rows are labelled `feed='opra'` (spec §4.1) — it means OPRA-sourced, not real-time; a Massive plan may be delayed — cost if wrong: the label could suggest real-time.
- Decision: Massive `quote_time` / `last_time` stay `''` — this key gets NOT_AUTHORIZED on Massive snapshots, so the timestamp fields cannot be verified — cost if wrong: Massive users lose timestamps they could have had.
- Decision: the skill helper `implied_move` is relabelled in this phase (method `atm_straddle`, `provider`/`feed` keys, `medium` confidence on the indicative feed) — phase 5 is what makes it hit Alpaca, and it would otherwise report Alpaca indicative mids as `polygon_atm_straddle` with `high` confidence — cost if wrong: consumers matching the string `polygon_atm_straddle` break.
- Decision: an expiration with no contracts returns an empty chain and the existing "No chain data" message — the contracts call already makes unknown underlyings loud — cost if wrong: a typo in an exact date shows "No chain data" rather than a list of valid dates.

## Known findings (do not fix here unless a task says so)

- Still on Massive after this phase (phase 9 moves them): `trader/tools/massive_research.py` `options_chain` → `options_data.chain_records` and `options_snapshot` → `options_data.contract_snapshot` (old normaliser: 0.0 fills, `O:` tickers); `options_expirations` → `chain.get_option_dates` and `options_implied` → `chain.implied_constant` (Massive adapter after Task 6); `web/command_center/routes_research.py` options routes and `web/command_center/research.py` timeouts call those methods.
- `skills/mmr-skill/SKILL.md` (lines ~81, 111, 123, 149, 369–372) still says options need `massive_api_key`; only the `implied_move` code and its one example are touched (Task 9). The rest is phase 9 docs.
- `implied_constant_helper` (degree-5 `polyfit`, `new_K` from 1.0 in 0.1 steps) is kept unchanged on purpose.
- `trader/data_providers/alpaca/client.py`: `requests.RequestException` is not wrapped as `ProviderError` (AUDIT_ROADMAP open minor) — a network failure in `options` still reaches the dispatcher's generic handler.

---

## File Structure

```
trader/data_providers/
├── option_symbols.py            # OptionSymbol, parse/build, provider spellings, strict dates     (Task 1)
├── capabilities.py              # + Capability.OPTIONS, OPTION_FIELDS, make_option_row,
│                                #   option_mid, sort_option_rows, OptionsProvider                 (Task 2)
├── builtin.py                   # + OPTIONS default; massive + alpaca option builders             (Tasks 2, 3, 4)
├── __init__.py                  # + exports                                                        (Task 2)
├── massive/options.py           # MassiveOptions                                                   (Task 3)
└── alpaca/options.py            # AlpacaOptions (expirations, chain; contract in Task 5)          (Tasks 4, 5)

trader/tools/options_data.py     # parse_option_ticker / build_option_ticker delegate             (Task 1)
trader/tools/chain.py            # implied_inputs, implied_distribution; Massive-backed helpers    (Task 6)
trader/sdk.py                    # options_* through the registry, source=                         (Task 7)
trader/mmr_cli.py                # options --source, strict expirations, labels, NaN rendering     (Task 8)
skills/mmr-skill/scripts/mmr_helpers.py, skills/mmr-skill/SKILL.md   # implied_move label          (Task 9)

tests/data_providers/
├── test_option_symbols.py       (Task 1)
├── test_options_capability.py   (Task 2)
├── test_massive_options.py      (Task 3 — replaces TestOptionsChain/TestOptionsSnapshot coverage)
├── test_alpaca_options.py       (Tasks 4, 5)
├── test_sdk_options.py          (Task 7)
├── test_cli_options.py          (Task 8)
├── fixtures/alpaca_option_snapshots_aapl.json, alpaca_option_contracts_aapl.json   (Task 4)
└── test_live_alpaca.py          (extended in Task 10)
tests/test_options_implied.py    (Task 6)
tests/test_mmr_skill_helpers.py  (extended in Task 9)

Modified tests: tests/test_options.py (TestOptionsChain + TestOptionsSnapshot removed in Task 7)
Docs: CLAUDE.md, docs/OPERATIONAL_STATE.md, docs/superpowers/plans/…-00-index.md (Task 10)
```

---

### Task 1: One strict option-symbol module

**Files:**
- Create: `trader/data_providers/option_symbols.py`
- Modify: `trader/tools/options_data.py` (`parse_option_ticker`, `build_option_ticker` delegate; nothing else changes)
- Test: `tests/data_providers/test_option_symbols.py`

**Interfaces:**
- Consumes: `trader.data_providers.errors.ProviderError`.
- Produces:
  - `@dataclass(frozen=True) OptionSymbol(root: str, expiration: dt.date, right: str, strike_thousandths: int)` with properties `strike -> float`, `contract_type -> str` (`'call'`/`'put'`), `occ -> str` (bare OCC body).
  - `parse_option_symbol(text: str) -> OptionSymbol` — accepts `O:`-prefixed or bare, case-insensitive, outer whitespace stripped; anything else raises `ValueError` starting `Cannot parse option symbol`.
  - `parse_provider_option_symbol(provider: str, text: str) -> OptionSymbol` — same, but raises `ProviderError` (a provider broke its own contract).
  - `build_option_symbol(root: str, expiration: str | dt.date, strike: float, right: str) -> OptionSymbol` — raises `ValueError` for an inexact strike, bad root/right/date.
  - `parse_expiration_date(text: str) -> dt.date` — strict `YYYY-MM-DD`; `ValueError` mentioning `YYYY-MM-DD` otherwise.
  - `to_alpaca_option_symbol(option: OptionSymbol) -> str` (bare; refuses 6-letter roots, which Alpaca rejects) and `to_massive_option_ticker(option: OptionSymbol) -> str` (`O:` + body).

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_option_symbols.py
import datetime as dt

import pytest

from trader.data_providers.errors import ProviderError
from trader.data_providers.option_symbols import (
    OptionSymbol, build_option_symbol, parse_expiration_date, parse_option_symbol,
    parse_provider_option_symbol, to_alpaca_option_symbol, to_massive_option_ticker,
)

AAPL_CALL = OptionSymbol('AAPL', dt.date(2026, 11, 20), 'C', 250_000)


def test_parses_both_provider_forms():
    assert parse_option_symbol('O:AAPL261120C00250000') == AAPL_CALL
    assert parse_option_symbol('AAPL261120C00250000') == AAPL_CALL
    assert parse_option_symbol('  o:aapl261120c00250000 ') == AAPL_CALL


def test_fields_and_provider_spellings():
    assert AAPL_CALL.strike == 250.0 and AAPL_CALL.contract_type == 'call'
    assert AAPL_CALL.occ == 'AAPL261120C00250000'
    assert to_alpaca_option_symbol(AAPL_CALL) == 'AAPL261120C00250000'
    assert to_massive_option_ticker(AAPL_CALL) == 'O:AAPL261120C00250000'


def test_fractional_and_small_strikes():
    assert parse_option_symbol('O:SPY260619P00520500').strike == 520.5
    put = parse_option_symbol('O:F260320P00012500')
    assert (put.root, put.strike, put.contract_type) == ('F', 12.5, 'put')


def test_adjusted_root_with_digit():
    option = parse_option_symbol('AAPL1261120C00250000')
    assert option.root == 'AAPL1' and option.expiration == dt.date(2026, 11, 20)
    assert option.occ == 'AAPL1261120C00250000'


@pytest.mark.parametrize('text', [
    'O:AAPL',                      # no date/right/strike
    'AAPL261120X00250000',         # right must be C or P
    'AAPL261131C00250000',         # 31 November
    'AAPL  261120C00250000',       # OCC space padding inside the symbol
    'AAPL261120C0025000',          # 7-digit strike
    'AAPL261120C00000000',         # zero strike
    'TOOLONGX261120C00250000',     # root longer than 6 letters
    '',
    'ÄAPL261120C00250000',
    'AAPL261120C00250000 extra',
    None,
])
def test_rejects_malformed_symbols(text):
    with pytest.raises(ValueError, match='Cannot parse option symbol'):
        parse_option_symbol(text)


def test_build_rounds_float_strike_exactly():
    # int(2.01 * 1000) == 2009: the old builder named a different contract.
    assert build_option_symbol('F', '2026-03-20', 2.01, 'c').occ == 'F260320C00002010'
    assert build_option_symbol('spy', dt.date(2026, 6, 19), 520.5, 'P').occ == 'SPY260619P00520500'


@pytest.mark.parametrize('root, expiration, strike, right', [
    ('AAPL', '2026-03-20', 250.0005, 'C'),     # finer than 1/1000
    ('AAPL', '2026-03-20', 0.0, 'C'),
    ('AAPL', '2026-03-20', -5.0, 'C'),
    ('AAPL', '2026-03-20', 100_000.0, 'C'),    # does not fit 8 digits
    ('AAPL', '2026-03-20', float('nan'), 'C'),
    ('AAPL', '2026-3-20', 250.0, 'C'),
    ('AAPL', '2026-03-20', 250.0, 'X'),
    ('BRK B', '2026-03-20', 250.0, 'C'),
])
def test_build_refuses_inexact_input(root, expiration, strike, right):
    with pytest.raises(ValueError):
        build_option_symbol(root, expiration, strike, right)


def test_parse_expiration_date_is_strict():
    assert parse_expiration_date('2026-11-20') == dt.date(2026, 11, 20)
    for bad in ('2026-3-20', '20261120', '2026-11-31', 'next friday', '2026-W47-5', ''):
        with pytest.raises(ValueError, match='YYYY-MM-DD'):
            parse_expiration_date(bad)


def test_alpaca_refuses_six_letter_roots():
    with pytest.raises(ValueError, match='alpaca'):
        to_alpaca_option_symbol(OptionSymbol('ABCDEF', dt.date(2026, 11, 20), 'C', 250_000))


def test_provider_symbol_parse_failure_is_a_provider_error():
    with pytest.raises(ProviderError, match="alpaca returned an option symbol MMR cannot parse: 'GARBAGE'"):
        parse_provider_option_symbol('alpaca', 'GARBAGE')


def test_options_data_helpers_delegate():
    from trader.tools.options_data import build_option_ticker, parse_option_ticker
    assert build_option_ticker('F', '2026-03-20', 2.01, 'C') == 'O:F260320C00002010'
    assert parse_option_ticker('AAPL260320C00250000') == {
        'symbol': 'AAPL', 'expiration': '2026-03-20', 'right': 'C', 'strike': 250.0}
    with pytest.raises(ValueError, match='Cannot parse'):
        parse_option_ticker('O:X')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_option_symbols.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.option_symbols'`

- [ ] **Step 3: Implement**

```python
# trader/data_providers/option_symbols.py
"""OCC option symbols in the spellings our providers use. Never guesses.

Massive writes `O:AAPL261120C00250000`, Alpaca `AAPL261120C00250000`: a root
(1-6 letters, plus one digit for adjusted contracts), YYMMDD expiry, C or P,
and the strike in thousandths as 8 digits.
"""

import datetime as dt
import math
import re
from dataclasses import dataclass

from trader.data_providers.errors import ProviderError

_OPTION_SYMBOL = re.compile(
    r'^(?:O:)?(?P<root>[A-Z]{1,6}\d?)(?P<date>\d{6})(?P<right>[CP])(?P<strike>\d{8})$')
_ROOT = re.compile(r'^[A-Z]{1,6}\d?$')
_ALPACA_ROOT = re.compile(r'^[A-Z]{1,5}\d?$')
_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_MAX_STRIKE_THOUSANDTHS = 99_999_999
_EXPECTED_SHAPE = 'expected ROOT + YYMMDD + C|P + 8-digit strike, e.g. AAPL261120C00250000 or O:AAPL261120C00250000'


@dataclass(frozen=True)
class OptionSymbol:
    root: str
    expiration: dt.date
    right: str
    strike_thousandths: int

    @property
    def strike(self) -> float:
        return self.strike_thousandths / 1000

    @property
    def contract_type(self) -> str:
        return 'call' if self.right == 'C' else 'put'

    @property
    def occ(self) -> str:
        return f'{self.root}{self.expiration:%y%m%d}{self.right}{self.strike_thousandths:08d}'


def parse_option_symbol(text: str) -> OptionSymbol:
    if not isinstance(text, str) or not text.isascii():
        raise ValueError(f'Cannot parse option symbol {text!r}: {_EXPECTED_SHAPE}')
    match = _OPTION_SYMBOL.match(text.strip().upper())
    if not match:
        raise ValueError(f'Cannot parse option symbol {text!r}: {_EXPECTED_SHAPE}')
    try:
        expiration = dt.datetime.strptime(match['date'], '%y%m%d').date()
    except ValueError:
        raise ValueError(f"Cannot parse option symbol {text!r}: {match['date']} is not a calendar date") from None
    strike_thousandths = int(match['strike'])
    if strike_thousandths == 0:
        raise ValueError(f'Cannot parse option symbol {text!r}: strike is zero')
    return OptionSymbol(match['root'], expiration, match['right'], strike_thousandths)


def parse_provider_option_symbol(provider: str, text: str) -> OptionSymbol:
    try:
        return parse_option_symbol(text)
    except ValueError as ex:
        raise ProviderError(f'{provider} returned an option symbol MMR cannot parse: {text!r}') from ex


def parse_expiration_date(text: str) -> dt.date:
    if not isinstance(text, str) or not _ISO_DATE.match(text):
        raise ValueError(f'expiration must be a YYYY-MM-DD date, got {text!r}')
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f'expiration must be a real YYYY-MM-DD date, got {text!r}') from None


def build_option_symbol(root: str, expiration, strike: float, right: str) -> OptionSymbol:
    root = root.strip().upper()
    if not _ROOT.match(root):
        raise ValueError(f'not a valid option root: {root!r}')
    right = right.strip().upper()
    if right not in ('C', 'P'):
        raise ValueError(f"option right must be 'C' or 'P', got {right!r}")
    if isinstance(expiration, str):
        expiration = parse_expiration_date(expiration)
    return OptionSymbol(root, expiration, right, _strike_thousandths(strike))


def _strike_thousandths(strike: float) -> int:
    if not math.isfinite(strike) or strike <= 0:
        raise ValueError(f'strike must be a positive number, got {strike!r}')
    thousandths = round(strike * 1000)
    if abs(thousandths - strike * 1000) > 1e-6:
        raise ValueError(f'strike {strike!r} is not a multiple of 0.001')
    if thousandths > _MAX_STRIKE_THOUSANDTHS:
        raise ValueError(f'strike {strike!r} does not fit an OCC symbol')
    return thousandths


def to_alpaca_option_symbol(option: OptionSymbol) -> str:
    if not _ALPACA_ROOT.match(option.root):
        raise ValueError(f'alpaca accepts option roots of at most 5 letters, got {option.root!r}')
    return option.occ


def to_massive_option_ticker(option: OptionSymbol) -> str:
    return f'O:{option.occ}'
```

In `trader/tools/options_data.py`, replace the bodies of `parse_option_ticker` and `build_option_ticker` (keep their names and signatures — the dashboard imports them) and drop the now-unused `from datetime import datetime`:

```python
from trader.data_providers.option_symbols import (
    build_option_symbol, parse_option_symbol, to_massive_option_ticker,
)


def parse_option_ticker(ticker: str) -> dict:
    """Parse ``O:AAPL260320C00250000`` (or the bare body) → {symbol, expiration, right, strike}."""
    option = parse_option_symbol(ticker)
    return {
        "symbol": option.root,
        "expiration": option.expiration.isoformat(),
        "right": option.right,
        "strike": option.strike,
    }


def build_option_ticker(symbol: str, expiration: str, strike: float, right: str) -> str:
    return to_massive_option_ticker(build_option_symbol(symbol, expiration, strike, right))
```

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_option_symbols.py tests/test_options_data.py tests/test_options.py::TestOptionTickerParsing tests/test_massive_research.py -q -p no:cacheprovider`
Expected: all pass (`test_parse_invalid_ticker` still matches "Cannot parse"; the dashboard tests are unaffected).

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers/option_symbols.py trader/tools/options_data.py tests/data_providers/test_option_symbols.py
git commit -m "feat(providers): add strict OCC option symbol parsing

One module parses and builds option symbols in Massive (O:) and Alpaca
(bare) spelling and raises on any other shape. Building a strike now
rounds exactly: int(2.01 * 1000) named the 2.009 contract.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: OPTIONS capability, row shape, default source

**Files:**
- Modify: `trader/data_providers/capabilities.py`, `trader/data_providers/builtin.py` (`BUILTIN_DEFAULTS`), `trader/data_providers/__init__.py`
- Test: `tests/data_providers/test_options_capability.py`

**Interfaces:**
- Consumes: `OptionSymbol` (Task 1); registry from phases 1–3a.
- Produces:
  - `Capability.OPTIONS = 'options'` (appended after `NEWS`; keep any members other lanes add).
  - `OPTION_FIELDS: tuple[str, ...]` — today's 17 chain columns first, then `underlying, quote_time, last_time, provider, feed`.
  - `option_mid(bid: float, ask: float) -> float` — NaN unless `0 <= bid <= ask`, `ask > 0`, both finite.
  - `make_option_row(option: OptionSymbol, **fields) -> dict` — identity (`ticker`, `type`, `strike`, `expiration`) from `option`, `mid` from `option_mid`; passing any of those raises `TypeError`; unknown fields raise `TypeError`.
  - `sort_option_rows(rows: list[dict]) -> list[dict]` — calls first, then strike ascending.
  - `OptionsProvider` protocol: `expirations(underlying: str) -> list[str]`, `chain(underlying: str, expiration: str, contract_type: Optional[str] = None, strike_min: Optional[float] = None, strike_max: Optional[float] = None) -> list[dict]`, `contract(option: OptionSymbol) -> dict`.
  - `BUILTIN_DEFAULTS[Capability.OPTIONS] = 'alpaca'`; `INHERITS_DEFAULT_DATA_SOURCE` unchanged (`{HISTORY}`).

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_options_capability.py
import datetime as dt
import math

import pytest

from trader.data_providers.capabilities import (
    OPTION_FIELDS, Capability, OptionsProvider, make_option_row, option_mid, sort_option_rows,
)
from trader.data_providers.option_symbols import OptionSymbol
from trader.data_providers.registry import ProviderRegistry

NAN = float('nan')
CALL = OptionSymbol('AAPL', dt.date(2026, 11, 20), 'C', 250_000)
PUT = OptionSymbol('AAPL', dt.date(2026, 11, 20), 'P', 220_000)


def test_capability_value():
    assert Capability.OPTIONS.value == 'options'


def test_option_fields_keep_todays_chain_columns_first():
    assert OPTION_FIELDS[:17] == (
        'ticker', 'type', 'strike', 'expiration', 'bid', 'ask', 'mid', 'last', 'volume', 'open_interest',
        'iv', 'delta', 'gamma', 'theta', 'vega', 'break_even', 'underlying_price')
    assert OPTION_FIELDS[17:] == ('underlying', 'quote_time', 'last_time', 'provider', 'feed')


def test_make_option_row_takes_identity_from_the_symbol():
    row = make_option_row(CALL, bid=82.65, ask=87.45, provider='alpaca', feed='indicative')
    assert tuple(row) == OPTION_FIELDS
    assert (row['ticker'], row['type'], row['strike'], row['expiration']) == (
        'AAPL261120C00250000', 'call', 250.0, '2026-11-20')
    assert row['mid'] == pytest.approx(85.05)
    assert math.isnan(row['iv']) and math.isnan(row['delta']) and math.isnan(row['break_even'])
    assert math.isnan(row['volume']) and math.isnan(row['open_interest'])
    assert row['quote_time'] == '' and row['underlying'] == '' and row['feed'] == 'indicative'


def test_make_option_row_rejects_unknown_and_derived_fields():
    with pytest.raises(TypeError, match='unknown option field'):
        make_option_row(CALL, greek=1.0)
    for derived in ('ticker', 'type', 'strike', 'expiration', 'mid'):
        with pytest.raises(TypeError, match='come from the option symbol'):
            make_option_row(CALL, **{derived: 1.0})


@pytest.mark.parametrize('bid, ask, expected', [
    (0.06, 0.17, 0.115),
    (0.0, 0.05, 0.025),
    (NAN, 1.0, NAN),
    (1.0, NAN, NAN),
    (1.2, 1.0, NAN),     # crossed
    (0.0, 0.0, NAN),
    (-0.1, 1.0, NAN),
])
def test_option_mid(bid, ask, expected):
    mid = option_mid(bid, ask)
    assert (math.isnan(mid) and math.isnan(expected)) or mid == pytest.approx(expected)


def test_sort_option_rows_calls_first_then_strike():
    low_call = OptionSymbol('AAPL', dt.date(2026, 11, 20), 'C', 100_000)
    rows = [make_option_row(PUT), make_option_row(CALL), make_option_row(low_call)]
    assert [r['ticker'] for r in sort_option_rows(rows)] == [
        'AAPL261120C00100000', 'AAPL261120C00250000', 'AAPL261120P00220000']


def test_protocol_is_structural():
    class Options:
        def expirations(self, underlying):
            return []

        def chain(self, underlying, expiration, contract_type=None, strike_min=None, strike_max=None):
            return []

        def contract(self, option):
            return {}

    assert isinstance(Options(), OptionsProvider)


def test_options_never_inherit_default_data_source():
    for global_default in ('massive', 'twelvedata', 'alpaca'):
        registry = ProviderRegistry.from_config({'default_data_source': global_default})
        assert registry.default_source(Capability.OPTIONS) == 'alpaca'


def test_data_providers_override_moves_options():
    registry = ProviderRegistry.from_config({'data_providers': {'options': 'massive'}})
    assert registry.default_source(Capability.OPTIONS) == 'massive'
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_options_capability.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'OPTION_FIELDS'`

- [ ] **Step 3: Implement**

`trader/data_providers/capabilities.py`: add `import math` and `from trader.data_providers.option_symbols import OptionSymbol` to the imports; append `OPTIONS = 'options'` as the last member of `Capability`; append at the end of the module:

```python
OPTION_FIELDS: tuple[str, ...] = (
    'ticker', 'type', 'strike', 'expiration', 'bid', 'ask', 'mid', 'last', 'volume', 'open_interest',
    'iv', 'delta', 'gamma', 'theta', 'vega', 'break_even', 'underlying_price',
    'underlying', 'quote_time', 'last_time', 'provider', 'feed',
)
_OPTION_TEXT_FIELDS = frozenset({
    'ticker', 'type', 'expiration', 'underlying', 'quote_time', 'last_time', 'provider', 'feed',
})
_OPTION_DERIVED_FIELDS = frozenset({'ticker', 'type', 'strike', 'expiration', 'mid'})


def option_mid(bid: float, ask: float) -> float:
    """Midpoint of a sane quote; NaN for missing, zero-ask or crossed quotes."""
    if not (math.isfinite(bid) and math.isfinite(ask)) or bid < 0 or ask <= 0 or bid > ask:
        return float('nan')
    return (bid + ask) / 2


def make_option_row(option: OptionSymbol, **fields: Any) -> dict:
    unknown = set(fields) - set(OPTION_FIELDS)
    if unknown:
        raise TypeError(f'unknown option field(s): {sorted(unknown)}')
    derived = set(fields) & _OPTION_DERIVED_FIELDS
    if derived:
        raise TypeError(f'option field(s) {sorted(derived)} come from the option symbol and quote')
    row = {name: ('' if name in _OPTION_TEXT_FIELDS else float('nan')) for name in OPTION_FIELDS}
    row.update(fields)
    row.update(ticker=option.occ, type=option.contract_type, strike=option.strike,
               expiration=option.expiration.isoformat())
    row['mid'] = option_mid(row['bid'], row['ask'])
    return row


def sort_option_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: (row['type'], row['strike']))


@runtime_checkable
class OptionsProvider(Protocol):
    def expirations(self, underlying: str) -> list[str]:
        """Sorted YYYY-MM-DD expirations that have not passed."""
        ...

    def chain(self, underlying: str, expiration: str, contract_type: Optional[str] = None,
              strike_min: Optional[float] = None, strike_max: Optional[float] = None) -> list[dict]:
        """make_option_row() dicts for one expiration, sorted by sort_option_rows()."""
        ...

    def contract(self, option: OptionSymbol) -> dict:
        """One make_option_row() dict for an exact contract; an unknown contract raises."""
        ...
```

`trader/data_providers/builtin.py`: add `Capability.OPTIONS: 'alpaca',` as the last entry of `BUILTIN_DEFAULTS`. Leave `INHERITS_DEFAULT_DATA_SOURCE` as is.

`trader/data_providers/__init__.py`: import and add to `__all__`: `OPTION_FIELDS`, `OptionsProvider`, `make_option_row`, `option_mid`, `sort_option_rows`.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers -q -p no:cacheprovider`
Expected: all pass (`test_every_builtin_default_points_at_a_registered_source` still passes: `alpaca` is registered; `source_choices(Capability.OPTIONS)` is `[]` until Tasks 3–4).

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers tests/data_providers/test_options_capability.py
git commit -m "feat(providers): add the options capability and row shape

Rows take ticker, type, strike and expiration from the parsed OCC symbol,
fill missing numbers with NaN and carry provider + feed labels. Options
default to alpaca and do not inherit default_data_source.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Massive options adapter

**Files:**
- Create: `trader/data_providers/massive/options.py`
- Modify: `trader/data_providers/builtin.py` (add `_massive_options`; register `Capability.OPTIONS` on the `massive` spec)
- Test: `tests/data_providers/test_massive_options.py` (takes over the coverage of `tests/test_options.py::TestOptionsChain` / `TestOptionsSnapshot`, which Task 7 removes)

**Interfaces:**
- Consumes: `make_option_row`, `sort_option_rows` (Task 2); `OptionSymbol`, `parse_provider_option_symbol` (Task 1); `ProviderEntitlementError`, `ProviderError`; `massive.exceptions.BadResponse`.
- Produces: `MassiveOptions(client)` implementing `OptionsProvider` (`FEED = 'opra'`, `CONTRACTS_PAGE_LIMIT = 1000`); `builtin._massive_options(config)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_massive_options.py
import math
from types import SimpleNamespace as NS

import pytest
from massive.exceptions import BadResponse

from trader.data_providers.capabilities import OPTION_FIELDS, Capability, OptionsProvider
from trader.data_providers.errors import ProviderEntitlementError, ProviderError
from trader.data_providers.massive.options import MassiveOptions
from trader.data_providers.option_symbols import parse_option_symbol
from trader.data_providers.registry import ProviderRegistry

NOT_AUTHORIZED = BadResponse('{"status":"NOT_AUTHORIZED","request_id":"x",'
                             '"message":"You are not entitled to this data. Please upgrade your plan"}')


def _snap(strike, contract_type='call', iv=0.3, bid=2.0, ask=2.5, delta=0.5, ticker=None):
    right = 'C' if contract_type == 'call' else 'P'
    return NS(
        details=NS(ticker=ticker or f'O:AAPL260320{right}{int(strike * 1000):08d}', contract_type=contract_type,
                   strike_price=strike, expiration_date='2026-03-20'),
        last_quote=NS(bid=bid, ask=ask), last_trade=NS(price=(bid + ask) / 2), day=NS(volume=500.0),
        greeks=NS(delta=delta, gamma=0.02, theta=-0.05, vega=0.15),
        open_interest=1000.0, implied_volatility=iv, break_even_price=strike + bid,
        underlying_asset=NS(price=150.0),
    )


class FakeClient:
    def __init__(self, snaps=(), contracts=(), snapshot=None, error=None):
        self.snaps, self.contracts, self.snapshot, self.error = list(snaps), list(contracts), snapshot, error
        self.calls = []

    def _maybe_fail(self):
        if self.error:
            raise self.error

    def list_snapshot_options_chain(self, underlying_asset, params):
        self.calls.append(('chain', underlying_asset, params))
        self._maybe_fail()
        return iter(self.snaps)

    def list_options_contracts(self, **kwargs):
        self.calls.append(('contracts', kwargs))
        self._maybe_fail()
        return iter(self.contracts)

    def get_snapshot_option(self, underlying_asset, option_contract):
        self.calls.append(('snapshot', underlying_asset, option_contract))
        self._maybe_fail()
        return self.snapshot


def test_chain_rows_have_shared_shape_and_percent_iv():
    client = FakeClient([_snap(250.0, delta=0.5), _snap(245.0, 'put', delta=-0.4), _snap(245.0, delta=0.6)])
    rows = MassiveOptions(client).chain('aapl', '2026-03-20')
    assert client.calls == [('chain', 'AAPL', {'expiration_date': '2026-03-20'})]
    assert [r['ticker'] for r in rows] == ['AAPL260320C00245000', 'AAPL260320C00250000', 'AAPL260320P00245000']
    first = rows[0]
    assert tuple(first) == OPTION_FIELDS
    assert first['iv'] == pytest.approx(30.0)
    assert (first['bid'], first['ask'], first['mid'], first['last']) == (2.0, 2.5, 2.25, 2.25)
    assert (first['volume'], first['open_interest'], first['break_even']) == (500.0, 1000.0, 247.0)
    assert first['delta'] == 0.6 and first['underlying_price'] == 150.0 and first['underlying'] == 'AAPL'
    assert first['provider'] == 'massive' and first['feed'] == 'opra'
    assert first['quote_time'] == '' and first['last_time'] == ''


def test_chain_filters_by_type():
    client = FakeClient([_snap(245.0), _snap(250.0), _snap(245.0, 'put')])
    rows = MassiveOptions(client).chain('AAPL', '2026-03-20', contract_type='call')
    assert [r['type'] for r in rows] == ['call', 'call']


def test_chain_filters_by_strike_range():
    client = FakeClient([_snap(240.0), _snap(250.0), _snap(260.0)])
    rows = MassiveOptions(client).chain('AAPL', '2026-03-20', strike_min=245.0, strike_max=255.0)
    assert [r['strike'] for r in rows] == [250.0]


def test_missing_fields_are_nan_not_zero():
    bare = NS(details=NS(ticker='O:AAPL260320P00250000', contract_type='put', strike_price=250.0,
                         expiration_date='2026-03-20'),
              last_quote=None, last_trade=None, day=None, greeks=None, open_interest=None,
              implied_volatility=None, break_even_price=None, underlying_asset=None)
    (row,) = MassiveOptions(FakeClient([bare])).chain('AAPL', '2026-03-20')
    for column in ('bid', 'ask', 'mid', 'last', 'volume', 'open_interest', 'iv', 'delta', 'gamma',
                   'theta', 'vega', 'break_even', 'underlying_price'):
        assert math.isnan(row[column]), column


def test_rows_without_details_are_skipped():
    no_details = NS(details=None)
    assert MassiveOptions(FakeClient([no_details, _snap(250.0)])).chain('AAPL', '2026-03-20')[0]['strike'] == 250.0


def test_unparseable_provider_ticker_raises():
    with pytest.raises(ProviderError, match='massive returned an option symbol'):
        MassiveOptions(FakeClient([_snap(250.0, ticker='O:WEIRD')])).chain('AAPL', '2026-03-20')


def test_contract_snapshot():
    snapshot = NS(break_even_price=253.0, implied_volatility=0.35, open_interest=5000,
                  last_quote=NS(bid=3.0, ask=3.5), last_trade=NS(price=3.25),
                  greeks=NS(delta=0.45, gamma=0.02, theta=-0.08, vega=0.20),
                  underlying_asset=NS(price=248.0, ticker='AAPL'), day=NS(volume=1200))
    client = FakeClient(snapshot=snapshot)
    row = MassiveOptions(client).contract(parse_option_symbol('O:AAPL260320C00250000'))
    assert client.calls == [('snapshot', 'AAPL', 'AAPL260320C00250000')]
    assert (row['ticker'], row['type'], row['strike'], row['expiration']) == (
        'AAPL260320C00250000', 'call', 250.0, '2026-03-20')
    assert row['iv'] == pytest.approx(35.0) and row['delta'] == 0.45
    assert row['underlying'] == 'AAPL' and row['underlying_price'] == 248.0 and row['mid'] == 3.25


def test_expirations_unique_sorted():
    contracts = [NS(expiration_date='2026-11-20'), NS(expiration_date='2026-10-16'),
                 NS(expiration_date='2026-11-20'), NS(expiration_date=None)]
    client = FakeClient(contracts=contracts)
    assert MassiveOptions(client).expirations('aapl') == ['2026-10-16', '2026-11-20']
    assert client.calls == [('contracts', dict(underlying_ticker='AAPL', expired=False, limit=1000,
                                               sort='expiration_date', order='asc'))]


@pytest.mark.parametrize('call', [
    lambda options: options.chain('AAPL', '2026-03-20'),
    lambda options: options.expirations('AAPL'),
    lambda options: options.contract(parse_option_symbol('O:AAPL260320C00250000')),
])
def test_not_authorized_maps_to_entitlement_error(call):
    with pytest.raises(ProviderEntitlementError) as raised:
        call(MassiveOptions(FakeClient(error=NOT_AUTHORIZED)))
    assert 'NOT_AUTHORIZED' in str(raised.value) and '--source alpaca' in str(raised.value)


def test_other_bad_response_is_a_plain_provider_error():
    with pytest.raises(ProviderError) as raised:
        MassiveOptions(FakeClient(error=BadResponse('{"status":"ERROR","message":"boom"}'))).chain('AAPL', '2026-03-20')
    assert not isinstance(raised.value, ProviderEntitlementError)


def test_registry_builds_massive_options():
    from trader.data_providers.builtin import source_choices
    provider = ProviderRegistry.from_config({'massive_api_key': 'k'}).get(Capability.OPTIONS, 'massive')
    assert isinstance(provider, MassiveOptions) and isinstance(provider, OptionsProvider)
    assert 'massive' in source_choices(Capability.OPTIONS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_massive_options.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.massive.options'`

- [ ] **Step 3: Implement**

```python
# trader/data_providers/massive/options.py
"""Option expirations, chains and contract snapshots from Massive (Polygon): OPRA data, paid plan."""

from typing import Callable, Optional

from massive.exceptions import BadResponse

from trader.data_providers.capabilities import make_option_row, sort_option_rows
from trader.data_providers.errors import ProviderEntitlementError, ProviderError
from trader.data_providers.option_symbols import OptionSymbol, parse_provider_option_symbol

FEED = 'opra'
CONTRACTS_PAGE_LIMIT = 1000


class MassiveOptions:
    def __init__(self, client):
        self._client = client

    def expirations(self, underlying: str) -> list[str]:
        contracts = _call_massive('expirations', lambda: list(self._client.list_options_contracts(
            underlying_ticker=underlying.strip().upper(), expired=False, limit=CONTRACTS_PAGE_LIMIT,
            sort='expiration_date', order='asc')))
        return sorted({contract.expiration_date for contract in contracts if contract.expiration_date})

    def chain(self, underlying: str, expiration: str, contract_type: Optional[str] = None,
              strike_min: Optional[float] = None, strike_max: Optional[float] = None) -> list[dict]:
        symbol = underlying.strip().upper()
        snaps = _call_massive('chain', lambda: list(self._client.list_snapshot_options_chain(
            underlying_asset=symbol, params={'expiration_date': expiration})))
        rows = []
        for snap in snaps:
            if not snap.details or not snap.details.ticker:
                continue
            option = parse_provider_option_symbol('massive', snap.details.ticker)
            if contract_type and option.contract_type != contract_type:
                continue
            if strike_min is not None and option.strike < strike_min:
                continue
            if strike_max is not None and option.strike > strike_max:
                continue
            rows.append(_option_row(option, snap, symbol))
        return sort_option_rows(rows)

    def contract(self, option: OptionSymbol) -> dict:
        snap = _call_massive('snapshot', lambda: self._client.get_snapshot_option(
            underlying_asset=option.root, option_contract=option.occ))
        underlying = getattr(snap.underlying_asset, 'ticker', None) or ''
        return _option_row(option, snap, underlying)


def _option_row(option: OptionSymbol, snap, underlying: str) -> dict:
    greeks = snap.greeks
    return make_option_row(
        option,
        bid=_number(getattr(snap.last_quote, 'bid', None)),
        ask=_number(getattr(snap.last_quote, 'ask', None)),
        last=_number(getattr(snap.last_trade, 'price', None)),
        volume=_number(getattr(snap.day, 'volume', None)),
        open_interest=_number(snap.open_interest),
        iv=_number(snap.implied_volatility) * 100,
        delta=_number(getattr(greeks, 'delta', None)),
        gamma=_number(getattr(greeks, 'gamma', None)),
        theta=_number(getattr(greeks, 'theta', None)),
        vega=_number(getattr(greeks, 'vega', None)),
        break_even=_number(snap.break_even_price),
        underlying=underlying,
        underlying_price=_number(getattr(snap.underlying_asset, 'price', None)),
        provider='massive',
        feed=FEED,
    )


def _number(value) -> float:
    return float('nan') if value is None else float(value)


def _call_massive(what: str, request: Callable):
    try:
        return request()
    except BadResponse as ex:
        if 'NOT_AUTHORIZED' in str(ex):
            raise ProviderEntitlementError(
                f'massive refused options {what} (NOT_AUTHORIZED: this Massive plan has no options data); '
                'use --source alpaca') from ex
        raise ProviderError(f'massive options {what} failed: {ex}') from ex
```

`getattr(None, 'bid', None)` returns `None`, so a missing quote/trade/day/greeks object yields NaN without extra branches. `_number(None) * 100` stays NaN.

`trader/data_providers/builtin.py`:

```python
def _massive_options(config: Mapping[str, Any]):
    from trader.data_providers.massive.options import MassiveOptions
    return MassiveOptions(_massive_rest_client(config))
```

and add `Capability.OPTIONS: _massive_options` to the `massive` spec's builders.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_massive_options.py tests/data_providers/test_builtin.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers tests/data_providers/test_massive_options.py
git commit -m "feat(providers): wrap Massive options as an options provider

Rows use the shared shape with NaN for missing data (the old Massive path
filled 0.0) and feed='opra'. NOT_AUTHORIZED becomes an entitlement error
that names --source alpaca.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Alpaca options — expirations and chain (indicative feed)

**Files:**
- Create: `trader/data_providers/alpaca/options.py`, `tests/data_providers/fixtures/alpaca_option_snapshots_aapl.json`, `tests/data_providers/fixtures/alpaca_option_contracts_aapl.json`
- Modify: `trader/data_providers/builtin.py` (add `_alpaca_trading_client`, `_alpaca_options`; register `Capability.OPTIONS` on the `alpaca` spec)
- Test: `tests/data_providers/test_alpaca_options.py`

**Interfaces:**
- Consumes: `AlpacaClient.get_json` / `.paginate` (phase 2), `AlpacaQuotes` (phase 3a, IEX stock snapshots), `number_or_nan`, `to_alpaca_symbol`, `make_option_row`, `sort_option_rows`, `parse_provider_option_symbol`, `ALPACA_PAPER_TRADING_URL` (`trader/data_providers/alpaca/assets.py`).
- Produces: `AlpacaOptions(data_client, trading_client, today: Callable[[], dt.date] = _today_et)` with `expirations(underlying)` and `chain(underlying, expiration, contract_type=None, strike_min=None, strike_max=None)` (`contract` arrives in Task 5); module constants `CONTRACTS_PATH = '/v2/options/contracts'`, `CHAIN_SNAPSHOTS_PATH = '/v1beta1/options/snapshots/{underlying}'`, `FEED = 'indicative'`, `CONTRACTS_PAGE_LIMIT = 5000`, `SNAPSHOTS_PAGE_LIMIT = 1000`; private helpers `_option_row`, `_session_volume`, `_et_date`, `_chain_filters`; `builtin._alpaca_trading_client(config)`, `builtin._alpaca_options(config)`.

Verified on 2026-10-04 (read-only probes, Basic plan, paper keys):
- `GET paper-api…/v2/options/contracts?underlying_symbols=AAPL&limit=10000` **without** an expiration filter returned only 344 contracts over 3 expirations (next week). With `expiration_date_gte=2026-10-05` it returned 3,532 contracts over 25 expirations (out to 2029-01-19). `limit=10001` → 422 "limit must be less than 10000"; `limit=5000` → 200. Pagination: `next_page_token` / `page_token` (same as `AlpacaClient.paginate`). `underlying_symbols=ZZZZQ` → 422 `invalid underlying symbols: ZZZZQ`. `underlying_symbols=BRK.B` → 200, contract symbols `BRKB…`. Filters `expiration_date`, `type`, `strike_price_gte/lte` (also as `240.0`) work. `open_interest` is a string or null (2,672 of 3,532 non-null).
- `GET data…/v1beta1/options/snapshots/AAPL?feed=indicative&expiration_date=2026-11-20` → 168 contracts, same set as the contracts list; 113 had `greeks` + `impliedVolatility`; key sets seen: full (93), no greeks (51), greeks + quote only (20), quote only (3). `limit` max 1000 (1001 → 400). `snapshots/ZZZZQ` → 200 `{"snapshots": {}}` (silent). `feed=opra` → 403 "OPRA agreement is not signed".
- Option snapshots carry no underlying price and no open interest.

- [ ] **Step 1: Save the fixtures** (real responses from the probes above, trimmed to five contracts; contract objects trimmed to the fields MMR reads)

`tests/data_providers/fixtures/alpaca_option_snapshots_aapl.json`:

```json
{
  "next_page_token": null,
  "snapshots": {
    "AAPL261120C00100000": {
      "dailyBar": {"c": 236.7, "h": 236.7, "l": 236.7, "n": 1, "o": 236.7, "t": "2026-09-30T04:00:00Z", "v": 7, "vw": 236.7},
      "latestQuote": {"ap": 239.02, "as": 126, "ax": "A", "bp": 230.21, "bs": 126, "bx": "E", "c": " ", "t": "2026-10-02T19:59:59.59954399Z"},
      "latestTrade": {"c": "f", "p": 235.7, "s": 7, "t": "2026-09-30T18:10:55.027401075Z", "x": "I"},
      "minuteBar": {"c": 236.7, "h": 236.7, "l": 236.7, "n": 1, "o": 236.7, "t": "2026-09-30T18:10:00Z", "v": 7, "vw": 236.7},
      "prevDailyBar": {"c": 237.25, "h": 237.25, "l": 237.25, "n": 1, "o": 237.25, "t": "2026-09-17T04:00:00Z", "v": 5, "vw": 237.25}
    },
    "AAPL261120C00250000": {
      "dailyBar": {"c": 83.7, "h": 85.75, "l": 83.7, "n": 2, "o": 85.75, "t": "2026-10-02T04:00:00Z", "v": 9, "vw": 84.838889},
      "greeks": {"delta": 0.9879, "gamma": 0.0007, "rho": 0.3149, "theta": -0.0411, "vega": 0.0375},
      "impliedVolatility": 0.3738,
      "latestQuote": {"ap": 87.45, "as": 334, "ax": "X", "bp": 82.65, "bs": 352, "bx": "J", "c": " ", "t": "2026-10-02T19:59:59.374582282Z"},
      "latestTrade": {"c": "f", "p": 83.13, "s": 4, "t": "2026-10-02T15:39:26.489876952Z", "x": "I"},
      "minuteBar": {"c": 83.7, "h": 83.7, "l": 83.7, "n": 1, "o": 83.7, "t": "2026-10-02T15:39:00Z", "v": 4, "vw": 83.7},
      "prevDailyBar": {"c": 81.63, "h": 82.1, "l": 81.63, "n": 3, "o": 82.1, "t": "2026-10-01T04:00:00Z", "v": 3, "vw": 81.943333}
    },
    "AAPL261120P00220000": {
      "dailyBar": {"c": 0.08, "h": 0.09, "l": 0.08, "n": 2, "o": 0.09, "t": "2026-09-30T04:00:00Z", "v": 2, "vw": 0.085},
      "greeks": {"delta": -0.0059, "gamma": 0.0003, "rho": -0.0027, "theta": -0.01, "vega": 0.02},
      "impliedVolatility": 0.4827,
      "latestQuote": {"ap": 0.17, "as": 145, "ax": "X", "bp": 0.06, "bs": 1, "bx": "X", "c": " ", "t": "2026-10-02T19:59:59.991240522Z"},
      "latestTrade": {"c": "g", "p": 0.08, "s": 1, "t": "2026-09-30T19:31:55.56137389Z", "x": "C"},
      "minuteBar": {"c": 0.08, "h": 0.08, "l": 0.08, "n": 1, "o": 0.08, "t": "2026-09-30T19:31:00Z", "v": 1, "vw": 0.08},
      "prevDailyBar": {"c": 0.1, "h": 0.15, "l": 0.1, "n": 2, "o": 0.15, "t": "2026-09-29T04:00:00Z", "v": 260, "vw": 0.101923}
    },
    "AAPL261120P00395000": {
      "greeks": {"delta": -0.9052, "gamma": 0.0042, "rho": -0.4675, "theta": -0.0327, "vega": 0.2021},
      "impliedVolatility": 0.3331,
      "latestQuote": {"ap": 62.38, "as": 127, "ax": "J", "bp": 59.83, "bs": 121, "bx": "H", "c": " ", "t": "2026-10-02T19:59:59.48343313Z"}
    },
    "AAPL261120P00580000": {
      "latestQuote": {"ap": 251.46, "as": 128, "ax": "J", "bp": 248.36, "bs": 127, "bx": "J", "c": "A", "t": "2026-10-02T19:59:58.884105223Z"}
    }
  }
}
```

`tests/data_providers/fixtures/alpaca_option_contracts_aapl.json`:

```json
{
  "next_page_token": null,
  "option_contracts": [
    {"symbol": "AAPL261120C00100000", "expiration_date": "2026-11-20", "root_symbol": "AAPL", "underlying_symbol": "AAPL", "type": "call", "strike_price": "100", "open_interest": "6", "open_interest_date": "2026-10-01", "close_price": "236.7"},
    {"symbol": "AAPL261120C00250000", "expiration_date": "2026-11-20", "root_symbol": "AAPL", "underlying_symbol": "AAPL", "type": "call", "strike_price": "250", "open_interest": "464", "open_interest_date": "2026-10-01", "close_price": "83.7"},
    {"symbol": "AAPL261120P00220000", "expiration_date": "2026-11-20", "root_symbol": "AAPL", "underlying_symbol": "AAPL", "type": "put", "strike_price": "220", "open_interest": "3803", "open_interest_date": "2026-10-01", "close_price": "0.08"},
    {"symbol": "AAPL261120P00395000", "expiration_date": "2026-11-20", "root_symbol": "AAPL", "underlying_symbol": "AAPL", "type": "put", "strike_price": "395", "open_interest": null, "open_interest_date": null, "close_price": null},
    {"symbol": "AAPL261120P00580000", "expiration_date": "2026-11-20", "root_symbol": "AAPL", "underlying_symbol": "AAPL", "type": "put", "strike_price": "580", "open_interest": null, "open_interest_date": null, "close_price": null}
  ]
}
```

- [ ] **Step 2: Write the failing test**

```python
# tests/data_providers/test_alpaca_options.py
import datetime as dt
import json
import math
from pathlib import Path

import pytest

from trader.data_providers.alpaca.options import AlpacaOptions
from trader.data_providers.capabilities import OPTION_FIELDS, Capability
from trader.data_providers.errors import ProviderError
from trader.data_providers.registry import ProviderRegistry

FIXTURES = Path(__file__).parent / 'fixtures'
SNAPSHOTS = json.loads((FIXTURES / 'alpaca_option_snapshots_aapl.json').read_text())
CONTRACTS = json.loads((FIXTURES / 'alpaca_option_contracts_aapl.json').read_text())
STOCK = json.loads((FIXTURES / 'alpaca_snapshots_aapl.json').read_text())   # phase 3a: AAPL last 333.75
CHAIN_PATH = '/v1beta1/options/snapshots/AAPL'
TODAY = dt.date(2026, 10, 4)


class FakeClient:
    """Routes by path: a list of pages for paginate(), a dict for get_json(), or an exception to raise."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _route(self, path, params):
        self.calls.append((path, dict(params)))
        response = self.routes[path]
        if isinstance(response, Exception):
            raise response
        return response

    def get_json(self, path, params):
        return self._route(path, params)

    def paginate(self, path, params):
        yield from self._route(path, params)


def _options(snapshot_pages=None, contracts=None, stock=None):
    data = FakeClient({CHAIN_PATH: snapshot_pages or [SNAPSHOTS],
                       '/v2/stocks/snapshots': STOCK if stock is None else stock})
    trading = FakeClient({'/v2/options/contracts': contracts or [CONTRACTS]})
    return AlpacaOptions(data, trading, today=lambda: TODAY), data, trading


def _rows(**kwargs):
    options, _, _ = _options(**kwargs)
    return {row['ticker']: row for row in options.chain('AAPL', '2026-11-20')}


def test_expirations_from_contract_list_across_pages():
    page1 = {'option_contracts': [{'symbol': 'AAPL261120C00250000', 'expiration_date': '2026-11-20'},
                                  {'symbol': 'AAPL261016C00250000', 'expiration_date': '2026-10-16'}],
             'next_page_token': 'Mw=='}
    page2 = {'option_contracts': [{'symbol': 'AAPL261120P00250000', 'expiration_date': '2026-11-20'},
                                  {'symbol': 'AAPL270115C00250000', 'expiration_date': '2027-01-15'}],
             'next_page_token': None}
    options, data, trading = _options(contracts=[page1, page2])
    assert options.expirations('aapl') == ['2026-10-16', '2026-11-20', '2027-01-15']
    assert trading.calls == [('/v2/options/contracts', {
        'underlying_symbols': 'AAPL', 'status': 'active', 'expiration_date_gte': '2026-10-04', 'limit': 5000})]
    assert data.calls == []


def test_expirations_map_class_shares():
    options, _, trading = _options(contracts=[{'option_contracts': [], 'next_page_token': None}])
    assert options.expirations('BRK B') == []
    assert trading.calls[0][1]['underlying_symbols'] == 'BRK.B'


def test_invalid_underlying_raises_before_any_request():
    options, data, trading = _options()
    with pytest.raises(ValueError, match='not a valid Alpaca stock symbol'):
        options.expirations('AAPL;DROP')
    assert trading.calls == [] and data.calls == []


def test_chain_maps_indicative_snapshots():
    row = _rows()['AAPL261120C00250000']
    assert tuple(row) == OPTION_FIELDS
    assert (row['type'], row['strike'], row['expiration']) == ('call', 250.0, '2026-11-20')
    assert (row['bid'], row['ask'], row['last']) == (82.65, 87.45, 83.13)
    assert row['mid'] == pytest.approx(85.05)
    assert row['volume'] == 9.0 and row['open_interest'] == 464.0
    assert row['iv'] == pytest.approx(37.38)
    assert (row['delta'], row['gamma'], row['theta'], row['vega']) == (0.9879, 0.0007, -0.0411, 0.0375)
    assert row['underlying'] == 'AAPL' and row['underlying_price'] == 333.75
    assert row['quote_time'] == '2026-10-02T19:59:59.374582282Z'
    assert row['last_time'] == '2026-10-02T15:39:26.489876952Z'
    assert math.isnan(row['break_even'])


def test_missing_greeks_iv_trade_and_bar_are_nan():
    rows = _rows()
    no_greeks = rows['AAPL261120C00100000']
    assert math.isnan(no_greeks['iv']) and math.isnan(no_greeks['delta']) and math.isnan(no_greeks['vega'])
    assert no_greeks['bid'] == 230.21 and no_greeks['open_interest'] == 6.0
    greeks_only = rows['AAPL261120P00395000']
    assert greeks_only['iv'] == pytest.approx(33.31) and greeks_only['delta'] == -0.9052
    assert math.isnan(greeks_only['last']) and math.isnan(greeks_only['volume'])
    assert math.isnan(greeks_only['open_interest']) and greeks_only['last_time'] == ''
    quote_only = rows['AAPL261120P00580000']
    assert quote_only['bid'] == 248.36 and quote_only['ask'] == 251.46
    for column in ('last', 'volume', 'open_interest', 'iv', 'delta', 'gamma', 'theta', 'vega', 'break_even'):
        assert math.isnan(quote_only[column]), column


def test_session_volume_rule():
    rows = _rows()
    assert rows['AAPL261120C00250000']['volume'] == 9.0    # daily bar from the quote's session (2026-10-02)
    assert rows['AAPL261120P00220000']['volume'] == 0.0    # last daily bar 2026-09-30: no trades since
    assert math.isnan(rows['AAPL261120P00395000']['volume'])  # no daily bar at all


def test_every_row_is_labelled_indicative():
    rows = _rows().values()
    assert len(rows) == 5
    assert all(row['provider'] == 'alpaca' and row['feed'] == 'indicative' for row in rows)


def test_rows_sorted_calls_then_strike():
    options, _, _ = _options()
    assert [r['ticker'] for r in options.chain('AAPL', '2026-11-20')] == [
        'AAPL261120C00100000', 'AAPL261120C00250000',
        'AAPL261120P00220000', 'AAPL261120P00395000', 'AAPL261120P00580000']


def test_chain_sends_filters_to_both_endpoints():
    options, data, trading = _options()
    options.chain('AAPL', '2026-11-20', contract_type='call', strike_min=240, strike_max=260.5)
    filters = {'expiration_date': '2026-11-20', 'type': 'call',
               'strike_price_gte': '240.0', 'strike_price_lte': '260.5'}
    assert trading.calls == [('/v2/options/contracts', {**filters, 'underlying_symbols': 'AAPL', 'limit': 5000})]
    assert data.calls == [
        ('/v2/stocks/snapshots', {'symbols': 'AAPL', 'feed': 'iex'}),
        (CHAIN_PATH, {**filters, 'feed': 'indicative', 'limit': 1000}),
    ]


def test_unknown_underlying_fails_before_snapshots():
    refused = ProviderError('alpaca /v2/options/contracts failed: HTTP 422 invalid underlying symbols: ZZZZQ')
    data = FakeClient({})
    trading = FakeClient({'/v2/options/contracts': refused})
    with pytest.raises(ProviderError, match='invalid underlying symbols: ZZZZQ'):
        AlpacaOptions(data, trading, today=lambda: TODAY).chain('ZZZZQ', '2026-11-20')
    assert data.calls == []


def test_underlying_price_nan_when_iex_has_no_trade():
    rows = _rows(stock={})
    assert len(rows) == 5 and all(math.isnan(row['underlying_price']) for row in rows.values())


def test_unexpected_contract_key_raises():
    page = {'snapshots': {'GARBAGE': {'latestQuote': {}}}, 'next_page_token': None}
    options, _, _ = _options(snapshot_pages=[page])
    with pytest.raises(ProviderError, match="alpaca returned an option symbol MMR cannot parse: 'GARBAGE'"):
        options.chain('AAPL', '2026-11-20')


def test_registry_builds_alpaca_options_with_paper_trading_client():
    from trader.data_providers.alpaca.assets import ALPACA_PAPER_TRADING_URL
    from trader.data_providers.alpaca.client import ALPACA_DATA_URL
    from trader.data_providers.builtin import source_choices
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'})
    provider = registry.get(Capability.OPTIONS)
    assert isinstance(provider, AlpacaOptions)
    assert provider._data._base_url == ALPACA_DATA_URL
    assert provider._trading._base_url == ALPACA_PAPER_TRADING_URL
    assert source_choices(Capability.OPTIONS) == ['alpaca', 'massive']
```

- [ ] **Step 3: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_alpaca_options.py -q -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.alpaca.options'`

- [ ] **Step 4: Implement**

```python
# trader/data_providers/alpaca/options.py
"""Option expirations and chains from Alpaca's free indicative feed.

The indicative feed is not the OPRA NBBO: its quotes are derived and its trades
delayed, and greeks / implied volatility exist only for liquid contracts. Every
row says feed='indicative'. Contract lists come from the paper trading API.
"""

import datetime as dt
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from trader.data_providers.alpaca._numbers import number_or_nan
from trader.data_providers.alpaca.quotes import AlpacaQuotes
from trader.data_providers.capabilities import make_option_row, sort_option_rows
from trader.data_providers.option_symbols import OptionSymbol, parse_provider_option_symbol
from trader.data_providers.symbols import to_alpaca_symbol

CONTRACTS_PATH = '/v2/options/contracts'
CHAIN_SNAPSHOTS_PATH = '/v1beta1/options/snapshots/{underlying}'
FEED = 'indicative'
# Verified 2026-10-04: contracts need limit < 10000; snapshots allow at most 1000 per page.
CONTRACTS_PAGE_LIMIT = 5000
SNAPSHOTS_PAGE_LIMIT = 1000
_ET = ZoneInfo('America/New_York')


def _today_et() -> dt.date:
    return dt.datetime.now(_ET).date()


class AlpacaOptions:
    def __init__(self, data_client, trading_client, today: Callable[[], dt.date] = _today_et):
        self._data = data_client
        self._trading = trading_client
        self._today = today

    def expirations(self, underlying: str) -> list[str]:
        # Without an expiration filter Alpaca returns only the next week's contracts.
        params = {'underlying_symbols': to_alpaca_symbol(underlying), 'status': 'active',
                  'expiration_date_gte': self._today().isoformat(), 'limit': CONTRACTS_PAGE_LIMIT}
        return sorted({contract['expiration_date']
                       for page in self._trading.paginate(CONTRACTS_PATH, params)
                       for contract in page.get('option_contracts') or []})

    def chain(self, underlying: str, expiration: str, contract_type: Optional[str] = None,
              strike_min: Optional[float] = None, strike_max: Optional[float] = None) -> list[dict]:
        symbol = to_alpaca_symbol(underlying)
        filters = _chain_filters(expiration, contract_type, strike_min, strike_max)
        # Contracts first: that endpoint rejects an unknown underlying (HTTP 422),
        # the snapshot endpoint just returns nothing.
        open_interest = self._open_interest(symbol, filters)
        underlying_price = self._underlying_price(symbol)
        snapshot_params = {**filters, 'feed': FEED, 'limit': SNAPSHOTS_PAGE_LIMIT}
        rows = []
        for page in self._data.paginate(CHAIN_SNAPSHOTS_PATH.format(underlying=symbol), snapshot_params):
            for key, snapshot in (page.get('snapshots') or {}).items():
                option = parse_provider_option_symbol('alpaca', key)
                rows.append(_option_row(option, snapshot, symbol, underlying_price,
                                        open_interest.get(key, float('nan'))))
        return sort_option_rows(rows)

    def _open_interest(self, symbol: str, filters: dict) -> dict[str, float]:
        params = {**filters, 'underlying_symbols': symbol, 'limit': CONTRACTS_PAGE_LIMIT}
        return {contract['symbol']: number_or_nan(contract, 'open_interest')
                for page in self._trading.paginate(CONTRACTS_PATH, params)
                for contract in page.get('option_contracts') or []}

    def _underlying_price(self, symbol: str) -> float:
        (quote,) = AlpacaQuotes(self._data).quotes([symbol])
        return quote['last']


def _chain_filters(expiration: str, contract_type: Optional[str],
                   strike_min: Optional[float], strike_max: Optional[float]) -> dict:
    filters = {'expiration_date': expiration}
    if contract_type:
        filters['type'] = contract_type
    if strike_min is not None:
        filters['strike_price_gte'] = str(float(strike_min))
    if strike_max is not None:
        filters['strike_price_lte'] = str(float(strike_max))
    return filters


def _option_row(option: OptionSymbol, snapshot: dict, underlying: str,
                underlying_price: float, open_interest: float) -> dict:
    quote = snapshot.get('latestQuote') or {}
    trade = snapshot.get('latestTrade') or {}
    greeks = snapshot.get('greeks') or {}
    return make_option_row(
        option,
        bid=number_or_nan(quote, 'bp'),
        ask=number_or_nan(quote, 'ap'),
        last=number_or_nan(trade, 'p'),
        volume=_session_volume(snapshot.get('dailyBar'), quote),
        open_interest=open_interest,
        iv=number_or_nan(snapshot, 'impliedVolatility') * 100,
        delta=number_or_nan(greeks, 'delta'),
        gamma=number_or_nan(greeks, 'gamma'),
        theta=number_or_nan(greeks, 'theta'),
        vega=number_or_nan(greeks, 'vega'),
        underlying=underlying,
        underlying_price=underlying_price,
        quote_time=quote.get('t', ''),
        last_time=trade.get('t', ''),
        provider='alpaca',
        feed=FEED,
    )


def _session_volume(daily_bar: Optional[dict], quote: dict) -> float:
    """Volume in the latest quote's session; a daily bar from an older session means no trades since."""
    if not daily_bar or not quote.get('t'):
        return float('nan')
    if _et_date(daily_bar['t']) == _et_date(quote['t']):
        return number_or_nan(daily_bar, 'v')
    return 0.0


def _et_date(timestamp: str) -> dt.date:
    return dt.datetime.fromisoformat(timestamp).astimezone(_ET).date()
```

`trader/data_providers/builtin.py`:

```python
def _alpaca_trading_client(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.assets import ALPACA_PAPER_TRADING_URL
    from trader.data_providers.alpaca.client import AlpacaClient
    return AlpacaClient(config['alpaca_api_key_id'], config['alpaca_api_secret_key'],
                        base_url=ALPACA_PAPER_TRADING_URL)


def _alpaca_options(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.options import AlpacaOptions
    return AlpacaOptions(_alpaca_client(config), _alpaca_trading_client(config))
```

and add `Capability.OPTIONS: _alpaca_options` to the `alpaca` spec's builders. Do not refactor `alpaca_asset_directory` (lane 3b may touch it).

- [ ] **Step 5: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_alpaca_options.py tests/data_providers/test_builtin.py tests/data_providers/test_alpaca_quotes.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add trader/data_providers tests/data_providers/test_alpaca_options.py tests/data_providers/fixtures/alpaca_option_*.json
git commit -m "feat(providers): add Alpaca option expirations and chains

Indicative feed (free; not OPRA NBBO). Missing greeks, IV, trades and bars
stay NaN and every row is labelled indicative. Open interest comes from
the contracts list, which also makes unknown underlyings fail loudly;
the underlying price is one IEX stock snapshot.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Alpaca single-contract snapshot

**Files:**
- Modify: `trader/data_providers/alpaca/options.py` (add `SNAPSHOTS_PATH`, `AlpacaOptions.contract`)
- Test: `tests/data_providers/test_alpaca_options.py` (append)

**Interfaces:**
- Consumes: Task 4 (`AlpacaOptions`, `_option_row`, `CONTRACTS_PATH`, `FEED`), `to_alpaca_option_symbol` (Task 1).
- Produces: `AlpacaOptions.contract(option: OptionSymbol) -> dict`; `SNAPSHOTS_PATH = '/v1beta1/options/snapshots'`. After this task `AlpacaOptions` satisfies `OptionsProvider`.

Verified on 2026-10-04: `GET paper-api…/v2/options/contracts/AAPL261120C00250000` → 200 with `underlying_symbol: "AAPL"`, `open_interest: "464"`; an unknown contract → 404 `option contract AAPL261120C00251234 not found`. `GET data…/v1beta1/options/snapshots?symbols=A,B,C&feed=indicative` → 200 and **silently omits** unknown contracts; an `O:`-prefixed symbol → 400 (regex `^[A-Z]{1,5}\d{6,7}[CP]\d{8}$`). BRKB options have `underlying_symbol: "BRK.B"` — the root is not the underlying.

- [ ] **Step 1: Write the failing test** (append to `tests/data_providers/test_alpaca_options.py`; move the two new imports up to the file's import block)

```python
from trader.data_providers.capabilities import OptionsProvider
from trader.data_providers.option_symbols import parse_option_symbol

CONTRACT_DETAILS = {   # trimmed real response, 2026-10-04
    'symbol': 'AAPL261120C00250000', 'expiration_date': '2026-11-20', 'root_symbol': 'AAPL',
    'underlying_symbol': 'AAPL', 'type': 'call', 'strike_price': '250', 'open_interest': '464',
    'open_interest_date': '2026-10-01', 'close_price': '83.7',
}
CONTRACT_PATH = '/v2/options/contracts/AAPL261120C00250000'


def _contract_options(details=CONTRACT_DETAILS, snapshots=None, contract_path=CONTRACT_PATH, stock=None):
    if snapshots is None:
        snapshots = {'AAPL261120C00250000': SNAPSHOTS['snapshots']['AAPL261120C00250000']}
    data = FakeClient({'/v1beta1/options/snapshots': {'snapshots': snapshots, 'next_page_token': None},
                       '/v2/stocks/snapshots': STOCK if stock is None else stock})
    trading = FakeClient({contract_path: details})
    return AlpacaOptions(data, trading, today=lambda: TODAY), data, trading


def test_contract_joins_details_snapshot_and_underlying():
    options, data, trading = _contract_options()
    row = options.contract(parse_option_symbol('O:AAPL261120C00250000'))
    assert trading.calls == [(CONTRACT_PATH, {})]
    assert ('/v1beta1/options/snapshots', {'symbols': 'AAPL261120C00250000', 'feed': 'indicative'}) in data.calls
    assert row['ticker'] == 'AAPL261120C00250000' and row['open_interest'] == 464.0
    assert (row['bid'], row['ask'], row['volume']) == (82.65, 87.45, 9.0)
    assert row['underlying'] == 'AAPL' and row['underlying_price'] == 333.75
    assert row['provider'] == 'alpaca' and row['feed'] == 'indicative'


def test_contract_uses_underlying_symbol_from_details():
    details = dict(CONTRACT_DETAILS, symbol='BRKB261009C00270000', root_symbol='BRKB',
                   underlying_symbol='BRK.B', open_interest=None)
    options, data, _ = _contract_options(details=details, snapshots={},
                                         contract_path='/v2/options/contracts/BRKB261009C00270000',
                                         stock={'BRK.B': STOCK['AAPL']})
    row = options.contract(parse_option_symbol('BRKB261009C00270000'))
    assert ('/v2/stocks/snapshots', {'symbols': 'BRK.B', 'feed': 'iex'}) in data.calls
    assert row['underlying'] == 'BRK.B' and math.isnan(row['open_interest'])


def test_unknown_contract_is_loud():
    missing = ProviderError('alpaca /v2/options/contracts/AAPL261120C00251234 failed: HTTP 404 '
                            'option contract AAPL261120C00251234 not found')
    options, data, _ = _contract_options(details=missing, contract_path='/v2/options/contracts/AAPL261120C00251234')
    with pytest.raises(ProviderError, match='not found'):
        options.contract(parse_option_symbol('AAPL261120C00251234'))
    assert data.calls == []


def test_contract_without_snapshot_is_a_nan_row():
    options, _, _ = _contract_options(snapshots={})
    row = options.contract(parse_option_symbol('AAPL261120C00250000'))
    assert math.isnan(row['bid']) and math.isnan(row['mid']) and math.isnan(row['iv'])
    assert row['quote_time'] == '' and row['open_interest'] == 464.0 and row['feed'] == 'indicative'


def test_details_without_underlying_raise():
    details = {key: value for key, value in CONTRACT_DETAILS.items() if key != 'underlying_symbol'}
    options, _, _ = _contract_options(details=details)
    with pytest.raises(ProviderError, match='no underlying_symbol'):
        options.contract(parse_option_symbol('AAPL261120C00250000'))


def test_alpaca_options_satisfies_protocol():
    options, _, _ = _contract_options()
    assert isinstance(options, OptionsProvider)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_alpaca_options.py -q -p no:cacheprovider`
Expected: FAIL with `AttributeError: 'AlpacaOptions' object has no attribute 'contract'`

- [ ] **Step 3: Implement**

In `trader/data_providers/alpaca/options.py`: add `SNAPSHOTS_PATH = '/v1beta1/options/snapshots'` next to the other path constants; extend the imports with `from trader.data_providers.errors import ProviderError` and `to_alpaca_option_symbol` from `option_symbols`; add the method to `AlpacaOptions` (after `chain`):

```python
    def contract(self, option: OptionSymbol) -> dict:
        alpaca_symbol = to_alpaca_option_symbol(option)
        # Contract details name the real underlying (BRKB options deliver BRK.B) and carry
        # open interest; an unknown contract is HTTP 404 here.
        details = self._trading.get_json(f'{CONTRACTS_PATH}/{alpaca_symbol}', {})
        underlying = details.get('underlying_symbol')
        if not underlying:
            raise ProviderError(f'alpaca contract {alpaca_symbol} has no underlying_symbol')
        page = self._data.get_json(SNAPSHOTS_PATH, {'symbols': alpaca_symbol, 'feed': FEED})
        snapshot = (page.get('snapshots') or {}).get(alpaca_symbol) or {}
        return _option_row(option, snapshot, underlying, self._underlying_price(underlying),
                           number_or_nan(details, 'open_interest'))
```

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_alpaca_options.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/data_providers/alpaca/options.py tests/data_providers/test_alpaca_options.py
git commit -m "feat(providers): add Alpaca single-contract option snapshots

The contract details give the real underlying (BRKB -> BRK.B) and open
interest; an unknown contract fails with Alpaca's 404 instead of an
empty result.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Implied-distribution inputs from capability rows

**Files:**
- Modify: `trader/tools/chain.py`
- Test: `tests/test_options_implied.py`

**Interfaces:**
- Consumes: `make_option_row`, `build_option_symbol` (tests), `MassiveOptions` (Task 3); `implied_constant_helper` (unchanged).
- Produces:
  - `MIN_IMPLIED_STRIKES = 8`, `DAYS_PER_YEAR = 365.0`.
  - `implied_inputs(rows: Sequence[Mapping], expiration: str, today: dt.date) -> tuple[pd.DataFrame, list[Mapping], int]` — frame with columns `IV` (fraction), `K`, `S`, `T` sorted by `K`; the usable call rows; the number of call rows excluded. Raises `ValueError` for a non-future expiration, fewer than `MIN_IMPLIED_STRIKES` usable calls, or no underlying price.
  - `implied_distribution(rows: Sequence[Mapping], expiration: str, risk_free_rate: float, today: dt.date) -> dict` — keys `x`, `market_implied`, `constant` (plain lists of float), `strikes_used`, `strikes_excluded`, `provider`, `feed`.
  - Kept, same signatures (the dashboard and its tests use them): `get_option_dates(symbol, api_key='')` and `implied_constant(symbol, date, risk_free_rate=0.001, api_key='')` — now backed by `MassiveOptions`. Removed (no callers): `get_chains`, `get_call_chain`, `get_put_chain`.

Today `get_chains` writes `snap.implied_volatility or 0.0`, so every contract without IV enters the degree-5 fit as IV 0, and `T` is calendar days / 255.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_implied.py
import datetime as dt
import math
from types import SimpleNamespace as NS

import numpy as np
import pytest

from trader.data_providers.capabilities import make_option_row
from trader.data_providers.option_symbols import build_option_symbol
from trader.tools import chain
from trader.tools.chain import MIN_IMPLIED_STRIKES, implied_distribution, implied_inputs

NAN = float('nan')
TODAY = dt.date(2026, 10, 4)
EXPIRATION = '2026-11-20'   # 47 calendar days after TODAY


def _row(strike, iv, right='C', underlying_price=333.75, provider='alpaca', feed='indicative'):
    return make_option_row(build_option_symbol('AAPL', EXPIRATION, strike, right), iv=iv,
                           underlying_price=underlying_price, provider=provider, feed=feed)


def _smile(strikes=range(250, 420, 10)):
    return [_row(strike, 40.0 - (strike - 330) * 0.05) for strike in strikes]   # 17 calls


def test_inputs_use_fraction_iv_calendar_year_and_spot():
    frame, usable, excluded = implied_inputs(_smile(), EXPIRATION, TODAY)
    assert len(frame) == 17 and len(usable) == 17 and excluded == 0
    assert list(frame.columns) == ['IV', 'K', 'S', 'T']
    assert frame['T'].tolist() == pytest.approx([47 / 365] * 17)
    assert (frame['S'] == 333.75).all()
    assert frame['IV'].iloc[0] == pytest.approx(0.44)
    assert frame['K'].is_monotonic_increasing


def test_nan_and_zero_iv_rows_are_excluded_and_counted():
    rows = _smile() + [_row(255, NAN), _row(265, 0.0), _row(275, -1.0), _row(300, 30.0, right='P')]
    frame, usable, excluded = implied_inputs(rows, EXPIRATION, TODAY)
    assert len(frame) == 17 and excluded == 3      # the put is not a call, so not "excluded"
    assert frame['IV'].gt(0).all() and frame['IV'].notna().all()


def test_nan_rows_do_not_change_the_distribution():
    clean = implied_distribution(_smile(), EXPIRATION, 0.05, TODAY)
    noisy = implied_distribution(_smile() + [_row(255, NAN), _row(265, 0.0)], EXPIRATION, 0.05, TODAY)
    assert noisy['x'] == clean['x']
    assert np.allclose(noisy['market_implied'], clean['market_implied'])
    assert (noisy['strikes_used'], noisy['strikes_excluded']) == (17, 2)


def test_too_few_usable_strikes_raises():
    rows = _smile(range(250, 320, 10)) + [_row(strike, NAN) for strike in range(320, 420, 10)]
    with pytest.raises(ValueError, match=f'only 7 of 17 call strikes have an implied volatility; need at least {MIN_IMPLIED_STRIKES}'):
        implied_inputs(rows, EXPIRATION, TODAY)


def test_same_day_expiration_raises():
    with pytest.raises(ValueError, match='is not after'):
        implied_inputs(_smile(), EXPIRATION, dt.date(2026, 11, 20))


def test_missing_underlying_price_raises():
    rows = [_row(strike, 40.0, underlying_price=NAN) for strike in range(250, 420, 10)]
    with pytest.raises(ValueError, match='underlying price'):
        implied_inputs(rows, EXPIRATION, TODAY)


def test_distribution_result_labels_and_plain_lists():
    result = implied_distribution(_smile(), EXPIRATION, 0.05, TODAY)
    assert set(result) == {'x', 'market_implied', 'constant', 'strikes_used', 'strikes_excluded', 'provider', 'feed'}
    assert all(type(result[key]) is list for key in ('x', 'market_implied', 'constant'))
    assert all(type(value) is float for value in result['x'][:3])
    assert len(result['market_implied']) == len(result['x']) - 1 == len(result['constant'])
    assert (result['provider'], result['feed']) == ('alpaca', 'indicative')


def test_get_option_dates_routes_through_massive_adapter(monkeypatch):
    contracts = [NS(expiration_date='2026-11-20'), NS(expiration_date='2026-10-16')]
    monkeypatch.setattr(chain, '_get_massive_client',
                        lambda api_key='': NS(list_options_contracts=lambda **kwargs: iter(contracts)))
    assert chain.get_option_dates('AAPL', api_key='k') == ['2026-10-16', '2026-11-20']


def test_implied_constant_dashboard_path_excludes_missing_iv(monkeypatch):
    expiration = (dt.date.today() + dt.timedelta(days=40)).isoformat()

    def snap(strike, iv):
        option = build_option_symbol('AAPL', expiration, strike, 'C')
        return NS(details=NS(ticker=f'O:{option.occ}'), last_quote=None, last_trade=None, day=None, greeks=None,
                  open_interest=None, implied_volatility=iv, break_even_price=None,
                  underlying_asset=NS(price=333.75))

    snaps = [snap(strike, 0.40 - (strike - 330) * 0.0005) for strike in range(250, 350, 10)]
    snaps += [snap(355, None), snap(365, None), snap(375, 0.0)]
    monkeypatch.setattr(chain, '_get_massive_client',
                        lambda api_key='': NS(list_snapshot_options_chain=lambda **kwargs: iter(snaps)))
    result = chain.implied_constant('AAPL', expiration, 0.05, api_key='k')
    assert (result['strikes_used'], result['strikes_excluded']) == (10, 3)
    assert (result['provider'], result['feed']) == ('massive', 'opra')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/test_options_implied.py -q -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'MIN_IMPLIED_STRIKES' from 'trader.tools.chain'`

- [ ] **Step 3: Implement** in `trader/tools/chain.py`

Replace the module docstring:

```python
"""Options chain analysis — probability distributions from market-implied volatility.

Chain data comes from an OPTIONS provider as make_option_row() dicts. The
binary-option pricing math (d2, binary_call, binary_put, monte_carlo_binary,
implied_constant_helper) is data-source agnostic.
"""
```

Add `import math` and change the typing import to `from typing import Any, Dict, List, Mapping, Sequence, Tuple`.

Delete `get_option_dates`, `get_chains`, `get_call_chain`, `get_put_chain` and `implied_constant`, and put this block where `implied_constant` was (between `plot_market_implied_vs_constant_console` and `plot_chain`; keep `_get_massive_client`, `implied_constant_helper`, `plot_market_implied_vs_constant_console`, `plot_chain` and `__main__` as they are):

```python
MIN_IMPLIED_STRIKES = 8   # the degree-5 vol-smile fit needs more points than coefficients
DAYS_PER_YEAR = 365.0     # provider IVs are annualised on calendar days


def get_option_dates(symbol: str, api_key: str = '') -> List[str]:
    """Expiration dates via Massive (dashboard path; the CLI/SDK use the OPTIONS capability)."""
    from trader.data_providers.massive.options import MassiveOptions
    logging.info('getting option dates for symbol %s', symbol)
    return MassiveOptions(_get_massive_client(api_key)).expirations(symbol)


def implied_inputs(
    rows: Sequence[Mapping], expiration: str, today: dt.date,
) -> Tuple[pd.DataFrame, List[Mapping], int]:
    """The IV/K/S/T frame implied_constant_helper fits, built from calls with a usable IV.

    Calls without an implied volatility (NaN, or zero/negative) are left out and counted:
    fitting them as 0 would bend the smile.
    """
    days = (dt.date.fromisoformat(expiration) - today).days
    if days <= 0:
        raise ValueError(f'expiration {expiration} is not after {today}; '
                         'the implied distribution needs time to expiry')
    calls = [row for row in rows if row['type'] == 'call']
    usable = sorted((row for row in calls if _is_positive(row['iv'])), key=lambda row: row['strike'])
    if len(usable) < MIN_IMPLIED_STRIKES:
        raise ValueError(
            f'only {len(usable)} of {len(calls)} call strikes have an implied volatility; '
            f'need at least {MIN_IMPLIED_STRIKES} (illiquid expiration, or the indicative feed has no '
            'greeks for it — try a nearer expiration or --source massive)')
    spot = next((row['underlying_price'] for row in usable if _is_positive(row['underlying_price'])), None)
    if spot is None:
        raise ValueError('the chain has no underlying price; cannot centre the implied distribution')
    frame = pd.DataFrame({
        'IV': [row['iv'] / 100 for row in usable],
        'K': [row['strike'] for row in usable],
        'S': spot,
        'T': days / DAYS_PER_YEAR,
    })
    return frame, usable, len(calls) - len(usable)


def implied_distribution(
    rows: Sequence[Mapping], expiration: str, risk_free_rate: float, today: dt.date,
) -> Dict[str, Any]:
    inputs, usable, excluded = implied_inputs(rows, expiration, today)
    result = implied_constant_helper(inputs, risk_free_rate)
    return {
        'x': [float(value) for value in result['x']],
        'market_implied': [float(value) for value in result['market_implied']],
        'constant': [float(value) for value in result['constant']],
        'strikes_used': len(usable),
        'strikes_excluded': excluded,
        'provider': usable[0]['provider'],
        'feed': usable[0]['feed'],
    }


def _is_positive(value) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


def implied_constant(symbol: str, date: str, risk_free_rate: float = 0.001,
                     api_key: str = '') -> Dict[str, Any]:
    """Implied distribution via Massive (dashboard path; the CLI/SDK use the OPTIONS capability)."""
    from trader.data_providers.massive.options import MassiveOptions
    rows = MassiveOptions(_get_massive_client(api_key)).chain(symbol, date, contract_type='call')
    return implied_distribution(rows, date, risk_free_rate, dt.date.today())
```

`plot_chain` keeps calling `get_option_dates` and `implied_constant` (unchanged); `plot_market_implied_vs_constant_console` slices `x[1:]`, which works on a list.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/test_options_implied.py tests/test_options.py::TestChainMath tests/test_massive_research.py -q -p no:cacheprovider`
Expected: all pass (`test_massive_research.py` monkeypatches `get_option_dates` / `implied_constant` by name, which still exist).

- [ ] **Step 5: Commit**

```bash
git add trader/tools/chain.py tests/test_options_implied.py
git commit -m "fix(options): build implied distributions only from real IVs

Calls without an implied volatility were fitted as IV 0, which bends the
smile. They are now excluded and counted; fewer than 8 usable strikes or
a non-future expiration raise. Time to expiry is calendar days / 365
(was / 255, which made T ~43% too long). Results are plain lists so
--json no longer prints the strike axis as a string.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: SDK options through the registry

**Files:**
- Modify: `trader/sdk.py` — replace the four methods in the "Options — Data" section (`options_expirations` ≈L3243, `options_chain`, `options_snapshot`, `options_implied` ≈L3349) and add `MMR._check_chain_filters` above them; leave `_parse_massive_option_ticker`, `_build_massive_option_ticker`, `_resolve_option_contract`, `buy_option`, `sell_option` unchanged.
- Modify: `tests/test_options.py` — delete classes `TestOptionsChain` and `TestOptionsSnapshot` (they patch `massive.RESTClient` and break once the SDK stops constructing it; their assertions now live in `tests/data_providers/test_massive_options.py` (Task 3) and the SDK tests below).
- Test: `tests/data_providers/test_sdk_options.py`

**Interfaces:**
- Consumes: `MMR._provider(capability, source)` (phase 3a), `Capability.OPTIONS`, `OPTION_FIELDS`, `parse_option_symbol`, `parse_expiration_date`, `implied_distribution` (Task 6).
- Produces (all `source: Optional[str] = None`; `None` → `data_providers.options`, else `alpaca`):
  - `MMR.options_expirations(symbol, source=None) -> List[str]`
  - `MMR.options_chain(symbol, expiration=None, contract_type=None, strike_min=None, strike_max=None, source=None) -> pd.DataFrame` (columns exactly `OPTION_FIELDS`; empty frame with those columns when there are no expirations)
  - `MMR.options_snapshot(option_ticker, source=None) -> dict` (accepts `O:` and bare spellings)
  - `MMR.options_implied(symbol, expiration, risk_free_rate=0.05, source=None) -> Dict` (keys of `implied_distribution`)
  - `MMR._check_chain_filters(contract_type, strike_min, strike_max) -> None` (static; `ValueError` on bad input)

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_sdk_options.py
import datetime as dt
from unittest.mock import MagicMock

import pytest

from trader.data_providers.capabilities import OPTION_FIELDS, Capability, make_option_row
from trader.data_providers.errors import ProviderNotConfigured
from trader.data_providers.option_symbols import build_option_symbol, parse_option_symbol

EXPIRATION = (dt.date.today() + dt.timedelta(days=40)).isoformat()


def _rows():
    return [make_option_row(build_option_symbol('AAPL', EXPIRATION, strike, 'C'),
                            iv=40.0 - (strike - 330) * 0.05, underlying_price=333.75,
                            provider='alpaca', feed='indicative')
            for strike in range(250, 420, 10)]


def _mmr(provider):
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    mmr._provider = MagicMock(return_value=provider)
    return mmr


def test_expirations_ask_the_default_options_provider():
    provider = MagicMock()
    provider.expirations.return_value = ['2026-10-16', '2026-11-20']
    mmr = _mmr(provider)
    assert mmr.options_expirations('AAPL') == ['2026-10-16', '2026-11-20']
    mmr._provider.assert_called_once_with(Capability.OPTIONS, None)


def test_source_is_passed_through():
    provider = MagicMock()
    provider.expirations.return_value = []
    mmr = _mmr(provider)
    mmr.options_expirations('AAPL', source='massive')
    mmr._provider.assert_called_once_with(Capability.OPTIONS, 'massive')


def test_chain_uses_nearest_expiration_and_shared_columns():
    provider = MagicMock()
    provider.expirations.return_value = [EXPIRATION, '2099-01-15']
    provider.chain.return_value = _rows()
    frame = _mmr(provider).options_chain('AAPL', contract_type='call', strike_min=250, strike_max=410)
    provider.chain.assert_called_once_with('AAPL', EXPIRATION, 'call', 250, 410)
    assert list(frame.columns) == list(OPTION_FIELDS) and len(frame) == 17


def test_chain_without_expirations_is_an_empty_frame_with_columns():
    provider = MagicMock()
    provider.expirations.return_value = []
    frame = _mmr(provider).options_chain('AAPL')
    assert frame.empty and list(frame.columns) == list(OPTION_FIELDS)
    provider.chain.assert_not_called()


@pytest.mark.parametrize('kwargs, message', [
    ({'expiration': 'next friday'}, 'YYYY-MM-DD'),
    ({'expiration': '2026-3-20'}, 'YYYY-MM-DD'),
    ({'contract_type': 'straddle'}, "'call' or 'put'"),
    ({'strike_min': 300.0, 'strike_max': 200.0}, 'above strike_max'),
])
def test_chain_rejects_bad_input_before_any_request(kwargs, message):
    provider = MagicMock()
    with pytest.raises(ValueError, match=message):
        _mmr(provider).options_chain('AAPL', **kwargs)
    provider.chain.assert_not_called()


def test_snapshot_accepts_both_forms():
    provider = MagicMock()
    provider.contract.return_value = {'ticker': 'AAPL261120C00250000'}
    mmr = _mmr(provider)
    mmr.options_snapshot('O:AAPL261120C00250000')
    mmr.options_snapshot('aapl261120c00250000')
    expected = parse_option_symbol('AAPL261120C00250000')
    assert [call.args[0] for call in provider.contract.call_args_list] == [expected, expected]


def test_snapshot_rejects_garbage_before_any_request():
    provider = MagicMock()
    with pytest.raises(ValueError, match='Cannot parse option symbol'):
        _mmr(provider).options_snapshot('O:AAPL')
    provider.contract.assert_not_called()


def test_implied_uses_call_rows_and_labels_the_result():
    provider = MagicMock()
    provider.chain.return_value = _rows()
    result = _mmr(provider).options_implied('AAPL', EXPIRATION, 0.04)
    provider.chain.assert_called_once_with('AAPL', EXPIRATION, 'call')
    assert result['strikes_used'] == 17 and result['strikes_excluded'] == 0
    assert (result['provider'], result['feed']) == ('alpaca', 'indicative')


def test_missing_alpaca_keys_name_the_env_vars():
    from trader.sdk import MMR
    mmr = object.__new__(MMR)
    mmr._container = MagicMock()
    mmr._container.config.return_value = {'massive_api_key': 'k'}
    with pytest.raises(ProviderNotConfigured, match='ALPACA_API_KEY_ID'):
        mmr.options_expirations('AAPL')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_sdk_options.py -q -p no:cacheprovider`
Expected: FAIL (e.g. `TypeError: MMR.options_expirations() got an unexpected keyword argument 'source'` and the default-path tests failing because the SDK still builds a Massive client).

- [ ] **Step 3: Implement** — replace the "Options — Data" section of `trader/sdk.py` (from the `# Options — Data (Massive API, …)` banner through the end of `options_implied`) with:

```python
    # ------------------------------------------------------------------
    # Options — Data (OPTIONS capability; default alpaca indicative feed, no trader_service needed)
    # ------------------------------------------------------------------

    @staticmethod
    def _check_chain_filters(contract_type: Optional[str], strike_min: Optional[float],
                             strike_max: Optional[float]) -> None:
        if contract_type not in (None, 'call', 'put'):
            raise ValueError(f"contract_type must be 'call' or 'put', got {contract_type!r}")
        if strike_min is not None and strike_max is not None and strike_min > strike_max:
            raise ValueError(f'strike_min {strike_min} is above strike_max {strike_max}')

    def options_expirations(self, symbol: str, source: Optional[str] = None) -> List[str]:
        """Sorted YYYY-MM-DD expirations that have not passed.

        `source=None` uses `data_providers.options`, else alpaca; options never inherit
        `default_data_source`.
        """
        from trader.data_providers import Capability
        return self._provider(Capability.OPTIONS, source).expirations(symbol)

    def options_chain(
        self,
        symbol: str,
        expiration: Optional[str] = None,
        contract_type: Optional[str] = None,
        strike_min: Optional[float] = None,
        strike_max: Optional[float] = None,
        source: Optional[str] = None,
    ) -> pd.DataFrame:
        """One expiration's chain (nearest when `expiration` is None), columns OPTION_FIELDS.

        Rows carry `provider` and `feed`; numbers the provider did not send are NaN.
        """
        from trader.data_providers import OPTION_FIELDS, Capability
        from trader.data_providers.option_symbols import parse_expiration_date
        self._check_chain_filters(contract_type, strike_min, strike_max)
        if expiration:
            expiration = parse_expiration_date(expiration).isoformat()
        provider = self._provider(Capability.OPTIONS, source)
        if not expiration:
            dates = provider.expirations(symbol)
            if not dates:
                return pd.DataFrame(columns=list(OPTION_FIELDS))
            expiration = dates[0]
        rows = provider.chain(symbol, expiration, contract_type, strike_min, strike_max)
        return pd.DataFrame(rows, columns=list(OPTION_FIELDS))

    def options_snapshot(self, option_ticker: str, source: Optional[str] = None) -> dict:
        """One contract; accepts `O:AAPL261120C00250000` or `AAPL261120C00250000`."""
        from trader.data_providers import Capability
        from trader.data_providers.option_symbols import parse_option_symbol
        option = parse_option_symbol(option_ticker)
        return self._provider(Capability.OPTIONS, source).contract(option)

    def options_implied(
        self,
        symbol: str,
        expiration: str,
        risk_free_rate: float = 0.05,
        source: Optional[str] = None,
    ) -> Dict:
        """Market-implied vs constant-vol distribution from the expiration's calls.

        Calls without an implied volatility are excluded and counted
        (`strikes_used`, `strikes_excluded`); too few usable strikes raise ValueError.
        """
        from trader.data_providers import Capability
        from trader.data_providers.option_symbols import parse_expiration_date
        from trader.tools.chain import implied_distribution
        expiration = parse_expiration_date(expiration).isoformat()
        rows = self._provider(Capability.OPTIONS, source).chain(symbol, expiration, 'call')
        return implied_distribution(rows, expiration, risk_free_rate, dt.date.today())
```

Then delete `class TestOptionsChain` and `class TestOptionsSnapshot` (with their section comments) from `tests/test_options.py`. Keep everything else in that file.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_sdk_options.py tests/test_options.py tests/test_options_implied.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add trader/sdk.py tests/test_options.py tests/data_providers/test_sdk_options.py
git commit -m "feat(sdk): route options data through the provider registry

options_expirations/chain/snapshot/implied take source= and default to
Alpaca's indicative feed. Chains use the shared row shape; snapshots
accept O: and bare OCC symbols; bad dates, filters and symbols raise
before any request.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: CLI — `--source`, strict expirations, labels, NaN rendering

**Files:**
- Modify: `trader/mmr_cli.py` — options parsers (≈L1021–1078), `_resolve_expiration` (≈L9851), `_handle_options` (≈L10274) replaced by a dispatcher plus one small function per action.
- Test: `tests/data_providers/test_cli_options.py`

**Interfaces:**
- Consumes: Task 7 SDK methods, `Capability.OPTIONS`, `source_choices`, `ProviderError`, `parse_expiration_date`, `print_df` / `print_dict` / `print_json_result` / `print_status` / `_print_trade_result`, `plot_market_implied_vs_constant_console`.
- Produces:
  - `--source {alpaca,massive}` (default `None`) on `options expirations|chain|snapshot|implied|buy|sell`.
  - `_resolve_expiration(mmr, symbol, expiration_arg, default_days=90, source=None) -> str | None` — exact dates are validated with `parse_expiration_date` and returned without any lookup; garbage raises `ValueError` mentioning `YYYY-MM-DD or relative`.
  - `_format_option_number(value, spec: str, suffix: str = '') -> str` (`'—'` for None/NaN), `_option_feed_label(provider: str, feed: str) -> str`, `_without_nan(mapping: dict) -> dict`, `_note_resolved_expiration(resolved: str, requested) -> None`.
  - `_handle_options(mmr, args)` dispatching to `_options_expirations`, `_options_chain`, `_options_snapshot`, `_options_implied`, `_options_order`; catches `ProviderError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_cli_options.py
import argparse
import datetime as dt
import json
import math
from unittest.mock import MagicMock

import pandas as pd
import pytest

from trader import mmr_cli
from trader.data_providers.capabilities import OPTION_FIELDS, make_option_row
from trader.data_providers.errors import ProviderEntitlementError, ProviderNotConfigured
from trader.data_providers.option_symbols import build_option_symbol

NO_KEYS = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID'),
                                           ('alpaca_api_secret_key', 'ALPACA_API_SECRET_KEY')])


def _row(strike=250.0, **fields):
    defaults = dict(bid=82.65, ask=87.45, provider='alpaca', feed='indicative')
    return make_option_row(build_option_symbol('AAPL', '2026-11-20', strike, 'C'), **{**defaults, **fields})


def _json_out(capsys):
    return json.loads(capsys.readouterr().out.strip())


@pytest.fixture
def json_mode(monkeypatch):
    monkeypatch.setattr(mmr_cli, '_json_mode', True)


@pytest.mark.parametrize('argv', [
    ['options', 'expirations', 'AAPL'],
    ['options', 'chain', 'AAPL'],
    ['options', 'snapshot', 'AAPL261120C00250000'],
    ['options', 'implied', 'AAPL'],
    ['options', 'buy', 'AAPL', '-e', '3m', '-s', '250', '-r', 'C', '-q', '1', '--market'],
    ['options', 'sell', 'AAPL', '-e', '3m', '-s', '250', '-r', 'C', '-q', '1', '--market'],
])
def test_every_options_action_takes_source(argv):
    parser = mmr_cli.build_parser()
    assert parser.parse_args(argv).source is None
    assert parser.parse_args(argv + ['--source', 'massive']).source == 'massive'
    with pytest.raises(SystemExit):
        parser.parse_args(argv + ['--source', 'twelvedata'])


@pytest.mark.parametrize('expiration', ['foobar', '2026-3-20', '2026-11-31'])
def test_resolve_rejects_garbage_without_lookup(expiration):
    mmr = MagicMock()
    with pytest.raises(ValueError, match='YYYY-MM-DD or relative'):
        mmr_cli._resolve_expiration(mmr, 'AAPL', expiration)
    mmr.options_expirations.assert_not_called()


def test_resolve_relative_passes_source():
    mmr = MagicMock()
    mmr.options_expirations.return_value = [(dt.date.today() + dt.timedelta(days=88)).isoformat()]
    assert mmr_cli._resolve_expiration(mmr, 'AAPL', '3m', source='massive') is not None
    mmr.options_expirations.assert_called_once_with('AAPL', source='massive')


def test_format_option_number_and_feed_label():
    assert mmr_cli._format_option_number(float('nan'), '.1f', '%') == '—'
    assert mmr_cli._format_option_number(None, '.2f') == '—'
    assert mmr_cli._format_option_number(37.384, '.1f', '%') == '37.4%'
    assert 'not OPRA NBBO' in mmr_cli._option_feed_label('alpaca', 'indicative')
    assert mmr_cli._option_feed_label('massive', 'opra') == 'massive, OPRA feed'


def test_chain_table_renders_nan_as_dash(monkeypatch, capsys):
    monkeypatch.setattr(mmr_cli, '_json_mode', False)
    monkeypatch.setattr(mmr_cli.console, 'width', 250)
    mmr = MagicMock()
    mmr.options_chain.return_value = pd.DataFrame([_row()], columns=list(OPTION_FIELDS))
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='chain', symbol='aapl', expiration=None, contract_type=None,
        strike_min=None, strike_max=None, source=None))
    out = capsys.readouterr().out
    assert '—' in out and 'nan' not in out.lower()
    assert 'indicative' in out and 'not OPRA NBBO' in out


def test_chain_json_has_labels_and_nulls(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_chain.return_value = pd.DataFrame([_row()], columns=list(OPTION_FIELDS))
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='chain', symbol='AAPL', expiration=None, contract_type=None,
        strike_min=None, strike_max=None, source='alpaca'))
    payload = _json_out(capsys)
    assert payload['data'][0]['feed'] == 'indicative' and payload['data'][0]['iv'] is None
    assert 'not OPRA NBBO' in payload['title']
    mmr.options_chain.assert_called_once_with('AAPL', expiration=None, contract_type=None,
                                              strike_min=None, strike_max=None, source='alpaca')


def test_snapshot_json_is_valid_without_nan_tokens(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_snapshot.return_value = _row()
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='snapshot', ticker='O:AAPL261120C00250000',
                                                    source=None))
    out = capsys.readouterr().out
    assert 'NaN' not in out
    payload = json.loads(out)
    assert payload['data']['iv'] is None and payload['data']['ticker'] == 'AAPL261120C00250000'


def test_provider_error_is_printed_not_raised(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_expirations.side_effect = ProviderEntitlementError('massive refused options expirations')
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='expirations', symbol='AAPL', source='massive'))
    payload = _json_out(capsys)
    assert payload['success'] is False and 'massive refused options expirations' in payload['message']


def test_buy_relative_expiry_without_keys_is_loud(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_expirations.side_effect = NO_KEYS
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='buy', symbol='AAPL', market=True, limit=None, expiration='3m',
        strike=250.0, right='C', quantity=1.0, source=None))
    payload = _json_out(capsys)
    assert payload['success'] is False and 'ALPACA_API_KEY_ID' in payload['message']
    mmr.buy_option.assert_not_called()


def test_buy_garbage_expiry_never_reaches_ib():
    mmr = MagicMock()
    with pytest.raises(ValueError, match='YYYY-MM-DD or relative'):
        mmr_cli._handle_options(mmr, argparse.Namespace(
            opt_action='buy', symbol='AAPL', market=True, limit=None, expiration='2026-3-20',
            strike=250.0, right='C', quantity=1.0, source=None))
    mmr.buy_option.assert_not_called()
    mmr.options_expirations.assert_not_called()


def test_sell_exact_date_places_order_without_data_provider(json_mode, capsys):
    mmr = MagicMock()
    mmr_cli._handle_options(mmr, argparse.Namespace(
        opt_action='sell', symbol='aapl', market=False, limit=3.5, expiration='2026-11-20',
        strike=250.0, right='P', quantity=2.0, source=None))
    mmr.sell_option.assert_called_once_with('AAPL', '2026-11-20', 250.0, 'P', 2.0, limit_price=3.5, market=False)
    mmr.options_expirations.assert_not_called()


def test_implied_json_carries_counts_and_labels(json_mode, capsys):
    mmr = MagicMock()
    mmr.options_implied.return_value = {'x': [1.0, 1.1], 'market_implied': [0.1], 'constant': [0.1],
                                        'strikes_used': 9, 'strikes_excluded': 4,
                                        'provider': 'alpaca', 'feed': 'indicative'}
    mmr_cli._handle_options(mmr, argparse.Namespace(opt_action='implied', symbol='AAPL',
                                                    expiration='2026-11-20', risk_free_rate=0.05, source=None))
    payload = _json_out(capsys)
    assert payload['data']['strikes_excluded'] == 4 and payload['data']['x'] == [1.0, 1.1]
    mmr.options_implied.assert_called_once_with('AAPL', '2026-11-20', 0.05, source=None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_cli_options.py -q -p no:cacheprovider`
Expected: FAIL (`AttributeError: 'Namespace' object has no attribute 'source'` from the parser test, `AttributeError: module 'trader.mmr_cli' has no attribute '_format_option_number'`, and the garbage-expiry tests not raising).

- [ ] **Step 3: Implement**

Parsers — in `build_parser`, right after the six options sub-parsers are defined (after `opt_sell_p`'s last `add_argument`, before `# forex`), add (`Capability` and `source_choices` are already imported earlier in `build_parser`):

```python
    option_sources = source_choices(Capability.OPTIONS)
    for data_parser in (opt_exp_p, opt_chain_p, opt_snap_p, opt_impl_p):
        data_parser.add_argument(
            '--source', choices=option_sources, default=None,
            help='Options data source (default: data_providers.options, else alpaca = free indicative '
                 'feed, not OPRA NBBO; massive = OPRA, paid plan)')
    for order_parser in (opt_buy_p, opt_sell_p):
        order_parser.add_argument(
            '--source', choices=option_sources, default=None,
            help='Source used only to resolve a relative -e like 3m (orders always go to IB; '
                 'exact dates need no data source)')
```

Also change the snapshot help to `opt_snap_p.add_argument('ticker', help='Option symbol: AAPL261120C00250000 or O:AAPL261120C00250000')`, the parser epilog line to `'  options snapshot AAPL260320C00250000      # O: prefix also accepted\n'`, and add `'  options chain AAPL --source massive        # OPRA via Massive (paid)\n'` to the epilog.

`_resolve_expiration` — replace the function:

```python
def _resolve_expiration(mmr: MMR, symbol: str, expiration_arg: str | None, default_days: int = 90,
                        source: str | None = None) -> str | None:
    """Resolve an expiration argument to a concrete YYYY-MM-DD date.

    - None → closest listed expiration to `default_days` out
    - '2026-03-20' → validated and returned as-is (no data provider involved)
    - '90d', '3m', '6 months' → closest listed expiration, from the OPTIONS `source`

    Returns None when the provider lists no expirations; raises ValueError for anything
    that is neither a real YYYY-MM-DD date nor a relative expression.
    """
    import datetime as dt_mod
    from trader.data_providers.option_symbols import parse_expiration_date

    if expiration_arg is None:
        target_days = default_days
    else:
        target_days = _parse_relative_expiration(expiration_arg)
        if target_days == -1:
            try:
                return parse_expiration_date(expiration_arg.strip()).isoformat()
            except ValueError:
                raise ValueError(f'expiration must be YYYY-MM-DD or relative like "90d", "3m": '
                                 f'got {expiration_arg!r}') from None

    dates = mmr.options_expirations(symbol, source=source)
    if not dates:
        return None

    target_date = dt_mod.date.today() + dt_mod.timedelta(days=target_days)
    return min(dates, key=lambda d: abs((dt_mod.date.fromisoformat(d) - target_date).days))
```

(`min` keeps the first of two equally close dates, like the old loop's strict `<`; the existing `TestRelativeExpiration` tests pin this.)

`_handle_options` — replace the whole function with:

```python
_OPTION_FEED_NOTES = {
    'indicative': 'indicative feed — not OPRA NBBO; greeks/IV only on liquid contracts',
    'opra': 'OPRA feed',
}
_CHAIN_TABLE_COLUMNS = (   # (header, row key, format spec, suffix)
    ('strike', 'strike', '.2f', ''), ('bid', 'bid', '.2f', ''), ('ask', 'ask', '.2f', ''),
    ('mid', 'mid', '.2f', ''), ('last', 'last', '.2f', ''), ('volume', 'volume', '.0f', ''),
    ('OI', 'open_interest', '.0f', ''), ('iv%', 'iv', '.1f', '%'), ('delta', 'delta', '.4f', ''),
    ('gamma', 'gamma', '.4f', ''), ('theta', 'theta', '.4f', ''), ('vega', 'vega', '.4f', ''),
    ('break_even', 'break_even', '.2f', ''),
)


def _format_option_number(value, spec: str, suffix: str = '') -> str:
    import math
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return '—'
    return f'{value:{spec}}{suffix}'


def _option_feed_label(provider: str, feed: str) -> str:
    return f'{provider}, {_OPTION_FEED_NOTES.get(feed, f"{feed} feed")}'


def _without_nan(mapping: dict) -> dict:
    """NaN → None, so --json prints null instead of the invalid token NaN."""
    import math
    return {key: (None if isinstance(value, float) and math.isnan(value) else value)
            for key, value in mapping.items()}


def _note_resolved_expiration(resolved: str, requested) -> None:
    if resolved != requested and not _json_mode:
        console.print(f'[dim]Using expiration: {resolved}[/dim]')


def _handle_options(mmr: MMR, args: argparse.Namespace):
    """Options data (OPTIONS capability) and trading (IB) commands."""
    handlers = {
        'expirations': _options_expirations, 'exp': _options_expirations,
        'chain': _options_chain,
        'snapshot': _options_snapshot, 'snap': _options_snapshot,
        'implied': _options_implied,
        'buy': _options_order, 'sell': _options_order,
    }
    action = getattr(args, 'opt_action', None)
    if action not in handlers:
        print_status('Usage: options expirations|chain|snapshot|implied|buy|sell', success=False)
        return

    import logging as _logging
    _logging.getLogger('urllib3').setLevel(_logging.WARNING)
    from trader.data_providers import ProviderError
    try:
        handlers[action](mmr, args)
    except ProviderError as ex:
        print_status(str(ex), success=False)


def _options_expirations(mmr: MMR, args: argparse.Namespace):
    import datetime as dt_mod
    import pandas as pd
    symbol = args.symbol.upper()
    dates = mmr.options_expirations(symbol, source=getattr(args, 'source', None))
    if not dates:
        print_status(f'No expiration dates found for {symbol}', success=False)
        return
    today = dt_mod.date.today()
    rows = [{'expiration': d, 'DTE': (dt_mod.date.fromisoformat(d) - today).days} for d in dates]
    print_df(pd.DataFrame(rows), title=f'Expirations: {symbol}')


def _options_chain(mmr: MMR, args: argparse.Namespace):
    symbol = args.symbol.upper()
    source = getattr(args, 'source', None)
    expiration = None
    if args.expiration:
        expiration = _resolve_expiration(mmr, symbol, args.expiration, source=source)
        if expiration is None:
            print_status(f'No expiration dates found for {symbol}', success=False)
            return
        _note_resolved_expiration(expiration, args.expiration)
    df = mmr.options_chain(symbol, expiration=expiration, contract_type=args.contract_type,
                           strike_min=args.strike_min, strike_max=args.strike_max, source=source)
    if df.empty:
        suffix = f' expiring {expiration}' if expiration else ''
        print_status(f'No chain data for {symbol}{suffix}', success=False)
        return

    first = df.iloc[0]
    title = f'Options Chain: {symbol} ({first["expiration"]}) — {_option_feed_label(first["provider"], first["feed"])}'
    if _json_mode:
        print_df(df, title=title)
        return

    table = Table(title=title)
    table.add_column('type', style='bold')
    for header, _, _, _ in _CHAIN_TABLE_COLUMNS:
        table.add_column(header, justify='right')
    for _, row in df.iterrows():
        type_cell = '[cyan]call[/cyan]' if row['type'] == 'call' else '[magenta]put[/magenta]'
        table.add_row(type_cell, *(_format_option_number(row[key], spec, suffix)
                                   for _, key, spec, suffix in _CHAIN_TABLE_COLUMNS))
    console.print(table)


def _options_snapshot(mmr: MMR, args: argparse.Namespace):
    result = mmr.options_snapshot(args.ticker, source=getattr(args, 'source', None))
    title = f'Option Snapshot: {result["ticker"]} — {_option_feed_label(result["provider"], result["feed"])}'
    print_dict(_without_nan(result), title=title)


def _options_implied(mmr: MMR, args: argparse.Namespace):
    symbol = args.symbol.upper()
    source = getattr(args, 'source', None)
    expiration = _resolve_expiration(mmr, symbol, args.expiration, source=source)
    if not expiration:
        print_status(f'No expiration dates found for {symbol}', success=False)
        return
    _note_resolved_expiration(expiration, args.expiration)
    data = mmr.options_implied(symbol, expiration, args.risk_free_rate, source=source)
    if _json_mode:
        print_json_result(data, title=f'Implied: {symbol} {expiration}')
        return
    console.print(f'[dim]{data["strikes_used"]} call strikes with an IV used, {data["strikes_excluded"]} '
                  f'without IV excluded — {_option_feed_label(data["provider"], data["feed"])}[/dim]')
    from trader.tools.chain import plot_market_implied_vs_constant_console
    plot_market_implied_vs_constant_console(
        data['x'], data['market_implied'], data['constant'],
        f'{symbol} for {expiration}, constant vs market implied'
    )


def _options_order(mmr: MMR, args: argparse.Namespace):
    """Options orders go to IB; only a relative -e asks the OPTIONS data source for expirations."""
    symbol = args.symbol.upper()
    if not args.market and args.limit is None:
        print_status('Specify --market or --limit', success=False)
        return
    expiration = _resolve_expiration(mmr, symbol, args.expiration, source=getattr(args, 'source', None))
    if not expiration:
        print_status(f'No expiration dates found for {symbol}', success=False)
        return
    _note_resolved_expiration(expiration, args.expiration)
    place = mmr.buy_option if args.opt_action == 'buy' else mmr.sell_option
    result = place(symbol, expiration, args.strike, args.right, args.quantity,
                   limit_price=args.limit, market=args.market)
    _print_trade_result(result, args.opt_action.upper(), f'{symbol} {expiration} {args.strike}{args.right}')
```

`Table` is the module-level `rich.table.Table` import already at the top of `mmr_cli.py`.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_cli_options.py tests/test_options.py "tests/test_sweep.py::test_options_buy_missing_price_emits_json" -q -p no:cacheprovider`
Expected: all pass (`test_sweep`'s `Namespace` has no `source`; the handlers read it with `getattr`).

- [ ] **Step 5: Commit**

```bash
git add trader/mmr_cli.py tests/data_providers/test_cli_options.py
git commit -m "feat(cli): options --source, strict expirations and feed labels

options commands take --source (default alpaca indicative). Tables and
titles say when data is the indicative feed, missing numbers print as a
dash (was nan%) and --json prints null. Exact -e dates are validated and
never touch a data source; garbage no longer reaches IB orders.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Skill helper `implied_move` — label the chain it used

**Files:**
- Modify: `skills/mmr-skill/scripts/mmr_helpers.py` (`MMRHelpers.implied_move`, ≈L1647–1840), `skills/mmr-skill/SKILL.md` (the `implied_move` return example, ≈L145–152)
- Test: `tests/test_mmr_skill_helpers.py` (append)

**Interfaces:**
- Consumes: `options chain --json` rows now carry `provider` and `feed` (Task 8).
- Produces: `implied_move(...)` result gains `provider` and `feed` keys; the straddle method is named `atm_straddle` (was `polygon_atm_straddle`); `confidence` is `medium` for `feed == 'indicative'`, `high` otherwise. The `prefer` values (`auto` / `polygon` / `realized`) are unchanged — `polygon` now means "options chain only, from whatever source serves it".

Why now: after Task 7 the helper's bare `options chain` call reaches Alpaca by default; without this change it would report Alpaca indicative mids as `polygon_atm_straddle` with `high` confidence.

- [ ] **Step 1: Write the failing test** (append to `tests/test_mmr_skill_helpers.py`)

```python
def _straddle_rows(provider, feed):
    return {'data': [
        {'type': 'call', 'strike': 100.0, 'bid': 2.0, 'ask': 2.2, 'mid': 2.1, 'provider': provider, 'feed': feed},
        {'type': 'put', 'strike': 100.0, 'bid': 1.8, 'ask': 2.0, 'mid': 1.9, 'provider': provider, 'feed': feed},
    ], 'title': 'Options Chain'}


def _implied_move(helpers, monkeypatch, provider, feed):
    import datetime as dt

    async def spot(symbol):
        return 100.0

    monkeypatch.setattr(helpers.mod, '_last_close_local_or_remote', spot)
    helpers.json_router['options'] = _straddle_rows(provider, feed)
    expiration = (dt.date.today() + dt.timedelta(days=30)).isoformat()
    return asyncio.run(helpers.H.implied_move('AAPL', expiration=expiration, prefer='polygon'))


def test_implied_move_labels_indicative_chain(helpers, monkeypatch):
    result = _implied_move(helpers, monkeypatch, 'alpaca', 'indicative')
    assert result['method'] == 'atm_straddle'
    assert (result['provider'], result['feed'], result['confidence']) == ('alpaca', 'indicative', 'medium')
    assert 'not OPRA NBBO' in result['notes']
    assert result['implied_move_pct'] == pytest.approx(4.0)


def test_implied_move_opra_chain_keeps_high_confidence(helpers, monkeypatch):
    result = _implied_move(helpers, monkeypatch, 'massive', 'opra')
    assert (result['method'], result['provider'], result['confidence']) == ('atm_straddle', 'massive', 'high')
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/test_mmr_skill_helpers.py -q -p no:cacheprovider -k implied_move`
Expected: FAIL with `AssertionError` (`'polygon_atm_straddle' == 'atm_straddle'`).

- [ ] **Step 3: Implement** in `skills/mmr-skill/scripts/mmr_helpers.py` (`implied_move`)

1. In the initial `result: dict = {...}`, add `"provider": None,` and `"feed": None,` after `"put_mid": None,`.
2. Replace every `result["method"] = "polygon_atm_straddle"` (two error paths) with `result["method"] = "atm_straddle"`.
3. Replace the success `result.update({...})` block (the one with `"notes": "ATM straddle from Polygon chain."`) with:

```python
                            provider = rows[0].get("provider") or "unknown"
                            feed = rows[0].get("feed") or "unknown"
                            indicative = feed == "indicative"
                            result.update({
                                "method": "atm_straddle",
                                "confidence": "medium" if indicative else "high",
                                "provider": provider,
                                "feed": feed,
                                "spot": spot,
                                "implied_move_pct": round(move_pct, 3),
                                "implied_move_dollar": round(cm + pm, 4),
                                "expected_low": round(spot - (cm + pm), 4),
                                "expected_high": round(spot + (cm + pm), 4),
                                "atm_strike": atm,
                                "call_mid": cm,
                                "put_mid": pm,
                                "source_rows": len(rows),
                                "notes": (f"ATM straddle from the {provider} options chain ({feed} feed"
                                          + ("; indicative quotes, not OPRA NBBO" if indicative else "")
                                          + ")."),
                            })
```

4. Docstring: tier 1 becomes ``atm_straddle`` — "pulls ATM call+put from ``options chain`` (default source Alpaca's free indicative feed → `medium` confidence; `--source massive` OPRA → `high`), computes ``(call_mid + put_mid) / spot``"; the `prefer` text says `"polygon"` = "only the options chain (name kept for compatibility)"; the return shape shows `"method": "realized_vol" | "atm_straddle"` and adds `"provider": "alpaca", "feed": "indicative",  # only set for atm_straddle`; the `# --- Tier 1: Polygon ATM straddle ---` comment becomes `# --- Tier 1: ATM straddle from the options chain ---`.

`skills/mmr-skill/SKILL.md`, in the `implied_move` return example: `"method": "atm_straddle",   // or "realized_vol"`, `"confidence": "medium",   // "high" for an OPRA straddle (--source massive), "medium" for Alpaca indicative or realized vol with ≥30 bars`, and add `"provider": "alpaca", "feed": "indicative",` lines.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/test_mmr_skill_helpers.py -q -p no:cacheprovider`
Expected: all pass. Also `grep -rn "polygon_atm_straddle" skills/ tests/` → no matches.

- [ ] **Step 5: Commit**

```bash
git add skills/mmr-skill/scripts/mmr_helpers.py skills/mmr-skill/SKILL.md tests/test_mmr_skill_helpers.py
git commit -m "fix(skill): label implied_move with the options chain it used

The options chain now defaults to Alpaca's indicative feed, so the
straddle method is atm_straddle with provider/feed keys, and indicative
quotes give medium confidence instead of high.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Live checks, real CLI run, docs

**Files:**
- Modify: `tests/data_providers/test_live_alpaca.py` (append live tests), `CLAUDE.md`, `docs/OPERATIONAL_STATE.md`, `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Add live tests** (append to `tests/data_providers/test_live_alpaca.py`; reuse its `pytestmark` gating; move the imports below up to the file's import block). Verified while planning: these six tests passed against the real APIs on 2026-10-04 (Massive took the NOT_AUTHORIZED branch).

```python
import math

from trader.data_providers.errors import ProviderEntitlementError, ProviderError
from trader.data_providers.option_symbols import parse_option_symbol
from trader.tools.chain import implied_distribution


def _options():
    return ProviderRegistry.from_config({
        'alpaca_api_key_id': os.environ['ALPACA_API_KEY_ID'],
        'alpaca_api_secret_key': os.environ['ALPACA_API_SECRET_KEY'],
    }).get(Capability.OPTIONS, 'alpaca')


def _expiration_at_least(days):
    target = dt.date.today() + dt.timedelta(days=days)
    return next(d for d in _options().expirations('AAPL') if dt.date.fromisoformat(d) >= target)


def test_live_option_expirations_reach_months_ahead():
    dates = _options().expirations('AAPL')
    assert len(dates) > 10 and dates == sorted(dates)
    assert dt.date.fromisoformat(dates[0]) >= dt.date.today() - dt.timedelta(days=1)
    assert dt.date.fromisoformat(dates[-1]) - dt.date.today() > dt.timedelta(days=180)


def test_live_option_chain_is_indicative_and_never_invents_greeks():
    expiration = _expiration_at_least(30)
    rows = _options().chain('AAPL', expiration)
    assert len(rows) > 20
    assert all(r['feed'] == 'indicative' and r['provider'] == 'alpaca' for r in rows)
    assert all(r['expiration'] == expiration for r in rows)
    assert all(math.isnan(r['delta']) == math.isnan(r['iv']) for r in rows)   # greeks and IV come together
    assert any(not math.isnan(r['open_interest']) for r in rows)
    assert rows[0]['underlying_price'] > 0


def test_live_option_contract_matches_chain_row():
    expiration = _expiration_at_least(30)
    row = next(r for r in _options().chain('AAPL', expiration, 'call') if not math.isnan(r['iv']))
    contract = _options().contract(parse_option_symbol('O:' + row['ticker']))
    assert contract['ticker'] == row['ticker'] and contract['underlying'] == 'AAPL'
    assert contract['feed'] == 'indicative'


def test_live_unknown_underlying_is_loud():
    with pytest.raises(ProviderError, match='invalid underlying'):
        _options().expirations('ZZZZQ')
    with pytest.raises(ProviderError, match='invalid underlying'):
        _options().chain('ZZZZQ', _expiration_at_least(30))


def test_live_implied_distribution_from_indicative_chain():
    expiration = _expiration_at_least(30)
    rows = _options().chain('AAPL', expiration, 'call')
    result = implied_distribution(rows, expiration, 0.05, dt.date.today())
    assert result['strikes_used'] >= 8 and result['feed'] == 'indicative'


@pytest.mark.skipif(not os.getenv('MASSIVE_API_KEY'), reason='needs MASSIVE_API_KEY')
def test_live_massive_options_entitlement_is_loud():
    provider = ProviderRegistry.from_config({'massive_api_key': os.environ['MASSIVE_API_KEY']}) \
        .get(Capability.OPTIONS, 'massive')
    expiration = provider.expirations('AAPL')[0]     # the contracts list works on free Massive plans
    try:
        rows = provider.chain('AAPL', expiration)
    except ProviderEntitlementError as ex:
        assert 'NOT_AUTHORIZED' in str(ex) and '--source alpaca' in str(ex)
    else:
        assert rows and all(r['feed'] == 'opra' for r in rows)
```

Run (keys from the main checkout's `.env`, never printed):

```bash
set -a; eval "$(grep -E '^(ALPACA_API_(KEY_ID|SECRET_KEY)|MASSIVE_API_KEY)=' /Users/mudryy/private/mmr/.env)"; set +a
MMR_LIVE_TESTS=1 /Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_live_alpaca.py -v -m live -p no:cacheprovider
```

Expected: all live tests pass (on 2026-10-04 the Massive key returned NOT_AUTHORIZED for option snapshots, so the Massive test takes the entitlement branch). Default run still skips them: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_live_alpaca.py -q -p no:cacheprovider` → all skipped.

- [ ] **Step 2: Real CLI run** (same shell, keys exported; `--json` so `implied` does not open the interactive plot)

```bash
PY=/Users/mudryy/private/mmr/.venv/bin/python
$PY -m trader.mmr_cli --json options expirations AAPL | head -c 400; echo
$PY -m trader.mmr_cli --json options chain AAPL -e 1m --type call --strike-min 300 --strike-max 360 | head -c 800; echo
$PY -m trader.mmr_cli --json options snapshot O:<ticker from the chain output> | head -c 800; echo
$PY -m trader.mmr_cli --json options implied AAPL -e 1m | head -c 400; echo
$PY -m trader.mmr_cli options chain AAPL -e 1m --type put --strike-min 300 --strike-max 330
$PY -m trader.mmr_cli --json options chain AAPL -e 1m --source massive | head -c 400; echo
$PY -m trader.mmr_cli --json options buy AAPL -e foobar -s 250 -r C -q 1 --market; echo
```

Expected: expirations list; chain rows with `"feed": "indicative"` and `null` for missing greeks; the snapshot accepts the `O:` form; implied shows `strikes_used` / `strikes_excluded`; the table title says "indicative feed — not OPRA NBBO" and shows `—` for missing values; `--source massive` prints the NOT_AUTHORIZED entitlement message (no traceback); the `buy` with `-e foobar` prints `Error: expiration must be YYYY-MM-DD or relative …` and places nothing. Record the outputs (trimmed) in the commit body.

- [ ] **Step 3: Docs**

- `CLAUDE.md`:
  - "Free providers first" → **Default sources** bullet: add "options market data (`options expirations|chain|snapshot|implied`) — Alpaca's free **indicative** feed: not the OPRA NBBO, greeks/IV only on liquid contracts, every row labelled `feed`; `--source massive` = OPRA (paid plan)". **Inheritance** bullet: options never inherit `default_data_source`; override with `data_providers.options`.
  - CLI command list: `options snapshot AAPL260320C00250000` (O: prefix also accepted); `options chain AAPL --source massive`; note that `options buy|sell` stay on IB, exact `-e YYYY-MM-DD` dates need no data source, a relative `-e 3m` asks the options source (`--source` on buy/sell picks it).
  - Note the output changes: chain/snapshot rows share one shape (`OPTION_FIELDS`: today's columns plus `underlying, quote_time, last_time, provider, feed`); `ticker` is the bare OCC symbol for every source (Massive used `O:`); missing numbers are NaN/null, not 0.0; `options snapshot` returns the row shape (`iv` as a number in percent, was the string `implied_volatility: "35.00%"`); `options implied` reports `strikes_used` / `strikes_excluded`, refuses fewer than 8 strikes with an IV or a non-future expiration, and uses T = calendar days / 365 (was / 255).
  - Command Service Requirements: change `` `financials`, `options` (Massive key) `` to `` `financials` (Massive key), `options` data (Alpaca keys by default; `--source massive` needs a Massive options plan) ``.
  - Project structure: `trader/data_providers/` line mentions options (`option_symbols.py`, `alpaca/options.py`, `massive/options.py`).
- `docs/OPERATIONAL_STATE.md` — new subsection after "Movers and news (phase 3a)":

  ```markdown
  ### Options (phase 5)

  `mmr options expirations|chain|snapshot|implied` default to Alpaca's free
  indicative feed **without any config edit** (options ignore
  `default_data_source`). Indicative ≠ OPRA NBBO: quotes are derived, trades
  delayed, greeks/IV only on liquid contracts (on 2026-11-20 AAPL, 113 of 168).
  Rows say `feed: indicative`. To keep Massive: set
  `data_providers: {options: massive}` in the live `trader.yaml` — this key's
  Massive plan returns NOT_AUTHORIZED for option snapshots (2026-10-04), so
  Massive options need a paid options plan. `options buy|sell` still go to IB.

  Live checks (date): <result of Task 10 Step 1>.
  ```
- `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`: mark row 5 `done`; add to "Operator steps": "After phase 5: `mmr options …` default to Alpaca indicative without a config edit; set `data_providers: {options: massive}` to keep Massive (needs a Massive options plan)."; add to row 9: "dashboard options still use `massive_research` → `options_data.chain_records/contract_snapshot` (old normaliser, 0.0 fills, `O:` tickers); move them to the OPTIONS capability and delete those two functions; update SKILL.md options lines (massive_api_key)".

- [ ] **Step 4: Verify**

Run: `grep -n "massive_api_key\|RESTClient" trader/sdk.py | sed -n '1,40p'` — no options method may still build a Massive client. `grep -rn "get_chains\|get_call_chain\|get_put_chain" trader web skills tests` → no matches. Then, only if the orchestrator confirms no other lane is running tests, the full suite: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/ --timeout=30 -q --ignore=tests/test_ibrx_async.py -p no:cacheprovider` → 0 failed. Otherwise run `tests/data_providers tests/test_options.py tests/test_options_data.py tests/test_options_implied.py tests/test_massive_research.py tests/test_dashboard_research_api.py tests/test_dashboard_research_service.py tests/test_mmr_skill_helpers.py tests/test_sweep.py` and say in the report that the full suite was not run.

- [ ] **Step 5: Commit**

```bash
git add tests/data_providers/test_live_alpaca.py CLAUDE.md docs
git commit -m "docs: document Alpaca indicative options

<live test result and trimmed CLI outputs from Steps 1-2>

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

# Free Data Providers — Phase 3b: One Idea Scanner (+ Alpaca)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the two near-duplicate US idea scanners (`IdeaScanner` for Massive, `TwelveDataIdeaScanner`) with one shared pipeline that takes a small per-provider *scan source*, route `ideas` through the provider registry, and add Alpaca as the free default scan source.

**Architecture:** `IdeaScanner(source)` in `trader/tools/idea_scanner.py` keeps the shared pipeline (discover → filter → pre-score → cap → indicators → score → sort → enrich → frame). Provider-specific work moves behind a `ScanSource` protocol: `MassiveScanSource` and `TwelveDataScanSource` are the existing code **moved unchanged**; `AlpacaScanSource` is new and uses Alpaca's free 15-minute-delayed consolidated (`delayed_sip`) snapshots, screener movers + most-actives for discovery, local RSI/EMA/SMA from Alpaca SIP daily bars, the Alpaca asset list for names/derivative filtering, and Alpaca news. A new registry capability `IDEAS` builds the scan source. `IBIdeaScanner` (international, `--location`) is untouched.

**Tech Stack:** Python 3.12, pandas, `requests`, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-04-free-data-providers-design.md` (§4.4 "One idea scanner", §4.3 defaults, §5 labels, §6). Index: `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md`. Conventions: `docs/superpowers/plans/2026-10-04-free-data-providers-03a-quotes-movers-news.md`.

## Global Constraints

- Lane worktree: `/Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner`, branch `feat/fdp-3b-scanner`. Run every command from that directory. Use `git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner …`; never `cd` elsewhere; never bare `git stash`.
- Python: `/Users/mudryy/private/mmr/.venv/bin/python` / `/Users/mudryy/private/mmr/.venv/bin/pytest` run from the worktree root (the worktree's own `trader` package is imported). Never use `.venv/bin/mmr` (it runs the main checkout's code); use `python -m trader.mmr_cli …`.
- Implementers run ONLY the targeted test files a task names (other lanes run tests in parallel; the full suite binds fixed ports). The controller runs the full suite at integration.
- One local commit per task; message ends with a blank line then `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` (or the implementer's exact model name). Never push.
- **Behaviour preservation:** Massive and TwelveData scan behaviour must stay byte-for-byte the same in Tasks 1–3 (same discovery, filters, indicators, fundamentals, news, notices). Moved code is cut-and-pasted, not rewritten.
- The silent Massive → TwelveData fallback in `MMR.scan_ideas` and the TwelveData-internal movers → liquid-tickers fallback stay as they are in 3b (phase 3c removes them).
- `ideas` does not inherit `default_data_source` (only HISTORY does). Per-capability override: `data_providers.ideas`.
- Alpaca scan data: `feed=delayed_sip` snapshots (consolidated volume and NBBO, 15-minute delayed). Every Alpaca scan result carries `df.attrs['ideas_notice']` saying the universe size and that prices are 15-minute delayed; `df.attrs['ideas_provider']` names the source for every provider.
- Alpaca `--fundamentals` raises `IdeaScannerError` naming the alternatives until free ratios (Finnhub) land in phase 4. Never invent sentiment; Alpaca news has none.
- Fail loudly: provider auth/entitlement/rate-limit errors propagate (as `ProviderError` / `IdeaScannerError`); unknown explicit tickers are named in the notice, never guessed.
- Comments only where the code is not obvious.

## Review Focus

1. **Volume filters on Alpaca data** (presets with `min_volume`, e.g. 500k): expect consolidated volume (AAPL 34.3M on 2026-10-02), not IEX volume (0.84M) — otherwise almost every name is filtered out. → Task 4 test `test_candidate_uses_consolidated_volume_and_vwap`.
2. **Market-scan presets on Alpaca** (`ideas gap-down`, `mean-reversion`, `volatile` — Massive scans 10k+ tickers): expect discovery from movers + most-actives with a notice stating the symbol count and "not the full market". → Task 4 test `test_market_scan_preset_uses_movers_and_actives_with_notice`.
3. **Unknown explicit tickers** (`ideas --tickers AAPL ZZZZQ`): expect AAPL scanned and ZZZZQ named in the notice. → Task 4 test `test_unknown_tickers_are_named_in_notice`.
4. **`--fundamentals` on the Alpaca default** (no free ratios yet): expect a clear error naming `--source massive|twelvedata`, not empty columns. → Task 5 test `test_fundamentals_raise_until_phase_4`.
5. **Existing users with `default_data_source: twelvedata`**: `ideas` must not move to TwelveData (Pro+ movers → 403). → Task 6 test `test_ideas_default_ignores_default_data_source`.

## Decisions

- Decision: scan sources live in `trader/data_providers/<provider>/scan.py`; the protocol and `Discovery` live in `trader/data_providers/capabilities.py` — keeps provider code in one place; `idea_scanner.py` never imports provider modules — cost if wrong: a later move.
- Decision: Massive keeps its server-side RSI/EMA/SMA inside `MassiveScanSource` (spec §4.4 says local indicators) — 3b must not change opt-in Massive behaviour; only the new Alpaca source computes locally — cost if wrong: Massive keeps one extra API style until someone ports it.
- Decision: Alpaca candidate `price`/`change_pct` use the regular-session daily bar (`dailyBar.c` vs `prevDailyBar.c`), matching Massive's `day.close` semantics; `volume`/`rel_vol`/`vwap` from consolidated daily bars; spread from the delayed NBBO — cost if wrong: after-hours moves are not reflected until the next session.
- Decision: Alpaca discovery for every non-explicit source = union of screener gainers + losers (top 50 each) + most-actives by volume (top 50) — the best free approximation; labelled with the count — cost if wrong: small-cap gaps outside the top lists are missed.
- Decision: Alpaca indicators use `AlpacaHistoryProvider` daily SIP bars (completed sessions, ~120 calendar days) and the shared `compute_rsi/ema/sma` — cost if wrong: one history call per indicator-cap survivor (≤ 45, within 200/min).
- Decision: keep `IdeaScanner._score_*`, `_apply_filters`, `_to_dataframe` thin wrappers (existing tests and `TestPresetDefinitions` use them) — cost if wrong: a few trivial methods.
- Decision: default `ideas` source switches to Alpaca in Task 6 (same user-visible step as `movers` in 3a) — free; Massive stays via `--source massive` or `data_providers.ideas: massive` — cost if wrong: users without Alpaca keys see `ProviderNotConfigured` naming the env vars.

---

## File Structure

```
trader/data_providers/
├── capabilities.py           # + Capability.IDEAS, Discovery, ScanSource protocol                (Task 1)
├── builtin.py                # + IDEAS builders/defaults                                         (Tasks 3, 6)
├── massive/scan.py           # MassiveScanSource (moved from IdeaScanner)                        (Task 1)
├── twelvedata/scan.py        # TwelveDataScanSource (moved from TwelveDataIdeaScanner)           (Task 2)
└── alpaca/scan.py            # AlpacaScanSource                                                  (Tasks 4, 5)
trader/tools/idea_scanner.py  # IdeaScanner(source) shared pipeline; TwelveDataIdeaScanner removed  (Tasks 1, 2)
trader/sdk.py                 # scan_ideas through the registry                                   (Task 3)
trader/mmr_cli.py             # ideas --source from the registry; ProviderError handling           (Tasks 3, 6)
trader/tools/massive_research.py  # construction sites only (dashboard; fallback stays until 3c)    (Tasks 1, 2)
tests/test_idea_scanner.py, tests/test_twelvedata_sdk.py  # migrated                               (Tasks 1, 2)
tests/data_providers/test_scan_pipeline.py, test_ideas_registry.py, test_alpaca_scan.py, fixtures/*  (Tasks 1, 3–6)
```

---

### Task 1: Shared pipeline + `MassiveScanSource` (moved, no behaviour change)

**Files:**
- Modify: `trader/data_providers/capabilities.py` (add `Capability.IDEAS = 'ideas'`, `Discovery`, `ScanSource`; export from `trader/data_providers/__init__.py`)
- Create: `trader/data_providers/massive/scan.py`
- Modify: `trader/tools/idea_scanner.py` (class `IdeaScanner`), `trader/sdk.py` (construction site in `scan_ideas`, ≈L4092), `trader/tools/massive_research.py` (construction site ≈L118)
- Modify tests: `tests/test_idea_scanner.py` (Massive section only, lines before `class TestIBIdeaScannerBuildCandidates`)
- Create test: `tests/data_providers/test_scan_pipeline.py`

**Interfaces:**
- Produces:
  - `Capability.IDEAS`.
  - `@dataclass(frozen=True) class Discovery: candidates: list[dict]; notice: str = ''`.
  - `ScanSource` (runtime-checkable Protocol): attribute `name: str`; `discover(source: str, tickers, universe_symbols, use_market_scan: bool) -> Discovery`; `indicators(tickers: list[str], needed: list[str]) -> dict[str, dict]`; `names(tickers) -> dict[str, str]`; `fundamentals(tickers) -> dict[str, dict]`; `news(tickers) -> dict[str, dict]`.
  - `MassiveScanSource(massive_client)` with `name = 'massive'` and the moved private methods `_discover`, `_discover_raw`, `_build_candidates`, `_fetch_indicators`, `_FUNDAMENTAL_FIELDS`, `_fetch_names`, `_fetch_fundamentals`, `_fetch_news` (bodies unchanged).
  - `IdeaScanner(source: ScanSource)` with attribute `source`, method `scan(...)` (same signature as today), and wrappers `_apply_filters`, `_score_*`, `_to_dataframe`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_scan_pipeline.py
import pandas as pd

from trader.data_providers.capabilities import Discovery, ScanSource
from trader.tools.idea_scanner import IdeaScanner


class FakeSource:
    name = 'fake'

    def __init__(self, candidates, notice=''):
        self._discovery = Discovery(candidates, notice)
        self.calls = []

    def discover(self, source, tickers, universe_symbols, use_market_scan):
        self.calls.append(('discover', source, use_market_scan))
        return self._discovery

    def indicators(self, tickers, needed):
        self.calls.append(('indicators', tuple(tickers), tuple(needed)))
        return {t: {'rsi': 55.0, 'ema_9': 1.0} for t in tickers}

    def names(self, tickers):
        return {t: f'{t} Inc' for t in tickers}

    def fundamentals(self, tickers):
        return {t: {'pe_ratio': 10.0} for t in tickers}

    def news(self, tickers):
        return {t: {'headline': f'{t} up', 'news_date': '2026-10-02', 'sentiment': '', 'catalyst': ''}
                for t in tickers}


def _candidate(ticker, change_pct, volume=2_000_000, price=50.0):
    return {'ticker': ticker, 'price': price, 'change_pct': change_pct, 'volume': volume,
            'gap_pct': 1.0, 'rel_vol': 2.0, 'range_pct': 3.0, 'spread_pct': 0.05, 'vwap': price}


def test_fake_source_satisfies_protocol():
    assert isinstance(FakeSource([]), ScanSource)


def test_pipeline_ranks_enriches_and_labels():
    source = FakeSource([_candidate('AAA', 6.0), _candidate('BBB', 4.0)], notice='heads up')
    df = IdeaScanner(source).scan(preset='momentum', top_n=5, names=True, fundamentals=True, news=True)
    assert list(df['ticker'])[:2] == ['AAA', 'BBB']
    assert set(df['name']) == {'AAA Inc', 'BBB Inc'}
    assert df.attrs['ideas_notice'] == 'heads up'
    assert df.attrs['ideas_provider'] == 'fake'
    assert ('discover', 'movers', False) in source.calls


def test_market_scan_preset_passes_flag():
    source = FakeSource([_candidate('AAA', -6.0)])
    IdeaScanner(source).scan(preset='gap-down', top_n=5)
    assert source.calls[0] == ('discover', 'movers', True)


def test_empty_discovery_returns_empty_frame_with_notice():
    df = IdeaScanner(FakeSource([], notice='nothing found')).scan(preset='momentum')
    assert df.empty and df.attrs.get('ideas_notice') == 'nothing found'


def test_candidate_name_survives_when_names_not_requested():
    candidate = dict(_candidate('AAA', 6.0), name='Alpha Corp')
    df = IdeaScanner(FakeSource([candidate])).scan(preset='momentum', names=True)
    assert df.loc[0, 'name'] in ('AAA Inc', 'Alpha Corp')
```

(The last test pins the merge rule `c['name'] = name_data.get(t) or c.get('name', '')`; with `FakeSource.names` returning a name, `AAA Inc` wins.)

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_scan_pipeline.py -v -p no:cacheprovider`
Expected: FAIL with `ImportError: cannot import name 'Discovery'`

- [ ] **Step 3: Implement**

1. `trader/data_providers/capabilities.py`: add `IDEAS = 'ideas'` to `Capability`, then:

```python
@dataclass(frozen=True)
class Discovery:
    """Scanner candidates from one provider, plus a user-facing notice (may be empty)."""
    candidates: list
    notice: str = ''


@runtime_checkable
class ScanSource(Protocol):
    name: str

    def discover(self, source: str, tickers, universe_symbols, use_market_scan: bool) -> Discovery:
        ...

    def indicators(self, tickers: list, needed: list) -> dict:
        ...

    def names(self, tickers: list) -> dict:
        ...

    def fundamentals(self, tickers: list) -> dict:
        ...

    def news(self, tickers: list) -> dict:
        ...
```

(add `from dataclasses import dataclass` to the imports; export `Discovery`, `ScanSource` in `__init__.py` `__all__`).

2. Create `trader/data_providers/massive/scan.py`:

```python
"""Massive (Polygon) scan source: snapshot discovery, server-side indicators, ratios, news."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from trader.data_providers.capabilities import Discovery
from trader.tools.idea_scanner import IdeaScannerError, is_data_entitlement_error

logger = logging.getLogger(__name__)


class MassiveScanSource:
    name = 'massive'

    def __init__(self, massive_client):
        self._client = massive_client

    def discover(self, source, tickers, universe_symbols, use_market_scan) -> Discovery:
        effective = 'market' if source == 'movers' and use_market_scan else source
        return Discovery(self._build_candidates(self._discover(effective, tickers, universe_symbols)))

    def indicators(self, tickers, needed):
        return self._fetch_indicators(tickers, needed)

    def names(self, tickers):
        return self._fetch_names(tickers)

    def fundamentals(self, tickers):
        return self._fetch_fundamentals(tickers)

    def news(self, tickers):
        return self._fetch_news(tickers)

    # --- moved unchanged from trader/tools/idea_scanner.py IdeaScanner ---
```

Then **cut** these members out of `IdeaScanner` in `trader/tools/idea_scanner.py` and **paste them unchanged** below that marker: `_discover`, `_discover_raw`, `_build_candidates`, `_fetch_indicators`, `_FUNDAMENTAL_FIELDS`, `_fetch_names`, `_fetch_fundamentals`, `_fetch_news` (today ≈L631–980). Keep their docstrings and comments. Fix only imports the pasted code needs (`ThreadPoolExecutor`, `as_completed`, `logger`, typing names) — no logic edits.

3. In `trader/tools/idea_scanner.py`, rewrite `IdeaScanner`:

```python
class IdeaScanner:
    """Discover → Enrich → Filter → Score → Rank pipeline over one scan source."""

    def __init__(self, source):
        self.source = source

    def scan(
        self,
        preset: str = 'momentum',
        source: str = 'movers',
        tickers: Optional[List[str]] = None,
        universe_symbols: Optional[List[str]] = None,
        top_n: int = 15,
        custom_filters: Optional[Dict[str, Any]] = None,
        fundamentals: bool = False,
        news: bool = False,
        names: bool = False,
    ) -> pd.DataFrame:
        scan_preset = PRESETS.get(preset)
        if not scan_preset:
            raise ValueError(f'Unknown preset: {preset}. Available: {", ".join(PRESETS.keys())}')
        filters = merge_filters(scan_preset, custom_filters)

        discovery = self.source.discover(source, tickers, universe_symbols, scan_preset.use_market_scan)
        candidates = self._apply_filters(list(discovery.candidates), filters) if discovery.candidates else []
        if not candidates:
            return self._labelled(pd.DataFrame(), discovery.notice)

        score_fn = _SCORE_FUNCTIONS[scan_preset.score_fn]
        for c in candidates:
            c['_pre_score'], _ = score_fn(c)
        indicator_cap = max(top_n * 3, 30)
        if len(candidates) > indicator_cap:
            candidates.sort(key=lambda c: c['_pre_score'], reverse=True)
            candidates = candidates[:indicator_cap]

        indicators = self.source.indicators([c['ticker'] for c in candidates], scan_preset.indicators)
        for c in candidates:
            c.update(indicators.get(c['ticker'], {}))
            score, signal = score_fn(c)
            c['score'] = round(score, 1)
            c['signal'] = signal
            c.pop('_pre_score', None)

        candidates.sort(key=lambda c: c['score'], reverse=True)
        candidates = candidates[:top_n]
        top = [c['ticker'] for c in candidates]

        if names and candidates:
            name_data = self.source.names(top)
            for c in candidates:
                c['name'] = name_data.get(c['ticker']) or c.get('name', '')
        if fundamentals and candidates:
            fund_data = self.source.fundamentals(top)
            for c in candidates:
                c.update(fund_data.get(c['ticker'], {}))
        if news and candidates:
            news_data = self.source.news(top)
            for c in candidates:
                c.update(news_data.get(c['ticker'], {}))

        return self._labelled(self._to_dataframe(candidates, fundamentals=fundamentals, news=news),
                              discovery.notice)

    def _labelled(self, frame: pd.DataFrame, notice: str) -> pd.DataFrame:
        frame.attrs['ideas_provider'] = self.source.name
        if notice:
            frame.attrs['ideas_notice'] = notice
        return frame
```

Keep the existing `_apply_filters`, `_score_*` and `_to_dataframe` methods of `IdeaScanner` unchanged. Compare the new `scan` with the old Massive `scan` and the TwelveData `scan` line by line: the order of steps, the cap formula and the re-score must match; the only intentional differences are (a) discovery goes through the source, (b) names keep a provider-supplied `name` when the name lookup has none (TwelveData behaviour), (c) the provider/notice attrs. If you find any other difference, keep the old behaviour and report it.

4. Construction sites: in `trader/sdk.py` `scan_ideas` replace `IdeaScanner(self._massive_client)` with `IdeaScanner(MassiveScanSource(self._massive_client))` (import `from trader.data_providers.massive.scan import MassiveScanSource` locally next to the existing import). Same in `trader/tools/massive_research.py` (`IdeaScanner(self._client)` → `IdeaScanner(MassiveScanSource(self._client))`, module-level import).

5. Migrate `tests/test_idea_scanner.py` (Massive section only). Find the boundary first: `grep -n "class TestIBIdeaScannerBuildCandidates" tests/test_idea_scanner.py` (≈1062). Then, with `N` = that line minus 1:

```bash
F=tests/test_idea_scanner.py; N=$(( $(grep -n "class TestIBIdeaScannerBuildCandidates" $F | cut -d: -f1) - 1 ))
sed -i '' -E "1,${N}s/scanner\._(discover|discover_raw|build_candidates|fetch_indicators|fetch_names|fetch_fundamentals|fetch_news)\(/scanner.source._\1(/g" $F
sed -i '' -E "1,${N}s/scanner\._FUNDAMENTAL_FIELDS/scanner.source._FUNDAMENTAL_FIELDS/g" $F
```

and change the fixture and the one direct construction:

```python
@pytest.fixture
def scanner(mock_client):
    return IdeaScanner(MassiveScanSource(mock_client))
```
```python
        scanner = IdeaScanner(MassiveScanSource(MagicMock()))
```

Add `from trader.data_providers.massive.scan import MassiveScanSource` to the test imports. Fix any `patch.object(IdeaScanner, '_fetch_…')` / `patch('trader.tools.idea_scanner.IdeaScanner._fetch_…')` targets in that section to `MassiveScanSource` (grep for them). Do not touch the IB section.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_scan_pipeline.py tests/test_idea_scanner.py tests/test_massive_research.py tests/test_scan_ideas_query.py -q -p no:cacheprovider`
Expected: all pass. The Massive pipeline tests (`TestScanPipeline`, `TestFundamentals`, `TestNews`) passing unchanged is the behaviour-preservation proof.

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner add trader tests
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner commit -m "refactor(scanner): shared IdeaScanner pipeline over a Massive scan source

Massive discovery/indicator/enrichment code moves unchanged into
MassiveScanSource; IdeaScanner keeps one pipeline.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `TwelveDataScanSource` (moved); remove `TwelveDataIdeaScanner`

**Files:**
- Create: `trader/data_providers/twelvedata/scan.py`
- Modify: `trader/tools/idea_scanner.py` (delete class `TwelveDataIdeaScanner` after moving its members), `trader/sdk.py` (two construction sites in `scan_ideas`: the explicit TwelveData path ≈L4066 and the Massive→TwelveData fallback ≈L4117), `trader/tools/massive_research.py` (`TwelveDataIdeaScanner(self._td_client).scan(` ≈L147)
- Modify tests: `tests/test_twelvedata_sdk.py`
- Test: add to `tests/data_providers/test_scan_pipeline.py`

**Interfaces:**
- Consumes: `Discovery`, `IdeaScanner(source)`.
- Produces: `TwelveDataScanSource(td_client)` with `name = 'twelvedata'`, the moved members `_TD_FUNDAMENTAL_FIELDS`, `_discover(source, tickers, universe_symbols, scan_preset)`, `_batch_quote`, `_build_candidates`, `_fetch_indicators`, `_fetch_fundamentals` (bodies unchanged), and the protocol methods.

- [ ] **Step 1: Write the failing test** (append to `tests/data_providers/test_scan_pipeline.py`)

```python
def test_twelvedata_source_keeps_movers_fallback_notice(monkeypatch):
    from trader.data_providers.twelvedata.scan import TwelveDataScanSource
    from trader.tools.idea_scanner import IdeaScannerError, LIQUID_US_FALLBACK_TICKERS

    source = TwelveDataScanSource(td_client=None)
    entitlement = IdeaScannerError('TwelveData movers discovery failed for all directions: 403 Pro or Ultra plan')
    monkeypatch.setattr(source, '_discover', lambda *a, **k: (_ for _ in ()).throw(entitlement))
    seen = {}

    def fake_batch(symbols):
        seen['symbols'] = list(symbols)
        return [{'symbol': 'AAPL', 'close': '10', 'open': '9', 'high': '11', 'low': '9',
                 'previous_close': '9', 'volume': '1000', 'average_volume': '500', 'percent_change': '11'}]

    monkeypatch.setattr(source, '_batch_quote', fake_batch)
    discovery = source.discover('movers', None, None, False)
    assert seen['symbols'] == list(LIQUID_US_FALLBACK_TICKERS)
    assert discovery.candidates[0]['ticker'] == 'AAPL'
    assert discovery.notice


def test_twelvedata_source_news_and_names_are_empty():
    from trader.data_providers.twelvedata.scan import TwelveDataScanSource
    source = TwelveDataScanSource(td_client=None)
    assert source.news(['AAPL']) == {} and source.names(['AAPL']) == {}
```

(Before writing the first test, read today's `TwelveDataIdeaScanner.scan` entitlement branch — it checks `is_data_entitlement_error(ex)` on the `IdeaScannerError`; make the fake error message one that check accepts, e.g. containing "403" / "Pro or Ultra" as the real code path does. If the check uses a different phrase, use that phrase and say so in the report.)

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_scan_pipeline.py -v -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.twelvedata.scan'`

- [ ] **Step 3: Implement**

Create `trader/data_providers/twelvedata/scan.py`:

```python
"""TwelveData scan source: movers/quote discovery, local indicators from time_series, statistics."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

import pandas as pd

from trader.data_providers.capabilities import Discovery
from trader.tools.idea_scanner import (
    LIQUID_US_FALLBACK_TICKERS, PRESETS, IdeaScannerError, compute_ema, compute_rsi, compute_sma,
    entitlement_fallback_notice, is_data_entitlement_error,
)

logger = logging.getLogger(__name__)


class TwelveDataScanSource:
    name = 'twelvedata'

    def __init__(self, td_client):
        self._client = td_client

    def discover(self, source, tickers, universe_symbols, use_market_scan) -> Discovery:
        scan_preset = next(p for p in PRESETS.values() if p.use_market_scan == use_market_scan)
        notice = ''
        try:
            quotes = self._discover(source, tickers, universe_symbols, scan_preset)
        except IdeaScannerError as ex:
            if source in ('tickers', 'universe') or not is_data_entitlement_error(ex):
                raise
            notice = entitlement_fallback_notice('twelvedata', str(ex))
            logger.warning(notice)
            quotes = self._batch_quote(list(LIQUID_US_FALLBACK_TICKERS))
        return Discovery(self._build_candidates(quotes) if quotes else [], notice)

    def indicators(self, tickers, needed):
        return self._fetch_indicators(tickers, needed)

    def names(self, tickers):
        return {}  # names come with the quote payload (candidate['name'])

    def fundamentals(self, tickers):
        return self._fetch_fundamentals(tickers)

    def news(self, tickers):
        return {}  # TwelveData has no news endpoint

    # --- moved unchanged from trader/tools/idea_scanner.py TwelveDataIdeaScanner ---
```

Check whether today's `TwelveDataIdeaScanner._discover` actually uses its `scan_preset` argument. If it does not, pass `None` instead of looking a preset up (and drop the `PRESETS` import); if it does, the lookup above keeps the same preset kind. Say which in the report.

Then cut `_TD_FUNDAMENTAL_FIELDS`, `_discover`, `_batch_quote`, `_build_candidates`, `_fetch_indicators`, `_fetch_fundamentals` (and any private helper they call) out of `TwelveDataIdeaScanner`, paste them unchanged below the marker, and delete the now-empty `TwelveDataIdeaScanner` class (its `scan` is replaced by the shared pipeline; its notice behaviour now comes from `discover`).

Construction sites:
- `trader/sdk.py` explicit TwelveData path: `scanner = IdeaScanner(TwelveDataScanSource(self._twelvedata_client))` with the same `scan(...)` arguments as today.
- `trader/sdk.py` fallback: `td = IdeaScanner(TwelveDataScanSource(self._twelvedata_client))` (rest unchanged).
- `trader/tools/massive_research.py`: `IdeaScanner(TwelveDataScanSource(self._td_client)).scan(` (rest unchanged); fix the imports (`TwelveDataIdeaScanner` no longer exists).

Tests: in `tests/test_twelvedata_sdk.py` replace the class name everywhere and the import:

```bash
F=tests/test_twelvedata_sdk.py
sed -i '' 's/TwelveDataIdeaScanner/TwelveDataScanSource/g' $F
sed -i '' 's/^from trader.tools.idea_scanner import TwelveDataScanSource, /from trader.data_providers.twelvedata.scan import TwelveDataScanSource\nfrom trader.tools.idea_scanner import /' $F
```

Check the resulting import lines by eye. `grep -rn "TwelveDataIdeaScanner" trader web skills tests docs/superpowers/specs` must print nothing outside historical plan files.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_scan_pipeline.py tests/test_twelvedata_sdk.py tests/test_idea_scanner.py tests/test_massive_research.py tests/test_scan_ideas_query.py -q -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner add trader tests
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner commit -m "refactor(scanner): TwelveData scan source replaces TwelveDataIdeaScanner

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `IDEAS` registry capability; `scan_ideas` and CLI through the registry (default Massive)

**Files:**
- Modify: `trader/data_providers/builtin.py`, `trader/sdk.py` (`scan_ideas`), `trader/mmr_cli.py` (ideas parser ≈L1223, `_handle_ideas` ≈L10709)
- Test: `tests/data_providers/test_ideas_registry.py`

**Interfaces:**
- Consumes: `MassiveScanSource`, `TwelveDataScanSource`, `_massive_rest_client(config)`.
- Produces: builders `_massive_ideas(config)`, `_twelvedata_ideas(config)`; `BUILTIN_DEFAULTS[Capability.IDEAS] = 'massive'` (Task 6 switches it); `MMR.scan_ideas(..., data_source: Optional[str] = None)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/data_providers/test_ideas_registry.py
from argparse import Namespace
from unittest.mock import MagicMock

import pandas as pd

from trader.data_providers.capabilities import Capability
from trader.data_providers.registry import ProviderRegistry


def test_ideas_sources_registered():
    from trader.data_providers.builtin import source_choices
    assert {'massive', 'twelvedata'} <= set(source_choices(Capability.IDEAS))


def test_massive_ideas_builder():
    from trader.data_providers.massive.scan import MassiveScanSource
    registry = ProviderRegistry.from_config({'massive_api_key': 'k'})
    assert isinstance(registry.get(Capability.IDEAS, 'massive'), MassiveScanSource)


def test_ideas_do_not_inherit_default_data_source():
    registry = ProviderRegistry.from_config({'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.IDEAS) != 'twelvedata'


def test_scan_ideas_uses_registry_source():
    from trader.sdk import MMR
    from trader.data_providers.capabilities import Discovery
    mmr = object.__new__(MMR)

    class Source:
        name = 'fake'

        def discover(self, *a):
            return Discovery([{'ticker': 'AAA', 'price': 50.0, 'change_pct': 6.0, 'volume': 2_000_000,
                               'gap_pct': 1.0, 'rel_vol': 2.0, 'range_pct': 3.0, 'spread_pct': 0.05,
                               'vwap': 50.0}])

        def indicators(self, tickers, needed):
            return {}

        def names(self, t):
            return {}

        def fundamentals(self, t):
            return {}

        def news(self, t):
            return {}

    mmr._provider = MagicMock(return_value=Source())
    mmr._container = MagicMock()
    df = mmr.scan_ideas(preset='momentum', data_source='fake-source')
    mmr._provider.assert_called_once_with(Capability.IDEAS, 'fake-source')
    assert df.attrs['ideas_provider'] == 'fake'


def test_cli_ideas_source_from_registry_and_default_none():
    from trader.mmr_cli import build_parser
    parser = build_parser()
    assert parser.parse_args(['ideas']).source is None
    assert parser.parse_args(['ideas', '--source', 'twelvedata']).source == 'twelvedata'


def test_cli_ideas_prints_provider_error(capsys):
    from trader.data_providers.errors import ProviderNotConfigured
    from trader.mmr_cli import _handle_ideas
    mmr = MagicMock()
    mmr.scan_ideas.side_effect = ProviderNotConfigured('alpaca', [('alpaca_api_key_id', 'ALPACA_API_KEY_ID')])
    args = Namespace(presets=False, tickers=None, universe=None, fundamentals=False, detail=False, news=False,
                     source=None, location=None, preset='momentum', num=15, min_price=None, max_price=None,
                     min_volume=None, min_change=None, max_change=None, news_bodies=False, news_bodies_limit=3)
    _handle_ideas(mmr, args)
    assert 'ALPACA_API_KEY_ID' in capsys.readouterr().out
```

(Check the real attribute names `_handle_ideas` reads from `args` — copy them into the Namespace; if one is missing the test will tell you.)

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_ideas_registry.py -v -p no:cacheprovider`
Expected: FAIL (no IDEAS builders / `scan_ideas` signature / CLI default `'massive'`).

- [ ] **Step 3: Implement**

`builtin.py`:

```python
def _massive_ideas(config: Mapping[str, Any]):
    from trader.data_providers.massive.scan import MassiveScanSource
    return MassiveScanSource(_massive_rest_client(config))


def _twelvedata_ideas(config: Mapping[str, Any]):
    from twelvedata import TDClient
    from trader.data_providers.twelvedata.scan import TwelveDataScanSource
    return TwelveDataScanSource(TDClient(apikey=config['twelvedata_api_key']))
```

Register `Capability.IDEAS` in the `massive` and `twelvedata` specs; `BUILTIN_DEFAULTS[Capability.IDEAS] = 'massive'`.

`trader/sdk.py` `scan_ideas`: signature `data_source: Optional[str] = None`. After the IB `location` branch and universe resolution, replace the TwelveData-path block and the Massive `scanner = IdeaScanner(...)` construction with:

```python
        from trader.data_providers import Capability
        resolved = data_source or self._provider_default(Capability.IDEAS)
        scan_source = self._provider(Capability.IDEAS, resolved)
        scan_kwargs = dict(preset=preset, source=source, tickers=tickers, universe_symbols=universe_symbols,
                           top_n=top_n, custom_filters=custom_filters or None, fundamentals=fundamentals,
                           news=news, names=names)
        if resolved != 'massive':
            return IdeaScanner(scan_source).scan(**scan_kwargs)
        try:
            return IdeaScanner(scan_source).scan(**scan_kwargs)
        except Exception as ex:
            # unchanged Massive -> TwelveData entitlement fallback (removed in phase 3c)
```

keeping the existing `except` body verbatim (it builds `IdeaScanner(TwelveDataScanSource(self._twelvedata_client))`). Note `_provider` is called with the resolved name (tests assert the explicit-name call; for `data_source=None` the call is `_provider(Capability.IDEAS, '<default>')`). Update the docstring's `data_source` paragraph.

`trader/mmr_cli.py`:
- ideas parser: replace the `--source` argument with

```python
    ideas_p.add_argument('--source', choices=source_choices(Capability.IDEAS), default=None,
                         help='Data source for US equities (default: data_providers.ideas, else the '
                              'builtin default). Ignored when --location is set (IB path).')
```

  (remove the stale "Massive-first … do NOT inherit" comment above it; `source_choices`/`Capability` are already imported in `build_parser` by phase 3a — reuse them).
- `_handle_ideas`: `data_source = getattr(args, 'source', None)`; keep the TwelveData news notice under `if data_source == 'twelvedata'`; `source_label = f' — {data_source}' if data_source and not args.location else ''`; wrap the `mmr.scan_ideas(...)` call and the code that prints its result in `try: … except ProviderError as ex: print_status(str(ex), success=False); return` (import `ProviderError` from `trader.data_providers` inside the function). If `scan_ideas` returns a frame with `attrs['ideas_provider']`, prefer it for the title suffix when `--source` was omitted.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers tests/test_idea_scanner.py tests/test_twelvedata_sdk.py tests/test_massive_research.py tests/test_scan_ideas_query.py tests/test_sdk.py -q -p no:cacheprovider`
Expected: all pass. Update only tests that assert the old **default** ideas source string (`grep -rn "data_source='massive'\|source='massive'" tests/ | grep -i idea`), listing each in the report.

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner add trader tests
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner commit -m "refactor(scanner): route ideas through the registry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `AlpacaScanSource` discovery and candidates (delayed SIP)

**Files:**
- Create: `trader/data_providers/alpaca/scan.py`, fixtures `tests/data_providers/fixtures/alpaca_snapshots_delayed_sip_aapl.json`, `tests/data_providers/fixtures/alpaca_most_actives.json`
- Test: `tests/data_providers/test_alpaca_scan.py`

**Interfaces:**
- Consumes: `AlpacaClient.get_json`, `to_alpaca_symbol`, `number_or_nan` (`trader/data_providers/alpaca/_numbers.py`), `AlpacaAssetDirectory` (`.is_derivative_unit`, `.name`), existing fixture `tests/data_providers/fixtures/alpaca_movers_stocks.json`.
- Produces: `AlpacaScanSource(client, assets=None, history=None, news=None)` with `name = 'alpaca'`; `discover(...)`; constants `SNAPSHOTS_PATH = '/v2/stocks/snapshots'`, `FEED = 'delayed_sip'`, `SCREENER_TOP = 50`, `SNAPSHOT_CHUNK = 100`; module function `candidate_from_snapshot(ticker, snapshot) -> Optional[dict]`.

- [ ] **Step 1: Create fixtures** (real responses captured 2026-10-04)

`alpaca_snapshots_delayed_sip_aapl.json`:
```json
{"AAPL": {"dailyBar": {"c": 333.69, "h": 334.54, "l": 330.61, "n": 628339, "o": 333.26, "t": "2026-10-02T04:00:00Z", "v": 34261610, "vw": 333.164889}, "latestQuote": {"ap": 333.69, "as": 560, "ax": "P", "bp": 333.35, "bs": 240, "bx": "P", "c": ["R"], "t": "2026-10-02T23:59:59.039401665Z", "z": "C"}, "latestTrade": {"c": ["@", "T"], "i": 56935, "p": 333.6, "s": 120, "t": "2026-10-02T23:59:13.758751989Z", "x": "P", "z": "C"}, "minuteBar": {"c": 333.6, "h": 333.6, "l": 333.37, "n": 47, "o": 333.37, "t": "2026-10-02T23:59:00Z", "v": 2093, "vw": 333.480458}, "prevDailyBar": {"c": 330.32, "h": 332.4816, "l": 325.81, "n": 707944, "o": 330, "t": "2026-10-01T04:00:00Z", "v": 36472464, "vw": 329.613744}}}
```

`alpaca_most_actives.json`:
```json
{"most_actives": [{"symbol": "NIVF", "trade_count": 289262, "volume": 303219873}, {"symbol": "FNGR", "trade_count": 203621, "volume": 253381339}, {"symbol": "AMOD", "trade_count": 1084761, "volume": 194430485}], "last_updated": "2026-10-02T23:59:00.151438156Z"}
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/data_providers/test_alpaca_scan.py
import json
from pathlib import Path

import pytest

from trader.data_providers.alpaca.scan import AlpacaScanSource, candidate_from_snapshot

FIX = Path(__file__).parent / 'fixtures'
SNAP = json.loads((FIX / 'alpaca_snapshots_delayed_sip_aapl.json').read_text())
MOVERS = json.loads((FIX / 'alpaca_movers_stocks.json').read_text())
ACTIVES = json.loads((FIX / 'alpaca_most_actives.json').read_text())


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get_json(self, path, params):
        self.calls.append((path, dict(params)))
        return self.routes(path, params)


class FakeAssets:
    def is_derivative_unit(self, symbol):
        return symbol.endswith('W')

    def name(self, symbol):
        return {'AAPL': 'Apple Inc. Common Stock'}.get(symbol, '')


def _routes(path, params):
    if path.endswith('/movers'):
        return MOVERS
    if path.endswith('/most-actives'):
        return ACTIVES
    if path == '/v2/stocks/snapshots':
        return {s: SNAP['AAPL'] for s in params['symbols'].split(',') if s in ('AAPL', 'AMOD', 'MN', 'SDEV')}
    raise AssertionError(path)


def test_candidate_uses_consolidated_volume_and_vwap():
    c = candidate_from_snapshot('AAPL', SNAP['AAPL'])
    assert c['price'] == 333.69 and c['volume'] == 34261610 and c['vwap'] == 333.16
    assert c['change_pct'] == pytest.approx(round((333.69 - 330.32) / 330.32 * 100, 2))
    assert c['gap_pct'] == pytest.approx(round((333.26 - 330.32) / 330.32 * 100, 2))
    assert c['rel_vol'] == pytest.approx(round(34261610 / 36472464, 2))
    assert c['range_pct'] == pytest.approx(round((334.54 - 330.61) / 330.61 * 100, 2))
    assert c['spread_pct'] == pytest.approx(round((333.69 - 333.35) / 333.69 * 100, 3))


def test_candidate_skips_missing_daily_bar_or_price():
    assert candidate_from_snapshot('X', {}) is None
    assert candidate_from_snapshot('X', {'dailyBar': {'c': None}}) is None


def test_tickers_path_requests_delayed_sip_snapshots():
    client = FakeClient(_routes)
    discovery = AlpacaScanSource(client).discover('tickers', ['aapl'], None, False)
    assert client.calls == [('/v2/stocks/snapshots', {'symbols': 'AAPL', 'feed': 'delayed_sip'})]
    assert [c['ticker'] for c in discovery.candidates] == ['AAPL']
    assert '15-minute delayed' in discovery.notice


def test_unknown_tickers_are_named_in_notice():
    discovery = AlpacaScanSource(FakeClient(_routes)).discover('tickers', ['AAPL', 'ZZZZQ'], None, False)
    assert [c['ticker'] for c in discovery.candidates] == ['AAPL']
    assert 'ZZZZQ' in discovery.notice


def test_invalid_ticker_is_named_without_request():
    client = FakeClient(_routes)
    discovery = AlpacaScanSource(client).discover('tickers', ['AAPL;DROP'], None, False)
    assert discovery.candidates == [] and 'AAPL;DROP' in discovery.notice
    assert client.calls == []


def test_market_scan_preset_uses_movers_and_actives_with_notice():
    client = FakeClient(_routes)
    discovery = AlpacaScanSource(client, assets=FakeAssets()).discover('movers', None, None, True)
    paths = [p for p, _ in client.calls]
    assert '/v1beta1/screener/stocks/movers' in paths and '/v1beta1/screener/stocks/most-actives' in paths
    requested = client.calls[-1][1]['symbols'].split(',')
    assert 'HPAIW' not in requested                    # derivative units never requested
    assert len(requested) == len(set(requested))       # union, no duplicates
    assert 'not the full market' in discovery.notice
    assert f'{len(requested)} symbols' in discovery.notice


def test_universe_path_uses_given_symbols():
    client = FakeClient(_routes)
    AlpacaScanSource(client).discover('universe', None, ['AAPL', 'MN'], False)
    assert client.calls[0][1]['symbols'] == 'AAPL,MN'
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_alpaca_scan.py -v -p no:cacheprovider`
Expected: FAIL with `ModuleNotFoundError: No module named 'trader.data_providers.alpaca.scan'`

- [ ] **Step 4: Implement**

```python
# trader/data_providers/alpaca/scan.py
"""Alpaca scan source for the shared idea scanner.

Uses the free plan's 15-minute-delayed consolidated feed (delayed_sip), so volume
and spread are market-wide, not IEX-only. Discovery cannot scan the whole market:
it uses the screener's top movers and most-actives and says so in the notice.
"""

import logging
from typing import Iterable, Optional

from trader.data_providers.alpaca._numbers import number_or_nan
from trader.data_providers.capabilities import Discovery
from trader.data_providers.symbols import to_alpaca_symbol

logger = logging.getLogger(__name__)

SNAPSHOTS_PATH = '/v2/stocks/snapshots'
FEED = 'delayed_sip'
SCREENER_TOP = 50
SNAPSHOT_CHUNK = 100
DELAY_NOTE = 'Alpaca prices are 15-minute delayed (consolidated SIP)'


def _looks_like_warrant(symbol: str) -> bool:
    # Same suffix rule as the Massive scanner: a trailing W only on 5+ char tickers.
    return symbol.endswith(('.WS', 'WS', '.U', '.R')) or (len(symbol) >= 5 and symbol.endswith('W'))


def candidate_from_snapshot(ticker: str, snapshot: dict) -> Optional[dict]:
    day = (snapshot or {}).get('dailyBar') or {}
    prev = (snapshot or {}).get('prevDailyBar') or {}
    quote = (snapshot or {}).get('latestQuote') or {}
    price = number_or_nan(day, 'c')
    if not price or price != price or price <= 0:
        return None
    prev_close = number_or_nan(prev, 'c')
    prev_volume = number_or_nan(prev, 'v')
    volume = number_or_nan(day, 'v')
    day_open, high, low = number_or_nan(day, 'o'), number_or_nan(day, 'h'), number_or_nan(day, 'l')
    bid, ask = number_or_nan(quote, 'bp'), number_or_nan(quote, 'ap')

    def pct(numerator, base):
        return (numerator / base) * 100.0 if base and base == base and base > 0 else 0.0

    return {
        'ticker': ticker,
        'price': round(price, 2),
        'change_pct': round(pct(price - prev_close, prev_close), 2),
        'volume': int(volume) if volume == volume else 0,
        'gap_pct': round(pct(day_open - prev_close, prev_close), 2),
        'rel_vol': round(volume / prev_volume, 2) if prev_volume and prev_volume == prev_volume else 0.0,
        'range_pct': round(pct(high - low, low), 2),
        'spread_pct': round(pct(ask - bid, price), 3) if bid > 0 and ask > 0 else 0.0,
        'vwap': round(number_or_nan(day, 'vw'), 2) if number_or_nan(day, 'vw') == number_or_nan(day, 'vw') else 0.0,
    }


class AlpacaScanSource:
    name = 'alpaca'

    def __init__(self, client, assets=None, history=None, news=None):
        self._client = client
        self._assets = assets
        self._history = history
        self._news = news

    def discover(self, source, tickers, universe_symbols, use_market_scan) -> Discovery:
        if source == 'tickers' and tickers:
            return self._snapshot_discovery(tickers, scope='requested tickers')
        if source == 'universe' and universe_symbols:
            return self._snapshot_discovery(universe_symbols, scope='universe')
        symbols = self._screener_symbols()
        discovery = self._snapshot_discovery(symbols, scope='top movers + most-actives')
        notice = (f'Alpaca discovery: {len(symbols)} symbols from top movers + most-actives '
                  f'(not the full market). {DELAY_NOTE}.')
        return Discovery(discovery.candidates, notice)

    def _screener_symbols(self) -> list:
        movers = self._client.get_json('/v1beta1/screener/stocks/movers', {'top': SCREENER_TOP})
        actives = self._client.get_json('/v1beta1/screener/stocks/most-actives',
                                        {'by': 'volume', 'top': SCREENER_TOP})
        rows = (movers.get('gainers') or []) + (movers.get('losers') or []) + (actives.get('most_actives') or [])
        seen, symbols = set(), []
        for row in rows:
            symbol = (row.get('symbol') or '').upper()
            if not symbol or symbol in seen or self._is_excluded(symbol):
                continue
            seen.add(symbol)
            symbols.append(symbol)
        return symbols

    def _is_excluded(self, symbol: str) -> bool:
        if _looks_like_warrant(symbol):
            return True
        return bool(self._assets is not None and self._assets.is_derivative_unit(symbol))

    def _snapshot_discovery(self, symbols: Iterable[str], scope: str) -> Discovery:
        requested, invalid = [], []
        for symbol in symbols:
            try:
                requested.append(to_alpaca_symbol(symbol))
            except ValueError:
                invalid.append(symbol)
        candidates, missing = [], []
        for start in range(0, len(requested), SNAPSHOT_CHUNK):
            chunk = requested[start:start + SNAPSHOT_CHUNK]
            snapshots = self._client.get_json(SNAPSHOTS_PATH, {'symbols': ','.join(chunk), 'feed': FEED})
            for symbol in chunk:
                candidate = candidate_from_snapshot(symbol, snapshots.get(symbol))
                if candidate is None:
                    missing.append(symbol)
                else:
                    candidates.append(candidate)
        parts = [f'{DELAY_NOTE}.']
        if invalid:
            parts.append(f'Not valid Alpaca symbols: {", ".join(invalid)}.')
        if missing:
            parts.append(f'No Alpaca snapshot for: {", ".join(missing)}.')
        return Discovery(candidates, ' '.join(parts))

    # Enrichment arrives in Task 5.
    def indicators(self, tickers, needed):
        return {}

    def names(self, tickers):
        return {}

    def fundamentals(self, tickers):
        return {}

    def news(self, tickers):
        return {}
```

(`vwap` uses `number_or_nan(day, 'vw')` twice; tidy it into one local variable while you are there — the test only checks the value.)

- [ ] **Step 5: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_alpaca_scan.py tests/data_providers/test_scan_pipeline.py -v -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner add trader tests
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner commit -m "feat(scanner): Alpaca scan discovery on delayed consolidated snapshots

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: `AlpacaScanSource` enrichment — indicators, names, news, fundamentals

**Files:**
- Modify: `trader/data_providers/alpaca/scan.py`
- Test: append to `tests/data_providers/test_alpaca_scan.py`

**Interfaces:**
- Consumes: a `HistoryProvider` (`get_history(ticker, BarSize.Days1, start, end)` → frame with `close`), a `NewsProvider` (`news(ticker, 1)` → `make_news_item` dicts), `compute_rsi/ema/sma` from `trader.tools.idea_scanner`, `IdeaScannerError`.
- Produces: `AlpacaScanSource.indicators/names/news/fundamentals` real implementations; constant `INDICATOR_LOOKBACK_DAYS = 120`.

- [ ] **Step 1: Write the failing tests** (append)

```python
import datetime as dt

import pandas as pd

from trader.data_providers.capabilities import make_news_item
from trader.tools.idea_scanner import IdeaScannerError, compute_ema, compute_rsi, compute_sma


class FakeHistory:
    def __init__(self, closes):
        self.closes = closes
        self.calls = []

    def get_history(self, ticker, bar_size, start_date, end_date, timezone='US/Eastern'):
        self.calls.append((ticker, str(bar_size)))
        index = pd.date_range('2026-05-01', periods=len(self.closes), freq='B', tz='US/Eastern', name='date')
        return pd.DataFrame({'close': self.closes}, index=index)


class FakeNews:
    def news(self, ticker, limit):
        return [make_news_item(title=f'{ticker} headline', published='2026-10-02T11:00:00', tickers=[ticker])]


def test_indicators_computed_locally_from_daily_bars():
    closes = [100 + i * 0.5 for i in range(80)]
    history = FakeHistory(closes)
    source = AlpacaScanSource(FakeClient(_routes), history=history)
    out = source.indicators(['AAPL'], ['rsi', 'ema_9', 'sma_20', 'sma_50'])
    assert history.calls == [('AAPL', '1 day')]
    assert out['AAPL']['rsi'] == compute_rsi(closes, period=14)
    assert out['AAPL']['ema_9'] == compute_ema(closes, window=9)
    assert out['AAPL']['sma_20'] == compute_sma(closes, window=20)
    assert out['AAPL']['sma_50'] == compute_sma(closes, window=50)


def test_indicator_failure_for_one_ticker_leaves_it_empty():
    class Broken:
        def get_history(self, *a, **k):
            raise RuntimeError('boom')

    out = AlpacaScanSource(FakeClient(_routes), history=Broken()).indicators(['AAPL'], ['rsi'])
    assert out == {'AAPL': {}}


def test_names_from_asset_list():
    assert AlpacaScanSource(FakeClient(_routes), assets=FakeAssets()).names(['AAPL', 'ZZZ']) == {
        'AAPL': 'Apple Inc. Common Stock'}


def test_news_headline_without_sentiment():
    out = AlpacaScanSource(FakeClient(_routes), news=FakeNews()).news(['AAPL'])
    assert out['AAPL'] == {'headline': 'AAPL headline', 'news_date': '2026-10-02', 'sentiment': '', 'catalyst': ''}


def test_fundamentals_raise_until_phase_4():
    with pytest.raises(IdeaScannerError, match='--source massive'):
        AlpacaScanSource(FakeClient(_routes)).fundamentals(['AAPL'])


def test_missing_history_or_news_provider_fails_loudly():
    source = AlpacaScanSource(FakeClient(_routes))
    with pytest.raises(IdeaScannerError):
        source.indicators(['AAPL'], ['rsi'])
    with pytest.raises(IdeaScannerError):
        source.news(['AAPL'])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_alpaca_scan.py -v -p no:cacheprovider`
Expected: the six new tests FAIL (stubs return `{}`).

- [ ] **Step 3: Implement** — replace the four stub methods:

```python
    def indicators(self, tickers, needed):
        if not needed or not tickers:
            return {}
        if self._history is None:
            raise IdeaScannerError('Alpaca scan needs a history provider for indicators')
        end = dt.datetime.now()
        start = end - dt.timedelta(days=INDICATOR_LOOKBACK_DAYS)

        def fetch_one(ticker):
            try:
                frame = self._history.get_history(ticker, BarSize.Days1, start, end)
                closes = [float(c) for c in frame['close'].dropna()] if not frame.empty else []
            except Exception as ex:
                logger.warning('alpaca indicator history failed for %s: %s', ticker, ex)
                return ticker, {}
            return ticker, _indicator_values(closes, needed)

        with ThreadPoolExecutor(max_workers=5) as pool:
            return dict(pool.map(fetch_one, tickers))

    def names(self, tickers):
        if self._assets is None:
            return {}
        return {t: self._assets.name(t) for t in tickers if self._assets.name(t)}

    def fundamentals(self, tickers):
        raise IdeaScannerError(
            'ideas --fundamentals has no free source yet (Finnhub ratios arrive in phase 4); '
            'use --source massive or --source twelvedata for fundamentals'
        )

    def news(self, tickers):
        if self._news is None:
            raise IdeaScannerError('Alpaca scan needs a news provider for --news')
        out = {}
        for ticker in tickers:
            items = self._news.news(ticker, 1)
            if items:
                title = items[0]['title']
                out[ticker] = {
                    'headline': title if len(title) <= 120 else title[:117] + '...',
                    'news_date': (items[0]['published'] or '')[:10],
                    'sentiment': '',
                    'catalyst': '',
                }
        return out
```

with module-level additions:

```python
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

from trader.objects import BarSize
from trader.tools.idea_scanner import IdeaScannerError, compute_ema, compute_rsi, compute_sma

INDICATOR_LOOKBACK_DAYS = 120


def _indicator_values(closes: list, needed: list) -> dict:
    values = {}
    for indicator in needed:
        if indicator == 'rsi':
            values['rsi'] = compute_rsi(closes, period=14)
        elif indicator == 'ema_9':
            values['ema_9'] = compute_ema(closes, window=9)
        elif indicator == 'sma_20':
            values['sma_20'] = compute_sma(closes, window=20)
        elif indicator == 'sma_50':
            values['sma_50'] = compute_sma(closes, window=50)
    return values
```

News: a provider error for one ticker propagates (the user asked for `--news`; fail loudly). Indicators: a history error for one ticker is logged and leaves that ticker without indicators (same as the Massive/TwelveData scanners today).

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_alpaca_scan.py tests/data_providers/test_scan_pipeline.py -v -p no:cacheprovider`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner add trader tests
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner commit -m "feat(scanner): Alpaca scan enrichment (local indicators, names, news)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Register Alpaca ideas; switch the `ideas` default to Alpaca

**Files:**
- Modify: `trader/data_providers/builtin.py`, `trader/mmr_cli.py` (ideas parser help/epilog)
- Test: append to `tests/data_providers/test_ideas_registry.py`

**Interfaces:**
- Consumes: `AlpacaScanSource`, `_alpaca_client`, `alpaca_asset_directory`, `_alpaca_history`, `_alpaca_news` builders from `builtin.py`.
- Produces: builder `_alpaca_ideas(config)`; `BUILTIN_DEFAULTS[Capability.IDEAS] = 'alpaca'`.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_alpaca_ideas_builder_wires_assets_history_news():
    from trader.data_providers.alpaca.scan import AlpacaScanSource
    registry = ProviderRegistry.from_config({'alpaca_api_key_id': 'k', 'alpaca_api_secret_key': 's'})
    source = registry.get(Capability.IDEAS)
    assert isinstance(source, AlpacaScanSource)
    assert source._history is not None and source._news is not None and source._assets is not None


def test_ideas_default_ignores_default_data_source():
    registry = ProviderRegistry.from_config({'default_data_source': 'twelvedata'})
    assert registry.default_source(Capability.IDEAS) == 'alpaca'


def test_data_providers_override_keeps_massive():
    registry = ProviderRegistry.from_config({'data_providers': {'ideas': 'massive'}})
    assert registry.default_source(Capability.IDEAS) == 'massive'
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_ideas_registry.py -v -p no:cacheprovider`
Expected: the first two new tests FAIL.

- [ ] **Step 3: Implement**

```python
def _alpaca_ideas(config: Mapping[str, Any]):
    from trader.data_providers.alpaca.scan import AlpacaScanSource
    return AlpacaScanSource(
        _alpaca_client(config),
        assets=alpaca_asset_directory(config),
        history=_alpaca_history(config),
        news=_alpaca_news(config),
    )
```

(`alpaca_asset_directory` returns an unloaded directory; loading happens on first lookup — do not call `.load()` here, so building the source stays cheap.) Register `Capability.IDEAS: _alpaca_ideas` in the alpaca spec; set `BUILTIN_DEFAULTS[Capability.IDEAS] = 'alpaca'`. In `mmr_cli.py`, update the ideas epilog line `'  ideas  # Momentum scan (default, US/Massive)'` to `'(default, US/Alpaca, 15-min delayed)'` and add `'  ideas --source massive  # full-market Massive scan (paid plan)'`.

Asset-list failures during discovery: `AlpacaScanSource._is_excluded` calls `assets.is_derivative_unit`, which loads the list. Wrap that call so a `ProviderError` / `requests.RequestException` / `ValueError` / `TypeError` / `AttributeError` while loading logs one warning, sets `self._assets = None`, and adds `'warrant filter off: Alpaca asset list unavailable'` to the notice (same wording as the movers filter). Add a test: assets whose `is_derivative_unit` raises `ProviderEntitlementError` → discovery still returns candidates and the notice contains that sentence.

- [ ] **Step 4: Run tests**

Run: `/Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers tests/test_idea_scanner.py tests/test_twelvedata_sdk.py tests/test_massive_research.py tests/test_scan_ideas_query.py tests/test_sdk.py -q -p no:cacheprovider`
Expected: all pass (update only default-source assertions; list them).

- [ ] **Step 5: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner add trader tests
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner commit -m "feat(scanner): make Alpaca the default ideas source

ideas no longer defaults to Massive; it ignores default_data_source
(data_providers.ideas overrides). Massive stays via --source massive.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Live checks and docs

**Files:**
- Modify: `tests/data_providers/test_live_alpaca.py`, `CLAUDE.md` (Ideas Scanner Architecture section, design principle bullets, CLI examples), `docs/OPERATIONAL_STATE.md` (operator note), `docs/superpowers/plans/2026-10-04-free-data-providers-00-index.md` (3b done)

- [ ] **Step 1: Add live tests** (reuse the file's gating and `_registry()` helper)

```python
def test_live_ideas_alpaca_momentum():
    from trader.tools.idea_scanner import IdeaScanner
    source = _registry().get(Capability.IDEAS, 'alpaca')
    df = IdeaScanner(source).scan(preset='momentum', top_n=5)
    assert 'not the full market' in df.attrs.get('ideas_notice', '')
    assert df.attrs['ideas_provider'] == 'alpaca'
    if not df.empty:
        assert (df['volume'] > 0).all()


def test_live_ideas_alpaca_tickers():
    from trader.tools.idea_scanner import IdeaScanner
    df = IdeaScanner(_registry().get(Capability.IDEAS, 'alpaca')).scan(
        preset='momentum', source='tickers', tickers=['AAPL', 'MSFT', 'ZZZZQ'], top_n=5,
        custom_filters={'min_change_pct': -100, 'max_change_pct': 100})
    assert 'ZZZZQ' in df.attrs.get('ideas_notice', '')
```

Run (keys from `.env`, never printed), from the worktree root:

```bash
set -a; eval "$(grep -E '^ALPACA_API_(KEY_ID|SECRET_KEY)=' /Users/mudryy/private/mmr/.env)"; set +a
MMR_LIVE_TESTS=1 /Users/mudryy/private/mmr/.venv/bin/pytest tests/data_providers/test_live_alpaca.py -v -m live -p no:cacheprovider
```

Expected: all live tests pass; without the env var they skip. (If `custom_filters` keys differ from `min_change_pct`/`max_change_pct`, use the real `ScanFilter` field names.)

- [ ] **Step 2: Docs**

- `CLAUDE.md`:
  - "Ideas Scanner Architecture": three US scan sources behind one `IdeaScanner(source)` pipeline — `alpaca` (default; free; movers + most-actives discovery, ~100–150 symbols, not the full market; 15-minute-delayed consolidated prices/volume; local indicators; names from the asset list; news without sentiment; no fundamentals until phase 4), `massive` (`--source massive`; full-market snapshots, server indicators, ratios, news with sentiment; paid plan), `twelvedata` (`--source twelvedata`; quotes, local indicators, statistics; movers need Pro+). IB path (`--location`) unchanged.
  - Replace the "`ideas` still defaults to Massive until phase 3b" statements with the new default; mention `data_providers.ideas`.
  - CLI examples: `ideas` (Alpaca), `ideas --source massive`, `ideas momentum --tickers AAPL MSFT` (works on Alpaca), `ideas --fundamentals` needs `--source massive|twelvedata` until phase 4.
- `docs/OPERATIONAL_STATE.md`: from phase 3b, bare `ideas` uses Alpaca (needs the Alpaca keys, like `movers`/`news`); keep Massive with `data_providers: {ideas: massive}`; the pending weekday intraday-movers check also gates `ideas` discovery quality.
- Plan index: mark 3b `done`.

- [ ] **Step 3: Verify** — `grep -rn "TwelveDataIdeaScanner" trader web skills` → nothing.

- [ ] **Step 4: Commit**

```bash
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner add tests/data_providers/test_live_alpaca.py CLAUDE.md docs
git -C /Users/mudryy/private/mmr/.worktrees/fdp-3b-scanner commit -m "docs: document the merged idea scanner and Alpaca ideas default

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

"""Trading ideas scanner — discover, enrich, filter, score, and rank day-trading candidates.

``IdeaScanner`` runs one shared pipeline over a scan source picked through the
provider registry (``Capability.IDEAS``): Alpaca (default, free, delayed prices),
Massive or TwelveData. Each source lives in ``trader/data_providers/<provider>/scan.py``
and supplies discovery, indicators, names, fundamentals and news. No
trader_service needed — only the chosen provider's API key.

When ``--location`` is provided, ``IBIdeaScanner`` uses IB's scanner API +
``get_snapshot()`` + ``reqHistoricalData`` for international markets.
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Protocol

import pandas as pd

if TYPE_CHECKING:
    from trader.data_providers.capabilities import ScanSource

logger = logging.getLogger(__name__)


class IdeaScannerError(RuntimeError):
    """Raised when the idea scanner cannot complete a scan (IB/scanner API
    failure, no symbols resolve, every history fetch fails). Surfaces to the
    CLI with a descriptive message instead of silently returning an empty
    DataFrame, per the project's "fail loudly" principle."""


def is_data_entitlement_error(exc: BaseException) -> bool:
    """True when the provider rejected the call for plan/tier reasons.

    Massive Stocks Basic returns ``NOT_AUTHORIZED`` / "not entitled" on
    snapshot + movers endpoints (Starter+ required). TwelveData Basic/Starter
    returns HTTP 403 on ``/market_movers/*`` (Pro+ required). Quotes and
    history on those same keys often still work.
    """
    text = str(exc).lower()
    return (
        'not_authorized' in text
        or 'not entitled' in text
        or 'exclusively with pro' in text
        or 'consider upgrading' in text
        or ('403' in text and ('upgrade' in text or 'pricing' in text or 'plan' in text))
    )


# Liquid large-cap US names used when market-movers APIs aren't entitled.
# Kept ≤8 so a TwelveData Basic key (8 credits/min) can finish one scan
# without a 429 — each /quote symbol costs 1 credit on most plans.
LIQUID_US_FALLBACK_TICKERS: List[str] = [
    'AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'AMD',
]


def entitlement_fallback_notice(provider: str, detail: str = '') -> str:
    """Plain-English notice when we fall back off a movers endpoint."""
    detail = (detail or '').strip()
    suffix = f' ({detail[:160]})' if detail else ''
    if provider == 'massive':
        return (
            'Massive Stocks Basic does not include snapshot/movers APIs '
            '(Starter+ required); fell back to TwelveData quotes on a liquid '
            f'US ticker set{suffix}. Upgrade Massive or pass --tickers / '
            '--universe for a custom scan.'
        )
    return (
        'TwelveData /market_movers requires Pro+; fell back to quotes on a '
        f'liquid US ticker set{suffix}. Upgrade TwelveData or pass --tickers / '
        '--universe for a custom scan.'
    )


# ------------------------------------------------------------------
# Dataclasses
# ------------------------------------------------------------------

@dataclass
class ScanFilter:
    min_price: float = 1.0
    max_price: float = 10000.0
    min_volume: int = 100_000
    min_change_pct: Optional[float] = None
    max_change_pct: Optional[float] = None
    min_relative_volume: float = 0.0
    max_spread_pct: float = 5.0  # filter out illiquid instruments


@dataclass
class ScanPreset:
    name: str
    description: str
    filters: ScanFilter
    indicators: List[str]
    score_fn: str  # function name in _SCORE_FUNCTIONS
    use_market_scan: bool = False  # True = scan full market via snapshot_all


# ------------------------------------------------------------------
# Preset definitions
# ------------------------------------------------------------------

PRESETS: Dict[str, ScanPreset] = {
    'momentum': ScanPreset(
        name='momentum',
        description='High momentum stocks with strong volume and price action',
        filters=ScanFilter(min_price=5.0, min_volume=500_000, min_change_pct=2.0),
        indicators=['rsi', 'ema_9'],
        score_fn='_score_momentum',
    ),
    'gap-up': ScanPreset(
        name='gap-up',
        description='Stocks gapping up significantly at open',
        filters=ScanFilter(min_price=5.0, min_volume=300_000, min_change_pct=3.0),
        indicators=['rsi'],
        score_fn='_score_gap_up',
    ),
    'gap-down': ScanPreset(
        name='gap-down',
        description='Stocks gapping down — potential reversal or short candidates',
        filters=ScanFilter(min_price=5.0, min_volume=300_000, max_change_pct=-3.0),
        indicators=['rsi', 'sma_20'],
        score_fn='_score_gap_down',
        use_market_scan=True,
    ),
    'mean-reversion': ScanPreset(
        name='mean-reversion',
        description='Oversold stocks near support — bounce candidates',
        filters=ScanFilter(min_price=5.0, min_volume=200_000, max_change_pct=0.0),
        indicators=['rsi', 'sma_20', 'sma_50'],
        score_fn='_score_mean_reversion',
        use_market_scan=True,
    ),
    'breakout': ScanPreset(
        name='breakout',
        description='Stocks breaking above key moving averages with volume',
        filters=ScanFilter(min_price=5.0, min_volume=500_000, min_change_pct=1.0),
        indicators=['ema_9', 'sma_20'],
        score_fn='_score_breakout',
    ),
    'volatile': ScanPreset(
        name='volatile',
        description='High intraday range and volume — scalping candidates',
        filters=ScanFilter(min_price=2.0, min_volume=300_000),
        indicators=['rsi'],
        score_fn='_score_volatile',
        use_market_scan=True,
    ),
}

# Preset → IB scanner scan-code mapping
PRESET_SCAN_CODES: Dict[str, str] = {
    'momentum': 'TOP_PERC_GAIN',
    'gap-up': 'HIGH_OPEN_GAP',
    'gap-down': 'LOW_OPEN_GAP',
    'mean-reversion': 'TOP_PERC_LOSE',
    'breakout': 'TOP_PERC_GAIN',
    'volatile': 'HIGH_VS_13W_HL',
}


# Location code → (resolution_exchange, expected_currency, {valid primaryExchanges}).
# resolution_exchange is what to put on the resolution Contract: SMART for US
# (smart-routed; primary tells NYSE/NASDAQ apart) and the specific foreign
# exchange otherwise, so IB returns the LOCAL listing rather than a US ADR.
# expected_currency + valid primaries are used to VALIDATE the resolved contract
# and reject a wrong-exchange/ADR match (the precision principle). Blindly using
# the last dotted token got this wrong: STK.US.MAJOR → 'MAJOR' (not an exchange),
# STK.JP.TSE → 'TSE' (Tokyo is 'TSEJ' at IB, not Toronto's 'TSE').
_US_PRIMARIES = {'NYSE', 'NASDAQ', 'ARCA', 'AMEX', 'BATS', 'ISLAND', 'PINK', 'NYSENAT', 'IEX'}
_LOCATION_EXCHANGE: Dict[str, tuple] = {
    'STK.US': ('SMART', 'USD', _US_PRIMARIES),
    'STK.US.MAJOR': ('SMART', 'USD', _US_PRIMARIES),
    'STK.US.NYSE': ('NYSE', 'USD', {'NYSE'}),
    'STK.US.NASDAQ': ('NASDAQ', 'USD', {'NASDAQ', 'ISLAND'}),
    'STK.AU.ASX': ('ASX', 'AUD', {'ASX'}),
    'STK.CA': ('SMART', 'CAD', {'TSE', 'VENTURE'}),  # Canada, smart-routed
    'STK.CA.TSE': ('TSE', 'CAD', {'TSE'}),          # Toronto
    'STK.JP.TSE': ('TSEJ', 'JPY', {'TSEJ'}),         # Tokyo — IB code is TSEJ, not TSE
    'STK.HK.SEHK': ('SEHK', 'HKD', {'SEHK'}),
}


def list_presets() -> pd.DataFrame:
    """Return a DataFrame describing all available presets."""
    rows = []
    for p in PRESETS.values():
        f = p.filters
        filter_parts = []
        if f.min_price > 1.0:
            filter_parts.append(f'price>={f.min_price}')
        if f.min_volume > 0:
            filter_parts.append(f'vol>={f.min_volume:,}')
        if f.min_change_pct is not None:
            filter_parts.append(f'chg>={f.min_change_pct}%')
        if f.max_change_pct is not None:
            filter_parts.append(f'chg<={f.max_change_pct}%')
        rows.append({
            'preset': p.name,
            'description': p.description,
            'filters': ', '.join(filter_parts),
            'indicators': ', '.join(p.indicators),
        })
    return pd.DataFrame(rows)


# ------------------------------------------------------------------
# Module-level scoring functions (shared by IdeaScanner & IBIdeaScanner)
# ------------------------------------------------------------------

def _score_momentum(c: Dict) -> tuple:
    """Score based on change%, relative volume, RSI headroom, and EMA position."""
    score = 0.0
    # Change contribution (0-30 points)
    score += min(abs(c.get('change_pct', 0)), 15) * 2
    # Relative volume (0-25 points)
    score += min(c.get('rel_vol', 0), 5) * 5
    # RSI headroom: higher RSI = less room but shows momentum
    rsi = c.get('rsi')
    if rsi is not None:
        if 50 < rsi < 80:
            score += (rsi - 50) * 0.5  # 0-15 points
        elif rsi >= 80:
            score += 10  # extended, still momentum
    # Price above EMA9
    ema_9 = c.get('ema_9')
    if ema_9 is not None and ema_9 > 0 and c['price'] > ema_9:
        score += 10

    signal = 'BUY' if score >= 40 else ('WATCH' if score >= 20 else 'WATCH')
    return (min(score, 100), signal)


def _score_gap_up(c: Dict) -> tuple:
    """Score based on gap size and relative volume."""
    score = 0.0
    # Gap contribution (0-40 points)
    gap = abs(c.get('gap_pct', 0))
    score += min(gap, 20) * 2
    # Relative volume (0-30 points)
    score += min(c.get('rel_vol', 0), 6) * 5
    # Change contribution (0-20 points)
    score += min(abs(c.get('change_pct', 0)), 10) * 2
    # RSI — not too overbought
    rsi = c.get('rsi')
    if rsi is not None and rsi < 70:
        score += 10

    signal = 'BUY' if score >= 40 else 'WATCH'
    return (min(score, 100), signal)


def _score_gap_down(c: Dict) -> tuple:
    """Score based on gap size, RSI oversold level, and SMA distance."""
    score = 0.0
    # Gap size (0-40 points)
    gap = abs(c.get('gap_pct', 0))
    score += min(gap, 20) * 2
    # RSI oversold bonus (0-30 points)
    rsi = c.get('rsi')
    if rsi is not None and rsi < 40:
        score += (40 - rsi) * 0.75
    # Price below SMA20
    sma_20 = c.get('sma_20')
    if sma_20 is not None and sma_20 > 0 and c['price'] < sma_20:
        distance_pct = ((sma_20 - c['price']) / sma_20) * 100
        score += min(distance_pct, 10) * 2
    # Relative volume
    score += min(c.get('rel_vol', 0), 4) * 2.5

    signal = 'SELL' if score >= 40 else 'WATCH'
    return (min(score, 100), signal)


def _score_mean_reversion(c: Dict) -> tuple:
    """Score based on RSI oversold level and distance below moving averages."""
    score = 0.0
    # RSI oversold (0-30 points)
    rsi = c.get('rsi')
    if rsi is not None:
        if rsi < 30:
            score += (30 - rsi) * 1.0
        elif rsi < 40:
            score += (40 - rsi) * 0.5

    # Distance below SMA20 (0-25 points)
    sma_20 = c.get('sma_20')
    if sma_20 is not None and sma_20 > 0 and c['price'] < sma_20:
        distance_pct = ((sma_20 - c['price']) / sma_20) * 100
        score += min(distance_pct, 10) * 2.5

    # Distance below SMA50 (0-25 points)
    sma_50 = c.get('sma_50')
    if sma_50 is not None and sma_50 > 0 and c['price'] < sma_50:
        distance_pct = ((sma_50 - c['price']) / sma_50) * 100
        score += min(distance_pct, 10) * 2.5

    # Relative volume (0-20 points)
    score += min(c.get('rel_vol', 0), 4) * 5

    signal = 'BUY' if score >= 35 else 'WATCH'
    return (min(score, 100), signal)


def _score_breakout(c: Dict) -> tuple:
    """Score based on price vs VWAP, EMA/SMA alignment, and volume."""
    score = 0.0
    price = c['price']

    # Price above VWAP (0-15 points)
    vwap = c.get('vwap', 0)
    if vwap > 0 and price > vwap:
        score += 15

    # Price > EMA9 > SMA20 alignment (0-30 points)
    ema_9 = c.get('ema_9')
    sma_20 = c.get('sma_20')
    if ema_9 is not None and sma_20 is not None:
        if price > ema_9 > sma_20:
            score += 30
        elif price > ema_9:
            score += 15
        elif price > sma_20:
            score += 10

    # Relative volume (0-30 points)
    score += min(c.get('rel_vol', 0), 6) * 5

    # Change contribution (0-15 points)
    score += min(abs(c.get('change_pct', 0)), 5) * 3

    signal = 'BUY' if score >= 45 else 'WATCH'
    return (min(score, 100), signal)


def _score_volatile(c: Dict) -> tuple:
    """Score based on intraday range, relative volume, and absolute change."""
    score = 0.0
    # Range % (0-40 points)
    score += min(c.get('range_pct', 0), 20) * 2
    # Relative volume (0-25 points)
    score += min(c.get('rel_vol', 0), 5) * 5
    # Absolute change % (0-25 points)
    score += min(abs(c.get('change_pct', 0)), 10) * 2.5
    # RSI extremes bonus
    rsi = c.get('rsi')
    if rsi is not None and (rsi > 70 or rsi < 30):
        score += 10

    signal = 'BUY' if c.get('change_pct', 0) < -3 else ('SELL' if c.get('change_pct', 0) > 3 else 'WATCH')
    return (min(score, 100), signal)


# Lookup dict for scoring functions by name
_SCORE_FUNCTIONS: Dict[str, Callable] = {
    '_score_momentum': _score_momentum,
    '_score_gap_up': _score_gap_up,
    '_score_gap_down': _score_gap_down,
    '_score_mean_reversion': _score_mean_reversion,
    '_score_breakout': _score_breakout,
    '_score_volatile': _score_volatile,
}


# ------------------------------------------------------------------
# Module-level filtering and formatting (shared by both scanners)
# ------------------------------------------------------------------

def apply_filters(candidates: List[Dict], filters: ScanFilter,
                  trading_filter=None) -> List[Dict]:
    """Apply ScanFilter criteria to candidate list.

    Parameters
    ----------
    trading_filter : TradingFilter, optional
        If provided, also apply denylist/allowlist/exchange/sec_type checks.
    """
    result = []
    for c in candidates:
        # Trading filter check (denylist, allowlist, etc.)
        if trading_filter:
            allowed, _ = trading_filter.is_allowed(c['ticker'], price=c.get('price', 0))
            if not allowed:
                continue
        if c['price'] < filters.min_price or c['price'] > filters.max_price:
            continue
        # A zero-volume instrument is the MOST illiquid — it must be filtered by
        # a min_volume liquidity gate, not exempted. The old `volume > 0` guard
        # let zero/unknown-volume names slip straight through the filter.
        if filters.min_volume > 0 and c['volume'] < filters.min_volume:
            continue
        if filters.min_change_pct is not None and c['change_pct'] < filters.min_change_pct:
            continue
        if filters.max_change_pct is not None and c['change_pct'] > filters.max_change_pct:
            continue
        if c['rel_vol'] < filters.min_relative_volume:
            continue
        if c.get('spread_pct', 0) > filters.max_spread_pct > 0:
            continue
        result.append(c)
    return result


def to_dataframe(candidates: List[Dict], fundamentals: bool = False, news: bool = False) -> pd.DataFrame:
    """Convert scored candidates to a DataFrame with consistent column order."""
    if not candidates:
        return pd.DataFrame()

    # Base columns always present
    base_cols = ['ticker', 'name', 'price', 'change_pct', 'volume', 'gap_pct',
                 'rel_vol', 'range_pct', 'score', 'signal']
    # Indicator columns that may be present
    indicator_cols = ['rsi', 'ema_9', 'sma_20', 'sma_50']
    # Fundamental columns (in display order)
    fundamental_cols = ['pe_ratio', 'pb_ratio', 'ps_ratio', 'debt_equity',
                       'roe', 'roa', 'div_yield', 'ev_ebitda',
                       'mkt_cap', 'eps', 'fcf']
    # News columns
    news_cols = ['sentiment', 'headline', 'catalyst', 'news_date']

    df = pd.DataFrame(candidates)

    # Order columns: base first, then indicators, then fundamentals, then news, then extras
    ordered = [c for c in base_cols if c in df.columns]
    ordered += [c for c in indicator_cols if c in df.columns]
    if fundamentals:
        ordered += [c for c in fundamental_cols if c in df.columns]
    if news:
        ordered += [c for c in news_cols if c in df.columns]
    ordered += [c for c in df.columns if c not in ordered]

    df = df[ordered]

    # Round indicator columns
    for col in indicator_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce').round(2)

    return df.reset_index(drop=True)


def merge_filters(scan_preset: ScanPreset, custom_filters: Optional[Dict[str, Any]] = None) -> ScanFilter:
    """Create a ScanFilter from a preset, optionally overriding with custom values."""
    filters = ScanFilter(
        min_price=scan_preset.filters.min_price,
        max_price=scan_preset.filters.max_price,
        min_volume=scan_preset.filters.min_volume,
        min_change_pct=scan_preset.filters.min_change_pct,
        max_change_pct=scan_preset.filters.max_change_pct,
        min_relative_volume=scan_preset.filters.min_relative_volume,
    )
    if custom_filters:
        for k, v in custom_filters.items():
            if hasattr(filters, k) and v is not None:
                setattr(filters, k, v)
    return filters


# ------------------------------------------------------------------
# Local indicator computation (pure pandas, no API)
# ------------------------------------------------------------------

def compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    """Compute RSI from a list of closing prices."""
    if len(closes) < period + 1:
        return None
    deltas = pd.Series(closes).diff()
    gain = deltas.clip(lower=0).rolling(period).mean()
    loss = (-deltas.clip(upper=0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - 100 / (1 + rs)
    val = rsi.iloc[-1]
    if pd.isna(val):
        return None
    return round(float(val), 2)


def compute_ema(closes: List[float], window: int) -> Optional[float]:
    """Compute EMA from a list of closing prices."""
    if len(closes) < window:
        return None
    val = pd.Series(closes).ewm(span=window, adjust=False).mean().iloc[-1]
    if pd.isna(val):
        return None
    return round(float(val), 2)


def compute_sma(closes: List[float], window: int) -> Optional[float]:
    """Compute SMA from a list of closing prices."""
    if len(closes) < window:
        return None
    val = pd.Series(closes).rolling(window).mean().iloc[-1]
    if pd.isna(val):
        return None
    return round(float(val), 2)


# ------------------------------------------------------------------
# IdeaScanner (shared pipeline over a scan source)
# ------------------------------------------------------------------

def _join_notices(*notices: str) -> str:
    return ' '.join(part.strip() for part in notices if part and part.strip())


class IdeaScanner:
    """Discover → Enrich → Filter → Score → Rank pipeline over one scan source."""

    def __init__(self, source: 'ScanSource'):
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
        fundamentals_if_available: bool = False,
    ) -> pd.DataFrame:
        """Run the full scan pipeline.

        Parameters
        ----------
        preset : str
            Preset name (momentum, gap-up, gap-down, mean-reversion, breakout, volatile).
        source : str
            'movers' (default), 'tickers', or 'universe'.
        tickers : list of str, optional
            Explicit ticker list (when source='tickers').
        universe_symbols : list of str, optional
            Symbol list from a universe (when source='universe').
        top_n : int
            Max results to return.
        custom_filters : dict, optional
            Override preset filter values (e.g. {'min_price': 10}).
        fundamentals : bool
            If True, enrich results with financial ratios (PE, D/E, ROE, etc.).
        news : bool
            If True, enrich results with latest news headline and sentiment.
        fundamentals_if_available : bool
            Like ``fundamentals``, but a source without ratios skips them and
            says so in the notice instead of raising.

        Returns
        -------
        pd.DataFrame
            Ranked ideas with columns: ticker, price, change_pct, volume,
            gap_pct, rel_vol, range_pct, score, signal, plus indicator columns.
        """
        scan_preset = PRESETS.get(preset)
        if not scan_preset:
            raise ValueError(f'Unknown preset: {preset}. Available: {", ".join(PRESETS.keys())}')
        filters = merge_filters(scan_preset, custom_filters)
        if fundamentals and not self.source.supports_fundamentals:
            # Raises the source's own explanation before any discovery or history call.
            self.source.fundamentals([])

        discovery = self.source.discover(source, tickers, universe_symbols, scan_preset.use_market_scan)
        candidates = self._apply_filters(list(discovery.candidates), filters) if discovery.candidates else []
        if not candidates:
            return self._labelled(pd.DataFrame(), discovery.notice)

        # Pre-score without indicators so market-wide scans can cap indicator API calls.
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
        notice = discovery.notice
        if fundamentals_if_available and not fundamentals:
            if self.source.supports_fundamentals:
                fundamentals = True
            else:
                notice = _join_notices(notice, self._no_fundamentals_notice())
        if fundamentals and candidates:
            fund_data = self.source.fundamentals(top)
            for c in candidates:
                c.update(fund_data.get(c['ticker'], {}))
        if news and candidates:
            news_data = self.source.news(top)
            for c in candidates:
                c.update(news_data.get(c['ticker'], {}))

        return self._labelled(self._to_dataframe(candidates, fundamentals=fundamentals, news=news), notice)

    def _no_fundamentals_notice(self) -> str:
        provider = self.source.name.capitalize()
        return (f'Fundamentals are not available from {provider} yet; '
                'use --source massive or --source twelvedata for ratios.')

    def _labelled(self, frame: pd.DataFrame, notice: str) -> pd.DataFrame:
        frame.attrs['ideas_provider'] = self.source.name
        if notice:
            frame.attrs['ideas_notice'] = notice
        return frame

    # ------------------------------------------------------------------
    # Filtering (delegates to module-level)
    # ------------------------------------------------------------------

    def _apply_filters(self, candidates: List[Dict], filters: ScanFilter) -> List[Dict]:
        from trader.trading.trading_filter import TradingFilter
        tf = TradingFilter.load()
        return apply_filters(candidates, filters, trading_filter=tf if not tf.is_empty() else None)

    # ------------------------------------------------------------------
    # Scoring (delegates to module-level functions)
    # ------------------------------------------------------------------

    def _score_momentum(self, c: Dict) -> tuple:
        return _score_momentum(c)

    def _score_gap_up(self, c: Dict) -> tuple:
        return _score_gap_up(c)

    def _score_gap_down(self, c: Dict) -> tuple:
        return _score_gap_down(c)

    def _score_mean_reversion(self, c: Dict) -> tuple:
        return _score_mean_reversion(c)

    def _score_breakout(self, c: Dict) -> tuple:
        return _score_breakout(c)

    def _score_volatile(self, c: Dict) -> tuple:
        return _score_volatile(c)

    # ------------------------------------------------------------------
    # Output formatting (delegates to module-level)
    # ------------------------------------------------------------------

    def _to_dataframe(self, candidates: List[Dict], fundamentals: bool = False, news: bool = False) -> pd.DataFrame:
        return to_dataframe(candidates, fundamentals=fundamentals, news=news)


# ------------------------------------------------------------------
# IBIdeaScanner (IB-backed, international markets)
# ------------------------------------------------------------------

def parse_report_snapshot(xml_str: str) -> Dict[str, Any]:
    """Parse IB ReportSnapshot XML into a flat dict of financial ratios.

    Returns keys matching the Massive path: pe_ratio, pb_ratio, debt_equity,
    roe, roa, div_yield, mkt_cap, eps, etc.
    """
    import xml.etree.ElementTree as ET

    data: Dict[str, Any] = {}
    if not xml_str:
        return data

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return data

    # IB ReportSnapshot uses <Ratio FieldName="...">value</Ratio> inside
    # <Group ID="..."> under <Ratios>.
    field_map = {
        'APENORM': 'pe_ratio',
        'TTMPRFCFPS': 'ps_ratio',
        'PRICE2BK': 'pb_ratio',
        'QTOTD2EQ': 'debt_equity',
        'TTMROEPCT': 'roe',
        'TTMROAPCT': 'roa',
        'YIELD': 'div_yield',
        'EV2EBITDA': 'ev_ebitda',
        'MKTCAP': 'mkt_cap',
        'TTMEPSXCLX': 'eps',
        'AFETEFCFPS': 'fcf',
    }

    for ratio in root.iter('Ratio'):
        field_name = ratio.get('FieldName', '')
        if field_name in field_map and ratio.text:
            try:
                val = float(ratio.text)
                col = field_map[field_name]
                if col in ('mkt_cap', 'fcf'):
                    data[col] = val
                else:
                    data[col] = round(val, 2)
            except (ValueError, TypeError):
                pass

    return data


class ScannerDataProvider(Protocol):
    """Synchronous data access for :class:`IBIdeaScanner`.

    Two implementations: :class:`RpcScannerProvider` (CLI / offline simulation,
    over the legacy dill RPC client) and
    ``trader.messaging.scanner_bridge.TraderScannerProvider`` (in-process, used
    by the ``scan_ideas`` typed query on the trader).

    Every method is *synchronous* and returns plain ``list``/``list[dict]``/
    ``str`` — the scanner runs a ThreadPoolExecutor over ``get_history_bars``
    and must never receive a coroutine.
    """

    def scanner_data(self, *, scan_code: str, location_code: str, num_rows: int) -> list[dict]: ...

    def get_snapshots_batch(self, contracts: list, delayed_ok: bool) -> list[dict]: ...

    def get_history_bars(self, contract, duration: str, bar_size: str) -> list[dict]: ...

    def resolve_contract(self, partial) -> list: ...

    def get_fundamental_data(self, contract, report_type: str) -> str: ...

    def get_news_headlines(self, con_id: int, provider_codes: str, count: int) -> list[dict]: ...


class RpcScannerProvider:
    """:class:`ScannerDataProvider` backed by the legacy dill RPC client.

    Wraps exactly the calls ``IBIdeaScanner`` used to make inline, so the CLI
    path (``mmr ideas --location ...``) is unchanged.
    """

    def __init__(self, rpc_client):
        self._rpc = rpc_client

    def scanner_data(self, *, scan_code, location_code, num_rows):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list[dict]).scanner_data(
            scan_code=scan_code, location_code=location_code, num_rows=num_rows))

    def get_snapshots_batch(self, contracts, delayed_ok):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list[dict]).get_snapshots_batch(
            contracts, delayed_ok))

    def get_history_bars(self, contract, duration, bar_size):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list[dict]).get_history_bars(
            contract, duration, bar_size))

    def resolve_contract(self, partial):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list).resolve_contract(partial))

    def get_fundamental_data(self, contract, report_type):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=str).get_fundamental_data(
            contract, report_type))

    def get_news_headlines(self, con_id, provider_codes, count):
        from trader.messaging.clientserver import consume
        return consume(self._rpc.rpc(return_type=list[dict]).get_news_headlines(
            con_id, provider_codes, count))


class IBIdeaScanner:
    """IB-backed idea scanner for international markets.

    Uses IB's scanner API for discovery, ``get_snapshot()`` for price data,
    and ``reqHistoricalData`` for indicator computation (RSI/EMA/SMA).
    Fundamentals via ``reqFundamentalData``, news via ``reqHistoricalNews``.

    When explicit tickers or a universe is provided, symbol resolution via IB
    is used instead of the scanner API (which may not support all locations).

    The scoring/filtering/formatting logic is shared with :class:`IdeaScanner`.
    """

    def __init__(self, provider: 'ScannerDataProvider'):
        self._provider = provider

    def scan(
        self,
        preset: str = 'momentum',
        location: str = 'STK.AU.ASX',
        top_n: int = 15,
        custom_filters: Optional[Dict[str, Any]] = None,
        fundamentals: bool = False,
        news: bool = False,
        tickers: Optional[List[str]] = None,
        universe_symbols: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Run the IB-backed scan pipeline.

        Parameters
        ----------
        preset : str
            Scoring preset name.
        location : str
            IB market location code (e.g. STK.AU.ASX, STK.CA, STK.HK.SEHK).
        top_n : int
            Max results to return.
        custom_filters : dict, optional
            Override preset filter values.
        fundamentals : bool
            If True, enrich with financial ratios via IB reqFundamentalData.
        news : bool
            If True, enrich with news headlines via IB reqHistoricalNews.
        tickers : list of str, optional
            Explicit ticker list. Resolved via IB (bypasses scanner).
        universe_symbols : list of str, optional
            Symbol list from a universe. Resolved via IB (bypasses scanner).
        """
        # Check location against trading filters before doing any work
        from trader.trading.trading_filter import TradingFilter
        tf = TradingFilter.load()
        if not tf.is_empty():
            allowed, reason = tf.is_allowed('', location=location)
            if not allowed:
                logger.warning('Trading filter blocked location %s: %s', location, reason)
                return pd.DataFrame()

        from ib_async.contract import Contract

        scan_preset = PRESETS.get(preset)
        if not scan_preset:
            raise ValueError(f'Unknown preset: {preset}. Available: {", ".join(PRESETS.keys())}')

        filters = merge_filters(scan_preset, custom_filters)
        score_fn = _SCORE_FUNCTIONS[scan_preset.score_fn]

        # 1. Discover candidates
        symbols_to_resolve = tickers or universe_symbols
        if symbols_to_resolve:
            # When user provides explicit symbols, don't filter on change_pct
            # (the preset's min_change_pct is for scanner-based discovery)
            if custom_filters is None or 'min_change_pct' not in custom_filters:
                filters.min_change_pct = None
            if custom_filters is None or 'max_change_pct' not in custom_filters:
                filters.max_change_pct = None
        if symbols_to_resolve:
            # Resolve explicit symbols via IB (bypasses scanner API). Failure
            # here is an error — the user asked for *these* symbols and got
            # back nothing, which almost always means bad tickers or IB being
            # unhealthy. "Fail loudly" per project policy.
            contracts, conid_map = self._resolve_symbols(
                symbols_to_resolve, location,
            )
            if not contracts:
                raise IdeaScannerError(
                    f'No symbols resolved via IB for {symbols_to_resolve!r} '
                    f'at location {location!r}. Check ticker spellings and '
                    f'that trader_service can reach IB Gateway.'
                )
        else:
            # Use IB scanner for discovery
            scan_code = PRESET_SCAN_CODES.get(preset, 'TOP_PERC_GAIN')
            scanner_results = self._provider.scanner_data(
                scan_code=scan_code,
                location_code=location,
                num_rows=top_n * 3,
            )
            if not scanner_results:
                # Distinguish "API failed" from "no matches". The previous
                # return-empty-DataFrame path made these indistinguishable.
                raise IdeaScannerError(
                    f'IB scanner returned no results for location={location!r} '
                    f'scan_code={scan_code!r}. This usually means the scanner '
                    f'does not support this location, or your IB account lacks '
                    f'market data subscriptions for it. Try --tickers or '
                    f'--universe to specify symbols explicitly.'
                )

            # Build Contract objects from scanner results
            # exchange_hint from the location code (e.g. STK.AU.ASX → ASX); it is
            # referenced below but was only defined in _resolve_tickers, so the
            # IB scanner-discovery path raised NameError. Derive it here too.
            exchange_hint = ''
            _loc_parts = (location or '').split('.')
            if len(_loc_parts) >= 3:
                exchange_hint = _loc_parts[2]
            contracts = []
            conid_map: Dict[str, int] = {}  # symbol → conId for news lookup
            for r in scanner_results:
                raw_exchange = r.get('exchange', '') or exchange_hint or 'SMART'
                primary = raw_exchange if raw_exchange != 'SMART' else ''
                c = Contract(
                    conId=r['conId'],
                    symbol=r['symbol'],
                    secType=r.get('secType', 'STK'),
                    exchange='SMART',
                    primaryExchange=primary,
                    currency=r.get('currency', ''),
                )
                contracts.append(c)
                conid_map[r['symbol']] = r['conId']

        # 3. Get snapshots via RPC → IB
        snapshots = self._provider.get_snapshots_batch(contracts, True)

        # 4. Get history for prev_close/prev_volume + local indicator computation
        #    in parallel. Limit history fetches to top candidates. If >50%
        #    of fetches fail, raise so we don't silently score on missing
        #    indicators.
        #
        #    Was sequential: 30 fetches × ~0.5s = 15s of pure wait, the
        #    main reason a 50-symbol scan took ~20s end-to-end. With an
        #    8-way pool it drops to ~3-4s.
        history_map: Dict[str, list[dict]] = {}
        history_limit = min(len(contracts), top_n * 2)
        history_failures: list = []

        def _fetch_one(contract):
            try:
                bars = self._provider.get_history_bars(contract, '60 D', '1 day')
                return (contract.symbol, bars, None)
            except Exception as ex:
                return (contract.symbol, None, ex)

        if history_limit > 0:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(_fetch_one, c): c for c in contracts[:history_limit]}
                for fut in as_completed(futures):
                    sym, bars, ex = fut.result()
                    if ex is not None:
                        history_failures.append((sym, str(ex)))
                        logger.warning('history fetch failed for %s: %s', sym, ex)
                    elif bars:
                        history_map[sym] = bars
                    else:
                        history_failures.append((sym, 'no bars returned'))

        # When every history fetch fails, indicators will be None and scoring
        # becomes meaningless — better to fail than return nonsense rankings.
        if history_limit > 0 and len(history_failures) == history_limit:
            raise IdeaScannerError(
                f'All {history_limit} history fetches failed — cannot compute '
                f'indicators. First failure: {history_failures[0][0]}: '
                f'{history_failures[0][1]}'
            )

        # 5. Build candidates (same dict format as Massive path)
        candidates = self._build_candidates(snapshots, history_map)
        if not candidates:
            return pd.DataFrame()

        # 6. Filter
        from trader.trading.trading_filter import TradingFilter
        tf = TradingFilter.load()
        candidates = apply_filters(candidates, filters,
                                   trading_filter=tf if not tf.is_empty() else None)
        if not candidates:
            return pd.DataFrame()

        # 7. Compute indicators locally from history bars
        for c in candidates:
            symbol = c['ticker']
            bars = history_map.get(symbol, [])
            if bars:
                closes = [b['close'] for b in bars if b.get('close') is not None]
                for ind in scan_preset.indicators:
                    if ind == 'rsi':
                        c['rsi'] = compute_rsi(closes)
                    elif ind == 'ema_9':
                        c['ema_9'] = compute_ema(closes, 9)
                    elif ind == 'sma_20':
                        c['sma_20'] = compute_sma(closes, 20)
                    elif ind == 'sma_50':
                        c['sma_50'] = compute_sma(closes, 50)

        # 8. Score
        for c in candidates:
            score, signal = score_fn(c)
            c['score'] = round(score, 1)
            c['signal'] = signal

        # 9. Sort + top_n
        candidates.sort(key=lambda c: c['score'], reverse=True)
        candidates = candidates[:top_n]

        # 10. Optionally enrich with fundamentals (IB ReportSnapshot)
        if fundamentals and candidates:
            fund_data = self._fetch_fundamentals(
                contracts, candidates,
            )
            for c in candidates:
                if c['ticker'] in fund_data:
                    c.update(fund_data[c['ticker']])

        # 11. Optionally enrich with news (IB reqHistoricalNews)
        if news and candidates:
            news_data = self._fetch_news(
                candidates, conid_map,
            )
            for c in candidates:
                if c['ticker'] in news_data:
                    c.update(news_data[c['ticker']])

        return to_dataframe(candidates, fundamentals=fundamentals, news=news)

    # ------------------------------------------------------------------
    # Symbol resolution (alternative to scanner discovery)
    # ------------------------------------------------------------------

    def _resolve_symbols(
        self,
        symbols: List[str],
        location: str,
    ) -> tuple:
        """Resolve explicit symbol names to IB Contracts via resolve_contract RPC.

        Builds a partial Contract with the exchange extracted from the location
        code (e.g. STK.AU.ASX → exchange=ASX) and uses IB's reqContractDetails
        to get the full contract. This avoids the SMART exchange fallback that
        would return US ADRs instead of local listings.

        Returns (contracts, conid_map) tuple.
        """
        from ib_async.contract import Contract

        loc = (location or '').upper()
        mapping = _LOCATION_EXCHANGE.get(loc)
        if mapping:
            resolution_exchange, expected_currency, valid_primaries = mapping
        else:
            # Unknown location: a 3-part STK.XX.<EXCH> uses the explicit exchange;
            # a 2-part STK.XX (country only) has no exchange to force, so fall back
            # to SMART. No currency validation (target currency unknown).
            parts = loc.split('.')
            resolution_exchange = parts[2] if len(parts) >= 3 else 'SMART'
            expected_currency, valid_primaries = '', set()
            logger.warning('location %r not in exchange map — resolving on %r without '
                           'currency validation', location, resolution_exchange)

        def _mismatch(currency: str, primary: str) -> str:
            """Return a reason string if the resolved contract is NOT the intended
            local listing, else ''."""
            cur = (currency or '').upper()
            pri = (primary or '').upper()
            if expected_currency and cur and cur != expected_currency:
                return f'currency {cur} != expected {expected_currency}'
            if valid_primaries and pri and pri not in valid_primaries:
                return f'primaryExchange {pri} not in {sorted(valid_primaries)}'
            return ''

        contracts = []
        conid_map: Dict[str, int] = {}
        for sym in symbols:
            try:
                partial = Contract(symbol=sym, secType='STK', exchange=resolution_exchange)
                defs = self._provider.resolve_contract(partial)
                if not defs:
                    logger.warning('Could not resolve %s on %s (location=%s)',
                                   sym, resolution_exchange, location)
                    continue

                # Pick the FIRST definition that passes the currency/exchange
                # validation — not blindly defs[0], which can be a US ADR when a
                # foreign local listing was requested.
                chosen = None
                for d in defs:
                    if hasattr(d, 'conId'):
                        cur, pri, conid, dsym = d.currency, d.primaryExchange, d.conId, d.symbol
                    else:
                        cur = d.get('currency', ''); pri = d.get('primaryExchange', '')
                        conid = d.get('conId', 0); dsym = d.get('symbol', sym)
                    reason = _mismatch(cur or '', pri or '')
                    if not reason:
                        chosen = (conid, dsym, pri or resolution_exchange, cur or expected_currency)
                        break
                if chosen is None:
                    # Every candidate failed validation — refuse rather than feed
                    # the scanner a wrong-market instrument (precision principle).
                    d0 = defs[0]
                    c0 = getattr(d0, 'currency', None) or (d0.get('currency', '') if isinstance(d0, dict) else '')
                    p0 = getattr(d0, 'primaryExchange', None) or (d0.get('primaryExchange', '') if isinstance(d0, dict) else '')
                    logger.warning('rejecting %s for location %s: no candidate on the intended '
                                   'market (%s); first was currency=%s primary=%s',
                                   sym, location, _mismatch(c0 or '', p0 or '') or 'mismatch', c0, p0)
                    continue

                conid, dsym, primary, currency = chosen
                contracts.append(Contract(
                    conId=conid, symbol=dsym, secType='STK',
                    exchange='SMART', primaryExchange=primary, currency=currency,
                ))
                conid_map[dsym] = conid
            except Exception as ex:
                # WARNING, not DEBUG — a requested ticker silently vanishing from
                # the scan (invisible at default log level) is exactly the kind of
                # "confidently-wrong" gap the scanner should surface.
                logger.warning('Failed to resolve symbol %s: %s', sym, ex)
        return contracts, conid_map

    @staticmethod
    def _build_candidates(
        snapshots: list[dict],
        history_map: Dict[str, list[dict]],
    ) -> List[Dict[str, Any]]:
        """Build candidate dicts from IB snapshot data + history bars."""
        candidates = []
        seen = set()
        for snap in snapshots:
            symbol = snap.get('symbol', '')
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)

            price = snap.get('last') or snap.get('close') or 0.0
            if not price or price != price:  # NaN check
                price = snap.get('bid') or snap.get('ask') or 0.0
            if not price or price != price:
                continue

            volume = snap.get('volume') or 0
            day_open = snap.get('open') or 0.0
            day_high = snap.get('high') or 0.0
            day_low = snap.get('low') or 0.0

            # Derive prev_close and prev_volume from history bars
            bars = history_map.get(symbol, [])
            prev_close = 0.0
            prev_volume = 0
            if len(bars) >= 2:
                prev_bar = bars[-2]
                prev_close = prev_bar.get('close', 0.0) or 0.0
                prev_volume = prev_bar.get('volume', 0) or 0

            # change_pct
            change_pct = 0.0
            if prev_close > 0:
                change_pct = ((price - prev_close) / prev_close) * 100.0

            # gap_pct
            gap_pct = 0.0
            if prev_close > 0 and day_open > 0:
                gap_pct = ((day_open - prev_close) / prev_close) * 100.0

            # relative volume
            rel_vol = 0.0
            if prev_volume > 0 and volume > 0:
                rel_vol = volume / prev_volume

            # intraday range %
            range_pct = 0.0
            if day_low > 0 and day_high > 0:
                range_pct = ((day_high - day_low) / day_low) * 100.0

            # spread
            spread_pct = 0.0
            bid = snap.get('bid') or 0.0
            ask = snap.get('ask') or 0.0
            if bid > 0 and ask > 0 and price > 0:
                spread_pct = ((ask - bid) / price) * 100.0

            candidates.append({
                'ticker': symbol,
                'price': round(float(price), 2),
                'change_pct': round(change_pct, 2),
                'volume': int(volume) if volume and volume == volume else 0,
                'gap_pct': round(gap_pct, 2),
                'rel_vol': round(rel_vol, 2),
                'range_pct': round(range_pct, 2),
                'spread_pct': round(spread_pct, 3),
                'vwap': 0.0,
                'exchange': snap.get('exchange', ''),
                'currency': snap.get('currency', ''),
            })

        return candidates

    # ------------------------------------------------------------------
    # Fundamentals (IB ReportSnapshot)
    # ------------------------------------------------------------------

    def _fetch_fundamentals(
        self,
        contracts: list,
        candidates: List[Dict],
    ) -> Dict[str, Dict[str, Any]]:
        """Fetch fundamental data via IB reqFundamentalData for each candidate."""
        # Build symbol → contract lookup from the full contract list
        contract_by_symbol: Dict[str, Any] = {}
        for c in contracts:
            contract_by_symbol[c.symbol] = c

        results: Dict[str, Dict[str, Any]] = {}
        for cand in candidates:
            symbol = cand['ticker']
            contract = contract_by_symbol.get(symbol)
            if not contract:
                continue
            try:
                xml_str = self._provider.get_fundamental_data(contract, 'ReportSnapshot')
                data = parse_report_snapshot(xml_str)
                if data:
                    results[symbol] = data
            except Exception:
                logger.debug('Fundamentals fetch failed for %s', symbol)

        return results

    # ------------------------------------------------------------------
    # News (IB reqHistoricalNews)
    # ------------------------------------------------------------------

    def _fetch_news(
        self,
        candidates: List[Dict],
        conid_map: Dict[str, int],
    ) -> Dict[str, Dict[str, Optional[str]]]:
        """Fetch latest news headlines via IB reqHistoricalNews for each candidate."""
        results: Dict[str, Dict[str, Optional[str]]] = {}
        for cand in candidates:
            symbol = cand['ticker']
            conId = conid_map.get(symbol)
            if not conId:
                continue
            try:
                headlines = self._provider.get_news_headlines(conId, '', 1)
                if headlines:
                    h = headlines[0]
                    title = h.get('headline', '')
                    # IB headlines include metadata prefix like
                    # {A:800015:L:en}Actual headline text
                    # Strip it to get clean headline
                    if title.startswith('{') and '}' in title:
                        title = title[title.index('}') + 1:]
                    title = title.strip()
                    if len(title) > 120:
                        title = title[:117] + '...'
                    news_date = h.get('time', '')[:10]
                    results[symbol] = {
                        'headline': title,
                        'news_date': news_date,
                    }
            except Exception:
                logger.debug('News fetch failed for %s', symbol)

        return results

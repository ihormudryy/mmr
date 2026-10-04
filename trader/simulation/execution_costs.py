"""Execution costs for the backtester: what a fill really costs.

Two models implement the same small interface (``fill_price``, ``commission``,
``scaled``):

- ``RealisticCosts`` (the default for CLI backtests and research): the broker's
  commission schedule for the instrument's venue (per-share with a minimum and
  a cap for US, percent-of-value with a minimum for ASX), a half-spread of
  ``spread_ticks`` ticks from the venue's price-banded tick table, and
  square-root market impact from the bar known when the order was decided.
- ``FlatCosts`` (legacy): a flat slippage model plus a per-share commission.

History carries no bid/ask, so the spread is an estimate. Cost stress
(``scaled(1.5)``, ``scaled(2.0)``) exists to cover that uncertainty.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

import pandas as pd
import yaml

from trader.objects import Action
from trader.simulation.slippage import FixedBPS, SlippageModel

EXECUTION_COSTS_FILE = 'execution_costs.yaml'


class ExecutionCostError(Exception):
    """An instrument's execution costs cannot be determined."""


class ExecutionCosts(Protocol):
    name: str

    def fill_price(self, conid: int, price: float, quantity: float,
                   action: Action, reference_bar: pd.Series) -> float: ...

    def commission(self, conid: int, quantity: float, price: float) -> float: ...

    def scaled(self, multiplier: float) -> 'ExecutionCosts': ...


@dataclass(frozen=True)
class CommissionSchedule:
    per_share: float = 0.0
    pct_of_value: float = 0.0
    minimum: float = 0.0
    max_pct_of_value: Optional[float] = None

    def fee(self, quantity: float, price: float) -> float:
        value = quantity * price
        fee = max(quantity * self.per_share + value * self.pct_of_value, self.minimum)
        if self.max_pct_of_value is not None:
            fee = min(fee, value * self.max_pct_of_value)
        return fee


class TickTable:
    """Price-banded minimum tick: ``[(band_floor_price, tick), ...]``."""

    def __init__(self, bands: Sequence[Tuple[float, float]]):
        ordered = sorted((float(floor), float(tick)) for floor, tick in bands)
        if not ordered or ordered[0][0] > 0.0:
            raise ExecutionCostError(f'tick table must start at price 0: {list(bands)}')
        if any(tick <= 0 for _, tick in ordered):
            raise ExecutionCostError(f'tick sizes must be positive: {list(bands)}')
        self._floors = [floor for floor, _ in ordered]
        self._ticks = [tick for _, tick in ordered]

    def tick_for(self, price: float) -> float:
        return self._ticks[bisect.bisect_right(self._floors, price) - 1]


@dataclass(frozen=True)
class Venue:
    name: str
    primary_exchanges: frozenset
    commission: CommissionSchedule
    ticks: TickTable
    calendar: Optional[str] = None


@dataclass(frozen=True)
class ExecutionCostsConfig:
    venues: Tuple[Venue, ...]
    spread_ticks: float
    min_half_spread_bps: float
    impact_k: float

    def venue_for(self, primary_exchange: str) -> Optional[Venue]:
        for venue in self.venues:
            if primary_exchange in venue.primary_exchanges:
                return venue
        return None


def parse_execution_costs_config(raw: Mapping[str, Any]) -> ExecutionCostsConfig:
    venues = []
    seen: Dict[str, str] = {}
    for name, spec in (raw.get('venues') or {}).items():
        exchanges = frozenset(spec['primary_exchanges'])
        for exchange in exchanges:
            if exchange in seen:
                raise ExecutionCostError(
                    f'exchange {exchange} is listed in venues {seen[exchange]!r} and {name!r}')
            seen[exchange] = name
        venues.append(Venue(
            name=name,
            primary_exchanges=exchanges,
            commission=CommissionSchedule(**spec['commission']),
            ticks=TickTable(spec['tick_table']),
            calendar=spec.get('calendar'),
        ))
    if not venues:
        raise ExecutionCostError('execution costs config defines no venues')
    return ExecutionCostsConfig(
        venues=tuple(venues),
        spread_ticks=float(raw['spread_ticks']),
        min_half_spread_bps=float(raw['min_half_spread_bps']),
        impact_k=float(raw['impact_k']),
    )


def load_execution_costs_config(path: Optional[str] = None) -> ExecutionCostsConfig:
    """Load from *path*, else ``~/.config/mmr/execution_costs.yaml``."""
    if path is None:
        from trader.container import ensure_config_dir
        path = str(ensure_config_dir() / EXECUTION_COSTS_FILE)
    if not Path(path).is_file():
        raise ExecutionCostError(f'execution costs config not found: {path}')
    with open(path) as f:
        return parse_execution_costs_config(yaml.safe_load(f) or {})


def _square_root_impact(k: float, price: float, quantity: float, bar: pd.Series) -> float:
    volume = float(bar.get('volume', 0) or 0)
    high = float(bar.get('high', price))
    low = float(bar.get('low', price))
    if volume <= 0 or high <= low or price <= 0:
        return 0.0
    return k * ((high - low) / price) * math.sqrt(quantity / volume)


@dataclass(frozen=True)
class RealisticCosts:
    venue_by_conid: Mapping[int, Venue]
    spread_ticks: float
    min_half_spread_bps: float
    impact_k: float
    multiplier: float = 1.0
    name: str = 'realistic'

    def _venue(self, conid: int) -> Venue:
        venue = self.venue_by_conid.get(int(conid))
        if venue is None:
            raise ExecutionCostError(f'no execution costs resolved for conid {conid}')
        return venue

    def fill_price(self, conid: int, price: float, quantity: float,
                   action: Action, reference_bar: pd.Series) -> float:
        tick = self._venue(conid).ticks.tick_for(price)
        half_spread = max(self.min_half_spread_bps / 10_000,
                          0.5 * self.spread_ticks * tick / price)
        impact = _square_root_impact(self.impact_k, price, quantity, reference_bar)
        cost = (half_spread + impact) * self.multiplier
        return price * (1 + cost) if action == Action.BUY else price * (1 - cost)

    def commission(self, conid: int, quantity: float, price: float) -> float:
        return self._venue(conid).commission.fee(quantity, price) * self.multiplier

    def scaled(self, multiplier: float) -> 'RealisticCosts':
        return replace(self, multiplier=self.multiplier * multiplier)


def build_realistic_costs(conids: Sequence[int], universe_accessor: Any,
                          config: ExecutionCostsConfig) -> RealisticCosts:
    """Resolve each conid's venue from the local universe DB. Every conid must
    resolve to exactly one primary exchange that has a configured venue."""
    venue_by_conid: Dict[int, Venue] = {}
    for conid in conids:
        definitions = universe_accessor.resolve_symbol(int(conid))
        exchanges = {d.primaryExchange for d in definitions}
        if not exchanges:
            raise ExecutionCostError(
                f'conid {conid} is not in any local universe; its venue (and so '
                f'its commission and tick size) is unknown')
        if len(exchanges) > 1:
            raise ExecutionCostError(
                f'conid {conid} resolves to several primary exchanges: {sorted(exchanges)}')
        exchange = exchanges.pop()
        venue = config.venue_for(exchange)
        if venue is None:
            symbol = definitions[0].symbol
            raise ExecutionCostError(
                f'no execution-cost venue for exchange {exchange!r} (conid {conid}, '
                f'{symbol}); add it to {EXECUTION_COSTS_FILE}')
        venue_by_conid[int(conid)] = venue
    return RealisticCosts(
        venue_by_conid=venue_by_conid,
        spread_ticks=config.spread_ticks,
        min_half_spread_bps=config.min_half_spread_bps,
        impact_k=config.impact_k,
    )


@dataclass(frozen=True)
class FlatCosts:
    """Legacy costs: ``slippage_bps`` (or a custom slippage model) per side and
    a flat per-share commission, the same for every instrument."""

    slippage_bps: float = 1.0
    commission_per_share: float = 0.005
    slippage_model: Optional[SlippageModel] = None
    name: str = 'legacy'

    def fill_price(self, conid: int, price: float, quantity: float,
                   action: Action, reference_bar: pd.Series) -> float:
        model = self.slippage_model or FixedBPS(self.slippage_bps)
        return model.calculate(price, quantity, action, reference_bar)

    def commission(self, conid: int, quantity: float, price: float) -> float:
        return quantity * self.commission_per_share

    def scaled(self, multiplier: float) -> 'FlatCosts':
        if self.slippage_model is not None:
            raise ExecutionCostError(
                'cannot stress a custom slippage model; use RealisticCosts for cost stress')
        return replace(self,
                       slippage_bps=self.slippage_bps * multiplier,
                       commission_per_share=self.commission_per_share * multiplier)

"""Per-instrument execution costs: broker commission schedules, tick-based
half-spread, impact from the bar known at decision time, and cost stress."""

from pathlib import Path

import pandas as pd
import pytest

from trader.data.data_access import SecurityDefinition
from trader.data.universe import UniverseAccessor
from trader.objects import Action
from trader.simulation.execution_costs import (
    CommissionSchedule,
    ExecutionCostError,
    FlatCosts,
    TickTable,
    build_realistic_costs,
    load_execution_costs_config,
    parse_execution_costs_config,
)

US_CONID = 4391
ASX_CONID = 4036812

CONFIG = {
    'venues': {
        'us': {
            'primary_exchanges': ['NASDAQ', 'NYSE', 'ARCA'],
            'commission': {'per_share': 0.005, 'minimum': 1.0, 'max_pct_of_value': 0.01},
            'tick_table': [[0.0, 0.0001], [1.0, 0.01]],
        },
        'asx': {
            'primary_exchanges': ['ASX'],
            'commission': {'pct_of_value': 0.0008, 'minimum': 6.0},
            'tick_table': [[0.0, 0.001], [0.10, 0.005], [2.0, 0.01]],
        },
    },
    'spread_ticks': 1.0,
    'min_half_spread_bps': 0.5,
    'impact_k': 0.1,
}

QUIET_BAR = pd.Series({'open': 100.0, 'high': 100.0, 'low': 100.0, 'close': 100.0, 'volume': 0.0})


def _definition(conid: int, symbol: str, primary_exchange: str) -> SecurityDefinition:
    return SecurityDefinition(
        symbol=symbol, exchange='SMART', conId=conid, secType='STK',
        primaryExchange=primary_exchange, currency='USD', tradingClass=symbol,
        includeExpired=False, secIdType='', secId='', description='',
        minTick=0.01, orderTypes='', validExchanges='', priceMagnifier=1,
        longName='', category='', subcategory='', tradingHours='',
        timeZoneId='', liquidHours='', stockType='', minSize=1.0,
        sizeIncrement=1.0, suggestedSizeIncrement=1.0, bondType='',
        couponType='', callable=False, putable=False, coupon=0.0,
        convertable=False, maturity='', issueDate='', nextOptionDate='',
        nextOptionPartial=False, nextOptionType='', marketRuleIds='',
    )


@pytest.fixture
def accessor(tmp_duckdb_path):
    universes = UniverseAccessor(tmp_duckdb_path, 'Universes')
    universes.insert('test', _definition(US_CONID, 'AMD', 'NASDAQ'))
    universes.insert('test', _definition(ASX_CONID, 'BHP', 'ASX'))
    return universes


@pytest.fixture
def costs(accessor):
    return build_realistic_costs([US_CONID, ASX_CONID], accessor,
                                 parse_execution_costs_config(CONFIG))


class TestCommissionSchedule:
    def test_us_minimum_applies_to_small_orders(self):
        us = CommissionSchedule(per_share=0.005, minimum=1.0, max_pct_of_value=0.01)
        assert us.fee(quantity=10, price=189.0) == pytest.approx(1.0)

    def test_us_per_share_rate_above_minimum(self):
        us = CommissionSchedule(per_share=0.005, minimum=1.0, max_pct_of_value=0.01)
        assert us.fee(quantity=1000, price=50.0) == pytest.approx(5.0)

    def test_us_cap_beats_minimum_on_tiny_orders(self):
        us = CommissionSchedule(per_share=0.005, minimum=1.0, max_pct_of_value=0.01)
        assert us.fee(quantity=1, price=20.0) == pytest.approx(0.20)

    def test_asx_percentage_with_minimum(self):
        asx = CommissionSchedule(pct_of_value=0.0008, minimum=6.0)
        assert asx.fee(quantity=50, price=45.0) == pytest.approx(6.0)
        assert asx.fee(quantity=1000, price=45.0) == pytest.approx(36.0)


class TestTickTable:
    def test_tick_follows_price_band(self):
        asx = TickTable([(0.0, 0.001), (0.10, 0.005), (2.0, 0.01)])
        assert asx.tick_for(0.05) == 0.001
        assert asx.tick_for(1.50) == 0.005
        assert asx.tick_for(45.0) == 0.01


class TestRealisticCosts:
    def test_buy_pays_half_spread_of_one_tick(self, costs):
        # US tick 0.01 at $20 -> half spread 0.005 / 20 = 2.5 bps.
        assert costs.fill_price(US_CONID, 20.0, 10, Action.BUY, QUIET_BAR) == pytest.approx(20.0 * 1.00025)

    def test_sell_receives_less_by_half_spread(self, costs):
        assert costs.fill_price(US_CONID, 20.0, 10, Action.SELL, QUIET_BAR) == pytest.approx(20.0 * 0.99975)

    def test_half_spread_never_below_floor(self, costs):
        # 0.005 / 500 = 0.1 bps, below the 0.5 bps floor.
        assert costs.fill_price(US_CONID, 500.0, 1, Action.BUY, QUIET_BAR) == pytest.approx(500.0 * 1.00005)

    def test_impact_uses_reference_bar_range_and_volume(self, costs):
        bar = pd.Series({'open': 20.0, 'high': 20.2, 'low': 19.8, 'close': 20.0, 'volume': 400.0})
        impact = 0.1 * (0.4 / 20.0) * (100 / 400.0) ** 0.5
        expected = 20.0 * (1 + 0.00025 + impact)
        assert costs.fill_price(US_CONID, 20.0, 100, Action.BUY, bar) == pytest.approx(expected)

    def test_commission_comes_from_the_instrument_venue(self, costs):
        assert costs.commission(US_CONID, 10, 189.0) == pytest.approx(1.0)
        assert costs.commission(ASX_CONID, 50, 45.0) == pytest.approx(6.0)

    def test_scaled_multiplies_spread_and_commission(self, costs):
        stressed = costs.scaled(2.0)
        assert stressed.commission(US_CONID, 10, 189.0) == pytest.approx(2.0)
        assert stressed.fill_price(US_CONID, 20.0, 10, Action.BUY, QUIET_BAR) == pytest.approx(20.0 * 1.0005)

    def test_unknown_conid_at_fill_time_fails_loudly(self, costs):
        with pytest.raises(ExecutionCostError, match='999'):
            costs.commission(999, 1, 10.0)


class TestBuildRealisticCosts:
    def test_conid_missing_from_universes_fails_loudly(self, accessor):
        with pytest.raises(ExecutionCostError, match='12345'):
            build_realistic_costs([12345], accessor, parse_execution_costs_config(CONFIG))

    def test_exchange_without_venue_fails_loudly(self, accessor):
        accessor.insert('test', _definition(777, 'SONY', 'TSEJ'))
        with pytest.raises(ExecutionCostError, match='TSEJ'):
            build_realistic_costs([777], accessor, parse_execution_costs_config(CONFIG))


class TestParseConfig:
    def test_exchange_listed_in_two_venues_is_rejected(self):
        bad = {**CONFIG, 'venues': {**CONFIG['venues'], 'dup': CONFIG['venues']['asx']}}
        with pytest.raises(ExecutionCostError, match='ASX'):
            parse_execution_costs_config(bad)


class TestFlatCosts:
    def test_matches_legacy_fixed_bps_and_per_share_commission(self):
        flat = FlatCosts(slippage_bps=1.0, commission_per_share=0.005)
        assert flat.fill_price(US_CONID, 100.0, 10, Action.BUY, QUIET_BAR) == pytest.approx(100.01)
        assert flat.commission(US_CONID, 10, 100.0) == pytest.approx(0.05)

    def test_scaled_multiplies_bps_and_commission(self):
        flat = FlatCosts(slippage_bps=1.0, commission_per_share=0.005).scaled(2.0)
        assert flat.slippage_bps == pytest.approx(2.0)
        assert flat.commission_per_share == pytest.approx(0.01)


def test_bundled_template_parses_and_covers_us_and_asx():
    config_path = Path(__file__).resolve().parent.parent / 'config_defaults' / 'execution_costs.yaml'

    config = load_execution_costs_config(str(config_path))

    assert config.venue_for('NASDAQ').commission.minimum == 1.0
    assert config.venue_for('ASX').commission.pct_of_value == 0.0008


def test_venue_calendar_is_parsed():
    with_calendar = {**CONFIG, 'venues': {'us': {**CONFIG['venues']['us'], 'calendar': 'XNYS'}}}
    assert parse_execution_costs_config(with_calendar).venue_for('NASDAQ').calendar == 'XNYS'


def test_bundled_template_declares_calendars():
    config_path = Path(__file__).resolve().parent.parent / 'config_defaults' / 'execution_costs.yaml'
    config = load_execution_costs_config(str(config_path))
    assert config.venue_for('NASDAQ').calendar == 'XNYS'
    assert config.venue_for('ASX').calendar == 'XASX'

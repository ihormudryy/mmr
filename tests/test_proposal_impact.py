"""AUDIT_ROADMAP H2: how approving a pending proposal moves the book, computed by the trader (USD only, owner)."""
from types import SimpleNamespace

import pytest

from trader.data.position_groups import PositionGroupStore
from trader.trading.portfolio_risk import PortfolioRiskAnalyzer, enrich_risk_projection
from trader.trading.proposal_impact import proposal_impacts

AAPL, MSFT, BHP = 265598, 272093, 4036812
NL = 100_000.0


def position(conid, symbol, market_value, currency="USD"):
    return SimpleNamespace(conid=conid, symbol=symbol, market_value=market_value, currency=currency)


def proposal(action="BUY", *, id=7, symbol="AAPL", conid=AAPL, quantity=10.0, price=230.0, amount=None,
             sec_type="STK"):
    return SimpleNamespace(id=id, symbol=symbol, conid=conid, action=action, quantity=quantity,
                           reference_price=price, amount=amount, sec_type=sec_type, account_id="DU1")


def group(name, members, budget=0.0):
    return SimpleNamespace(name=name, members=members, max_allocation_pct=budget)


def impact(the_proposal, positions=(), *, base_currency="USD", groups=(), currency_of=lambda conid: "USD"):
    (row,) = proposal_impacts([the_proposal], positions=positions, net_liquidation=NL,
                              base_currency=base_currency, groups=groups, instrument_currency=currency_of)
    return row


def test_a_buy_raises_the_symbol_and_its_group():
    row = impact(proposal("BUY", quantity=10, price=230.0),
                 [position(AAPL, "AAPL", 10_600.0), position(MSFT, "MSFT", 37_400.0)],
                 groups=[group("tech", ["AAPL", "MSFT"], 0.60), group("energy", ["XOM"])])
    assert row == {"proposal_id": 7, "currency": "USD", "fill_value": 2300.0,
                   "symbol_before_pct": 10.6, "symbol_after_pct": 12.9,
                   "groups": [{"name": "tech", "before_pct": 48.0, "after_pct": 50.3, "budget_pct": 60.0}],
                   "gross_delta": 2300.0}


def test_a_new_name_starts_at_zero():
    row = impact(proposal("BUY", quantity=20, price=50.0))
    assert (row["symbol_before_pct"], row["symbol_after_pct"], row["gross_delta"]) == (0.0, 1.0, 1000.0)


def test_a_sell_reduces_the_position():
    row = impact(proposal("SELL", quantity=5, price=200.0), [position(AAPL, "AAPL", 3_000.0)])
    assert (row["symbol_before_pct"], row["symbol_after_pct"], row["gross_delta"]) == (3.0, 2.0, -1000.0)


def test_a_sell_past_the_long_flips_short_and_gross_can_rise():
    row = impact(proposal("SELL", quantity=50, price=100.0), [position(AAPL, "AAPL", 2_000.0)])
    assert (row["symbol_before_pct"], row["symbol_after_pct"]) == (2.0, 3.0)      # long 2k -> short 3k
    assert row["gross_delta"] == 1000.0


def test_an_amount_sized_proposal_uses_its_amount():
    row = impact(proposal("BUY", quantity=None, price=None, amount=5_000.0))
    assert row["fill_value"] == 5000.0 and row["symbol_after_pct"] == 5.0


def test_a_position_matches_by_conid_not_by_symbol():
    other_listing = position(999, "AAPL", 9_000.0)                    # same ticker, another conId
    row = impact(proposal("BUY", quantity=10, price=100.0), [other_listing])
    assert row["symbol_before_pct"] == 0.0


@pytest.mark.parametrize("the_proposal, reason", [
    (proposal(sec_type="OPT"), "NOT_A_STOCK"),
    (proposal(conid=None), "NO_CONID"),
    (proposal(quantity=10, price=None), "NO_PRICE"),
    (proposal(quantity=None, price=None, amount=None), "NO_PRICE"),
    (proposal(quantity=0, price=100.0), "NO_PRICE"),
    (proposal("SHORT"), "UNKNOWN_SIDE"),
])
def test_a_proposal_without_a_sure_number_has_no_impact(the_proposal, reason):
    assert impact(the_proposal) == {"proposal_id": 7, "unavailable": reason}


@pytest.mark.parametrize("kwargs, positions, reason", [
    ({"base_currency": None}, (), "ACCOUNT_CURRENCY_UNKNOWN"),
    ({"base_currency": "EUR"}, (), "ACCOUNT_NOT_USD"),
    ({"currency_of": lambda conid: None}, (), "INSTRUMENT_CURRENCY_UNKNOWN"),
    ({"currency_of": lambda conid: "AUD"}, (), "INSTRUMENT_NOT_USD"),
    ({}, (position(AAPL, "AAPL", 1_000.0, currency="AUD"),), "INSTRUMENT_NOT_USD"),
    ({"groups": [group("tech", ["AAPL", "BHP"])]}, (position(BHP, "BHP", 1_000.0, currency="AUD"),),
     "MIXED_CURRENCY"),
    ({}, (position(AAPL, "AAPL.OLD", 1_000.0),), "SYMBOL_MISMATCH"),
])
def test_one_currency_or_no_impact(kwargs, positions, reason):            # owner ruling 2026-10-13
    assert impact(proposal(), positions, **kwargs) == {"proposal_id": 7, "unavailable": reason}


def test_a_held_position_gives_the_currency_without_a_lookup():
    def no_lookup(conid):
        raise AssertionError("a held position names its currency")
    row = impact(proposal(), [position(AAPL, "AAPL", 1_000.0)], currency_of=no_lookup)
    assert "unavailable" not in row


def test_the_before_weights_match_the_risk_analyzer(tmp_duckdb_path):
    store = PositionGroupStore(tmp_duckdb_path)
    store.create_group("tech", max_allocation_pct=0.6)
    store.add_member("tech", "AAPL")
    store.add_member("tech", "MSFT")
    positions = [position(AAPL, "AAPL", 10_600.0), position(MSFT, "MSFT", -7_400.0)]
    report = PortfolioRiskAnalyzer().analyze(
        [{"symbol": p.symbol, "marketValue": p.market_value} for p in positions], NL, group_store=store)
    row = impact(proposal(), positions, groups=store.list_groups())
    top = {p["symbol"]: p["pct"] for p in report.top_positions}
    assert row["symbol_before_pct"] == pytest.approx(top["AAPL"] * 100)
    assert row["groups"][0]["before_pct"] == pytest.approx(report.group_allocations[0]["pct"] * 100)


def test_the_risk_projection_carries_the_impacts():
    snapshot = SimpleNamespace(net_liquidation=NL, daily_pnl=0.0, open_order_count=0, base_currency="USD",
                               positions=(position(AAPL, "AAPL", 10_000.0),))
    payload = enrich_risk_projection(snapshot, pending_proposals=[proposal()],
                                     instrument_currency=lambda conid: "USD")
    assert payload["impacts"][0]["symbol_after_pct"] == 12.3
    assert enrich_risk_projection(snapshot)["impacts"] == []


def test_a_failing_impact_never_drops_the_projection():
    snapshot = SimpleNamespace(net_liquidation=NL, daily_pnl=0.0, open_order_count=0, base_currency="USD",
                               positions=(position(AAPL, "AAPL", 16_000.0),))

    def broken(conid):
        raise RuntimeError("universe store down")
    payload = enrich_risk_projection(snapshot, pending_proposals=[proposal(conid=MSFT, symbol="MSFT")],
                                     instrument_currency=broken)
    assert payload["impacts"] == []
    assert any(row["id"] == "concentration:AAPL" for row in payload["limits"])


@pytest.mark.parametrize("balances, currency", [
    ({"NetLiquidation:USD": "1", "NetLiquidation:BASE": "1", "DailyPnL:USD": "0"}, "USD"),
    ({"NetLiquidation:EUR": "1"}, "EUR"),
    ({"DailyPnL:USD": "0"}, None),                                    # no net liquidation reported
    ({"NetLiquidation:USD": "1", "NetLiquidation:AUD": "1"}, None),   # ambiguous
])
def test_the_snapshot_names_the_net_liquidation_currency(balances, currency):
    from trader.data.broker_state import _net_liquidation_currency
    assert _net_liquidation_currency(balances) == currency

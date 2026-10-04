"""Shared, client-taking Massive option normalization for CLI + dashboard."""
from __future__ import annotations

from typing import Any

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


def chain_records(
    client: Any,
    symbol: str,
    *,
    expiration: str,
    contract_type: str | None,
    strike_min: float | None,
    strike_max: float | None,
) -> list[dict]:
    rows: list[dict] = []
    for snap in client.list_snapshot_options_chain(
        underlying_asset=symbol, params={"expiration_date": expiration},
    ):
        details = snap.details
        if not details or not details.strike_price:
            continue
        ct = (details.contract_type or "").lower()
        if contract_type and ct != contract_type.lower():
            continue
        strike = details.strike_price
        if strike_min is not None and strike < strike_min:
            continue
        if strike_max is not None and strike > strike_max:
            continue
        bid = ask = last = volume = 0.0
        if snap.last_quote:
            bid = snap.last_quote.bid or 0.0
            ask = snap.last_quote.ask or 0.0
        if snap.last_trade:
            last = getattr(snap.last_trade, "price", 0.0) or 0.0
        if snap.day:
            volume = getattr(snap.day, "volume", 0.0) or 0.0
        mid = (bid + ask) / 2.0 if (bid and ask) else 0.0
        underlying_price = snap.underlying_asset.price or 0.0 if snap.underlying_asset else 0.0
        row = {
            "ticker": details.ticker or "",
            "type": ct,
            "strike": strike,
            "expiration": details.expiration_date or expiration,
            "bid": bid, "ask": ask, "mid": mid, "last": last, "volume": volume,
            "open_interest": snap.open_interest or 0.0,
            "break_even": snap.break_even_price or 0.0,
            "underlying_price": underlying_price,
        }
        # No fabricated data: emit IV/greeks only when the payload carries them.
        if snap.implied_volatility is not None:
            row["iv"] = snap.implied_volatility * 100.0
        if snap.greeks:
            row["delta"] = snap.greeks.delta
            row["gamma"] = snap.greeks.gamma
            row["theta"] = snap.greeks.theta
            row["vega"] = snap.greeks.vega
        rows.append(row)
    rows.sort(key=lambda r: (r["type"], r["strike"]))
    return rows


def contract_snapshot(client: Any, option_ticker: str) -> dict:
    parsed = parse_option_ticker(option_ticker)
    ticker_clean = option_ticker[2:] if option_ticker.startswith("O:") else option_ticker
    snap = client.get_snapshot_option(
        underlying_asset=parsed["symbol"], option_contract=ticker_clean,
    )
    result = {
        "ticker": option_ticker,
        "symbol": parsed["symbol"],
        "expiration": parsed["expiration"],
        "strike": parsed["strike"],
        "right": parsed["right"],
        "break_even": snap.break_even_price or 0.0,
        "open_interest": snap.open_interest or 0.0,
    }
    if snap.last_quote:
        result["bid"] = snap.last_quote.bid or 0.0
        result["ask"] = snap.last_quote.ask or 0.0
        result["mid"] = ((snap.last_quote.bid or 0.0) + (snap.last_quote.ask or 0.0)) / 2.0
    if snap.last_trade:
        result["last"] = getattr(snap.last_trade, "price", 0.0) or 0.0
    if snap.implied_volatility is not None:
        result["implied_volatility"] = f"{snap.implied_volatility * 100.0:.2f}%"
    if snap.greeks:
        result["delta"] = snap.greeks.delta or 0.0
        result["gamma"] = snap.greeks.gamma or 0.0
        result["theta"] = snap.greeks.theta or 0.0
        result["vega"] = snap.greeks.vega or 0.0
    if snap.underlying_asset:
        result["underlying_price"] = snap.underlying_asset.price or 0.0
    if snap.day:
        result["volume"] = getattr(snap.day, "volume", 0.0) or 0.0
    return result

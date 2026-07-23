"""Shared, client-taking Massive option normalization for CLI + dashboard."""
from __future__ import annotations

from datetime import datetime
from typing import Any


def parse_option_ticker(ticker: str) -> dict:
    """Parse ``O:AAPL260320C00250000`` → {symbol, expiration, right, strike}."""
    t = ticker[2:] if ticker.startswith("O:") else ticker
    i = 0
    while i < len(t) and t[i].isalpha():
        i += 1
    symbol = t[:i]
    rest = t[i:]
    if len(rest) < 9:
        raise ValueError(f"Cannot parse option ticker: {ticker}")
    date_str, right, strike_str = rest[:6], rest[6], rest[7:]
    return {
        "symbol": symbol,
        "expiration": f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:6]}",
        "right": right,
        "strike": float(strike_str) / 1000.0,
    }


def build_option_ticker(symbol: str, expiration: str, strike: float, right: str) -> str:
    date_str = datetime.strptime(expiration, "%Y-%m-%d").strftime("%y%m%d")
    return f"O:{symbol}{date_str}{right.upper()}{int(strike * 1000):08d}"


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
        greeks = snap.greeks
        delta = gamma = theta = vega = 0.0
        if greeks:
            delta = greeks.delta or 0.0
            gamma = greeks.gamma or 0.0
            theta = greeks.theta or 0.0
            vega = greeks.vega or 0.0
        underlying_price = snap.underlying_asset.price or 0.0 if snap.underlying_asset else 0.0
        rows.append({
            "ticker": details.ticker or "",
            "type": ct,
            "strike": strike,
            "expiration": details.expiration_date or expiration,
            "bid": bid, "ask": ask, "mid": mid, "last": last, "volume": volume,
            "open_interest": snap.open_interest or 0.0,
            "iv": (snap.implied_volatility or 0.0) * 100.0,
            "delta": delta, "gamma": gamma, "theta": theta, "vega": vega,
            "break_even": snap.break_even_price or 0.0,
            "underlying_price": underlying_price,
        })
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
        "implied_volatility": f"{(snap.implied_volatility or 0.0) * 100.0:.2f}%",
        "open_interest": snap.open_interest or 0.0,
    }
    if snap.last_quote:
        result["bid"] = snap.last_quote.bid or 0.0
        result["ask"] = snap.last_quote.ask or 0.0
        result["mid"] = ((snap.last_quote.bid or 0.0) + (snap.last_quote.ask or 0.0)) / 2.0
    if snap.last_trade:
        result["last"] = getattr(snap.last_trade, "price", 0.0) or 0.0
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

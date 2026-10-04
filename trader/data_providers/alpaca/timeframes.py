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

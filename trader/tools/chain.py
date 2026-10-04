"""Options chain analysis — probability distributions from market-implied volatility.

Chain data comes from an OPTIONS provider as make_option_row() dicts. The
binary-option pricing math (d2, binary_call, binary_put, monte_carlo_binary,
implied_constant_helper) is data-source agnostic.
"""

import datetime as dt
import logging
import math
import numbers
import numpy as np
import pandas as pd

from scipy.stats import norm
from typing import Any, Dict, List, Mapping, Sequence, Tuple
from uniplot.uniplot import plot


def monte_carlo_binary(S, K, T, r, sigma, Q,
                       type_='call', Ndraws=10_000_000, seed=0):
    np.random.seed(seed)
    dS = np.random.normal((r - sigma**2 / 2) * T, sigma * np.sqrt(T), size=Ndraws)
    ST = S * np.exp(dS)
    if type_ == 'call':
        return len(ST[ST > K]) / Ndraws * Q * np.exp(-r * T)
    elif type_ == 'put':
        return len(ST[ST < K]) / Ndraws * Q * np.exp(-r * T)
    else:
        raise ValueError('Type must be put or call')


def d2(S, K, T, r, sigma):
    return (np.log(S / K) + (r - sigma**2 / 2) * T) / (sigma * np.sqrt(T))


def binary_call(S, K, T, r, sigma, Q=1):
    N = norm.cdf
    return np.exp(-r * T) * N(d2(S, K, T, r, sigma)) * Q


def binary_put(S, K, T, r, sigma, Q=1):
    N = norm.cdf
    return np.exp(-r * T) * N(-d2(S, K, T, r, sigma)) * Q


def vol_by_strike(polymdl, K):
    return np.poly1d(polymdl)(K)


def new_K(chain: pd.DataFrame):
    """Strike grid over the fitted strikes only: the vol-smile polynomial is meaningless outside them."""
    return np.arange(chain.K.min(), chain.K.max(), 0.1)


def _get_massive_client(api_key: str = ''):
    """Get a Massive REST client, reading api_key from config if not provided."""
    if not api_key:
        from trader.container import Container
        cfg = Container.instance().config()
        api_key = cfg.get('massive_api_key', '')
    if not api_key:
        raise ValueError("massive_api_key not configured in trader.yaml")
    from massive import RESTClient
    return RESTClient(api_key=api_key)


def implied_constant_helper(chain: pd.DataFrame, risk_free_rate: float = 0.001):
    df = chain
    S = df.S.iloc[0]
    T = df['T'].iloc[0]

    r = risk_free_rate

    logging.info('calculating implied and constant distributions')
    vols = chain.IV.values
    Ks = chain.K.values
    poly = np.polyfit(Ks, vols, 5)
    newK = new_K(chain)
    newVols = np.poly1d(poly)(newK)

    binaries = binary_put(S, newK, T, r, newVols)

    PsT = []
    for i in range(1, len(binaries)):
        p = binaries[i] - binaries[i - 1]
        PsT.append(p)

    constant_vol = vol_by_strike(poly, S)
    binaries_const = binary_put(S, newK, T, r, constant_vol)

    const_p = []
    for i in range(1, len(binaries_const)):
        p = binaries_const[i] - binaries_const[i - 1]
        const_p.append(p)

    logging.debug('finished calculating')
    return {
        'x': newK,
        'market_implied': PsT,
        'constant': const_p,
    }


def plot_market_implied_vs_constant_console(x, market_implied, constant, title):
    plot(
        [x[1:], x[1:]],
        [constant, market_implied],
        lines=True,
        color=True,
        interactive=True,
        height=55,
        width=100,
        legend_labels=['constant', 'market_implied'],
        title=title
    )


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
    calls = [row for row in rows if row['type'] == 'call' and row['expiration'] == expiration]
    if not calls:
        raise ValueError(f'no call contracts for {expiration}')
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
    return isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def implied_constant(symbol: str, date: str, risk_free_rate: float = 0.001,
                     api_key: str = '') -> Dict[str, Any]:
    """Implied distribution via Massive (dashboard path; the CLI/SDK use the OPTIONS capability)."""
    from trader.data_providers.massive.options import MassiveOptions
    rows = MassiveOptions(_get_massive_client(api_key)).chain(symbol, date, contract_type='call')
    return implied_distribution(rows, date, risk_free_rate, dt.date.today())


def plot_chain(
    symbol: str,
    list_dates: bool,
    date: str,
    risk_free_rate: float = 0.001,
    api_key: str = '',
):
    if list_dates:
        dates = get_option_dates(symbol, api_key)
        for d in dates:
            option_date = dt.datetime.strptime(d, '%Y-%m-%d')
            print('{} [{}]   {} days from today'.format(symbol, d, (option_date - dt.datetime.now()).days))
        return

    if not date:
        dates = get_option_dates(symbol, api_key)
        if not dates:
            print(f'No option dates found for {symbol}')
            return
        date = dates[0]

    data = implied_constant(symbol, date, risk_free_rate, api_key)
    plot_market_implied_vs_constant_console(
        data['x'], data['market_implied'],
        data['constant'],
        '{} for {}, constant vs market implied'.format(symbol, date)
    )


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('Usage: python -m trader.tools.chain SYMBOL [--date YYYY-MM-DD] [--list-dates] [--risk-free-rate 0.05]')
        sys.exit(1)

    symbol = sys.argv[1]
    list_dates = '--list-dates' in sys.argv
    date = ''
    rfr = 0.05
    for i, arg in enumerate(sys.argv):
        if arg == '--date' and i + 1 < len(sys.argv):
            date = sys.argv[i + 1]
        if arg == '--risk-free-rate' and i + 1 < len(sys.argv):
            rfr = float(sys.argv[i + 1])

    plot_chain(symbol, list_dates, date, rfr)

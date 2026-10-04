"""Removes noise from stock movers: sub-$1 names and warrants/rights/units."""

import pandas as pd

INSTRUMENT_FILTER_OFF_NOTE = 'warrant filter off: Alpaca not configured'
ASSET_LIST_UNAVAILABLE_NOTE = 'warrant filter off: Alpaca asset list unavailable'


def filter_stock_movers(
    frame: pd.DataFrame, min_price: float, assets, off_note: str = INSTRUMENT_FILTER_OFF_NOTE
) -> pd.DataFrame:
    keep = frame['close'].notna() & (frame['close'] >= min_price)
    if assets is not None:
        keep &= ~frame['ticker'].map(assets.is_derivative_unit).astype(bool)
    filtered = frame[keep].copy()
    if assets is None:
        filtered['note'] = _append_note(filtered['note'], off_note)
    else:
        missing_name = filtered['name'].fillna('') == ''
        filtered.loc[missing_name, 'name'] = filtered.loc[missing_name, 'ticker'].map(assets.name)
    return filtered.reset_index(drop=True)


def _append_note(notes: pd.Series, extra: str) -> pd.Series:
    return notes.fillna('').map(lambda note: f'{note}; {extra}' if note else extra)

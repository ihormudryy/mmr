#!/usr/bin/env python3
"""[COMPAT] Parity: compare the legacy dashboard's data against /api/snapshot.

Spec §14.1: account/mode, cash + net liquidation, positions, proposals
(storage + display status), strategy state/params, risk warnings + limits,
orders, and fills must reconcile field-for-field. Floats compare within 1e-6,
timestamps at one-second resolution; everything else is exact.

Exit codes: 0 parity (explained divergences allowed), 1 unexplained
divergence, 2 collection failure. Runnable ad hoc and from the pycron
`parity_compare` one-shot.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import fnmatch
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FLOAT_TOL = 1e-6


@dataclass(frozen=True)
class FieldDivergence:
    section: str
    key: str
    field: str
    legacy: Any
    center: Any
    explained: bool


@dataclass
class ParityReport:
    generated_at: str
    counts: dict[str, int]
    divergences: list[FieldDivergence]

    @property
    def unexplained(self) -> list[FieldDivergence]:
        return [d for d in self.divergences if not d.explained]

    def exit_code(self) -> int:
        return 1 if self.unexplained else 0

    def to_json(self) -> str:
        return json.dumps({
            'generated_at': self.generated_at,
            'counts': self.counts,
            'unexplained_count': len(self.unexplained),
            'divergences': [dataclasses.asdict(d) for d in self.divergences],
        }, indent=2, default=str)


class CollectionError(RuntimeError):
    """A surface could not be read — exit 2, never report false parity."""


def to_epoch_second(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        d = value
    else:
        d = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp())


def values_match(a: Any, b: Any, kind: str = 'exact') -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if kind == 'float':
        try:
            fa, fb = float(a), float(b)
        except (TypeError, ValueError):
            return False
        if math.isnan(fa) or math.isnan(fb):
            return math.isnan(fa) and math.isnan(fb)
        return abs(fa - fb) <= FLOAT_TOL
    if kind == 'timestamp':
        try:
            return to_epoch_second(a) == to_epoch_second(b)
        except ValueError:
            return False
    if kind == 'bool':
        return bool(a) == bool(b)
    if kind == 'sorted_list':
        return sorted(map(str, a)) == sorted(map(str, b))
    if kind == 'loose_map':
        if not isinstance(a, dict) or not isinstance(b, dict) or set(a) != set(b):
            return False
        for k in a:
            try:
                if not values_match(float(a[k]), float(b[k]), 'float'):
                    return False
            except (TypeError, ValueError):
                if str(a[k]) != str(b[k]):
                    return False
        return True
    return a == b


SECTION_FIELDS: dict[str, dict[str, str]] = {
    'account':    {'account_id': 'exact', 'mode': 'exact', 'net_liquidation': 'float'},
    'cash':       {'amount': 'float'},
    'positions':  {'quantity': 'float', 'avg_cost': 'float',
                   'market_value': 'float', 'unrealized_pnl': 'float'},
    'proposals':  {'storage_status': 'exact', 'display_status': 'exact',
                   'symbol': 'exact', 'action': 'exact', 'quantity': 'float',
                   'amount': 'float', 'confidence': 'float'},
    'strategies': {'enabled': 'bool', 'params': 'loose_map'},
    'risk':       {'warnings': 'sorted_list', 'limits': 'loose_map'},
    'orders':     {'status': 'exact', 'action': 'exact', 'quantity': 'float',
                   'filled': 'float', 'avg_fill_price': 'float',
                   'limit_price': 'float'},
    'fills':      {'side': 'exact', 'quantity': 'float', 'price': 'float',
                   'commission': 'float', 'time': 'timestamp'},
}


def _allowed(allow: list[str], section: str, key: str, field: str) -> bool:
    probe = f'{section}:{key}:{field}'
    return any(fnmatch.fnmatch(probe, pattern) for pattern in allow)


def compare_keyed(section: str, legacy_rows: list[dict], center_rows: list[dict],
                  fields: dict[str, str], allow: list[str]) -> list[FieldDivergence]:
    lmap = {str(r['key']): r for r in legacy_rows}
    cmap = {str(r['key']): r for r in center_rows}
    out: list[FieldDivergence] = []
    for k in sorted(set(lmap) | set(cmap)):
        if k not in lmap or k not in cmap:
            out.append(FieldDivergence(section, k, '<presence>', k in lmap, k in cmap,
                                       _allowed(allow, section, k, '<presence>')))
            continue
        for field, kind in fields.items():
            a, b = lmap[k].get(field), cmap[k].get(field)
            if not values_match(a, b, kind):
                out.append(FieldDivergence(section, k, field, a, b,
                                           _allowed(allow, section, k, field)))
    return out


def compare_surfaces(legacy: dict, center: dict, allow: list[str]) -> ParityReport:
    divergences: list[FieldDivergence] = []
    counts: dict[str, int] = {}
    for section, fields in SECTION_FIELDS.items():
        lrows, crows = legacy.get(section, []), center.get(section, [])
        counts[section] = max(len(lrows), len(crows))
        divergences.extend(compare_keyed(section, lrows, crows, fields, allow))
    now = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return ParityReport(generated_at=now, counts=counts, divergences=divergences)


# --- normalizers -----------------------------------------------------------

def normalize_legacy_proposal(row: dict) -> dict:
    return {'key': row.get('id'), 'storage_status': row.get('storage_status'),
            'display_status': row.get('display_status'), 'symbol': row.get('symbol'),
            'action': row.get('action'), 'quantity': row.get('quantity'),
            'amount': row.get('amount'), 'confidence': row.get('confidence')}


def normalize_center_proposal(payload: dict) -> dict:
    from trader.sdk import proposal_display_status
    return {'key': payload.get('id'), 'storage_status': payload.get('status'),
            'display_status': proposal_display_status(str(payload.get('status'))),
            'symbol': payload.get('symbol'), 'action': payload.get('action'),
            'quantity': payload.get('quantity'), 'amount': payload.get('amount'),
            'confidence': payload.get('confidence')}


def _mode_for_account(account: str | None) -> str | None:
    if not account:
        return None
    return 'paper' if account.startswith('DU') else 'live'


def collect_legacy() -> dict:
    """Read the legacy surface through its own fetchers + the SDK."""
    import web.app as webapp
    from trader.trading.order_tracker import normalize_ib_status

    status = webapp.fetch_status()
    snapshot = webapp.fetch_snapshot()
    cash = webapp.fetch_cash()
    risk = webapp.fetch_risk()
    limits = webapp.fetch_risk_limits()
    if status is None or snapshot is None:
        raise CollectionError('legacy: trader_service unreachable')

    positions = [{'key': r.get('conId') or r.get('symbol'),
                  'quantity': r.get('position'), 'avg_cost': r.get('avgCost'),
                  'market_value': r.get('marketValue'),
                  'unrealized_pnl': r.get('unrealizedPNL')}
                 for r in webapp.fetch_positions()]
    proposals = [normalize_legacy_proposal(r) for r in webapp.fetch_proposals()]
    strategies = [{'key': r.get('name'), 'enabled': r.get('enabled'),
                   'params': r.get('params') or {}}
                  for r in webapp.fetch_strategies()]
    orders = [{'key': r.get('orderId'),
               'status': normalize_ib_status(str(r.get('status'))),
               'action': r.get('action'), 'quantity': r.get('quantity'),
               'filled': r.get('filled'), 'avg_fill_price': r.get('avgFillPrice'),
               'limit_price': r.get('lmtPrice')}
              for r in webapp._records(webapp._call(lambda m: m.orders()))]
    fills = [{'key': r.get('execution_id'), 'side': r.get('side'),
              'quantity': r.get('quantity'), 'price': r.get('price'),
              'commission': r.get('commission'), 'time': r.get('time')}
             for r in webapp._records(webapp._call(lambda m: m.fills()))]
    return {
        'account': [{'key': 'account', 'account_id': status.get('account'),
                     'mode': _mode_for_account(status.get('account')),
                     'net_liquidation': snapshot.get('net_liquidation')}],
        'cash': [{'key': ccy, 'amount': row.get('cash')}
                 for ccy, row in ((cash or {}).get('currencies') or {}).items()],
        'positions': positions,
        'proposals': proposals,
        'strategies': strategies,
        'risk': [{'key': 'risk', 'warnings': (risk or {}).get('warnings') or [],
                  'limits': limits or {}}],
        'orders': orders,
        'fills': fills,
    }


def login(base_url: str, token: str) -> str:
    """POST /session and return the session cookie ('name=value')."""
    req = urllib.request.Request(
        base_url + '/session',
        data=urllib.parse.urlencode({'token': token}).encode(), method='POST')
    with urllib.request.urlopen(req, timeout=10) as resp:
        cookie = (resp.headers.get('Set-Cookie') or '').split(';', 1)[0]
    if not cookie:
        raise CollectionError('center: /session returned no session cookie')
    return cookie


def collect_center(base_url: str, token: str) -> dict:
    cookie = login(base_url, token)
    req = urllib.request.Request(base_url + '/api/snapshot',
                                 headers={'Cookie': cookie})
    with urllib.request.urlopen(req, timeout=30) as resp:
        entities = json.load(resp)['entities']
    accounts = entities.get('account') or []
    positions = entities.get('position') or []
    return {
        'account': [{'key': 'account', 'account_id': a.get('account_id'),
                     'mode': a.get('mode'),
                     'net_liquidation': a.get('net_liquidation')} for a in accounts],
        'cash': [{'key': ccy, 'amount': bal.get('cash')}
                 for a in accounts
                 for ccy, bal in (a.get('balances') or {}).items()],
        'positions': [{'key': p.get('conid'), 'quantity': p.get('quantity'),
                       'avg_cost': p.get('avg_cost'),
                       'market_value': p.get('market_value'),
                       'unrealized_pnl': p.get('unrealized_pnl')} for p in positions],
        'proposals': [normalize_center_proposal(p)
                      for p in entities.get('proposal') or []],
        'strategies': [{'key': s.get('name'), 'enabled': s.get('enabled'),
                        'params': s.get('params') or {}}
                       for s in entities.get('strategy') or []],
        'risk': [{'key': 'risk', 'warnings': r.get('warnings') or [],
                  'limits': r.get('limits') or {}}
                 for r in entities.get('risk') or []
                 if str(r.get('id', '')).startswith('projection:')],
        'orders': [{'key': o.get('client_order_id'), 'status': o.get('status'),
                    'action': o.get('action'), 'quantity': o.get('quantity'),
                    'filled': o.get('filled'),
                    'avg_fill_price': o.get('avg_fill_price'),
                    'limit_price': o.get('limit_price')}
                   for o in entities.get('order') or []],
        'fills': [{'key': f.get('execution_id'), 'side': f.get('side'),
                   'quantity': f.get('quantity'), 'price': f.get('price'),
                   'commission': f.get('commission'), 'time': f.get('time')}
                  for f in entities.get('fill') or []],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:7424')
    parser.add_argument('--token-file',
                        default=os.environ.get('DASHBOARD_TOKEN_FILE', ''))
    parser.add_argument('--allow', action='append', default=[],
                        help="explained divergence pattern 'section:key:field'")
    parser.add_argument('--report-dir',
                        default=str(Path('~/.local/share/mmr/reports').expanduser()))
    parser.add_argument('--json', action='store_true',
                        help='print the report JSON to stdout')
    args = parser.parse_args()

    try:
        token = Path(args.token_file).read_text().strip()
        legacy = collect_legacy()
        center = collect_center(args.base_url, token)
    except Exception as exc:  # noqa: BLE001 — fail loudly, never false parity
        print(f'parity collection failed: {type(exc).__name__}: {exc}',
              file=sys.stderr)
        return 2

    report = compare_surfaces(legacy, center, allow=args.allow)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_path = report_dir / f'parity_{stamp}.json'
    out_path.write_text(report.to_json())
    if args.json:
        print(report.to_json())
    else:
        print(f'{len(report.unexplained)} unexplained / '
              f'{len(report.divergences)} total divergences -> {out_path}')
    return report.exit_code()


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""[COMPAT] Parity: compare the legacy dashboard's data against /api/snapshot.

Spec §14.1: account/mode, cash + net liquidation, positions, proposals
(storage + display status), strategy state/params, risk warnings + limits,
orders, and fills must reconcile field-for-field. Floats compare within 1e-6,
timestamps at one-second resolution; everything else is exact.

Exit codes: 0 parity (explained divergences allowed), 1 unexplained
divergence, 2 collection failure. Runnable ad hoc and from the pycron
`parity_compare` one-shot.

## Coverage note — sections NOT actually compared today

Three of the eight §14.1 sections cannot be honestly reconciled against the
real legacy/center APIs as they exist today, and are deliberately collected
as empty lists on BOTH sides (zero divergence, not a fabricated match) rather
than crashing or reporting systematic false positives:

- **fills**: the legacy SDK (`trader/sdk.py`) has no fills/executions
  fetcher at all -- `trades()` is the live order book, not settled
  executions. There is no legacy feed to compare the center's real
  `BrokerFillRow` data against.
- **orders**: legacy identifies an order by IB's `orderId`; the center
  identifies it by `order_entity_id`, which per
  `trader/domain/identity.py` is either `order_group_id:leg` (MMR-placed
  orders) or a random `ext:<uuid>` (externally-placed orders) -- neither
  scheme is derived from `orderId`, and legacy exposes no
  `order_group_id`/`perm_id` to bridge them. Keying by either side's native
  id against the other would flag every single order as a false
  presence-divergence, which is worse than not comparing at all.
- **risk**: `RiskProducer` (`trader/trading/risk_producer.py`) is never
  instantiated anywhere in this codebase (nothing publishes a
  `projection:<account_id>` risk row in prod), while the legacy SDK always
  has *some* live risk report. Comparing "always populated" against
  "always empty" would be a permanent, uninformative divergence, not a real
  defect to chase. `collect_center` still does a best-effort extraction of
  any `projection:*` row so this section starts working the moment
  `RiskProducer` is wired up, without touching this file again.

`account`/`cash`/`positions`/`proposals`/`strategies` ARE genuinely compared
(see `collect_legacy`/`collect_center` docstrings for the exact field
mapping and the couple of sub-fields -- proposal quantity/amount, strategy
params -- that are dropped for the same "no real source on one side" reason).
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


# NOTE on 'proposals' and 'strategies' below: the section *keys* here are a
# fixed contract `compare_surfaces` counts against (see
# `test_compare_surfaces_covers_every_required_section`) and are NOT changed.
# Only the per-section field *lists* are narrowed, for fields that have no
# real counterpart on one side (verified by reading the actual source, not
# assumed) -- comparing them would be a permanent false divergence, not a
# real defect:
#   - proposals 'quantity'/'amount': the legacy SDK's `proposals()` DataFrame
#     (`trader/sdk.py`) exposes only a formatted display string (`size`,
#     e.g. "100 sh" / "$5,000"), never numeric quantity/amount. Parsing that
#     string back into a float would reintroduce the exact rounding error it
#     was formatted with (`f'${amount:,.0f}'` truncates to whole dollars),
#     i.e. a *guaranteed* spurious float divergence on any amount-based
#     proposal -- worse than just not comparing it.
#   - strategies 'params': the center's real "strategy" entity payload
#     (`StrategyControlCommandService.acknowledge_state`,
#     `trader/trading/command_coordinator.py`) is exactly
#     `{strategy_name, action, strategy_state, control_revision,
#     state_revision, error}` (+ generic entity_id/entity_revision) --
#     documented in `web/static/command_center.js`'s "Source-vs-brief drift"
#     comment. There is no `params` field on the wire at all, so it would
#     always read back as `{}` and false-diverge against any legacy strategy
#     that actually has tunable params configured.
SECTION_FIELDS: dict[str, dict[str, str]] = {
    'account':    {'account_id': 'exact', 'mode': 'exact', 'net_liquidation': 'float'},
    'cash':       {'amount': 'float'},
    'positions':  {'quantity': 'float', 'avg_cost': 'float',
                   'market_value': 'float', 'unrealized_pnl': 'float'},
    'proposals':  {'storage_status': 'exact', 'display_status': 'exact',
                   'symbol': 'exact', 'action': 'exact', 'confidence': 'float'},
    'strategies': {'enabled': 'bool'},
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


# Raw IB order-status strings (as `trades()`/`orders()` on the legacy SDK
# surface -- `trader/sdk.py`'s `orders()` reads `t.orderStatus.status`
# straight off ib_async, no canonicalization) mapped to an UPPER vocabulary.
# `trader.trading.order_tracker` (imported by the ORIGINAL, broken version of
# this collector) does not exist anywhere in the codebase -- there is no
# shared status-mapping utility to import, so this is a small local
# implementation, per the rebuild brief. It is currently unexercised by the
# orders comparison itself (see the module docstring's "orders" coverage
# note -- orders are dropped from keyed comparison because there is no
# shared legacy/center order identity, not because of status formatting) but
# is kept + unit-tested as the documented mapping for if/when a shared order
# key makes that comparison possible again.
_IB_STATUS_TO_CANONICAL = {
    'PendingSubmit': 'PENDING_SUBMIT',
    'PendingCancel': 'PENDING_CANCEL',
    'PreSubmitted': 'PRESUBMITTED',
    'Submitted': 'SUBMITTED',
    'ApiPending': 'API_PENDING',
    'ApiCancelled': 'CANCELLED',
    'Cancelled': 'CANCELLED',
    'Filled': 'FILLED',
    'Inactive': 'INACTIVE',
}


def normalize_ib_status(raw: Any) -> str:
    """Map a raw IB order-status string to the canonical UPPER vocabulary
    (see `_IB_STATUS_TO_CANONICAL` above). Unknown statuses fall back to a
    plain `.upper()` rather than raising -- an unrecognized-but-real IB
    status should surface as a mismatch against the center's canonical set,
    not crash the whole collection."""
    if raw is None:
        return ''
    return _IB_STATUS_TO_CANONICAL.get(str(raw), str(raw).upper())


def collect_legacy() -> dict:
    """Read the legacy surface through its own fetchers + the SDK.

    `risk`/`orders`/`fills` are intentionally always `[]` here -- see the
    module docstring's "Coverage note" for why each one has no genuine
    legacy/center counterpart to compare today. They are still real keys in
    the returned dict (not omitted) so `compare_surfaces` -- which iterates
    the fixed `SECTION_FIELDS` section set -- always finds a (trivially
    matching) list rather than crashing on a missing key.
    """
    import web.app as webapp

    status = webapp.fetch_status()
    snapshot = webapp.fetch_snapshot()
    cash = webapp.fetch_cash()
    if status is None or snapshot is None:
        raise CollectionError('legacy: trader_service unreachable')

    # Positions: `fetch_positions()` -> `_records(m.portfolio())`, whose
    # columns are already the real, verified field names (conId/position/
    # avgCost/marketValue/unrealizedPNL) -- no rename needed here.
    positions = [{'key': r.get('conId') or r.get('symbol'),
                  'quantity': r.get('position'), 'avg_cost': r.get('avgCost'),
                  'market_value': r.get('marketValue'),
                  'unrealized_pnl': r.get('unrealizedPNL')}
                 for r in webapp.fetch_positions()]
    proposals = [normalize_legacy_proposal(r) for r in webapp.fetch_proposals()]
    # Strategies: `fetch_strategies()` already derives 'enabled' from the
    # runtime state name via `_ENABLED_STATES` (RUNNING/
    # WAITING_HISTORICAL_DATA/INSTALLED) -- reused as-is on the center side
    # below so both collectors apply the identical predicate. 'params' is
    # deliberately NOT read here even though the legacy row has it -- see
    # `SECTION_FIELDS`'s comment for why the center side can never supply it.
    strategies = [{'key': r.get('name'), 'enabled': r.get('enabled')}
                  for r in webapp.fetch_strategies()]
    return {
        'account': [{'key': 'account', 'account_id': status.get('account'),
                     'mode': _mode_for_account(status.get('account')),
                     'net_liquidation': snapshot.get('net_liquidation')}],
        'cash': [{'key': ccy, 'amount': row.get('cash')}
                 for ccy, row in ((cash or {}).get('currencies') or {}).items()],
        'positions': positions,
        'proposals': proposals,
        'strategies': strategies,
        'risk': [],
        'orders': [],
        'fills': [],
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


_CASH_TAG = 'TotalCashValue'


def _center_cash_rows(account: dict) -> list[dict]:
    """Parse one account's `balances` (`dict[str, str]` keyed
    `"TAG:CURRENCY"`, e.g. `"TotalCashValue:USD" -> "12345.67"` -- see
    `trader/trading/broker_ingest.py`'s `merge_account_value`, which stores
    literally `f"{tag}:{currency}"` -> `str(value)`) into the same
    `{key: <currency>, amount: <float>}` shape `collect_legacy` builds from
    `account_cash()['currencies']` (C6).

    Only the `TotalCashValue` tag is used (the brief's own worked example,
    and the same tag `broker_ingest.py`'s `_ACCOUNT_TAG_COLUMNS` maps to the
    account's scalar `total_cash` column) -- `balances` otherwise contains
    every account-value tag IB streams (NetLiquidation, BuyingPower, ...),
    not just cash-shaped ones. The pseudo-currency `BASE` row (IB's
    consolidated summary line) is skipped, mirroring the legacy SDK's own
    `get_account_cash_by_currency` doc: "a consolidated BASE row we skip".
    """
    rows = []
    for composite, raw_value in (account.get('balances') or {}).items():
        tag, _, currency = str(composite).partition(':')
        if tag != _CASH_TAG or not currency or currency == 'BASE':
            continue
        try:
            amount = float(raw_value)
        except (TypeError, ValueError):
            amount = None
        rows.append({'key': currency, 'amount': amount})
    return rows


def _center_risk_rows(risk: dict) -> list[dict]:
    """Best-effort extraction of the center's risk PROJECTION rows.

    `risk` (from `snapshot_view()`) is a dict keyed by `entity_id`, e.g.
    `"projection:<account_id>"` (`trader/domain/identity.py`'s
    `risk_projection_entity_id`) -- not a list, and the field is `entity_id`
    (added generically by `DashboardState._place`), not `id`.

    `RiskProducer` (`trader/trading/risk_producer.py`) is never
    instantiated anywhere in this codebase today (no call site constructs
    one), so in a real deployment nothing ever publishes a `projection:*`
    row and this returns `[]` -- matching `collect_legacy`'s permanently
    empty `risk` (see the module docstring's coverage note for why both
    sides stay empty rather than reporting a manufactured divergence). This
    extraction is real (not stubbed) so wiring `RiskProducer` up in the
    future makes this section start working without touching this file --
    though `collect_legacy` would then need a matching follow-up, since its
    `risk_report()`/`get_risk_limits()` fields aren't guaranteed to line up
    with whatever `compute_projection()` callable ends up injected.
    """
    return [{'key': entity_id, 'warnings': payload.get('warnings') or [],
             'limits': payload.get('limits') or {}}
            for entity_id, payload in (risk or {}).items()
            if str(entity_id).startswith('projection:')]


def _merge_active_terminal(section: dict) -> list[dict]:
    """`proposals`/`orders` in `snapshot_view()` are each
    `{"active": [...], "terminal": [...]}` (C2) -- flatten to one list, the
    shape `compare_keyed` expects."""
    return list((section or {}).get('active') or []) + \
        list((section or {}).get('terminal') or [])


def _round2(value: Any) -> Any:
    """Round `net_liquidation` to 2dp (C5) -- the legacy side's
    `portfolio_snapshot()` already rounds, and the center's raw
    `BrokerAccountRow.net_liquidation` is an unrounded double; comparing the
    two unrounded would false-diverge on sub-cent noise."""
    return round(value, 2) if isinstance(value, (int, float)) else value


def collect_center(base_url: str, token: str) -> dict:
    """Read the command-center surface via `/api/snapshot`
    (`DashboardState.snapshot_view()`, `web/command_center/state.py`), and
    normalize it into the same shape `collect_legacy` produces.

    `/api/snapshot` has NO `entities` key (C0) -- its top level is the
    PLURAL collections `accounts`/`positions`/`proposals`/`orders`/`fills`/
    `strategies`/`risk` (+ `quotes`/`health`/schema bookkeeping, unused
    here). Field names come from `Broker*Row.to_payload()`
    (`trader/data/broker_state.py`) and `ProposalRecord.to_payload()`
    (`trader/data/proposal_repository.py`). See the module docstring's
    "Coverage note" for `risk`/`orders`/`fills`.
    """
    import web.app as webapp  # reuse `_ENABLED_STATES` so both sides apply
    # the identical strategy-enabled predicate (RUNNING/
    # WAITING_HISTORICAL_DATA/INSTALLED) -- see `collect_legacy`'s comment.

    cookie = login(base_url, token)
    req = urllib.request.Request(base_url + '/api/snapshot',
                                 headers={'Cookie': cookie})
    with urllib.request.urlopen(req, timeout=30) as resp:
        view = json.load(resp)

    accounts = view.get('accounts') or []
    positions = view.get('positions') or []
    proposals = _merge_active_terminal(view.get('proposals') or {})
    strategies = view.get('strategies') or []

    return {
        'account': [{'key': 'account', 'account_id': a.get('account_id'),
                     'mode': a.get('account_mode'),  # C4
                     'net_liquidation': _round2(a.get('net_liquidation'))}  # C5
                    for a in accounts],
        'cash': [row for a in accounts for row in _center_cash_rows(a)],  # C6
        'positions': [{'key': p.get('conid'), 'quantity': p.get('quantity'),
                       'avg_cost': p.get('average_cost'),  # C7
                       'market_value': p.get('market_value'),
                       'unrealized_pnl': p.get('unrealized_pnl')}
                      for p in positions],
        'proposals': [normalize_center_proposal(p) for p in proposals],  # C2
        # Strategies: the real wire payload has `strategy_name` (not
        # `name`) and `strategy_state` (not `enabled`) -- see
        # `web/static/command_center.js`'s "Source-vs-brief drift" comment
        # for the verified real shape. `enabled` is derived with the exact
        # same predicate `webapp.fetch_strategies()` uses on the legacy side
        # so both sides classify state names identically.
        'strategies': [
            {'key': s.get('strategy_name') or s.get('entity_id'),
             'enabled': str(s.get('strategy_state') or '').upper()
             in webapp._ENABLED_STATES}
            for s in strategies],
        'risk': _center_risk_rows(view.get('risk') or {}),
        'orders': [],
        'fills': [],
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

"""The "AI-written vs catalogue" scoreboard view (SP3 spec 7.8; Plan 5 rulings 18-21).

Read-only and paper labelled: a report that never changes a gate. Pure over the experiment's round trips, its sealed
sessions and the judged deployment versions. ``None`` is unknown and never 0.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from trader.automation.ai_deployments import STRATEGY_KIND
from trader.research.strategy_key import authored_digest_of_path, is_authored_path
from trader.scoreboard.metrics import daily_sharpe
from trader.scoreboard.ports import session_date_et

logger = logging.getLogger(__name__)
MIN_CLOSED_TRIPS = 20
YES, NO, NOT_ENOUGH_DATA = "yes", "no", "not enough data"
LABEL = "PAPER"
NOTE = "A report only: it never changes a gate."
AUTHORED, CATALOGUE = "authored", "catalogue"


class AuthoredScopeUnreadable(Exception):
    """A judged version that cannot be put on exactly one side: the whole view is unknown, never a guess."""


@dataclass(frozen=True)
class DeploymentWindow:
    version_digest: str
    strategy_digest: str
    conids: frozenset[int]
    first_session: dt.date
    last_session: dt.date

    def active_on(self, day: dt.date) -> bool:
        return self.first_session <= day <= self.last_session


@dataclass(frozen=True)
class AuthoredScope:
    windows: tuple[DeploymentWindow, ...]
    authored_digests: frozenset[str]

    def side_of(self, strategy_digest: Any) -> Optional[str]:
        """Ruling 18: authored by Plan 3's records, catalogue by any other judged version, else neither."""
        if strategy_digest in self.authored_digests:
            return AUTHORED
        if any(window.strategy_digest == strategy_digest for window in self.windows):
            return CATALOGUE
        return None


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def common_scope(scope: AuthoredScope, sessions: Iterable[Any]) -> dict[dt.date, frozenset[int]]:
    """Ruling 19: the sessions on which both sides had an active judged version, with the conids both bind."""
    common: dict[dt.date, frozenset[int]] = {}
    for day in sorted({_as_date(session) for session in sessions}):
        covered: dict[str, set[int]] = {AUTHORED: set(), CATALOGUE: set()}
        for window in scope.windows:
            if window.active_on(day):
                covered[scope.side_of(window.strategy_digest)].update(window.conids)
        shared = frozenset(covered[AUTHORED] & covered[CATALOGUE])
        if shared:
            common[day] = shared
    return common


def pnl_drawdown_usd(daily_pnl: Sequence[float]) -> Optional[float]:
    """The largest fall of cumulative P&L from its running peak, in USD; the curve starts at 0 (Ruling 20)."""
    if not daily_pnl:
        return None
    total = peak = worst = 0.0
    for pnl in daily_pnl:
        total += pnl
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def _trips_by_side(trips: Iterable[Mapping[str, Any]], scope: AuthoredScope,
                   common: Mapping[dt.date, frozenset[int]]) -> dict[str, list]:
    by_side: dict[str, list] = {AUTHORED: [], CATALOGUE: []}
    for trip in trips:
        if trip.get("status") != "CLOSED":
            continue
        side = scope.side_of(trip.get("strategy_version"))
        if side is None:
            continue
        if int(trip["conid"]) in common.get(_as_date(trip["opened_session"]), frozenset()):
            by_side[side].append(trip)
    return by_side


def _side_view(trips: Sequence[Mapping[str, Any]], days: Sequence[dt.date]) -> dict:
    unresolved = sum(1 for trip in trips if trip["net_pnl_usd"] is None)
    daily = dict.fromkeys(days, 0.0)
    for trip in trips:
        if trip["net_pnl_usd"] is not None:
            daily[_as_date(trip["opened_session"])] += float(trip["net_pnl_usd"])
    series = [daily[day] for day in days]
    known = unresolved == 0
    return {"closed_trips": len(trips), "unresolved_fee_trips": unresolved,
            "pnl_usd": sum(series) if known else None,
            "sharpe_daily": daily_sharpe(series)[0] if known else None,
            "max_drawdown_usd": pnl_drawdown_usd(series) if known else None,
            "strategies": sorted({trip["strategy_version"] for trip in trips})}


def _verdict(authored: Mapping[str, Any], catalogue: Mapping[str, Any]) -> tuple[str, Optional[str]]:
    if min(authored["closed_trips"], catalogue["closed_trips"]) < MIN_CLOSED_TRIPS:
        return NOT_ENOUGH_DATA, "TOO_FEW_TRIPS"
    if authored["unresolved_fee_trips"] or catalogue["unresolved_fee_trips"]:
        return NOT_ENOUGH_DATA, "FEES_PENDING"
    if authored["sharpe_daily"] is None or catalogue["sharpe_daily"] is None:
        return NOT_ENOUGH_DATA, "SHARPE_UNKNOWN"
    better = authored["sharpe_daily"] > catalogue["sharpe_daily"]
    no_worse = authored["max_drawdown_usd"] <= catalogue["max_drawdown_usd"]
    return (YES, None) if better and no_worse else (NO, None)


def _view(verdict: str, reason: Optional[str], days: Sequence[dt.date], conids: Iterable[int],
          authored: Optional[dict], catalogue: Optional[dict]) -> dict:
    return {"label": LABEL, "note": NOTE, "verdict": verdict, "reason": reason, "min_closed_trips": MIN_CLOSED_TRIPS,
            "sessions": len(days), "conids": sorted(set(conids)), "authored": authored, "catalogue": catalogue}


def compare_authored_vs_catalogue(*, trips: Sequence[Mapping[str, Any]], sessions: Iterable[Any],
                                  scope: Optional[AuthoredScope]) -> dict:
    """Spec 7.8: yes only with a higher daily-P&L Sharpe, no worse max drawdown and at least 20 closed trips on
    each side, on the same conids and sessions; no when both sides have 20 trips and that fails."""
    if scope is None:
        return _view(NOT_ENOUGH_DATA, "SCOPE_UNKNOWN", (), (), None, None)
    common = common_scope(scope, sessions)
    if not common:
        return _view(NOT_ENOUGH_DATA, "NO_COMMON_SESSIONS", (), (), None, None)
    days = sorted(common)
    by_side = _trips_by_side(trips, scope, common)
    authored, catalogue = _side_view(by_side[AUTHORED], days), _side_view(by_side[CATALOGUE], days)
    verdict, reason = _verdict(authored, catalogue)
    return _view(verdict, reason, days, set().union(*common.values()), authored, catalogue)


def read_authored_scope(db: Any) -> AuthoredScope:
    """SP2c judged versions with their base deployment and withdrawal, and Plan 3's authored digests. Raises when
    a table is missing, a row does not parse or a version fits neither side; the report then says SCOPE_UNKNOWN
    with a warning."""
    rows = db.execute(
        "SELECT v.digest, v.record_json, d.kind, d.record_json, w.withdrawn_at FROM ai_deployment_versions v "
        "LEFT JOIN ai_deployments d ON d.digest = v.base_digest "
        "LEFT JOIN ai_deployment_withdrawals w ON w.version_digest = v.digest ORDER BY v.digest", fetch="all")
    authored = frozenset(row[0] for row in db.execute("SELECT digest FROM authored_strategies", fetch="all"))
    return AuthoredScope(tuple(_window(*row, authored_digests=authored) for row in rows), authored)


def _window(version_digest: str, version_json: str, base_kind: Optional[str], base_json: Optional[str],
            withdrawn_at: Any, *, authored_digests: frozenset[str]) -> DeploymentWindow:
    if base_json is None:
        raise AuthoredScopeUnreadable(f"version {version_digest} has no base deployment")
    if base_kind != STRATEGY_KIND:
        raise AuthoredScopeUnreadable(f"version {version_digest} has a {base_kind!r} base deployment")
    version, base = json.loads(version_json), json.loads(base_json)
    _require_one_side(version_digest, base, authored_digests)
    last = dt.date.fromisoformat(version["expiry_session"])
    if withdrawn_at is not None:
        last = min(last, session_date_et(withdrawn_at))
    return DeploymentWindow(version_digest, base["strategy_digest"], frozenset(int(c) for c in base["conids"]),
                            dt.date.fromisoformat(version["first_session"]), last)


def _require_one_side(version_digest: str, base: Mapping[str, Any], authored_digests: frozenset[str]) -> None:
    """The path form and Plan 3's records must agree: an ``authored:sha256:<hex>`` path names its own strategy
    digest and a stored authored record; a catalogue path never carries an authored digest."""
    path, digest = base.get("strategy_path"), base["strategy_digest"]
    if path is None:
        return
    if is_authored_path(path):
        if authored_digest_of_path(path) != digest:
            raise AuthoredScopeUnreadable(f"version {version_digest}: path {path} names another digest than "
                                          f"{digest}")
        if digest not in authored_digests:
            raise AuthoredScopeUnreadable(f"version {version_digest}: authored {digest} has no authored record")
    elif digest in authored_digests:
        raise AuthoredScopeUnreadable(f"version {version_digest}: catalogue path {path} carries authored {digest}")


def scope_or_warning(read: Optional[Callable[[], AuthoredScope]]) -> tuple[Optional[AuthoredScope], list[dict]]:
    """No reader: unknown. A reader that fails: unknown plus a warning that says why, never an empty scope."""
    if read is None:
        return None, []
    try:
        return read(), []
    except Exception as exc:
        detail = (f"the AI-written vs catalogue scope could not be read ({type(exc).__name__}: {exc}); "
                  "the view says not enough data")
        logger.error("scoreboard: %s", detail)
        return None, [{"code": "AUTHORED_SCOPE_UNKNOWN", "detail": detail}]

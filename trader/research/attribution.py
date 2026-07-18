"""P&L attribution over a frozen regime taxonomy (P2 Task 5, design §8.4).

Attribution answers "where did the edge come from?" -- by month, instrument,
regime, or volatility bucket -- so the eligibility gate can refuse a book whose
profit is one month, one name, or one regime. Two invariants make it honest:

* **The regime taxonomy is FROZEN before any result is inspected** (§8.4). It is
  a fixed, ordered tuple of ``RegimeDefinition`` with fixed numeric thresholds,
  and ``regime_taxonomy_digest()`` captures its identity so it can be committed
  ahead of time. ``classify_regime`` is a pure function of a per-trade context.
* **Insufficient buckets are NEVER assumed positive.** A bucket below
  ``min_samples`` is flagged ``adequate=False``; the caller must treat it as "no
  evidence", not "passes".

Extreme / abnormal-volatility round trips are partitioned out and reported
SEPARATELY (§8.1) yet remain part of the record.

Pure + deterministic: no I/O, no wall-clock, no RNG.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from trader.research.canonical import sha256_digest

REGIME_TAXONOMY_PREFIX = "regime_taxonomy"

# Frozen volatility-band thresholds (fractional per-period volatility). A bar's
# regime volatility is measured however the caller supplies it in ``context``;
# these bounds define the band names and never change without a new taxonomy id.
VOL_LOW_MAX = 0.01     # v <  1%  -> "low"
VOL_HIGH_MIN = 0.02    # v >= 2%  -> "high"; between -> "normal"

_VALID_BY = ("month", "instrument", "regime", "volatility")


@dataclass(frozen=True)
class RegimeDefinition:
    """One cell of the frozen taxonomy: a (trend, volatility-band) pair."""

    label: str
    trend: str      # "bull" | "bear"
    vol_band: str   # "low" | "normal" | "high"


# The taxonomy: 2 trend states x 3 volatility bands, in a fixed order. Frozen
# BEFORE results are inspected -- its digest is committed with the experiment.
FROZEN_REGIMES: tuple[RegimeDefinition, ...] = (
    RegimeDefinition("bull_low_vol", "bull", "low"),
    RegimeDefinition("bull_normal_vol", "bull", "normal"),
    RegimeDefinition("bull_high_vol", "bull", "high"),
    RegimeDefinition("bear_low_vol", "bear", "low"),
    RegimeDefinition("bear_normal_vol", "bear", "normal"),
    RegimeDefinition("bear_high_vol", "bear", "high"),
)

REGIME_LABELS: tuple[str, ...] = tuple(r.label for r in FROZEN_REGIMES)


def volatility_band(volatility: float) -> str:
    """Fixed-threshold volatility band. Deterministic, no data-dependent cuts."""
    v = float(volatility)
    if v < VOL_LOW_MAX:
        return "low"
    if v < VOL_HIGH_MIN:
        return "normal"
    return "high"


def classify_regime(context: Mapping[str, Any]) -> str:
    """Pure classifier: ``context`` carries ``trend`` (signed) and ``volatility``.

    ``trend > 0`` -> "bull", else "bear". Volatility is banded by the frozen
    thresholds. Returns a label from ``REGIME_LABELS``.
    """
    trend = "bull" if float(context["trend"]) > 0.0 else "bear"
    band = volatility_band(context["volatility"])
    return f"{trend}_{band}_vol"


def regime_taxonomy_digest() -> str:
    """Content digest of the frozen taxonomy identity (labels + thresholds).

    Committing this ahead of results proves the regimes were fixed before the
    numbers were seen.
    """
    body = {
        "regimes": [
            {"label": r.label, "trend": r.trend, "vol_band": r.vol_band}
            for r in FROZEN_REGIMES
        ],
        "vol_low_max": VOL_LOW_MAX,
        "vol_high_min": VOL_HIGH_MIN,
    }
    return sha256_digest(REGIME_TAXONOMY_PREFIX, body)


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RoundTrip:
    """One realized BUY-open -> SELL-close round trip.

    Carries the close timestamp + conid + realized ``pnl`` so month/instrument
    grouping works. ``regime`` / ``volatility_bucket`` are optional annotations
    (set by the caller); ``extreme`` flags an abnormal-volatility episode for the
    §8.1 partition.
    """

    conid: int
    open_time: datetime
    close_time: datetime
    quantity: float
    entry_price: float
    exit_price: float
    pnl: float
    regime: Optional[str] = None
    volatility_bucket: Optional[str] = None
    extreme: bool = False


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _trade_time(t: Any) -> Any:
    ts = _field(t, "timestamp")
    return ts if ts is not None else datetime.min


def build_round_trips(trades: Sequence[Any], *,
                      annotate: Optional[Callable[[RoundTrip], RoundTrip]] = None
                      ) -> list[RoundTrip]:
    """Build FIFO round trips from a trade list (``BacktestTrade`` or dicts).

    Mirrors ``backtest_stats.round_trip_pnls``: BUY fills are stacked per conid
    and matched FIFO by subsequent SELLs, apportioning commission per share and
    subtracting BOTH sides. Unmatched BUYs (still-open at end) are ignored.
    ``annotate`` receives each freshly-built ``RoundTrip`` and returns a
    (possibly ``replace``-d) copy -- the hook that attaches regime / volatility /
    extreme labels.
    """
    lots: dict[int, list[list[Any]]] = {}  # conid -> [ [qty, price, comm_ps, open_time], ... ]
    out: list[RoundTrip] = []
    for t in sorted(trades, key=_trade_time):
        try:
            conid = int(_field(t, "conid", 0))
            qty = float(_field(t, "quantity", 0))
            price = float(_field(t, "price", 0))
            comm = float(_field(t, "commission", 0.0))
        except (TypeError, ValueError):
            continue
        if qty <= 0 or price <= 0:
            continue
        action = str(_field(t, "action", "")).upper()
        ts = _field(t, "timestamp")
        comm_ps = comm / qty
        if "BUY" in action:
            lots.setdefault(conid, []).append([qty, price, comm_ps, ts])
        elif "SELL" in action:
            remaining = qty
            queue = lots.get(conid)
            while remaining > 0 and queue:
                lot = queue[0]
                matched = min(lot[0], remaining)
                pnl = (matched * (price - lot[1])
                       - matched * lot[2]        # buy-side commission
                       - matched * comm_ps)      # sell-side commission
                rt = RoundTrip(
                    conid=conid, open_time=lot[3], close_time=ts,
                    quantity=matched, entry_price=lot[1], exit_price=price,
                    pnl=pnl)
                if annotate is not None:
                    rt = annotate(rt)
                out.append(rt)
                lot[0] -= matched
                remaining -= matched
                if lot[0] <= 1e-12:
                    queue.pop(0)
    return out


# --------------------------------------------------------------------------- #
# Attribution table
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BucketStat:
    """One attribution bucket.

    ``share`` is NET: ``pnl / total_net_pnl`` (for reporting). The eligibility
    concentration gate uses ``statistics.profit_concentration`` (positive-only
    denominator) instead -- these are deliberately different denominators.
    ``adequate`` is ``False`` when ``n_trades < min_samples``; such a bucket
    carries NO positive assumption.
    """

    key: Any
    pnl: float
    n_trades: int
    share: float
    adequate: bool


@dataclass(frozen=True)
class AttributionTable:
    by: str
    buckets: tuple[BucketStat, ...]
    total_pnl: float
    min_samples: int

    def bucket(self, key: Any) -> Optional[BucketStat]:
        for b in self.buckets:
            if b.key == key:
                return b
        return None

    @property
    def adequate_buckets(self) -> tuple[BucketStat, ...]:
        return tuple(b for b in self.buckets if b.adequate)

    @property
    def positive_fraction_of_adequate(self) -> Optional[float]:
        """Fraction of adequate-sample buckets with net-positive P&L, or ``None``
        when there are no adequate buckets (explicitly "no evidence")."""
        adeq = self.adequate_buckets
        if not adeq:
            return None
        return sum(1 for b in adeq if b.pnl > 0) / len(adeq)


def _month_key(rt: RoundTrip) -> str:
    ct = rt.close_time
    return f"{ct.year:04d}-{ct.month:02d}"


def _key_fn(by: str) -> Callable[[RoundTrip], Any]:
    if by == "month":
        return _month_key
    if by == "instrument":
        return lambda rt: rt.conid
    if by == "regime":
        def _regime(rt: RoundTrip) -> Any:
            if rt.regime is None:
                raise ValueError("attribute(by='regime') requires each round trip "
                                 "to carry a regime; got an unannotated round trip")
            return rt.regime
        return _regime
    if by == "volatility":
        def _vol(rt: RoundTrip) -> Any:
            if rt.volatility_bucket is None:
                raise ValueError("attribute(by='volatility') requires each round "
                                 "trip to carry a volatility_bucket")
            return rt.volatility_bucket
        return _vol
    raise ValueError(f"unknown attribution axis {by!r}; expected one of {_VALID_BY}")


def attribute(round_trips: Sequence[RoundTrip], *, by: str,
              min_samples: int = 5) -> AttributionTable:
    """Group round trips by ``by`` in {month, instrument, regime, volatility}.

    Each bucket reports ``pnl``, ``n_trades``, net ``share``, and an
    ``adequate`` flag (``n_trades >= min_samples``). Buckets are sorted by key
    for determinism.
    """
    if by not in _VALID_BY:
        raise ValueError(f"unknown attribution axis {by!r}; expected one of {_VALID_BY}")
    key_fn = _key_fn(by)
    groups: dict[Any, list[RoundTrip]] = {}
    for rt in round_trips:
        groups.setdefault(key_fn(rt), []).append(rt)
    total = sum(rt.pnl for rt in round_trips)
    buckets: list[BucketStat] = []
    for key in sorted(groups, key=lambda k: (str(type(k)), k)):
        rts = groups[key]
        b_pnl = sum(rt.pnl for rt in rts)
        n = len(rts)
        share = (b_pnl / total) if total != 0 else 0.0
        buckets.append(BucketStat(key=key, pnl=b_pnl, n_trades=n, share=share,
                                  adequate=(n >= min_samples)))
    return AttributionTable(by=by, buckets=tuple(buckets), total_pnl=float(total),
                            min_samples=min_samples)


def extreme_event_partition(round_trips: Sequence[RoundTrip], *,
                            is_extreme: Optional[Callable[[RoundTrip], bool]] = None
                            ) -> tuple[tuple[RoundTrip, ...], tuple[RoundTrip, ...]]:
    """Split into ``(normal, extreme)`` round trips.

    By default partitions on the ``RoundTrip.extreme`` flag; pass ``is_extreme``
    to classify by a predicate (e.g. abnormal realized volatility). The extreme
    trips are RETURNED (part of the record), never dropped -- §8.1 requires they
    be reported separately yet remain in eligibility.
    """
    pred = is_extreme if is_extreme is not None else (lambda rt: rt.extreme)
    normal = tuple(rt for rt in round_trips if not pred(rt))
    extreme = tuple(rt for rt in round_trips if pred(rt))
    return normal, extreme

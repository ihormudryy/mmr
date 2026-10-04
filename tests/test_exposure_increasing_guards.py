"""Unit tests for the pure pre-dispatch guard ``check_exposure_increasing_guards``.

Focus: the configurable PAPER quote-staleness bound. Live mode already demands a
fresh live-feed quote (age > 5s → QUOTE_STALE); paper mode historically enforced
only the price-drift band, so a market-closed / cached-last-value snapshot could
pass. ``paper_max_quote_age_seconds`` adds an opt-in age gate for paper without
penalising the delayed-data quotes paper commonly runs on (hence a generous,
operator-set bound rather than the live 5s).
"""
import datetime as dt
from types import SimpleNamespace

from trader.trading.command_coordinator import check_exposure_increasing_guards
from trader.trading.proposal_command_service import ExecutableQuote

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 16, 14, 0, tzinfo=UTC)


def _record(reference_price=210.0, max_drift_bps=50.0, action="BUY"):
    return SimpleNamespace(
        action=action, execution={"order_type": "MARKET"},
        reference_price=reference_price, max_price_drift_bps=max_drift_bps,
    )


def _quote(age_seconds=0.0, price=210.0, side="ask", feed_type="delayed"):
    return ExecutableQuote(
        conid=265598, side=side, price=price,
        market_timestamp=NOW - dt.timedelta(seconds=age_seconds),
        feed_type=feed_type, session_state="continuous",
    )


def test_paper_stale_quote_rejected_when_bound_set():
    problem = check_exposure_increasing_guards(
        _record(), _quote(age_seconds=3600), NOW, "paper",
        paper_max_quote_age_seconds=1800.0,
    )
    assert problem is not None and problem.code == "QUOTE_STALE"


def test_paper_fresh_delayed_quote_passes_with_bound():
    # A 60s-old delayed quote is fine under a 30-min paper bound — the gate
    # targets hours-old cached last-values, not normal delayed data.
    problem = check_exposure_increasing_guards(
        _record(), _quote(age_seconds=60), NOW, "paper",
        paper_max_quote_age_seconds=1800.0,
    )
    assert problem is None


def test_paper_no_bound_is_backward_compatible():
    # Default (bound None): no paper age gate, legacy behaviour — even a very
    # old quote passes the age check (drift band still applies elsewhere).
    problem = check_exposure_increasing_guards(
        _record(), _quote(age_seconds=99_999), NOW, "paper",
    )
    assert problem is None


def test_paper_future_quote_rejected_as_clock_skew():
    problem = check_exposure_increasing_guards(
        _record(), _quote(age_seconds=-3600), NOW, "paper",
        paper_max_quote_age_seconds=1800.0,
    )
    assert problem is not None and problem.code == "SOURCE_CLOCK_SKEW"

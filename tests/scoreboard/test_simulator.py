import datetime as dt

from trader.scoreboard.simulator import Bar, SimInput, simulate_long_bracket

UTC = dt.timezone.utc
DECIDED = dt.datetime(2026, 10, 6, 14, 30, 20, tzinfo=UTC)       # inside the 14:30 minute
FLATTEN = dt.datetime(2026, 10, 6, 19, 45, tzinfo=UTC)
TRADE = SimInput(conid=265598, quantity=10, reference_price=100.0, stop_price=98.0, target_price=104.0,
                 decided_at=DECIDED, flatten_start_utc=FLATTEN)


def bar(hhmm, o=100.0, h=100.5, l=99.5, c=100.0, day=6):
    hour, minute = divmod(hhmm, 100)
    return Bar(dt.datetime(2026, 10, day, hour, minute, tzinfo=UTC), o, h, l, c)


def quiet_day(first=1431, last=1944, step=15):
    """One quiet bar every ``step`` minutes: a calm stock that trades often enough."""
    out, now = [], dt.datetime(2026, 10, 6, first // 100, first % 100, tzinfo=UTC)
    while now.hour * 100 + now.minute <= last:
        out.append(bar(now.hour * 100 + now.minute))
        now += dt.timedelta(minutes=step)
    return out


def with_flatten(bars, open_=101.0):
    return [*bars, bar(1945, o=open_, h=open_ + 0.2, l=open_ - 0.2, c=open_)]


def test_no_hit_exits_at_the_open_of_the_flatten_bar():
    result = simulate_long_bracket(TRADE, with_flatten(quiet_day(), 101.0))
    assert (result.status, result.exit_kind, result.exit_price, result.pnl_usd, result.trades) == (
        "COMPLETE", "FLATTEN", 101.0, 10.0, 1)
    assert result.exit_at == FLATTEN and len(result.bars_digest) == 64


def test_the_stop_exits_at_the_stop():
    bars = with_flatten([bar(1431), bar(1500, o=99.0, l=97.5), bar(1600)])
    result = simulate_long_bracket(TRADE, bars)
    assert (result.exit_kind, result.exit_price, result.pnl_usd) == ("STOP", 98.0, -20.0)


def test_gap_through_the_stop_fills_at_the_open():
    result = simulate_long_bracket(TRADE, with_flatten([bar(1431), bar(1500, o=96.5, h=97.0, l=96.0, c=96.8)]))
    assert (result.exit_kind, result.exit_price, result.pnl_usd) == ("STOP", 96.5, -35.0)


def test_the_target_exits_at_the_target():
    result = simulate_long_bracket(TRADE, with_flatten([bar(1431), bar(1500, h=104.7)]))
    assert (result.exit_kind, result.exit_price, result.pnl_usd) == ("TARGET", 104.0, 40.0)


def test_same_bar_stop_and_target_is_a_stop():
    result = simulate_long_bracket(TRADE, with_flatten([bar(1431), bar(1500, h=105.0, l=97.0)]))
    assert result.exit_kind == "STOP" and result.exit_price == 98.0


def test_the_minute_of_the_decision_is_not_scanned():
    bars = with_flatten([bar(1430, l=90.0), *quiet_day()])
    assert simulate_long_bracket(TRADE, bars).exit_kind == "FLATTEN"


def test_bars_at_or_after_the_flatten_start_are_not_scanned_for_a_stop():
    bars = [*quiet_day(), bar(1945, o=101.0, h=101.0, l=90.0, c=100.0)]
    result = simulate_long_bracket(TRADE, bars)
    assert result.exit_kind == "FLATTEN" and result.exit_price == 101.0


def test_missing_flatten_bar_is_incomplete():
    result = simulate_long_bracket(TRADE, quiet_day())
    assert (result.status, result.reason, result.pnl_usd) == ("INCOMPLETE", "NO_FLATTEN_BAR", None)
    late = simulate_long_bracket(TRADE, [*quiet_day(), bar(1956)])
    assert late.reason == "NO_FLATTEN_BAR"


def test_a_thirty_minute_hole_is_incomplete():
    bars = with_flatten([bar(1431), bar(1445), bar(1530), bar(1600)])
    assert simulate_long_bracket(TRADE, bars).reason == "BAR_GAP"


def test_a_hole_before_the_flatten_bar_is_incomplete():
    steps = [1431, 1500, 1530, 1600, 1630, 1700, 1730, 1800, 1830, 1900]
    assert simulate_long_bracket(TRADE, [bar(t) for t in [*steps, 1930, 1945]]).status == "COMPLETE"
    assert simulate_long_bracket(TRADE, [bar(t) for t in [*steps, 1945]]).reason == "BAR_GAP"


def test_no_bars_is_incomplete():
    assert simulate_long_bracket(TRADE, []).reason == "NO_BARS"
    assert simulate_long_bracket(TRADE, [bar(1000)]).reason == "NO_BARS"


def test_bad_and_repeated_bars_are_incomplete():
    assert simulate_long_bracket(TRADE, with_flatten([bar(1431, h=99.0, l=99.5)])).reason == "BAD_BAR"
    assert simulate_long_bracket(TRADE, with_flatten([bar(1431, o=float("nan"))])).reason == "BAD_BAR"
    assert simulate_long_bracket(TRADE, with_flatten([bar(1431), bar(1431)])).reason == "DUPLICATE_BAR"


def test_the_result_does_not_depend_on_input_order():
    bars = with_flatten(quiet_day())
    assert simulate_long_bracket(TRADE, list(reversed(bars))) == simulate_long_bracket(TRADE, bars)


def test_a_gap_just_over_thirty_minutes_before_a_stop_is_incomplete():          # review 4210055084
    for later in (1502, 1503):                                                  # 31 and 32 minutes after 14:31
        bars = with_flatten([bar(1431), bar(later, o=99.0, l=97.0)])
        result = simulate_long_bracket(TRADE, bars)
        assert (result.status, result.reason, result.pnl_usd) == ("INCOMPLETE", "BAR_GAP", None)
    exactly = simulate_long_bracket(TRADE, with_flatten([bar(1431), bar(1501, o=99.0, l=97.0)]))
    assert (exactly.status, exactly.exit_kind) == ("COMPLETE", "STOP")         # 30 minutes is still allowed


def test_the_first_bar_more_than_thirty_minutes_after_the_decision_minute_is_a_gap():
    result = simulate_long_bracket(TRADE, with_flatten([bar(1501, o=99.0, l=97.0)]))   # 31 minutes after 14:30
    assert (result.status, result.reason) == ("INCOMPLETE", "BAR_GAP")

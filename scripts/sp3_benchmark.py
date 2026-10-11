#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from multiprocessing import Pipe, Process
from pathlib import Path

import numpy as np
import pandas as pd

# Run as `python scripts/sp3_benchmark.py`: the repo root must be importable first.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trader.data.data_access import TickStorage  # noqa: E402
from trader.data.duckdb_store import DuckDBDataStore  # noqa: E402
from trader.objects import BarSize  # noqa: E402
from trader.simulation.backtester import BacktestConfig, Backtester  # noqa: E402

COST_MULTIPLIERS = 3
NEIGHBOURS_PER_TUNABLE = 2
HOLDOUT_REPLAYS = 2
FOUR_HOURS_SECONDS = 4 * 3600
MIN_PERIOD_SESSIONS = 132
PARAMS = {"RANGE_MINUTES": 30, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570,
          "RTH_CLOSE_MIN": 960, "VOLUME_SMA_BARS": 20, "MIN_BARS": 40}
COLUMNS = ("open", "high", "low", "close", "volume")
PROTOCOL_VERSION = 1
CHILD_POLL_SECONDS = 0.1
CHILD_JOIN_SECONDS = 10
WARMUP_TIMEOUT_SECONDS = 60
REPLY_TIMEOUT_SECONDS = 30
EMBARGO_SESSIONS = 5
FULL_SCALE = dict(conids=10, period_sessions=690, holdout_sessions=90, cohort_points=3,
                  tunables=2, bars_per_session=390, folds=6)
QUIET_LOGGERS = ("data", "backtester", "trader.data.duckdb_store")


@dataclass(frozen=True)
class BenchmarkTiming:
    bars: int
    wall_seconds: float

    @property
    def per_bar_ms(self) -> float:
        return 1000.0 * self.wall_seconds / self.bars if self.bars else 0.0


def synthetic_session(*, conid_seed: int, sessions: int, bars_per_session: int = 390) -> pd.DataFrame:
    rng = np.random.default_rng(conid_seed)
    frames = []
    for day in range(sessions):
        start = pd.Timestamp("2024-01-02 14:30:00", tz="UTC") + pd.Timedelta(days=day)
        index = pd.date_range(start, periods=bars_per_session, freq="1min", tz="UTC")
        close = np.full(bars_per_session, 100.0)
        up = bars_per_session // 2
        close[up:] = 100.0 + 2.0 + rng.normal(0, 0.05, bars_per_session - up)
        volume = np.full(bars_per_session, 1000.0)
        volume[up] = 12_000.0
        frame = pd.DataFrame(
            {"open": close - 0.05, "high": close + 0.3, "low": close - 0.3,
             "close": close, "volume": volume}, index=index)
        frame.index.name = "date"
        frames.append(frame)
    combined = pd.concat(frames)
    combined.index.name = "date"
    return combined


def _child(conn, module_name, class_name, lookback, params):
    strategy = getattr(importlib.import_module(module_name), class_name)()
    buffer = np.zeros((lookback, 5), dtype=float)
    stamps: list[pd.Timestamp] = []
    count = 0
    while True:
        raw = conn.recv_bytes()
        message = json.loads(raw.decode())
        if message.get("type") == "close":
            return
        row = [message[c] for c in COLUMNS]
        if count < lookback:
            buffer[count] = row
            stamps.append(pd.Timestamp(message["t"]))
        else:
            buffer[:-1] = buffer[1:]
            buffer[-1] = row
            stamps.pop(0)
            stamps.append(pd.Timestamp(message["t"]))
        count += 1
        size = min(count, lookback)
        history = pd.DataFrame(buffer[:size], columns=COLUMNS)
        history.index = pd.DatetimeIndex(stamps, tz="UTC", name="date")
        decision = strategy.on_bar(history, params)
        conn.send_bytes(json.dumps({
            "v": PROTOCOL_VERSION, "type": "SIGNAL", "session": message["session"],
            "t": message["t"],
            "action": None if decision is None else decision.action,
            "probability": None if decision is None else decision.probability}).encode())


def parse_signal(raw: bytes, *, session: int, t: str) -> dict:
    reply = json.loads(raw.decode())
    expected = {"v": PROTOCOL_VERSION, "type": "SIGNAL", "session": session, "t": t}
    for key, value in expected.items():
        if reply.get(key) != value:
            raise ValueError(f"bad SIGNAL reply: {key}={reply.get(key)!r}, expected {value!r}")
    if (reply.get("action") is None) != (reply.get("probability") is None):
        raise ValueError(f"bad SIGNAL reply: action and probability disagree: {reply!r}")
    return reply


def _bar_frame(session: int, iso: str, values) -> bytes:
    return json.dumps({"v": PROTOCOL_VERSION, "session": session, "conid": 1, "t": iso,
                       "open": values[0], "high": values[1], "low": values[2],
                       "close": values[3], "volume": values[4]}).encode()


def _await_reply(parent, process, timeout_seconds: float) -> bytes:
    deadline = time.monotonic() + timeout_seconds
    while not parent.poll(CHILD_POLL_SECONDS):
        if not process.is_alive() and not parent.poll(0):
            raise RuntimeError(f"benchmark child exited with code {process.exitcode} without replying")
        if time.monotonic() > deadline:
            raise TimeoutError(f"benchmark child sent no reply within {timeout_seconds:.0f} s")
    try:
        return parent.recv_bytes()
    except EOFError:
        raise RuntimeError(f"benchmark child closed its pipe (exit code {process.exitcode})") from None


def _exchange(parent, process, frame: bytes, *, session: int, iso: str, timeout_seconds: float) -> dict:
    try:
        parent.send_bytes(frame)
    except OSError as error:
        raise RuntimeError(f"benchmark child is gone: {error}") from error
    return parse_signal(_await_reply(parent, process, timeout_seconds), session=session, t=iso)


def _stop_child(parent, process) -> None:
    try:
        parent.send_bytes(json.dumps({"type": "close"}).encode())
    except OSError:
        pass
    process.join(timeout=CHILD_JOIN_SECONDS)
    if process.is_alive():
        process.terminate()
        process.join()
    parent.close()


def stream_evaluation(module_name, class_name, frames, *, lookback, params):
    rows = []
    for session_index, frame in enumerate(frames):
        values = frame[list(COLUMNS)].to_numpy(dtype=float)
        times = [t.isoformat() for t in frame.index]
        for i in range(len(frame)):
            rows.append((session_index, times[i], values[i]))
    parent, child = Pipe()
    process = Process(target=_child, args=(child, module_name, class_name, lookback, params), daemon=True)
    process.start()
    child.close()
    try:
        # one warm-up bar, not timed, so the child's pandas import is not charged to bar 1
        s0, t0, v0 = rows[0]
        _exchange(parent, process, _bar_frame(s0, t0, v0), session=s0, iso=t0,
                  timeout_seconds=WARMUP_TIMEOUT_SECONDS)
        decisions: list[str | None] = []
        started = time.perf_counter()
        for session_index, iso, values in rows[1:]:
            reply = _exchange(parent, process, _bar_frame(session_index, iso, values),
                              session=session_index, iso=iso, timeout_seconds=REPLY_TIMEOUT_SECONDS)
            decisions.append(reply["action"])
        elapsed = time.perf_counter() - started
    finally:
        _stop_child(parent, process)
    return BenchmarkTiming(bars=len(rows) - 1, wall_seconds=elapsed), decisions


def in_process_baseline(frames, conid=4391):
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    combined = pd.concat(frames)
    combined.index.name = "date"
    config = BacktestConfig(start_date=combined.index[0].to_pydatetime(),
                            end_date=combined.index[-1].to_pydatetime(), bar_size=BarSize.Mins1)
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "bench.duckdb")
        DuckDBDataStore(path).write(str(conid), combined)
        started = time.perf_counter()
        result = Backtester(TickStorage(path), config).run_from_module(
            "strategies/opening_range_breakout.py", "OpeningRangeBreakout", [conid])
        elapsed = time.perf_counter() - started
    return BenchmarkTiming(bars=len(combined), wall_seconds=elapsed), result.total_trades


def _session_breakdown(*, period_sessions, holdout_sessions, cohort_points, tunables, folds) -> dict:
    pool_sessions = period_sessions - holdout_sessions
    fold_test_sessions = pool_sessions - pool_sessions // (folds + 1)
    pool_passes_per_point = COST_MULTIPLIERS + NEIGHBOURS_PER_TUNABLE * tunables
    sessions_per_conid = (cohort_points * pool_passes_per_point * fold_test_sessions
                          + HOLDOUT_REPLAYS * holdout_sessions)
    return {"pool_passes_per_point": pool_passes_per_point,
            "fold_test_sessions": fold_test_sessions, "sessions_per_conid": sessions_per_conid}


def extrapolate_seconds(*, per_bar_ms, conids, period_sessions, holdout_sessions, cohort_points,
                        tunables, bars_per_session, folds) -> float:
    breakdown = _session_breakdown(period_sessions=period_sessions, holdout_sessions=holdout_sessions,
                                   cohort_points=cohort_points, tunables=tunables, folds=folds)
    total_bar_sends = conids * breakdown["sessions_per_conid"] * bars_per_session
    return total_bar_sends * per_bar_ms / 1000.0


def _fit_period(*, per_bar_ms, conids, max_period_sessions, holdout_sessions, cohort_points,
                tunables, bars_per_session, folds) -> int | None:
    for period in range(max_period_sessions, MIN_PERIOD_SESSIONS - 1, -1):
        seconds = extrapolate_seconds(
            per_bar_ms=per_bar_ms, conids=conids, period_sessions=period,
            holdout_sessions=holdout_sessions, cohort_points=cohort_points, tunables=tunables,
            bars_per_session=bars_per_session, folds=folds)
        if seconds <= FOUR_HOURS_SECONDS:
            return period
    return None


def extrapolate(*, per_bar_ms, conids, period_sessions, holdout_sessions, cohort_points,
                tunables, bars_per_session, folds, embargo_sessions=EMBARGO_SESSIONS):
    inputs = dict(conids=conids, period_sessions=period_sessions, holdout_sessions=holdout_sessions,
                  cohort_points=cohort_points, tunables=tunables, bars_per_session=bars_per_session,
                  folds=folds)
    breakdown = _session_breakdown(
        period_sessions=period_sessions, holdout_sessions=holdout_sessions,
        cohort_points=cohort_points, tunables=tunables, folds=folds)
    seconds = extrapolate_seconds(per_bar_ms=per_bar_ms, **inputs)
    seconds_at_minimum = extrapolate_seconds(
        per_bar_ms=per_bar_ms, **{**inputs, "period_sessions": MIN_PERIOD_SESSIONS})
    fitted_period = _fit_period(per_bar_ms=per_bar_ms, max_period_sessions=period_sessions,
                                **{k: v for k, v in inputs.items() if k != "period_sessions"})
    return {**inputs, "embargo_sessions": embargo_sessions, "per_bar_ms": per_bar_ms, **breakdown,
            "total_bar_sends": conids * breakdown["sessions_per_conid"] * bars_per_session,
            "seconds": seconds, "hours": seconds / 3600.0,
            "over_four_hours": seconds > FOUR_HOURS_SECONDS,
            "fits_four_hours_at_minimum": fitted_period is not None,
            "hours_at_minimum_period": seconds_at_minimum / 3600.0,
            "proposed_period_sessions": MIN_PERIOD_SESSIONS if fitted_period is None else fitted_period}


def _ruling_decision(estimate) -> str:
    if not estimate["over_four_hours"]:
        return (f"keep the SP2c research_service.period_sessions default "
                f"({estimate['period_sessions']}, under 4 h); never weaken the sandbox")
    if not estimate["fits_four_hours_at_minimum"]:
        return (f"even the minimum period_sessions={MIN_PERIOD_SESSIONS} is estimated at "
                f"{estimate['hours_at_minimum_period']:.2f} h, still over 4 h: a shorter period does not "
                f"fit; the owner must rule on fewer conids or cohort points, or a longer budget; "
                f"never weaken the sandbox")
    return (f"reduce research_service.period_sessions for authored keys only to "
            f"N={estimate['proposed_period_sessions']} (>= {MIN_PERIOD_SESSIONS}: "
            f"{estimate['folds']} folds, embargo {estimate['embargo_sessions']}, "
            f"holdout {estimate['holdout_sessions']}); never weaken the sandbox")


def ruling_text(estimate):
    hours = estimate["hours"]
    over = estimate["over_four_hours"]
    return (f"Plan 1 ruling 1: a full authored evaluation ({estimate['conids']} conids x "
            f"{estimate['period_sessions']} sessions x {estimate['cohort_points']} cohort points, "
            f"{estimate['tunables']} tunables, {NEIGHBOURS_PER_TUNABLE} neighbours each, "
            f"{estimate['folds']} folds, embargo {estimate['embargo_sessions']}, "
            f"+ {estimate['holdout_sessions']}-session holdout) is estimated at "
            f"{hours:.2f} h at an upper-bound combined cost of {estimate['per_bar_ms']:.4f} ms/bar "
            f"(streamed + in-process backtest, strategy time counted twice). "
            f"Since {hours:.2f} h is {'over' if over else 'under'} 4 h: {_ruling_decision(estimate)}.")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--conids", type=int, default=1)
    parser.add_argument("--sessions", type=int, default=20)
    parser.add_argument("--bars-per-session", type=int, default=390)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args(argv)
    frames = [synthetic_session(conid_seed=c, sessions=args.sessions,
                                bars_per_session=args.bars_per_session) for c in range(args.conids)]
    samples, decisions = [], []
    for _ in range(args.runs):
        timing, decisions = stream_evaluation("tests.authored.fixtures.orb_authored",
                                              "OpeningRangeBreakout", frames, lookback=420, params=PARAMS)
        samples.append(timing.per_bar_ms)
    per_bar_ms = sorted(samples)[len(samples) // 2]
    fired = sum(1 for d in decisions if d is not None)
    base, trades = in_process_baseline([synthetic_session(conid_seed=0, sessions=args.sessions,
                                                          bars_per_session=args.bars_per_session)])
    combined = per_bar_ms + base.per_bar_ms
    estimate = extrapolate(per_bar_ms=combined, **FULL_SCALE)
    print(f"streamed per-bar (median of {args.runs}): {per_bar_ms:.4f} ms ({fired} signals fired)")
    print(f"in-process backtest per-bar: {base.per_bar_ms:.4f} ms ({trades} trades)")
    print(f"upper-bound combined cost per bar (streamed + in-process backtest, "
          f"strategy time counted twice): {combined:.4f} ms")
    print(f"total bar sends (full scale): {estimate['total_bar_sends']:,}")
    print(f"estimated full evaluation: {estimate['hours']:.2f} h")
    print(ruling_text(estimate))
    return 0


if __name__ == "__main__":
    sys.exit(main())

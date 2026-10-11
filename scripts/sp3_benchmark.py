#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib, json, sys, time
from dataclasses import dataclass
from multiprocessing import Pipe, Process
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COST_MULTIPLIERS = 3
NEIGHBOURS_PER_TUNABLE = 2
HOLDOUT_REPLAYS = 2
FOUR_HOURS_SECONDS = 4 * 3600
MIN_PERIOD_SESSIONS = 132
PARAMS = {"RANGE_MINUTES": 30, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570,
          "RTH_CLOSE_MIN": 960, "VOLUME_SMA_BARS": 20, "MIN_BARS": 40}
COLUMNS = ("open", "high", "low", "close", "volume")


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
        conn.send_bytes(json.dumps(
            {"action": None if decision is None else decision.action}).encode())


def stream_evaluation(module_name, class_name, frames, *, lookback, params):
    parent, child = Pipe()
    process = Process(target=_child, args=(child, module_name, class_name, lookback, params))
    process.start()
    decisions: list[str | None] = []
    rows = []
    for session_index, frame in enumerate(frames):
        values = frame[list(COLUMNS)].to_numpy(dtype=float)
        times = [t.isoformat() for t in frame.index]
        for i in range(len(frame)):
            rows.append((session_index, times[i], values[i]))
    # one warm-up bar, not timed, so the child's pandas import is not charged to bar 1
    s0, t0, v0 = rows[0]
    parent.send_bytes(json.dumps({"v": 1, "session": s0, "conid": 1, "t": t0, "open": v0[0], "high": v0[1],
                                  "low": v0[2], "close": v0[3], "volume": v0[4]}).encode())
    parent.recv_bytes()
    bars = 0
    started = time.perf_counter()
    for session_index, iso, v in rows[1:]:
        parent.send_bytes(json.dumps({"v": 1, "session": session_index, "conid": 1, "t": iso, "open": v[0],
                                      "high": v[1], "low": v[2], "close": v[3], "volume": v[4]}).encode())
        decisions.append(json.loads(parent.recv_bytes().decode())["action"])
        bars += 1
    elapsed = time.perf_counter() - started
    parent.send_bytes(json.dumps({"type": "close"}).encode())
    process.join(timeout=10)
    if process.is_alive():
        process.terminate()
    return BenchmarkTiming(bars=bars, wall_seconds=elapsed), decisions


def in_process_baseline(frames, conid=4391):
    from trader.data.data_access import TickStorage
    from trader.data.duckdb_store import DuckDBDataStore
    from trader.objects import BarSize
    from trader.simulation.backtester import Backtester, BacktestConfig
    import datetime as dt, tempfile, os
    path = os.path.join(tempfile.mkdtemp(), "bench.duckdb")
    combined = pd.concat(frames); combined.index.name = "date"
    DuckDBDataStore(path).write(str(conid), combined)
    config = BacktestConfig(start_date=combined.index[0].to_pydatetime(),
                            end_date=combined.index[-1].to_pydatetime(), bar_size=BarSize.Mins1)
    started = time.perf_counter()
    result = Backtester(TickStorage(path), config).run_from_module(
        "strategies/opening_range_breakout.py", "OpeningRangeBreakout", [conid])
    elapsed = time.perf_counter() - started
    return BenchmarkTiming(bars=len(combined), wall_seconds=elapsed), result.total_trades


def extrapolate(*, per_bar_ms, conids, period_sessions, holdout_sessions, cohort_points,
                tunables, bars_per_session, folds):
    pool_sessions = period_sessions - holdout_sessions
    fold_test_sessions = pool_sessions - pool_sessions // (folds + 1)
    pool_passes_per_point = COST_MULTIPLIERS + NEIGHBOURS_PER_TUNABLE * tunables
    point_sessions = pool_passes_per_point * fold_test_sessions
    holdout_pass_sessions = HOLDOUT_REPLAYS * holdout_sessions
    sessions_per_conid = cohort_points * point_sessions + holdout_pass_sessions
    total_bar_sends = conids * sessions_per_conid * bars_per_session
    seconds = total_bar_sends * per_bar_ms / 1000.0
    return {"per_bar_ms": per_bar_ms, "pool_passes_per_point": pool_passes_per_point,
            "fold_test_sessions": fold_test_sessions, "sessions_per_conid": sessions_per_conid,
            "total_bar_sends": total_bar_sends, "seconds": seconds, "hours": seconds / 3600.0,
            "over_four_hours": seconds > FOUR_HOURS_SECONDS,
            "proposed_period_sessions": _fit_period(per_bar_ms, conids, holdout_sessions,
                                                    cohort_points, tunables, bars_per_session, folds)}


def _fit_period(per_bar_ms, conids, holdout_sessions, cohort_points, tunables, bars_per_session, folds):
    for period in range(690, MIN_PERIOD_SESSIONS - 1, -1):
        e = extrapolate_seconds(per_bar_ms, conids, period, holdout_sessions, cohort_points,
                                tunables, bars_per_session, folds)
        if e <= FOUR_HOURS_SECONDS:
            return period
    return MIN_PERIOD_SESSIONS


def extrapolate_seconds(per_bar_ms, conids, period_sessions, holdout_sessions, cohort_points,
                        tunables, bars_per_session, folds):
    pool = period_sessions - holdout_sessions
    fold_test = pool - pool // (folds + 1)
    passes = COST_MULTIPLIERS + NEIGHBOURS_PER_TUNABLE * tunables
    per_conid = cohort_points * passes * fold_test + HOLDOUT_REPLAYS * holdout_sessions
    return conids * per_conid * bars_per_session * per_bar_ms / 1000.0


def ruling_text(estimate):
    hours = estimate["hours"]
    n = estimate["proposed_period_sessions"]
    if estimate["over_four_hours"]:
        decision = (f"reduce research_service.period_sessions for authored keys only to N={n} "
                    f"(>= {MIN_PERIOD_SESSIONS}: 6 folds, embargo 5, holdout 90); never weaken the sandbox")
    else:
        decision = (f"keep the SP2c research_service.period_sessions default (690, under 4 h); "
                    f"never weaken the sandbox")
    return (f"Plan 1 ruling 1: a full authored evaluation (10 conids x 690 sessions x 3 cohort "
            f"points, 2 tunables, 4 neighbours each, + 90-session holdout) is estimated at "
            f"{hours:.2f} h at {estimate['per_bar_ms']:.4f} ms/bar streamed. "
            f"Since {hours:.2f} h is {'over' if estimate['over_four_hours'] else 'under'} 4 h: "
            f"{decision}.")


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
    estimate = extrapolate(per_bar_ms=combined, conids=10, period_sessions=690, holdout_sessions=90,
                           cohort_points=3, tunables=2, bars_per_session=390, folds=6)
    print(f"streamed per-bar (median of {args.runs}): {per_bar_ms:.4f} ms ({fired} signals fired)")
    print(f"in-process backtest per-bar: {base.per_bar_ms:.4f} ms ({trades} trades)")
    print(f"combined per-bar (streamed + backtester loop): {combined:.4f} ms")
    print(f"total bar sends (full scale): {estimate['total_bar_sends']:,}")
    print(f"estimated full evaluation: {estimate['hours']:.2f} h")
    print(ruling_text(estimate))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# tests/authored/test_orb_equivalence.py
"""SP3 spec 10 (equivalence): ORB ported to AuthoredStrategy gives the same signals bar for bar through the
sandbox as the catalogue ORB, in a backtest (research proxy), live (strategy proxy) and in Plan 2's child runtime."""
import datetime as dt
import itertools
import logging

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from strategies.opening_range_breakout import OpeningRangeBreakout
from tests.authored.fake_sandbox import IMAGE_DIGEST, FakeSandbox, orb_fixture_source
from tests.sandbox.fakes import InProcessSpawner
from trader.authored.api import Decision
from trader.authored.keys import source_digest
from trader.authored.sandbox_client import SandboxFault, SandboxUnavailable
from trader.objects import BarSize
from trader.research.authored_backtest import AuthoredProxyStrategy
from trader.sandbox import protocol
from trader.sandbox.supervisor import LIVE_POOL_SIZE, RESEARCH_POOL_SIZE, Supervisor
from trader.strategy.authored_runtime import AuthoredLiveStrategy
from trader.trading.strategy import StrategyContext

DAYS = ("2024-03-04", "2024-03-05", "2024-03-06")
CONID = 265598
# Callers always send the full point (SP2c); Plan 2's child passes OPEN params only.
ORB_PARAMS = {"RANGE_MINUTES": 30, "VOLUME_MULT": 1.5, "RTH_OPEN_MIN": 570, "RTH_CLOSE_MIN": 960,
              "VOLUME_SMA_BARS": 20, "MIN_BARS": 40}
LAST_DAY_END = dt.datetime(2024, 3, 7, tzinfo=dt.timezone.utc)   # after every bar: none is skipped as future-dated
PORT_CLASS = "OpeningRangeBreakout"          # Plan 1's fixture class; the catalogue class has the same name


def orb_session_bars(seed=7):
    """Three regular sessions of 1-minute bars with a loud breakout up and a slide down each day."""
    calendar = xcals.get_calendar("XNYS")
    rng = np.random.default_rng(seed)
    frames, price = [], 100.0
    for day in DAYS:
        index = pd.date_range(calendar.session_open(day), calendar.session_close(day) - pd.Timedelta(minutes=1),
                              freq="1min", tz="UTC")
        steps = rng.normal(0.0, 0.08, len(index))
        steps[45:60] += 0.12
        steps[200:230] -= 0.15
        closes = price + np.cumsum(steps)
        price = float(closes[-1])
        volume = rng.uniform(800, 1200, len(index))
        volume[45:60] *= 3.0
        volume[200:230] *= 3.0
        frames.append(pd.DataFrame({"open": closes - 0.02, "high": closes + 0.05, "low": closes - 0.05,
                                    "close": closes, "volume": volume}, index=index))
    frame = pd.concat(frames)
    frame.index.name = "date"
    return frame


def signal_key(signal):
    return None if signal is None else (str(signal.action), signal.probability)


def catalogue_signals(frame):
    orb = OpeningRangeBreakout()
    state = orb.precompute(frame)
    return [signal_key(orb.on_bar(frame.iloc[:i + 1], state, i)) for i in range(len(frame))]


def research_signals(client, digest, frame):
    proxy = AuthoredProxyStrategy(client=client, digest=digest, class_name=PORT_CLASS, params=ORB_PARAMS,
                                  bar_size="1 min")
    state = proxy.precompute(frame)
    return [signal_key(proxy.on_bar(frame.iloc[:i + 1], state, i)) for i in range(len(frame))]


# Plan 2 ruling 7: one session per connection. Each session gets its own connection id, as the server gives it.
_connection_ids = itertools.count(1)


class SupervisorSession:
    """Plan 2's supervisor called in this process; maps replies as Plan 2's SandboxClient does."""

    def __init__(self, supervisor, conn_id, session_id):
        self._supervisor, self._conn_id, self._id = supervisor, conn_id, session_id

    def bar(self, conid, t, open, high, low, close, volume):
        reply = _call(self._supervisor, self._conn_id,
                      {"type": "BAR", "session": self._id, "conid": conid, "t": t.isoformat(),
                       "open": open, "high": high, "low": low, "close": close, "volume": volume})
        if reply["type"] != "SIGNAL":
            _raise_for(reply)
        return None if reply["action"] is None else Decision(action=reply["action"],
                                                             probability=float(reply["probability"]))

    def close(self):
        _call(self._supervisor, self._conn_id, {"type": "CLOSE", "session": self._id})


class SupervisorClient:
    def __init__(self, supervisor):
        self._supervisor = supervisor

    def open_session(self, digest, class_name, params, bar_size, conids):
        conn_id = next(_connection_ids)
        reply = _call(self._supervisor, conn_id, {"type": "OPEN", "digest": digest, "class": class_name,
                                                  "params": dict(params), "bar_size": bar_size,
                                                  "conids": list(conids)})
        if reply["type"] != "OPENED":
            _raise_for(reply)
        return SupervisorSession(self._supervisor, conn_id, reply["session"])


def _call(supervisor, conn_id, request):
    return supervisor.handle("research", conn_id, protocol.parse_request({"v": 1, **request}))


def _raise_for(reply):
    # Availability, as the client maps it (Plan 2 ruling 34): BUSY and UNAVAILABLE are never the strategy's fault.
    if reply.get("code") in (protocol.SANDBOX_BUSY, protocol.SANDBOX_UNAVAILABLE):
        raise SandboxUnavailable(reply.get("detail", ""))
    raise SandboxFault(reply.get("detail", ""), code=reply.get("code", protocol.SANDBOX_ERROR))


def plan_2_sandbox(store_dir, source):
    """If Plan 2's Supervisor constructor or parse_request differ on the base, adapt only this and _call."""
    digest = source_digest(source.encode())
    (store_dir / f"{digest.split(':')[1]}.py").write_text(source)
    supervisor = Supervisor(spawners={"live": InProcessSpawner(), "research": InProcessSpawner()},
                            pool_sizes={"live": LIVE_POOL_SIZE, "research": RESEARCH_POOL_SIZE},
                            store_dir=str(store_dir),
                            image_digest=IMAGE_DIGEST, posture={},
                            wiper=lambda slot: True)   # the real wiper forks a helper against /tmp/s2000X
    return SupervisorClient(supervisor), digest


def test_the_research_proxy_matches_the_catalogue_orb_bar_for_bar():
    frame, sandbox = orb_session_bars(), FakeSandbox()
    expected = catalogue_signals(frame)
    assert {s[0] for s in expected if s} == {"BUY", "SELL"}        # not a vacuous comparison
    assert research_signals(sandbox, sandbox.add(orb_fixture_source()), frame) == expected
    assert sandbox.most_open == 1


def test_the_live_proxy_matches_the_catalogue_orb_bar_for_bar():
    frame, sandbox = orb_session_bars(), FakeSandbox(research=False)
    live = AuthoredLiveStrategy(client=sandbox, digest=sandbox.add(orb_fixture_source()), class_name=PORT_CLASS,
                                params=ORB_PARAMS, bar_size="1 min", conids=[CONID],
                                on_fault=lambda digest, code: None, proof_gate=lambda build: True,
                                now=lambda: LAST_DAY_END)
    live.install(StrategyContext(name="aidv-orb", bar_size=BarSize.Mins1, conids=[CONID], universe=None,
                                 historical_days_prior=5, paper_only=True, storage=None, universe_accessor=None,
                                 logger=logging))
    live.enable()
    live._dispatch_conid = CONID
    ported = [signal_key(live.on_prices(frame.iloc[:i + 1])) for i in range(len(frame))]
    assert ported == catalogue_signals(frame)
    assert len(sandbox.sessions) == len(DAYS)                       # one session per deployment per New York day


def test_plan_2s_child_runtime_gives_the_catalogue_signals(tmp_path):
    frame = orb_session_bars()
    client, digest = plan_2_sandbox(tmp_path, orb_fixture_source())
    assert research_signals(client, digest, frame) == catalogue_signals(frame)

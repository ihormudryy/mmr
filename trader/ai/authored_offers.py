"""Stored authored strategies offered to the research cycle (SP3 spec 7.3 "offer candidates", 7.6; Plan 5 ruling 7).

Read as ai_author at most every few minutes. A STORED record becomes one INITIAL candidate of tonight's cycle;
the trader's claim (Plan 3) and the research service (Plan 4) stay the authority.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Mapping, Optional, Sequence

from trader.ai.author_wire import AuthoredRecord, parse_authored_strategies
from trader.ai.research_wire import WireError
from trader.ai.rpc_clients import RpcNotSent, RpcOutcomeUnknown, RpcRefused

logger = logging.getLogger(__name__)
REFRESH_SECONDS = 300


def default_point(record: AuthoredRecord) -> dict[str, float]:
    """Every tunable of the record at the default the trader stored with it (Plan 3 ruling 5, decision D5), as a
    float (spec 7.1: params are floats). The trader checked at submit that each default is the class value."""
    return {name: float(tunable.default) for name, tunable in sorted(record.tunables.items())}


def authored_submit_body(record: AuthoredRecord, conids: Sequence[int], point: Mapping[str, float]) -> dict:
    """One INITIAL cohort point on the authored universe (Ruling 6)."""
    return {"kind": "INITIAL", "strategy_key": record.key, "cohort": [dict(point)], "conids": sorted(conids),
            "bar_size": record.bar_size}


class AuthoredOffers:
    def __init__(self, *, client: Any, clock: Any, refresh_seconds: float = REFRESH_SECONDS):
        self._client, self._clock = client, clock
        self._refresh = dt.timedelta(seconds=refresh_seconds)
        self._next_read: Optional[dt.datetime] = None
        self._reported: set[str] = set()           # reasons for no offer already logged since the last good read

    async def due_records(self) -> tuple[AuthoredRecord, ...]:
        """STORED records while the owner switch is on. Nothing between reads, while the trader is away, or on a
        reply of another shape: the pump's other steps go on. Each reason is logged once until a good read."""
        now = self._clock.now()
        if self._next_read is not None and now < self._next_read:
            return ()
        self._next_read = now + self._refresh
        try:
            reply = parse_authored_strategies(await self._client.call("get_authored_strategies", {}))
        except (RpcNotSent, RpcOutcomeUnknown) as exc:
            self._report_once(f"AWAY_{exc.code}", logging.WARNING,
                              "authored strategies unreadable (%s); offered on a later pump", exc.code)
            return ()
        except RpcRefused as exc:
            self._report_once(f"REFUSED_{exc.code}", logging.ERROR,
                              "get_authored_strategies refused (%s); no authored strategy is offered", exc.code)
            return ()
        except WireError as exc:
            self._report_once("BAD_REPLY", logging.ERROR, "authored strategies: %s", exc)
            return ()
        if not reply.enabled:
            self._report_once("SWITCH_OFF", logging.INFO,
                              "authored strategies are switched off at the trader; none is offered")
            return ()
        self._reported.clear()
        return tuple(record for record in reply.records if record.state == "STORED")

    def _report_once(self, reason: str, level: int, message: str, *args: Any) -> None:
        if reason not in self._reported:
            self._reported.add(reason)
            logger.log(level, message, *args)

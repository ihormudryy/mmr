"""Command-center dependency health (spec §12) — session-gated `/api/cc-health`.

Per the M1-R Task 7 addendum: the G0 `/healthz` (boolean liveness), `/readyz`
(LB-drain readiness flag, `{"ready": _READY}`), and `/api/health` (its own
`MMR_WEB_TOKEN` gate + `build_health_payload`) routes already live in
`web/app.py` and are pinned, unedited, by `tests/test_service_health.py`.
This module must NEVER redefine any of them — it only adds a brand-new
route, `/api/cc-health`, carrying the rich per-dependency detail the base
brief originally sketched for `/api/health`: bridge lifecycle/reconnects/
cursor, reducer stream/sequence/last-event/transport-lag, SSE client count,
and quote-plane stats.

`/api/cc-health` is under `/api/` and is NOT in `SessionSecurityMiddleware`'s
exempt-path set (`web/command_center/session.py::_EXEMPT_PATHS`), so the
middleware already 401s an unauthenticated caller before this handler ever
runs -- the same reliance `create_read_router`'s `/api/snapshot` and
`/api/events` routes have on the middleware, no redundant
`Depends(require_session)` needed.
"""
from __future__ import annotations

from fastapi import APIRouter


def create_health_router(cc) -> APIRouter:
    """Build the command-center-only health router.

    Mounts `/api/cc-health` ONLY -- never `/healthz`, `/readyz`, or
    `/api/health` (those stay exactly as defined in `web/app.py`, per the
    Task 7 addendum's resolution of the G0 collision).
    """
    router = APIRouter()

    @router.get("/api/cc-health")
    async def cc_health():
        bridge_health = (cc.bridge.health() if cc.bridge else
                         {"lifecycle": "starting", "reconnects": 0,
                          "cursor": None, "sources": {}})
        feed_types = sorted({q.get("feed_type") for q in cc.state.quotes.values()
                             if q.get("feed_type")})
        return {
            # "has a coherent fenced snapshot" -- the dashboard's own detail
            # surface for readiness, distinct from `/readyz`'s LB-drain flag.
            "ready": bool(cc.state.has_baseline),
            "lifecycle": bridge_health["lifecycle"],
            "reconnects": bridge_health["reconnects"],
            "cursor": bridge_health["cursor"],
            "stream_id": cc.state.stream_id,
            "sequence": cc.state.sequence,
            "last_event_at": cc.state.last_event_at,
            "transport_lag_ms": cc.state.last_transport_lag_ms,
            "sse_clients": cc.fanout.client_count(),
            "sources": bridge_health["sources"],  # per-dependency state, age,
                                                  # last safe error, reconnects
            "quote_plane": {
                "instruments": len(cc.state.quotes),
                "feed_types": feed_types,
                "dropped": getattr(cc.quote_plane, "dropped", None),
            },
            # COMPAT Task 4 soak-metric exporters: instantaneous read-model
            # counts. `scripts/run_paper_soak.py` samples these over the
            # soak window and keeps the MAX of each, feeding them into
            # `evaluate_soak` as `max_replay_ring_events` /
            # `max_client_fifo_depth` / `max_terminal_rows` -- three of the
            # six soak thresholds that previously had no live exporter and
            # always reported `observed=None`.
            "replay_ring_events": cc.state.ring_depth(),
            "terminal_rows": cc.state.terminal_row_count(),
            "client_fifo_depth_max": cc.fanout.max_fifo_depth(),
        }

    return router

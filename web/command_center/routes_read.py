"""Read-only routes: snapshot (also the degraded polling fallback), SSE, page."""
from __future__ import annotations

import asyncio
import json
import logging
import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

logger = logging.getLogger("web.command_center.routes")

SSE_PING_SECONDS = 10
MANAGE_PAGE_TIMEOUT_S = float(os.environ.get('MMR_MANAGE_PAGE_TIMEOUT_S', '12'))
PAPER_AUTOMATION_QUERY_TIMEOUT_S = float(
    os.environ.get("MMR_PAPER_AUTOMATION_QUERY_TIMEOUT_S", "1"))


def create_read_router(cc, templates, manage_context_provider=None,
                       empty_manage_context=None) -> APIRouter:
    router = APIRouter()

    def _require_session(request: Request) -> str:
        """Resolve the session dependency per request, not at router-build
        time.

        ``cc.require_session`` is a property: accessing it lazily calls
        ``cc.ensure_session_manager()`` (idempotent -- cheap after the first
        call) and builds a fresh check against it. Resolving it INSIDE this
        wrapper -- rather than once via ``require_session = cc.require_session``
        while this router is being constructed -- means constructing the
        router (and therefore ``create_app()``) never needs credentials to
        be configured; the hard failure point stays ``ensure_session_manager``
        at either lifespan startup or the first real request, matching the
        session router's own lazy ``manager_provider`` design.
        """
        return cc.require_session(request)

    @router.get("/api/snapshot")
    async def api_snapshot(_session: str = Depends(_require_session)):
        if not cc.state.has_baseline:
            return JSONResponse({"detail": "snapshot not ready"}, status_code=503)
        view = cc.state.snapshot_view()
        view["paper_automation"] = None
        query_client = getattr(cc, "_query_client", None)
        if query_client is not None:
            try:
                view["paper_automation"] = await asyncio.to_thread(
                    query_client.call,
                    "get_paper_automation_status",
                    {},
                    dict,
                    timeout=PAPER_AUTOMATION_QUERY_TIMEOUT_S,
                )
            except Exception as exc:  # noqa: BLE001 - optional read-model enrichment
                logger.debug("paper automation status unavailable: %s", exc)
        view["health"] = cc.bridge.health() if cc.bridge else {
            "lifecycle": "starting", "reconnects": 0, "cursor": None, "sources": {}}
        return JSONResponse(view)

    @router.get("/api/events")
    async def api_events(request: Request, _session: str = Depends(_require_session)):
        after = request.headers.get("last-event-id") or request.query_params.get("after")
        client, replay, quote_baseline = cc.fanout.register(after)

        async def stream():
            try:
                if replay is None:
                    yield {"event": "resync_required", "data": json.dumps(
                        {"reason": "replay_unavailable", "stream_id": cc.state.stream_id})}
                    return
                for env in replay:
                    yield {"id": f"{env['stream_id']}:{env['sequence']}",
                           "event": env["event_type"], "data": json.dumps(env)}
                # Client-local control frame: deliberately NO SSE id (spec §6/§7).
                yield {"event": "quotes.snapshot", "data": json.dumps(
                    {"stream_id": cc.state.stream_id, "quotes": quote_baseline})}
                while not client.closed:
                    await client.wake.wait()
                    client.wake.clear()
                    if client.resync:
                        yield {"event": "resync_required", "data": json.dumps(
                            {"reason": "overflow_or_stream_change",
                             "stream_id": cc.state.stream_id})}
                        return
                    while client.fifo:
                        env = client.fifo.popleft()
                        yield {"id": f"{env['stream_id']}:{env['sequence']}",
                               "event": env["event_type"], "data": json.dumps(env)}
                    if client.quote_map:
                        batch, client.quote_map = client.quote_map, {}
                        yield {"event": "quote.updated",
                               "data": json.dumps({"quotes": batch})}  # no id
            finally:
                cc.fanout.unregister(client)

        return EventSourceResponse(stream(), ping=SSE_PING_SECONDS)

    @router.get("/cc", response_class=HTMLResponse)
    async def command_center_page(request: Request,
                                  flash: str = '',
                                  flash_err: int = 0,
                                  _session: str = Depends(_require_session)):
        # [M1-C] UI-wiring pass: `commands_enabled` gates every command
        # affordance (action buttons + the drawers/dialogs they open) in the
        # template below. Read straight off `app.state.command_flags` -- the
        # same `CommandFlags` instance `routes_commands.py`'s gateway routes
        # and `web/app.py`'s legacy-mutation guard already treat as the one
        # authority for this flag (set once at app-build time, see
        # `web/app.py`'s `create_app`) -- rather than re-deriving it from
        # `cc` (the `CommandCenter` doesn't itself own this flag; it only
        # takes a `commands_enabled` constructor kwarg used to decide whether
        # to build the command gateway at all).
        commands_enabled = request.app.state.command_flags.commands_enabled
        ctx: dict = {
            "degraded_after_ms": int(os.environ.get("CC_DEGRADED_AFTER_MS", "15000")),
            "poll_interval_ms": int(os.environ.get("CC_POLL_INTERVAL_MS", "5000")),
            "commands_enabled": commands_enabled,
            "flash": flash,
            "flash_err": bool(flash_err),
        }
        if manage_context_provider is not None:
            # Seed the Deploy/Watchlists tabs from local disk + YAML immediately
            # so a slow RPC overlay never leaves both sections blank.
            if empty_manage_context is not None:
                ctx.update(empty_manage_context(flash))
            try:
                ctx.update(await asyncio.wait_for(
                    asyncio.to_thread(manage_context_provider, flash),
                    timeout=MANAGE_PAGE_TIMEOUT_S,
                ))
            except asyncio.TimeoutError:
                logger.warning('manage context exceeded %.0fs on /cc', MANAGE_PAGE_TIMEOUT_S)
                page_errors = dict(ctx.get('errors') or {})
                page_errors['page'] = (
                    f'live sections timed out after {MANAGE_PAGE_TIMEOUT_S:.0f}s '
                    '— showing local data'
                )
                ctx['errors'] = page_errors
            except Exception as exc:  # noqa: BLE001 - degrade, don't 500 the page
                logger.warning('manage context failed on /cc: %s', exc)
                if empty_manage_context is not None:
                    ctx.update(empty_manage_context(
                        flash,
                        error=f'{type(exc).__name__}: {exc}',
                    ))
                else:
                    ctx.update({
                        'strategies': [], 'available_strategies': [], 'watchlists': [],
                        'deployed_count': 0,
                        'errors': {'page': f'{type(exc).__name__}: {exc}'},
                    })
        return templates.TemplateResponse(request, "command_center.html", ctx)

    return router

"""GET /api/scoreboard: the trader's PAPER report over typed RPC (SP1 Plan 5 Task 8).

The dashboard never opens a DuckDB file. Errors are loud: never an empty 200.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

QUERY_TIMEOUT_S = 8
logger = logging.getLogger(__name__)
_EXPERIMENT_ID = re.compile(r"^exp-[0-9a-f]{20}$")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _reject(key: str, value: object, message: str, error_type: str = "value_error") -> RequestValidationError:
    return RequestValidationError([{"type": error_type, "loc": ("query", key), "msg": message, "input": value}])


def create_scoreboard_router(cc) -> APIRouter:
    router = APIRouter(tags=["scoreboard"])

    def require_session(request: Request) -> str:
        return cc.require_session(request)

    @router.get("/api/scoreboard")
    async def scoreboard(request: Request, _session: str = Depends(require_session)):
        unknown = sorted(set(request.query_params.keys()) - {"experiment_id"})
        if unknown:
            raise _reject(unknown[0], request.query_params.get(unknown[0]), "Extra inputs are not permitted",
                          "extra_forbidden")
        experiment_id: Optional[str] = request.query_params.get("experiment_id")
        if experiment_id is not None and not _EXPERIMENT_ID.fullmatch(experiment_id):
            raise _reject("experiment_id", experiment_id, "experiment_id must match exp-<20 hex>")
        query_client = getattr(cc, "_query_client", None)
        if query_client is None:
            return _error(503, "TRADER_UNAVAILABLE", "trader_service typed query is not connected")
        body = {} if experiment_id is None else {"experiment_id": experiment_id}
        from trader.messaging.typed_rpc import TypedRpcRemoteError
        try:
            report = await asyncio.to_thread(query_client.call, "get_scoreboard", body, dict,
                                             timeout=QUERY_TIMEOUT_S)
        except TypedRpcRemoteError as exc:
            status = 403 if exc.code == "PERMISSION_DENIED" else 502
            return _error(status, exc.code, exc.message)
        except (TimeoutError, ConnectionError):
            logger.warning("get_scoreboard did not answer", exc_info=True)
            return _error(502, "TRADER_TIMEOUT", f"get_scoreboard did not answer within {QUERY_TIMEOUT_S} s")
        if isinstance(report, dict) and report.get("error_code"):
            return _error(404, report["error_code"], f"no experiment {experiment_id}")
        return JSONResponse(report)

    return router

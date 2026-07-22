"""Wire contracts for the strategy service's typed trader capabilities.

The strategy runtime resolves conIds and starts market-data publication
through the trader's typed ``query`` socket instead of the legacy dill RPC
(port 42001, never bound in the split-container production posture). These are
the request/response models for that boundary.

This is a deliberately minimal subset (resolve + publish). Universe listing,
symbol discovery, and watchlist CRUD for the /manage dashboard are registered
on the trader typed query/command sockets via ``manage_surface``; the broader
strategy→trader typed surface (signal recording, account / position reads beyond
manage) is tracked separately.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ResolveInstrumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instrument_id: int


class InstrumentResponse(BaseModel):
    """JSON-safe instrument projection: exactly the fields the strategy needs
    to build an IB ``Contract`` (subscribe path) and to drive the historical
    fetch (exchange-calendar + timezone lookup)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    instrument_id: int
    symbol: str
    exchange: str
    primary_exchange: str
    currency: str
    security_type: str
    time_zone_id: str


class ResolveInstrumentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instruments: list[InstrumentResponse]


class PublishInstrumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    instrument_id: int
    delayed: bool


class PublishInstrumentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    published: bool

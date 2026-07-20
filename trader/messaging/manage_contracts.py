"""Typed wire contracts for the /manage dashboard surface (universes + deploy).

These methods live on the trader's typed query/command sockets and the
strategy service's typed query/command sockets so the dashboard container
never needs legacy dill RPC (port 42001) or a local DuckDB mount.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_WATCHLIST_NAME_RE = re.compile(r'^[a-z0-9_-]{1,40}$')


class _UniverseNameMixin(BaseModel):
    name: str = Field(min_length=1, max_length=40)

    @field_validator('name')
    @classmethod
    def _normalize_name(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _WATCHLIST_NAME_RE.match(normalized):
            raise ValueError('watchlist name must match a-z, 0-9, dash, underscore (max 40)')
        return normalized


class ListUniversesRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


class UniverseSummary(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    count: int


class ListUniversesResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    universes: list[UniverseSummary]


class GetUniverseRequest(_UniverseNameMixin):
    model_config = ConfigDict(extra='forbid')
    symbol_limit: int = Field(default=40, ge=1, le=200)


class GetUniverseResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    count: int
    symbols: list[str]


class CreateUniverseRequest(_UniverseNameMixin):
    model_config = ConfigDict(extra='forbid')


class AddUniverseSymbolsRequest(_UniverseNameMixin):
    model_config = ConfigDict(extra='forbid')
    symbols: list[str] = Field(min_length=1, max_length=100)
    exchange: str = ''
    currency: str = ''
    sec_type: str = 'STK'


class RemoveUniverseSymbolRequest(_UniverseNameMixin):
    model_config = ConfigDict(extra='forbid')
    symbol: str = Field(min_length=1, max_length=32)


class DeleteUniverseRequest(_UniverseNameMixin):
    model_config = ConfigDict(extra='forbid')


class ImportUniverseCsvRequest(_UniverseNameMixin):
    model_config = ConfigDict(extra='forbid')
    csv_text: str = Field(min_length=1, max_length=1_000_000)


class DiscoverInstrumentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol: str = Field(min_length=1, max_length=32)
    exchange: str = ''
    currency: str = ''
    sec_type: str = 'STK'


class DiscoverInstrumentResponse(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instruments: list[dict]


class ListStrategiesRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ReloadStrategiesRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')


class EnableStrategyByNameRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    strategy_name: str = Field(min_length=1, max_length=80)

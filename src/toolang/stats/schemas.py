"""Statistics protocol models, without storage or service dependencies."""

from datetime import date
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from .types import Scope, Window

T = TypeVar("T")


class Quantities(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    in_: int | None = Field(serialization_alias="in")
    out: int | None
    cached: int | None
    spend: float | None
    model_calls: int
    tool_calls: int
    root_runs: int | None
    spend_approximate: bool


class WindowRow(Quantities):
    window: Window
    start: str | None
    end: str | None


class ModelRow(Quantities):
    model_ref: str


class DailyRow(Quantities):
    date: date
    start: str
    end: str
    partial: bool


class Snapshot(BaseModel):
    queried_at: str
    computed_at: str | None
    initializing: bool
    stale: bool
    refreshing: bool
    refresh_error: str | None


class StatsResponse(BaseModel, Generic[T]):
    scope: Scope
    snapshot: Snapshot
    data: T | None


class ModelsResponse(StatsResponse[list[ModelRow]]):
    window: Window
    start: str | None
    end: str | None

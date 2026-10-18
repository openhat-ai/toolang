"""Statistics protocol models, without storage or service dependencies."""

from datetime import date
from typing import Generic, TypeVar

from pydantic import BaseModel, computed_field, ConfigDict, Field

from toolang.execution.schemas import RunNode

from .types import Metrics, Scope, Window

STATS_API_VERSION = 0

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


class RecentUsage(BaseModel):
    """Exact source-only usage in a bounded half-open interval."""

    observed_at: str
    start: str
    end: str
    metrics: Metrics


class CompletionCounts(BaseModel):
    succeeded: int = 0
    failed: int = 0
    canceled: int = 0

    @computed_field
    @property
    def total(self) -> int:
        return self.succeeded + self.failed + self.canceled


class CompletedRoots(BaseModel):
    """One bounded page, with counts over the entire completion interval."""

    observed_at: str
    start: str
    end: str
    counts: CompletionCounts
    roots: tuple[RunNode, ...]

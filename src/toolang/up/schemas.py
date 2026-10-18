"""Read-only activity observation protocols."""

from pydantic import BaseModel, computed_field

from toolang.execution.schemas import LiveSnapshot
from toolang.stats.schemas import CompletedRoots, CompletionCounts
from toolang.stats.schemas import RecentUsage
from toolang.stats.types import Metrics
from toolang.common.layout import AgentPlacement

from .types import RuntimeInstanceSnapshot


class ActivitySnapshot(BaseModel):
    live: LiveSnapshot | None
    completed: CompletedRoots


class SourceActivity(BaseModel):
    identity: str
    label: str
    placement: AgentPlacement
    live: LiveSnapshot | None = None
    live_stale: bool = True
    completed: CompletedRoots | None = None
    usage: RecentUsage | None = None
    activity_error: str | None = None
    usage_error: str | None = None


class TopSummary(BaseModel):
    resident: int | None
    visiting: int | None
    roaming: int | None
    active: int | None
    completed: CompletionCounts | None
    usage: Metrics | None


class TopSnapshot(BaseModel):
    observed_at: str
    agent: str | None = None
    period: int
    recent: int
    inventory: RuntimeInstanceSnapshot
    sources: tuple[SourceActivity, ...]
    inventory_updating: bool = False

    @computed_field
    @property
    def summary(self) -> TopSummary:
        unknown = self.inventory_updating or self.inventory.partial
        counts = [
            None
            if unknown
            else sum(item.placement == placement for item in self.inventory.instances)
            for placement in ("resident", "visiting", "roaming")
        ]
        active = (
            None
            if unknown or any(source.live_stale for source in self.sources)
            else sum(len(source.live.trees) for source in self.sources if source.live)
        )
        completed = None
        if not unknown and all(
            source.completed is not None and source.activity_error is None
            for source in self.sources
        ):
            completed = CompletionCounts(
                **{
                    state: sum(
                        getattr(source.completed.counts, state)
                        for source in self.sources
                        if source.completed
                    )
                    for state in ("succeeded", "failed", "canceled")
                }
            )
        return TopSummary(
            resident=counts[0],
            visiting=counts[1],
            roaming=counts[2],
            active=active,
            completed=completed,
            usage=self.usage_total(),
        )

    def usage_total(self) -> Metrics | None:
        if self.inventory_updating or self.inventory.partial:
            return None
        result = Metrics()
        for source in self.sources:
            if source.usage is None or source.usage_error is not None:
                return None
            result += source.usage.metrics
        return result

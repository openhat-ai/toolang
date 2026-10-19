"""Read-only activity observation protocols."""

from pydantic import BaseModel, computed_field

from toolang.execution.schemas import LiveSnapshot, RunNode
from toolang.stats.schemas import CompletedRoots, CompletionCounts
from toolang.stats.schemas import RecentUsage
from toolang.stats.types import Metrics
from toolang.common.layout import AgentPlacement

from .types import RuntimeInstanceSnapshot


class ActivitySnapshot(BaseModel):
    live: LiveSnapshot | None
    completed: CompletedRoots
    retired: tuple[RunNode, ...] = ()
    live_error: str | None = None


class SourceActivity(BaseModel):
    identity: str
    label: str
    placement: AgentPlacement
    live: LiveSnapshot | None = None
    live_stale: bool = True
    live_error: str | None = None
    completed: CompletedRoots | None = None
    usage: RecentUsage | None = None
    activity_error: str | None = None
    usage_error: str | None = None
    page_error: str | None = None


class TopSummary(BaseModel):
    running: int | None
    total: int | None
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

    @property
    def membership_unknown(self) -> bool:
        return self.inventory_updating or bool(self.inventory.diagnostics)

    @property
    def live_identities(self) -> set[str]:
        """Instances whose active roots still require live observation."""
        return {
            item.identity
            for item in self.inventory.instances
            if item.state != "not running"
            and not (
                item.state == "unknown"
                and item.observed_state in ("preparing", "starting")
            )
        }

    @computed_field
    @property
    def summary(self) -> TopSummary:
        unknown = self.membership_unknown
        counts = [
            None
            if unknown
            else sum(item.placement == placement for item in self.inventory.instances)
            for placement in ("resident", "visiting", "roaming")
        ]
        current = self.live_identities
        live_sources = {
            source.identity: source
            for source in self.sources
            if source.identity in current
        }
        active = (
            None
            if unknown
            or len(live_sources) != len(current)
            or any(source.live_stale for source in live_sources.values())
            else sum(
                len(source.live.trees)
                for source in live_sources.values()
                if source.live
            )
        )
        completed = None
        if (
            not unknown
            and not self._missing_sources()
            and all(
                source.completed is not None and source.activity_error is None
                for source in self.sources
            )
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
            running=None
            if unknown
            or any(
                item.state == "unknown" and item.identity in current
                for item in self.inventory.instances
            )
            else sum(item.state == "running" for item in self.inventory.instances),
            total=None if unknown else len(self.inventory.instances),
            resident=counts[0],
            visiting=counts[1],
            roaming=counts[2],
            active=active,
            completed=completed,
            usage=self.usage_total(),
        )

    def usage_total(self) -> Metrics | None:
        if self.membership_unknown or self._missing_sources():
            return None
        result = Metrics()
        for source in self.sources:
            if source.usage is None or source.usage_error is not None:
                return None
            result += source.usage.metrics
        return result

    def _missing_sources(self) -> bool:
        available = {source.identity for source in self.sources}
        return any(item.identity not in available for item in self.inventory.instances)

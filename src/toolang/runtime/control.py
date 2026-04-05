"""Runtime control-plane helpers."""

from __future__ import annotations

from toolang.bus.db import BusStore
from toolang.bus.events import AgentChanged, utc_now
from toolang.concepts.caps import CapKind
from toolang.concepts.identity import AgentRef
from toolang.runtime.cap_defs import (
    CapMutationResult,
    delete_cap_definition,
    put_cap_definition,
)
from toolang.runtime.work import (
    patch_chore_item,
    patch_task_item,
    patch_will_item,
    put_chore_item,
    put_task_item,
    put_will_item,
)

SHORT_AGENT_ID_LENGTH = 12


def put_cap(
    bus: BusStore,
    agent: AgentRef,
    *,
    kind: CapKind,
    name: str,
    scope: str,
    source: str | None,
    ref: str | None,
    content: str | None,
) -> CapMutationResult:
    """Create or replace one authored cap definition."""

    result = put_cap_definition(
        agent,
        kind=kind,
        name=name,
        scope=scope,
        source=source,
        ref=ref,
        content=content,
    )
    _append_caps_updated(bus, agent, detail=result.detail)
    return result


def delete_cap(
    bus: BusStore,
    agent: AgentRef,
    *,
    kind: CapKind,
    name: str,
    scope: str,
    source: str | None,
) -> CapMutationResult:
    """Delete one authored cap definition."""

    result = delete_cap_definition(
        agent,
        kind=kind,
        name=name,
        scope=scope,
        source=source,
    )
    _append_caps_updated(bus, agent, detail=result.detail)
    return result


def _append_caps_updated(bus: BusStore, agent: AgentRef, *, detail: str) -> None:
    bus.append(
        AgentChanged(
            at=utc_now(),
            agent_uri=agent.uri,
            agent_id=agent.id[:SHORT_AGENT_ID_LENGTH],
            name=agent.name,
            change_type="caps_updated",
            detail=detail,
            agent_home=str(agent.home),
            source_file=agent.source.name,
        )
    )


__all__ = [
    "delete_cap",
    "patch_chore_item",
    "patch_task_item",
    "patch_will_item",
    "put_cap",
    "put_chore_item",
    "put_task_item",
    "put_will_item",
]

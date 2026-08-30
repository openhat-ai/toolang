"""Public query schema for authored runnable declarations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from toolang.common.query import CollectionDefinition, CollectionSchema, IdentitySpec

RunnableKind = Literal["agic", "flow"]
RouteAction = Literal["run", "execute"]


@dataclass(frozen=True, slots=True)
class RunnableQueryView:
    """Explicitly public runnable query representation."""

    record: object
    kind: RunnableKind
    name: str
    module: str
    description: str | None
    parameters: tuple[str, ...]
    required_parameters: tuple[str, ...]
    route_actions: tuple[RouteAction, ...]


RUNNABLE_SCHEMA = CollectionSchema.from_type(
    "runnables",
    RunnableQueryView,
    key=("kind", "name"),
    identity=IdentitySpec(
        paths=("kind", "name"),
        labels=("kind", "runnable"),
        separator=":",
    ),
    exclude=("record",),
)
RUNNABLE_DEFINITION = CollectionDefinition(RUNNABLE_SCHEMA)


__all__ = [
    "RUNNABLE_DEFINITION",
    "RUNNABLE_SCHEMA",
    "RouteAction",
    "RunnableKind",
    "RunnableQueryView",
]

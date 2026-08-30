"""Public query view for State runnables."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from toolang.common.query import QueryDataset
from toolang.lang.ast import AgicDecl, FlowDecl, Program
from toolang.lang.runnable_query import (
    RUNNABLE_DEFINITION,
    RUNNABLE_SCHEMA,
    RouteAction,
    RunnableKind,
    RunnableQueryView,
)

from .state import effective_agics, state_program


def runnable_dataset(
    state: object,
    *,
    route_actions: Mapping[str, Sequence[RouteAction]] | None = None,
) -> QueryDataset[RunnableQueryView]:
    """Materialize the complete public runnable index from captured State."""

    actions = route_actions or {}
    raw_index = getattr(state, "runnables", None)
    raw_modules = getattr(state, "runnable_modules", None)
    if isinstance(state, Program):
        index = {item.name: item for item in (*effective_agics(state), *state.flows)}
        modules = {name: "agent" for name in index}
    elif isinstance(raw_index, Mapping) and isinstance(raw_modules, Mapping):
        index = cast(Mapping[str, AgicDecl | FlowDecl], raw_index)
        modules = cast(Mapping[str, str], raw_modules)
    else:
        program = state_program(state)
        index = {
            item.name: item for item in (*effective_agics(program), *program.flows)
        }
        modules = {name: "agent" for name in index}
    return RUNNABLE_DEFINITION.dataset(
        tuple(
            _runnable_view(
                name,
                runnable,
                module=modules[name],
                actions=actions.get(f"{runnable.kind}:{name}", ()),
            )
            for name, runnable in index.items()
        )
    )


def _runnable_view(
    name: str,
    runnable: AgicDecl | FlowDecl,
    *,
    module: str,
    actions: Sequence[RouteAction],
) -> RunnableQueryView:
    parameters = (
        *((runnable.input.name,) if runnable.input is not None else ()),
        *(parameter.name for parameter in runnable.params),
    )
    required = (
        *(
            (runnable.input.name,)
            if runnable.input is not None and not runnable.input.optional
            else ()
        ),
        *(parameter.name for parameter in runnable.params if not parameter.optional),
    )
    description = runnable.instruct if isinstance(runnable, AgicDecl) else None
    return RunnableQueryView(
        record=runnable,
        kind=cast(RunnableKind, runnable.kind),
        name=name,
        module=module,
        description=description,
        parameters=parameters,
        required_parameters=required,
        route_actions=tuple(actions),
    )


__all__ = [
    "RUNNABLE_DEFINITION",
    "RUNNABLE_SCHEMA",
    "RouteAction",
    "RunnableKind",
    "RunnableQueryView",
    "runnable_dataset",
]

"""Parse caller input and resolve immutable run specifications."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.base.types.message import PerceptPart
from toolang.base.types.policy import RunBindings
from toolang.lang.ast import AgicDecl, FlowDecl
from toolang.lang.input import (
    NamedInputSources,
    RunnableInput,
    coerce_input,
    parse_input,
    perceive_input,
)
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

from .policy import parse_policy_prefix, resolve_commands
from .runnables import parse_runnable_ref, resolve_runnable
from .types import PolicyCommand

if TYPE_CHECKING:
    from .executor.executor import RunSpec

IncludeResolver = Callable[[str], PerceptPart]
Runnable = AgicDecl | FlowDecl


def parse_call(source: str) -> tuple[tuple[PolicyCommand, ...], RunnableInput]:
    """Parse one run-only source into policy commands and runnable input."""

    body = _strip_final_line_break(source)
    commands, named, primary = parse_policy_prefix(body)
    return commands, parse_input(primary or None, named=named)


def resolve_spec(
    commands: Sequence[PolicyCommand],
    input: RunnableInput,
    *,
    setup: AgentSetup,
    state: AgentState,
    thread: str,
    default_runnable: str,
    surface: RunBindings = RunBindings(),
    session_commands: Sequence[PolicyCommand] = (),
    surface_named: Mapping[str, object] | None = None,
    surface_named_sources: NamedInputSources = (),
    include: IncludeResolver | None = None,
) -> RunSpec:
    """Resolve structured caller input against current immutable snapshots."""

    from .executor.executor import RunSpec

    restrictions, bindings, limits = resolve_commands(
        setup,
        surface=surface,
        session=session_commands,
        run=commands,
    )
    runnable_ref = bindings.runnable or default_runnable
    runnable_name, runnable_kind = parse_runnable_ref(runnable_ref)
    runnable = resolve_runnable(
        state.program,
        runnable_name,
        kind=runnable_kind,
    )
    if surface_named and surface_named_sources:
        raise ValueError("surface named inputs cannot be both bound and sourced")
    if input.named and (surface_named or surface_named_sources):
        raise ValueError("named inputs cannot be supplied by both source and surface")
    raw_named = input.named or surface_named_sources
    named = (
        _bind_named_sources(
            raw_named,
            runnable=runnable,
            state=state,
            include=include,
        )
        if raw_named
        else dict(surface_named or {})
    )
    _validate_named(runnable, named)
    primary = perceive_input(
        input.primary or "",
        program=state.program,
        include=include,
    )
    return RunSpec(
        setup=setup,
        state=state,
        thread=thread,
        bindings=RunBindings(
            model=bindings.model,
            runnable=f"{runnable.kind}:{runnable.name}",
        ),
        limits=limits,
        ceilings=restrictions,
        primary=primary,
        named=named or None,
    )


def validate_commands(
    commands: Sequence[PolicyCommand],
    *,
    setup: AgentSetup,
    state: AgentState,
    default_runnable: str,
    surface: RunBindings = RunBindings(),
) -> None:
    """Validate one prospective session policy without requiring run input."""

    from .executor.ceiling import (
        restrict_agent_ceiling,
        resolve_agent_ceiling,
        validate_root_run_resources,
    )

    restrictions, bindings, _limits = resolve_commands(
        setup,
        surface=surface,
        session=commands,
    )
    runnable_name, runnable_kind = parse_runnable_ref(
        bindings.runnable or default_runnable
    )
    runnable = resolve_runnable(
        state.program,
        runnable_name,
        kind=runnable_kind,
    )
    ceiling = resolve_agent_ceiling(setup, state, setup.ceiling)
    for restriction in restrictions:
        ceiling = restrict_agent_ceiling(
            setup,
            state,
            ceiling,
            restriction,
        )
    validate_root_run_resources(
        setup,
        state,
        executable=runnable,
        agent=ceiling,
        model=bindings.model,
    )


def _bind_named_sources(
    sources: NamedInputSources,
    *,
    runnable: Runnable,
    state: AgentState,
    include: IncludeResolver | None,
) -> dict[str, object]:
    params = {parameter.name: parameter for parameter in runnable.params}
    structs = {struct.name: struct for struct in state.program.structs}
    result: dict[str, object] = {}
    for name, source in sources:
        parameter = params.get(name)
        if parameter is None:
            raise ValueError(f"unknown named inputs for {runnable.name}: {name}")
        percept = perceive_input(
            source,
            program=state.program,
            include=include,
        )
        result[name] = coerce_input(
            percept,
            parameter.type_name or "Part[]",
            structs=structs,
        )
    return result


def _validate_named(
    runnable: Runnable,
    named: Mapping[str, object],
) -> None:
    params = {parameter.name: parameter for parameter in runnable.params}
    unknown = sorted(set(named) - set(params))
    if unknown:
        raise ValueError(
            f"unknown named inputs for {runnable.name}: {', '.join(unknown)}"
        )
    missing = sorted(
        name
        for name, parameter in params.items()
        if not parameter.optional and name not in named
    )
    if missing:
        raise ValueError(
            f"missing named inputs for {runnable.name}: {', '.join(missing)}"
        )


def _strip_final_line_break(source: str) -> str:
    if source.endswith("\r\n"):
        return source[:-2]
    if source.endswith("\n"):
        return source[:-1]
    return source

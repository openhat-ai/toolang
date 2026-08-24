"""Parse caller input and resolve immutable run specifications."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from toolang.base.types.message import Part
from toolang.base.types.policy import RunBindings
from toolang.lang.input import (
    NamedInputSources,
    RunnableInputRaw,
    parse_input,
    resolve_input_parts,
    resolve_runnable_input,
)
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

from .policy import parse_policy_prefix, resolve_commands
from .runnables import parse_runnable_ref, resolve_runnable
from .types import RunOverride, RunSpace

if TYPE_CHECKING:
    from .executor.executor import RunSpec

IncludeResolver = Callable[[str], Part]


def parse_call(source: str) -> tuple[tuple[RunOverride, ...], RunnableInputRaw]:
    """Parse one run-only source into policy commands and runnable input."""

    body = _strip_final_line_break(source)
    commands, named, primary = parse_policy_prefix(body)
    return commands, parse_input(primary or None, named=named)


def resolve_spec(
    commands: Sequence[RunOverride],
    input: RunnableInputRaw,
    *,
    setup: AgentSetup,
    state: AgentState,
    thread: str,
    space: RunSpace,
    default_runnable: str,
    surface: RunBindings = RunBindings(),
    session_commands: Sequence[RunOverride] = (),
    surface_named: Mapping[str, object] | None = None,
    surface_named_sources: NamedInputSources = (),
    include: IncludeResolver | None = None,
) -> RunSpec:
    """Resolve structured caller input against current immutable snapshots."""

    from .executor.executor import RunSpec

    ceilings, bindings, limits = resolve_commands(
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
        _resolve_named_sources(
            raw_named,
            state=state,
            include=include,
        )
        if raw_named
        else dict(surface_named or {})
    )
    primary = (
        resolve_input_parts(
            input.primary,
            program=state.program,
            include=include,
        )
        if input.primary is not None
        else None
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
        space=space,
        ceilings=ceilings,
        input=resolve_runnable_input(
            runnable,
            primary=primary,
            named=named,
            structs={struct.name: struct for struct in state.program.structs},
        ),
    )


def validate_commands(
    commands: Sequence[RunOverride],
    *,
    setup: AgentSetup,
    state: AgentState,
    default_runnable: str,
    surface: RunBindings = RunBindings(),
) -> None:
    """Validate one prospective session policy without requiring run input."""

    from .executor.resources import (
        apply_agent_ceiling,
        resolve_agent_resources,
        resolve_runnable_resources,
        snapshot_model_selection,
        validate_model_binding,
    )

    ceilings, bindings, _limits = resolve_commands(
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
    resources = resolve_agent_resources(setup, state, setup.ceiling)
    for ceiling in ceilings:
        resources = apply_agent_ceiling(
            setup,
            state,
            resources,
            ceiling,
        )
    resources = resolve_runnable_resources(
        snapshot_model_selection(setup, state),
        executable=runnable,
        base=resources,
        setup=setup,
        state=state,
    )
    validate_model_binding(
        snapshot_model_selection(setup, state),
        executable=runnable,
        resources=resources,
        model=bindings.model,
    )


def _resolve_named_sources(
    sources: NamedInputSources,
    *,
    state: AgentState,
    include: IncludeResolver | None,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, source in sources:
        result[name] = resolve_input_parts(
            source,
            program=state.program,
            include=include,
        )
    return result


def _strip_final_line_break(source: str) -> str:
    if source.endswith("\r\n"):
        return source[:-2]
    if source.endswith("\n"):
        return source[:-1]
    return source

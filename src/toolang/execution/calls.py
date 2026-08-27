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
from toolang.lang.ast import Program
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

from .policy import parse_policy_prefix, resolve_commands
from .runnables import parse_runnable_ref, resolve_state_runnable
from .schemas import RunRequest
from .types import RunOverride

if TYPE_CHECKING:
    from .executor.executor import RunSpec

IncludeResolver = Callable[[str], Part]


def parse_call(source: str) -> tuple[tuple[RunOverride, ...], RunnableInputRaw]:
    """Parse one run-only source into policy commands and runnable input."""

    body = _strip_final_line_break(source)
    commands, named, primary = parse_policy_prefix(body)
    return commands, parse_input(primary or None, named=named)


def resolve_run_request(
    request: RunRequest,
    *,
    setup: AgentSetup,
    state: AgentState,
    include: IncludeResolver | None = None,
) -> RunSpec:
    """Resolve one caller request against one setup and state snapshot pair."""

    return resolve_spec(
        request.commands,
        request.input,
        setup=setup,
        state=state,
        thread=request.thread,
        default_runnable=_select_runnable_fallback(
            state,
            request.runnable_fallbacks,
        ),
        session_commands=request.session_commands,
        include=include,
    )


def resolve_spec(
    commands: Sequence[RunOverride],
    input: RunnableInputRaw,
    *,
    setup: AgentSetup,
    state: AgentState,
    thread: str,
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
    resolved = resolve_state_runnable(
        state,
        runnable_name,
        kind=runnable_kind,
    )
    runnable = resolved.executable
    program = resolved.module.program
    if surface_named and surface_named_sources:
        raise ValueError("surface named inputs cannot be both bound and sourced")
    if input.named and (surface_named or surface_named_sources):
        raise ValueError("named inputs cannot be supplied by both source and surface")
    raw_named = input.named or surface_named_sources
    named = (
        _resolve_named_sources(
            raw_named,
            program=program,
            include=include,
        )
        if raw_named
        else dict(surface_named or {})
    )
    primary = (
        resolve_input_parts(
            input.primary,
            program=program,
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
            runnable=f"{resolved.public.kind}:{resolved.public.name}",
        ),
        limits=limits,
        ceilings=ceilings,
        input=resolve_runnable_input(
            runnable,
            primary=primary,
            named=named,
            structs={struct.name: struct for struct in program.structs},
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
    resolved = resolve_state_runnable(
        state,
        runnable_name,
        kind=runnable_kind,
    )
    runnable = resolved.executable
    resources = resolve_agent_resources(
        setup,
        state,
        setup.ceiling,
        module=resolved.module.name,
    )
    for ceiling in ceilings:
        resources = apply_agent_ceiling(
            setup,
            state,
            resources,
            ceiling,
            module=resolved.module.name,
        )
    resources = resolve_runnable_resources(
        snapshot_model_selection(setup, state),
        executable=runnable,
        base=resources,
        setup=setup,
        state=state,
        module=resolved.module.name,
    )
    validate_model_binding(
        snapshot_model_selection(setup, state),
        executable=runnable,
        resources=resources,
        model=bindings.model,
    )


def validate_session_commands(
    commands: Sequence[RunOverride],
    *,
    setup: AgentSetup,
    state: AgentState,
    runnable_fallbacks: tuple[str, ...],
) -> None:
    """Validate session commands against the first available runnable fallback."""

    validate_commands(
        commands,
        setup=setup,
        state=state,
        default_runnable=_select_runnable_fallback(state, runnable_fallbacks),
    )


def _resolve_named_sources(
    sources: NamedInputSources,
    *,
    program: Program,
    include: IncludeResolver | None,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, source in sources:
        result[name] = resolve_input_parts(
            source,
            program=program,
            include=include,
        )
    return result


def _select_runnable_fallback(
    state: AgentState,
    candidates: tuple[str, ...],
) -> str:
    for candidate in candidates:
        name, kind = parse_runnable_ref(candidate)
        entry = state.catalog.get(name)
        if entry is not None and (kind is None or entry.kind == kind):
            return candidate
    joined = ", ".join(candidates)
    raise ValueError(f"no runnable fallback is available: {joined}")


def _strip_final_line_break(source: str) -> str:
    if source.endswith("\r\n"):
        return source[:-2]
    if source.endswith("\n"):
        return source[:-1]
    return source

"""Parse caller input and resolve immutable run specifications."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from toolang.base.types.message import Part
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.lang.input import (
    NamedInputSources,
    PromptDefinitionIdentity,
    PromptInvocation,
    RunnableInputRaw,
    parse_input,
    prompt_definition_identity,
    resolve_input_parts_with_provenance,
    resolve_runnable_input,
)
from toolang.lang.ast import Program
from toolang.setup import AgentSetup
from toolang.state.state import AgentState, state_module_caps

from .policy import parse_policy_prefix, resolve_commands
from .runnables import (
    parse_runnable_ref,
    resolve_state_runnable,
)
from .schemas import RerunRequest, RetryRequest, RunRequest
from .types import RunOverride

if TYPE_CHECKING:
    from .executor.executor import RunSpec

IncludeResolver = Callable[[str], Part]


@dataclass(frozen=True, slots=True)
class RestartSpec:
    """One restart request resolved against immutable runtime snapshots."""

    setup: AgentSetup
    state: AgentState
    ceiling: AgentCeiling
    model: str | None
    limits: RunLimits


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


def resolve_restart_request(
    request: RetryRequest | RerunRequest,
    *,
    setup: AgentSetup,
    state: AgentState,
) -> RestartSpec:
    """Resolve restart policy against exactly one setup and state snapshot pair."""

    ceilings, bindings, limits = resolve_commands(setup, run=request.commands)
    if len(ceilings) > 1:  # pragma: no cover - one request contributes one layer
        raise RuntimeError("restart request resolved multiple run ceilings")
    return RestartSpec(
        setup=setup,
        state=state,
        ceiling=ceilings[0] if ceilings else AgentCeiling(),
        model=bindings.model,
        limits=limits,
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
    module, runnable = resolve_state_runnable(
        state,
        runnable_name,
        kind=runnable_kind,
    )
    program = state.modules[module]
    if surface_named and surface_named_sources:
        raise ValueError("surface named inputs cannot be both bound and sourced")
    if input.named and (surface_named or surface_named_sources):
        raise ValueError("named inputs cannot be supplied by both source and surface")
    raw_named = input.named or surface_named_sources
    definitions = _prompt_definitions(state, module=module, program=program)
    invocations: list[PromptInvocation] = []
    if input.primary is not None:
        primary_resolution = resolve_input_parts_with_provenance(
            input.primary,
            program=program,
            include=include,
            prompt_definitions=definitions,
        )
        primary = primary_resolution.parts
        _extend_invocations(invocations, primary_resolution.prompts)
    else:
        primary = None
    if raw_named:
        named = _resolve_named_sources(
            raw_named,
            program=program,
            include=include,
            prompt_definitions=definitions,
            invocations=invocations,
        )
    else:
        named = dict(surface_named or {})
    return RunSpec(
        setup=setup,
        state=state,
        thread=thread,
        bindings=RunBindings(
            model=bindings.model,
            runnable=f"{runnable.kind}:{runnable_name}",
        ),
        limits=limits,
        ceilings=ceilings,
        input=resolve_runnable_input(
            runnable,
            primary=primary,
            named=named,
            structs={struct.name: struct for struct in program.structs},
        ),
        authored_input=RunnableInputRaw(primary=input.primary, named=raw_named),
        authored_commands=tuple(commands),
        authored_session_commands=tuple(session_commands),
        prompt_invocations=tuple(invocations),
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
    module, runnable = resolve_state_runnable(
        state,
        runnable_name,
        kind=runnable_kind,
    )
    resources = resolve_agent_resources(
        setup,
        state,
        setup.ceiling,
        module=module,
    )
    for ceiling in ceilings:
        resources = apply_agent_ceiling(
            setup,
            state,
            resources,
            ceiling,
            module=module,
        )
    resources = resolve_runnable_resources(
        snapshot_model_selection(setup, state),
        runnable=runnable,
        base=resources,
        setup=setup,
        state=state,
        module=module,
    )
    validate_model_binding(
        snapshot_model_selection(setup, state),
        runnable=runnable,
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
    prompt_definitions: Mapping[str, PromptDefinitionIdentity],
    invocations: list[PromptInvocation],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, source in sources:
        resolution = resolve_input_parts_with_provenance(
            source,
            program=program,
            include=include,
            prompt_definitions=prompt_definitions,
        )
        result[name] = resolution.parts
        _extend_invocations(invocations, resolution.prompts)
    return result


def _prompt_definitions(
    state: AgentState,
    *,
    module: str,
    program: Program,
) -> dict[str, PromptDefinitionIdentity]:
    cap_refs = {
        cap.name: cap.ref
        for cap in state_module_caps(state, module)
        if cap.kind == "prompt"
    }
    return {
        prompt.name: prompt_definition_identity(
            prompt,
            ref=cap_refs.get(prompt.name),
        )
        for prompt in program.caps
        if prompt.kind == "prompt"
    }


def _extend_invocations(
    target: list[PromptInvocation],
    additions: Sequence[PromptInvocation],
) -> None:
    offset = len(target)
    target.extend(
        replace(
            invocation,
            parent=(
                invocation.parent + offset if invocation.parent is not None else None
            ),
        )
        for invocation in additions
    )


def _select_runnable_fallback(
    state: AgentState,
    candidates: tuple[str, ...],
) -> str:
    for candidate in candidates:
        name, kind = parse_runnable_ref(candidate)
        entry = state.runnables.get(name)
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

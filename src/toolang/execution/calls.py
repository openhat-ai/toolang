"""Parse caller input and resolve immutable run specifications."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from toolang.base.types.message import Part
from toolang.base.types.model import ModelRequest
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
from toolang.lang.ast import AgicDecl, Program
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
    model: ModelRequest | None
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

    return _resolve_concrete_spec(
        request.runnable.input,
        setup=setup,
        state=state,
        thread=request.thread_id,
        bindings=RunBindings(
            runnable=request.runnable.ref,
            model=request.model.ref if request.model is not None else None,
        ),
        model_request=request.model,
        ceilings=request.policy.allow,
        limits=request.policy.limits,
        include=include,
        require_model_request=True,
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
        model=_rerun_model_request(request, bindings),
        limits=limits,
    )


def _rerun_model_request(
    request: RetryRequest | RerunRequest,
    bindings: RunBindings,
) -> ModelRequest | None:
    """Return only an explicit rerun replacement, never a setup default."""

    if not isinstance(request, RerunRequest):
        return None
    if request.model is not None:
        return request.model
    replaces_model = any(
        command.group == "default" and command.field == "model"
        for command in request.commands
    )
    if not replaces_model or bindings.model is None:
        return None
    return ModelRequest(bindings.model)


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

    ceilings, bindings, limits = resolve_commands(
        setup,
        surface=surface,
        session=session_commands,
        run=commands,
    )
    if bindings.runnable is None:
        bindings = RunBindings(model=bindings.model, runnable=default_runnable)
    return _resolve_concrete_spec(
        input,
        setup=setup,
        state=state,
        thread=thread,
        bindings=bindings,
        model_request=(
            ModelRequest(bindings.model) if bindings.model is not None else None
        ),
        ceilings=ceilings,
        limits=limits,
        surface_named=surface_named,
        surface_named_sources=surface_named_sources,
        include=include,
        authored_commands=tuple(commands),
        authored_session_commands=tuple(session_commands),
    )


def _resolve_concrete_spec(
    input: RunnableInputRaw,
    *,
    setup: AgentSetup,
    state: AgentState,
    thread: str,
    bindings: RunBindings,
    model_request: ModelRequest | None,
    ceilings: tuple[AgentCeiling, ...],
    limits: RunLimits,
    surface_named: Mapping[str, object] | None = None,
    surface_named_sources: NamedInputSources = (),
    include: IncludeResolver | None = None,
    authored_commands: tuple[RunOverride, ...] = (),
    authored_session_commands: tuple[RunOverride, ...] = (),
    require_model_request: bool = False,
) -> RunSpec:
    """Resolve one already-materialized request against runtime snapshots."""

    from .executor.executor import RunSpec

    runnable_ref = bindings.runnable
    if runnable_ref is None:
        raise ValueError("run request requires a concrete runnable ref")
    runnable_name, runnable_kind = parse_runnable_ref(runnable_ref)
    module, runnable = resolve_state_runnable(
        state,
        runnable_name,
        kind=runnable_kind,
    )
    if (
        require_model_request
        and isinstance(runnable, AgicDecl)
        and model_request is None
    ):
        raise ValueError("run request requires a model for an agic runnable")
    program = state.modules[module]
    if surface_named and surface_named_sources:
        raise ValueError("surface named inputs cannot be both bound and sourced")
    if input.named and (surface_named or surface_named_sources):
        raise ValueError("named inputs cannot be supplied by both source and surface")
    raw_named = input.named or surface_named_sources
    definitions = prompt_definitions(state, module=module, program=program)
    invocations: list[PromptInvocation] = []
    if input._ is not None:
        primary_resolution = resolve_input_parts_with_provenance(
            input._,
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
        model_request=model_request,
        limits=limits,
        ceilings=ceilings,
        input=resolve_runnable_input(
            runnable,
            primary=primary,
            named=named,
            structs={struct.name: struct for struct in program.structs},
        ),
        authored_input=RunnableInputRaw(_=input._, named=raw_named),
        authored_commands=authored_commands,
        authored_session_commands=authored_session_commands,
        prompt_invocations=tuple(invocations),
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
    for item in sources:
        resolution = resolve_input_parts_with_provenance(
            item.source,
            program=program,
            include=include,
            prompt_definitions=prompt_definitions,
        )
        result[item.name] = resolution.parts
        _extend_invocations(invocations, resolution.prompts)
    return result


def prompt_definitions(
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


def _strip_final_line_break(source: str) -> str:
    if source.endswith("\r\n"):
        return source[:-2]
    if source.endswith("\n"):
        return source[:-1]
    return source

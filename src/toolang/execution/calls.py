"""Bind textual runnable calls to immutable executor inputs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from toolang.base.types.message import PerceptPart
from toolang.base.types.policy import AgentCeiling
from toolang.lang.ast import AgicDecl, FlowDecl
from toolang.lang.input import coerce_input, perceive_input
from toolang.lang.submission import (
    Arguments,
    RunnableCall,
    RunOverride,
    SettingCommand,
)
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

from .runnables import parse_runnable_ref, resolve_runnable

if TYPE_CHECKING:
    from .executor.executor import RunSpec

IncludeResolver = Callable[[str], PerceptPart]
Runnable = AgicDecl | FlowDecl


@dataclass(frozen=True, slots=True)
class _Selection:
    runnable: Runnable
    model: str | None
    args: Mapping[str, object]


def bind_runnable_call(
    call: RunnableCall,
    *,
    setup: AgentSetup,
    state: AgentState,
    thread: str,
    default_runnable: str,
    selected_runnable: str | None = None,
    ceiling: AgentCeiling | None = None,
    default_model: str | None = None,
    default_args: Mapping[str, object] | None = None,
    default_raw_args: Arguments = (),
    settings: tuple[SettingCommand, ...] = (),
    include: IncludeResolver | None = None,
) -> RunSpec:
    """Resolve one call against explicit surface defaults and snapshots."""

    from .executor.executor import RunSpec
    resolved_ceiling = ceiling or AgentCeiling()
    bound_runnable, bound_kind = parse_runnable_ref(
        selected_runnable or setup.bindings.runnable or default_runnable
    )

    selection = _resolve_selection(
        state=state,
        default_runnable=bound_runnable,
        default_runnable_kind=bound_kind,
        default_model=(
            default_model if default_model is not None else setup.bindings.model
        ),
        default_args=default_args,
        default_raw_args=default_raw_args,
        settings=settings,
        overrides=call.overrides,
        include=include,
    )
    percept = perceive_input(
        call.content,
        program=state.program,
        include=include,
    )
    return RunSpec(
        setup=setup,
        state=state,
        ceiling=resolved_ceiling,
        thread=thread,
        runnable=selection.runnable.name,
        input=percept,
        model=selection.model,
        args=selection.args or None,
    )


def validate_setting_commands(
    settings: tuple[SettingCommand, ...],
    *,
    setup: AgentSetup,
    state: AgentState,
    default_runnable: str,
    selected_runnable: str | None = None,
    ceiling: AgentCeiling | None = None,
    default_model: str | None = None,
    default_args: Mapping[str, object] | None = None,
    default_raw_args: Arguments = (),
    include: IncludeResolver | None = None,
) -> None:
    """Validate one complete prospective chat setting state atomically."""

    bound_runnable, bound_kind = parse_runnable_ref(
        selected_runnable or setup.bindings.runnable or default_runnable
    )
    selection = _resolve_selection(
        state=state,
        default_runnable=bound_runnable,
        default_runnable_kind=bound_kind,
        default_model=(
            default_model if default_model is not None else setup.bindings.model
        ),
        default_args=default_args,
        default_raw_args=default_raw_args,
        settings=settings,
        overrides=(),
        include=include,
    )
    from .executor.ceiling import (
        restrict_agent_ceiling,
        resolve_agent_ceiling,
        validate_root_run_resources,
    )
    resolved_ceiling = ceiling or AgentCeiling()
    base = resolve_agent_ceiling(setup, state, setup.ceiling)
    agent = restrict_agent_ceiling(setup, state, base, resolved_ceiling)
    validate_root_run_resources(
        setup,
        state,
        executable=selection.runnable,
        agent=agent,
        model=selection.model,
    )


def _resolve_selection(
    *,
    state: AgentState,
    default_runnable: str,
    default_runnable_kind: str | None,
    default_model: str | None,
    default_args: Mapping[str, object] | None,
    default_raw_args: Arguments,
    settings: tuple[SettingCommand, ...],
    overrides: tuple[RunOverride, ...],
    include: IncludeResolver | None,
) -> _Selection:
    runnable_name = default_runnable
    runnable_kind = default_runnable_kind
    model = default_model
    if default_args and default_raw_args:
        raise ValueError("default arguments cannot be both bound and raw")
    typed_args = dict(default_args or {})
    raw_args: Arguments | None = default_raw_args or None

    for command in settings:
        if command.kind == "model":
            model = (
                default_model
                if command.selector == "default"
                else command.selector
            )
            continue
        runnable_name = (
            default_runnable
            if command.kind == "agic" and command.selector == "default"
            else command.selector
        )
        runnable_kind = (
            default_runnable_kind
            if command.selector == "default"
            else command.kind
        )
        typed_args = (
            dict(default_args or {}) if command.selector == "default" else {}
        )
        raw_args = (
            default_raw_args or None
            if command.selector == "default"
            else command.args
        )

    for command in overrides:
        if command.kind == "model":
            model = (
                default_model
                if command.selector == "default"
                else command.selector
            )
            continue
        runnable_name = (
            default_runnable
            if command.kind == "agic" and command.selector == "default"
            else command.selector
        )
        runnable_kind = (
            default_runnable_kind
            if command.selector == "default"
            else command.kind
        )
        typed_args = (
            dict(default_args or {}) if command.selector == "default" else {}
        )
        raw_args = (
            default_raw_args or None
            if command.selector == "default"
            else command.args
        )

    runnable = resolve_runnable(state.program, runnable_name, kind=runnable_kind)
    bound_args = (
        _bind_raw_args(
            raw_args,
            runnable=runnable,
            state=state,
            include=include,
        )
        if raw_args is not None
        else typed_args
    )
    _validate_argument_names(runnable, bound_args)
    return _Selection(runnable=runnable, model=model, args=bound_args)


def _bind_raw_args(
    args: Arguments,
    *,
    runnable: Runnable,
    state: AgentState,
    include: IncludeResolver | None,
) -> dict[str, object]:
    params = {parameter.name: parameter for parameter in runnable.params}
    structs = {struct.name: struct for struct in state.program.structs}
    result: dict[str, object] = {}
    for name, source in args:
        parameter = params.get(name)
        if parameter is None:
            raise ValueError(f"unknown arguments for {runnable.name}: {name}")
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


def _validate_argument_names(
    runnable: Runnable,
    args: Mapping[str, object],
) -> None:
    params = {parameter.name: parameter for parameter in runnable.params}
    unknown = sorted(set(args) - set(params))
    if unknown:
        raise ValueError(
            f"unknown arguments for {runnable.name}: {', '.join(unknown)}"
        )
    missing = sorted(
        name
        for name, parameter in params.items()
        if not parameter.optional and name not in args
    )
    if missing:
        raise ValueError(
            f"missing arguments for {runnable.name}: {', '.join(missing)}"
        )

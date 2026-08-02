"""Bind textual runnable calls to immutable executor inputs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from toolang.base.errors import ToolangError
from toolang.base.types.message import PerceptPart
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

from .runnables import effective_agics

if TYPE_CHECKING:
    from .executor.ceiling import CeilingSpec
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
    ceiling: CeilingSpec | None = None,
    default_model: str | None = None,
    default_args: Mapping[str, object] | None = None,
    default_raw_args: Arguments = (),
    settings: tuple[SettingCommand, ...] = (),
    include: IncludeResolver | None = None,
) -> RunSpec:
    """Resolve one call against explicit surface defaults and snapshots."""

    from .executor.executor import RunSpec
    from .executor.ceiling import CeilingSpec

    resolved_ceiling = ceiling or CeilingSpec()

    selection = _resolve_selection(
        state=state,
        default_runnable=default_runnable,
        default_model=default_model,
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
    ceiling: CeilingSpec | None = None,
    default_model: str | None = None,
    default_args: Mapping[str, object] | None = None,
    default_raw_args: Arguments = (),
    include: IncludeResolver | None = None,
) -> None:
    """Validate one complete prospective chat setting state atomically."""

    selection = _resolve_selection(
        state=state,
        default_runnable=default_runnable,
        default_model=default_model,
        default_args=default_args,
        default_raw_args=default_raw_args,
        settings=settings,
        overrides=(),
        include=include,
    )
    from .executor.ceiling import (
        CeilingSpec,
        resolve_agent_ceiling,
        validate_root_run_resources,
    )

    resolved_ceiling = ceiling or CeilingSpec()
    agent = resolve_agent_ceiling(setup, state, resolved_ceiling)
    validate_root_run_resources(
        setup,
        state,
        executable=selection.runnable,
        agent=agent,
        model=selection.model,
    )


def resolve_runnable(
    state: AgentState,
    name: str,
    *,
    kind: str | None = None,
) -> Runnable:
    """Resolve one unique runnable and optionally require its declaration kind."""

    if not name or name != name.strip():
        raise ValueError("run spec requires a canonical runnable name")
    matches: tuple[Runnable, ...] = (
        *(agic for agic in effective_agics(state.program) if agic.name == name),
        *(flow for flow in state.program.flows if flow.name == name),
    )
    if kind is not None:
        matches = tuple(item for item in matches if item.kind == kind)
    if not matches:
        raise ToolangError(f"Runnable not found: {name}")
    if len(matches) > 1:
        raise ToolangError(f"Runnable name is not unique: {name}")
    return matches[0]


def _resolve_selection(
    *,
    state: AgentState,
    default_runnable: str,
    default_model: str | None,
    default_args: Mapping[str, object] | None,
    default_raw_args: Arguments,
    settings: tuple[SettingCommand, ...],
    overrides: tuple[RunOverride, ...],
    include: IncludeResolver | None,
) -> _Selection:
    runnable_name = default_runnable
    runnable_kind: str | None = None
    model = default_model
    if default_args and default_raw_args:
        raise ValueError("default arguments cannot be both bound and raw")
    typed_args = dict(default_args or {})
    raw_args: Arguments | None = default_raw_args or None

    for command in settings:
        if command.kind == "model":
            model = default_model if command.selector == "auto" else command.selector
            continue
        runnable_name = (
            default_runnable
            if command.kind == "agic" and command.selector == "auto"
            else command.selector
        )
        runnable_kind = None if command.selector == "auto" else command.kind
        typed_args = dict(default_args or {}) if command.selector == "auto" else {}
        raw_args = (
            default_raw_args or None
            if command.selector == "auto"
            else command.args
        )

    for command in overrides:
        if command.kind == "model":
            model = default_model if command.selector == "auto" else command.selector
            continue
        runnable_name = (
            default_runnable
            if command.kind == "agic" and command.selector == "auto"
            else command.selector
        )
        runnable_kind = None if command.selector == "auto" else command.kind
        typed_args = dict(default_args or {}) if command.selector == "auto" else {}
        raw_args = (
            default_raw_args or None
            if command.selector == "auto"
            else command.args
        )

    runnable = resolve_runnable(state, runnable_name, kind=runnable_kind)
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

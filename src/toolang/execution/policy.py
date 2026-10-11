"""Apply session settings and materialize execution policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import posixpath

from toolang.base.errors import ToolangError
from toolang.base.model_settings import apply_model_override
from toolang.base.types.policy import AgentCeiling, RunBindings, RunLimits
from toolang.common.policy import resolve_query_sentinels
from toolang.setup import AgentSetup

from .types import (
    ALLOW_FIELDS,
    AllowOverride,
    LimitOverride,
    RunCommand,
    RunOverride,
    SessionSetting,
)


def merge_commands(
    current: Sequence[RunCommand],
    updates: Sequence[RunCommand],
) -> tuple[RunCommand, ...]:
    """Apply low-level updates to a compact retained command sequence."""

    merged = list(_normalize_commands(current))
    for command in _normalize_commands(updates):
        key = (command.group, command.field)
        merged = [item for item in merged if (item.group, item.field) != key]
        if command.value is None and command.group in {"allow", "default"}:
            continue
        merged.append(command)
    return tuple(merged)


def commands_from_run_override(
    override: RunOverride, *, session: bool = False
) -> tuple[RunCommand, ...]:
    """Lower policy-compatible authored fields for durable execution provenance."""

    commands: list[RunCommand] = []
    if override.model is not None and override.model.identity not in {None, "unset"}:
        commands.append(
            RunCommand(
                "default",
                "model",
                None
                if override.model.identity == "default"
                else override.model.identity,
            )
        )
    if override.runnable is not None:
        commands.append(
            RunCommand(
                "default",
                "runnable",
                None
                if session and override.runnable == "default"
                else override.runnable,
            )
        )
    commands.extend(
        RunCommand("allow", item.field, item.value) for item in override.allow
    )
    commands.extend(
        RunCommand("limit", item.field, item.value) for item in override.limits
    )
    return tuple(commands)


def apply_session_setting(
    surface: SessionSetting,
    current: SessionSetting,
    update: RunOverride,
) -> SessionSetting:
    """Apply one validated colon setting body atomically to a session."""

    model = apply_model_override(current.model, surface.model, update.model)
    runnable = current.runnable
    runnable_follows_default = current.runnable_follows_default
    if update.runnable is not None:
        runnable_follows_default = update.runnable == "default"
        runnable = surface.runnable if runnable_follows_default else update.runnable
    allow = _replace_allow_fields(current.allow, update)
    limits = _apply_limit_overrides(current.limits, update.limits)
    return SessionSetting(
        model=model,
        runnable=runnable,
        runnable_follows_default=runnable_follows_default,
        allow=allow,
        limits=limits,
        workdir=update.workdir if update.workdir is not None else current.workdir,
        workdir_base=current.workdir_base,
    )


def materialize_run_setting(
    surface: SessionSetting,
    session: SessionSetting,
    override: RunOverride,
) -> tuple[tuple[AgentCeiling, ...], SessionSetting]:
    """Materialize one input-local override over concrete session settings."""

    model = apply_model_override(session.model, surface.model, override.model)
    runnable_follows_default = session.runnable_follows_default
    runnable = surface.runnable if runnable_follows_default else session.runnable
    if override.runnable is not None:
        runnable_follows_default = False
        runnable = override.runnable
    limits = _apply_limit_overrides(session.limits, override.limits)
    run_ceiling = _allow_ceiling(override.allow)
    ceilings = tuple(
        ceiling
        for ceiling in (session.allow, run_ceiling)
        if ceiling is not None and _ceiling_restricts(ceiling)
    )
    workdir, workdir_base = session.workdir, session.workdir_base
    if override.workdir is not None:
        workdir = override.workdir
        if workdir.startswith("/") or "://" in workdir:
            workdir_base = None
        elif session.workdir is None:
            workdir_base = session.workdir_base
        elif "://" in session.workdir:
            workdir_base = session.workdir
        else:
            workdir = posixpath.normpath(posixpath.join(session.workdir, workdir))
            workdir_base = (
                None if session.workdir.startswith("/") else session.workdir_base
            )
    return ceilings, SessionSetting(
        model=model,
        runnable=runnable,
        runnable_follows_default=runnable_follows_default,
        allow=session.allow,
        limits=limits,
        workdir=workdir,
        workdir_base=workdir_base,
    )


def resolve_commands(
    setup: AgentSetup,
    *,
    surface: RunBindings = RunBindings(),
    session: Sequence[RunCommand] = (),
    run: Sequence[RunCommand] = (),
) -> tuple[tuple[AgentCeiling, ...], RunBindings, RunLimits]:
    """Resolve retained low-level command layers for execution protocols."""

    base = RunBindings(
        model=(
            surface.model
            if surface.model is not None
            else setup.defaults.model.ref
            if setup.defaults.model is not None
            else None
        ),
        runnable=(
            surface.runnable
            if surface.runnable is not None
            else setup.defaults.runnable
        ),
    )
    return materialize_policy(base, setup.limits, session=session, run=run)


def materialize_policy(
    defaults: RunBindings,
    default_limits: RunLimits,
    *,
    session: Sequence[RunCommand] = (),
    run: Sequence[RunCommand] = (),
) -> tuple[tuple[AgentCeiling, ...], RunBindings, RunLimits]:
    """Materialize retained low-level command layers over concrete defaults."""

    bindings = _apply_binding_commands(defaults, defaults, session)
    bindings = _apply_binding_commands(bindings, defaults, run)
    limits = _apply_command_limits(default_limits, session)
    limits = _apply_command_limits(limits, run)
    ceilings = tuple(
        ceiling
        for commands in (session, run)
        if (ceiling := _command_agent_ceiling(commands)) is not None
    )
    return ceilings, bindings, limits


def _replace_allow_fields(current: AgentCeiling, update: RunOverride) -> AgentCeiling:
    if not update.allow:
        return current
    return replace(
        current,
        **{item.field: item.value for item in update.allow},
    )


def _apply_limit_overrides(
    current: RunLimits,
    overrides: Sequence[LimitOverride],
) -> RunLimits:
    return replace(current, **{item.field: item.value for item in overrides})


def _apply_binding_commands(
    current: RunBindings,
    base: RunBindings,
    commands: Sequence[RunCommand],
) -> RunBindings:
    fields: dict[str, str | None] = {
        "model": current.model,
        "runnable": current.runnable,
    }
    for command in commands:
        if command.group != "default":
            continue
        value = command.value
        if value is not None and not isinstance(value, str):
            raise TypeError(f"default {command.field} must be a string or none")
        fields[command.field] = getattr(base, command.field) if value is None else value
    return RunBindings(**fields)


def _normalize_commands(commands: Sequence[RunCommand]) -> tuple[RunCommand, ...]:
    result: list[RunCommand] = []
    positions: dict[tuple[str, str], int] = {}
    for command in commands:
        key = (command.group, command.field)
        position = positions.get(key)
        if position is None:
            positions[key] = len(result)
            result.append(command)
            continue
        if command.group != "allow":
            raise ValueError(f"duplicate {command.group} field: {command.field}")
        previous = result[position]
        if not isinstance(previous.value, tuple) or not isinstance(
            command.value, tuple
        ):
            raise ValueError(f"allow {command.field} cannot combine queries with all")
        if not previous.value or not command.value:
            raise ValueError(f"allow {command.field} cannot combine queries with none")
        result[position] = RunCommand(
            "allow",
            command.field,
            tuple(dict.fromkeys((*previous.value, *command.value))),
        )
    return tuple(result)


def _apply_command_limits(
    current: RunLimits,
    commands: Sequence[RunCommand],
) -> RunLimits:
    fields = {
        command.field: command.value for command in commands if command.group == "limit"
    }
    return replace(current, **fields)


def _command_agent_ceiling(
    commands: Sequence[RunCommand],
) -> AgentCeiling | None:
    fields: dict[str, tuple[str, ...]] = {}
    present: set[str] = set()
    for command in commands:
        if command.group != "allow" or command.value is None:
            continue
        if not isinstance(command.value, tuple):
            raise TypeError(f"allow {command.field} must be queries, all, or none")
        try:
            normalized = resolve_query_sentinels(
                command.value,
                label=f"allow {command.field}",
            )
        except ToolangError as error:
            raise ValueError(str(error)) from error
        if normalized is None:
            continue
        present.add(command.field)
        fields[command.field] = normalized
    ceiling = AgentCeiling(
        **{
            field: fields.get(field) if field in present else None
            for field in ALLOW_FIELDS
        }
    )
    return ceiling if _ceiling_restricts(ceiling) else None


def _allow_ceiling(overrides: Sequence[AllowOverride]) -> AgentCeiling | None:
    if not overrides:
        return None
    return AgentCeiling(**{item.field: item.value for item in overrides})


def _ceiling_restricts(ceiling: AgentCeiling) -> bool:
    return any(getattr(ceiling, field) is not None for field in ALLOW_FIELDS)

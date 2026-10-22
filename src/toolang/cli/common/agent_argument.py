"""Native leaf parameters for agent prefixes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import typer
from typer._click import Command, Context, Parameter
from typer._click.core import ParameterSource
from typer._click.shell_completion import CompletionItem
from typer._click.types import StringParamType
from typer.core import TyperArgument, TyperGroup
from typer.models import ArgumentInfo

from toolang.common.layout import AgentPlacement, validate_agent_name

_PREFIX = f"{__name__}.prefix"
AgentRole = Literal["source", "home", "execute"]
_ROLES = frozenset({"source", "home", "execute"})
_DRIVERS = frozenset({"chat", "talk", "chore"})
_PLACEMENTS = frozenset({"resident", "visiting", "roaming"})


@dataclass(frozen=True, slots=True)
class AgentContract:
    role: AgentRole
    placements: frozenset[AgentPlacement]
    requires_live: bool = False
    required_driver: str | None = None

    @classmethod
    def parse(
        cls,
        role: str,
        placements: str,
        *,
        requires_live: bool = False,
        required_driver: str | None = None,
    ) -> AgentContract:
        if role not in _ROLES:
            raise ValueError("AGENT requires a source, home, or execute role")
        tags = frozenset(tag.strip() for tag in placements.split(","))
        if not tags or tags - _PLACEMENTS:
            raise ValueError(
                "AGENT requires valid resident/visiting/roaming placements"
            )
        if required_driver is not None and required_driver not in _DRIVERS:
            raise ValueError("AGENT requires a valid driver")
        if role == "source" and (requires_live or required_driver is not None):
            raise ValueError("source AGENT cannot require a runtime")
        return cls(
            cast(AgentRole, role),
            cast(frozenset[AgentPlacement], tags),
            requires_live,
            required_driver,
        )

    @property
    def forms(self) -> tuple[str, ...]:
        forms = []
        if "resident" in self.placements:
            forms.append("name")
        if "roaming" in self.placements:
            forms.append("path")
        if "visiting" in self.placements:
            forms.extend(("ref", "url"))
        if self.role in {"home", "execute"} and self.placements != {"resident"}:
            forms.append("instance")
        return tuple(forms)

    @property
    def description(self) -> str:
        forms = tuple("URL" if form == "url" else form for form in self.forms)
        joined = (
            forms[0] if len(forms) == 1 else f"{', '.join(forms[:-1])} or {forms[-1]}"
        )
        label = "Running agent" if self.requires_live else "Agent"
        return f"{label} {joined}"


class AgentNameType(StringParamType):
    """Validate an ordinary catalog name without binding an agent prefix."""

    def convert(self, value: Any, param: Parameter | None, ctx: Context | None) -> str:
        name = super().convert(value, param, ctx)
        try:
            return validate_agent_name(name)
        except ValueError as exc:
            self.fail(str(exc), param, ctx)


class _AgentType(StringParamType):
    def __init__(self, contract: AgentContract) -> None:
        self.contract = contract

    def convert(self, value: Any, param: Parameter | None, ctx: Context | None) -> str:
        from .context import (
            CliContext,
            cli_context,
            resolve_root,
            target_context,
        )

        from toolang.up.source import parse_agent_selector

        text = super().convert(value, param, ctx)
        if ctx is None:
            return text
        state = ctx.obj
        if not isinstance(state, CliContext) and ctx.resilient_parsing:
            # Completion parses contexts without invoking the root callback.
            explicit = ctx.find_root().params.get("toolang_root")
            ctx.obj = CliContext(
                resolve_root(Path(explicit) if explicit is not None else None)
            )
        try:
            selected = target_context(cli_context(ctx).root, parse_agent_selector(text))
        except (OSError, RuntimeError, ValueError) as exc:
            self.fail(str(exc), param, ctx)
        if selected.address is not None and self.contract.role == "source":
            self.fail("instances are not supported", param, ctx)
        placement = (
            selected.address.placement
            if selected.address is not None
            else selected.layout.placement
            if selected.layout
            else "resident"
        )
        if placement not in self.contract.placements:
            label = (
                f"{placement} instances"
                if selected.address is not None
                else {
                    "resident": "agent names",
                    "visiting": "remote agents",
                    "roaming": "paths",
                }[placement]
            )
            self.fail(f"{label} are not supported", param, ctx)
        ctx.obj = selected
        return (selected.agent or text) if placement == "resident" else text

    def shell_complete(
        self, ctx: Context, param: Parameter, incomplete: str
    ) -> list[CompletionItem]:
        if "resident" not in self.contract.placements:
            return []
        return complete_agent_names(ctx, incomplete)


def complete_agent_names(ctx: Context, incomplete: str) -> list[CompletionItem]:
    """Complete lexical name prefixes without resolving or acquiring a home."""
    from toolang.catalog.agent import LocalAgents
    from .context import CliContext, resolve_root

    explicit = ctx.find_root().params.get("toolang_root")
    root = (
        ctx.obj.root
        if isinstance(ctx.obj, CliContext)
        else resolve_root(Path(explicit) if explicit is not None else None)
    )
    requested_escape = incomplete.startswith("agent:")
    stem = incomplete.removeprefix("agent:") if requested_escape else incomplete
    commands = getattr(ctx.find_root().command, "commands", {})
    try:
        names = LocalAgents(root / "agents").list()
    except OSError:
        return []
    return [
        CompletionItem(
            f"agent:{name}" if requested_escape or name in commands else name
        )
        for name in names
        if name.startswith(stem)
    ]


def prefix_supplied(ctx: Context) -> bool:
    return _PREFIX in ctx.meta


def agent_prefix(ctx: Context) -> str | None:
    """Return the prefix carried by this command's selected subtree."""
    bound = ctx.meta.get(_PREFIX)
    if bound is not None:
        owner, raw = bound
        current: Context | None = ctx
        while current is not None:
            if current.command is owner:
                return raw
            current = current.parent
    return None


def AgentArgument(
    role: AgentRole,
    *,
    placements: str = "resident,visiting,roaming",
    requires_live: bool = False,
    required_driver: str | None = None,
    **kwargs: Any,
) -> ArgumentInfo:
    """Declare one native agent-prefix argument and its leaf contract."""
    contract = AgentContract.parse(
        role,
        placements,
        requires_live=requires_live,
        required_driver=required_driver,
    )
    return typer.Argument(
        metavar=kwargs.pop("metavar", "AGENT"),
        click_type=_AgentType(contract),
        show_default=False,
        help=kwargs.pop("help", contract.description),
        **kwargs,
    )


class AgentParameter(TyperArgument):
    """Adapt only the value's parser source; native processing remains intact."""

    def __init__(self, argument: TyperArgument) -> None:
        super().__init__(
            param_decls=argument.opts,
            type=argument.type,
            required=argument.required,
            default=argument.default,
            callback=argument.callback,
            nargs=argument.nargs,
            metavar=argument.metavar,
            expose_value=argument.expose_value,
            is_eager=argument.is_eager,
            envvar=list(argument.envvar)
            if argument.envvar is not None and not isinstance(argument.envvar, str)
            else argument.envvar,
            shell_complete=argument._custom_shell_complete,
            show_default=argument.show_default,
            show_choices=argument.show_choices,
            show_envvar=argument.show_envvar,
            help=argument.help,
            hidden=argument.hidden,
            rich_help_panel=argument.rich_help_panel,
        )
        assert isinstance(self.type, _AgentType)
        self.contract = self.type.contract

    def add_to_parser(self, parser: Any, ctx: Context) -> None:
        # The argument is supplied only by the command header, never its tail.
        return None

    def consume_value(
        self, ctx: Context, opts: Mapping[str, Any]
    ) -> tuple[Any, ParameterSource]:
        if (prefix := agent_prefix(ctx)) is not None:
            return prefix, ParameterSource.COMMANDLINE
        return super().consume_value(ctx, opts)

    def get_usage_pieces(self, ctx: Context) -> list[str]:
        return []


def install_agent_parameter(command: Command) -> None:
    """Adapt native leaf arguments once, during command construction."""
    for index, param in enumerate(command.params):
        if (
            isinstance(param, TyperArgument)
            and not isinstance(param, AgentParameter)
            and isinstance(param.type, _AgentType)
        ):
            command.params[index] = AgentParameter(param)
    agent = agent_parameter(command)
    if agent is None:
        return
    if isinstance(command, TyperGroup):
        raise ValueError("AGENT belongs to executable leaves, not group callbacks")
    if agent.nargs != 1:
        raise ValueError("AGENT must be a single value")
    # The prefix precedes business operands, regardless of Python defaults.
    command.params.remove(agent)
    command.params.insert(0, agent)
    # An inherited prefix can satisfy this leaf even when its native tail is empty.
    command.no_args_is_help = False


def agent_parameter(command: Command) -> AgentParameter | None:
    parameters = [
        param for param in command.params if isinstance(param, AgentParameter)
    ]
    if len(parameters) > 1:
        raise ValueError("a command can declare only one AGENT parameter")
    return next(iter(parameters), None)


def agent_parameters(command: Command) -> tuple[AgentParameter, ...]:
    """Read leaf contracts, including the selected group's native descendants."""
    from .lazy import LazyCommand

    if isinstance(command, LazyCommand):
        return command.agent_parameters()
    if (agent := agent_parameter(command)) is not None:
        return (agent,)
    if isinstance(command, TyperGroup):
        return tuple(
            agent
            for child in command.commands.values()
            for agent in agent_parameters(child)
        )
    return ()


def bind_prefix(ctx: Context, command: Command, raw: str) -> None:
    ctx.meta[_PREFIX] = command, raw

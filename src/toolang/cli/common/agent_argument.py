"""Native agent parameters with an optional position before their command."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import typer
from rich.text import Text
from typer._click import Command, Context, Parameter
from typer._click.core import ParameterSource
from typer._click.shell_completion import CompletionItem
from typer._click.types import StringParamType
from typer.core import TyperArgument
from typer.models import ArgumentInfo

from toolang.common.layout import AgentPlacement

_PREFIX = f"{__name__}.prefix"
_POSITIONS = frozenset({"before", "after"})
_PLACEMENTS = frozenset({"resident", "visiting", "roaming"})


@dataclass(frozen=True, slots=True)
class AgentContract:
    placements: frozenset[AgentPlacement]
    positions: frozenset[str]
    recommended: str

    @classmethod
    def parse(cls, tags: str) -> AgentContract:
        ordered = list(dict.fromkeys(tag.strip() for tag in tags.split(",")))
        unknown = set(ordered) - _POSITIONS - _PLACEMENTS
        if unknown:
            raise ValueError(
                f"invalid AGENT tags: {', '.join(repr(tag) for tag in sorted(unknown))}"
            )
        positions = [tag for tag in ordered if tag in _POSITIONS]
        placements = frozenset(ordered) & _PLACEMENTS
        if not positions or not placements:
            raise ValueError("AGENT requires placement and position tags")
        return cls(
            cast(frozenset[AgentPlacement], placements),
            frozenset(positions),
            positions[0],
        )


class _AgentType(StringParamType):
    def __init__(self, contract: AgentContract) -> None:
        self.contract = contract

    def convert(self, value: Any, param: Parameter | None, ctx: Context | None) -> str:
        from .context import (
            CliContext,
            cli_context,
            parse_target,
            resolve_root,
            target_context,
        )

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
            selected = target_context(cli_context(ctx).root, parse_target(text))
        except (OSError, RuntimeError, ValueError) as exc:
            self.fail(str(exc), param, ctx)
        placement = selected.layout.placement if selected.layout else "resident"
        if placement not in self.contract.placements:
            label = {
                "resident": "resident",
                "visiting": "remote",
                "roaming": "local .too",
            }[placement]
            self.fail(
                f"command '{ctx.info_name}' does not support {label} agents", param, ctx
            )
        ctx.obj = selected
        return (selected.agent or text) if placement == "resident" else text

    def shell_complete(
        self, ctx: Context, param: Parameter, incomplete: str
    ) -> list[CompletionItem]:
        from toolang.catalog.agent import LocalAgents
        from .context import CliContext, resolve_root

        if "resident" not in self.contract.placements:
            return []
        explicit = ctx.find_root().params.get("toolang_root")
        root = (
            ctx.obj.root
            if isinstance(ctx.obj, CliContext)
            else resolve_root(Path(explicit) if explicit is not None else None)
        )
        escape = "agent:" if incomplete.startswith("agent:") else ""
        return [
            CompletionItem(f"{escape}{name}")
            for name in LocalAgents(root / "agents").list()
            if f"{escape}{name}".startswith(incomplete)
        ]


def AgentArgument(tags: str, **kwargs: Any) -> ArgumentInfo:
    """Declare accepted placements and positions, in usage recommendation order."""
    contract = AgentContract.parse(tags)
    labels = ", ".join(
        tag for tag in ("resident", "visiting", "roaming") if tag in contract.placements
    )
    help_text = kwargs.pop(
        "help",
        "Local agent name"
        if contract.placements == {"resident"}
        else "Agent name, GitHub ref, or URL"
        if "roaming" not in contract.placements
        else "Agent name, .too path, GitHub ref, or URL",
    )
    help_text = f"{help_text}; placements: {labels}"
    if len(contract.positions) == 2:
        help_text += "; accepted before or after the command"
    return typer.Argument(
        metavar="AGENT",
        click_type=_AgentType(contract),
        show_default=False,
        help=help_text,
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

    def _prefix(self, ctx: Context) -> str | None:
        bound = ctx.meta.get(_PREFIX)
        return bound[1] if bound is not None and bound[0] is ctx.command else None

    def add_to_parser(self, parser: Any, ctx: Context) -> None:
        if "after" in self.contract.positions and self._prefix(ctx) is None:
            super().add_to_parser(parser, ctx)

    def consume_value(
        self, ctx: Context, opts: Mapping[str, Any]
    ) -> tuple[Any, ParameterSource]:
        if (prefix := self._prefix(ctx)) is not None:
            return prefix, ParameterSource.COMMANDLINE
        return super().consume_value(ctx, opts)

    def get_usage_pieces(self, ctx: Context) -> list[str]:
        return (
            []
            if self.contract.recommended == "before"
            else super().get_usage_pieces(ctx)
        )


def install_agent_parameter(command: Command) -> None:
    """Adapt Typer-built arguments once, at command construction."""
    for index, param in enumerate(command.params):
        if isinstance(param, TyperArgument) and isinstance(param.type, _AgentType):
            command.params[index] = AgentParameter(param)
    agent = agent_parameter(command)
    if agent is None:
        return
    if agent.nargs != 1:
        raise ValueError("AGENT must be a single value")
    positional = [param for param in command.params if isinstance(param, TyperArgument)]
    if not agent.required and "after" in agent.contract.positions:
        if positional[-1] is not agent or any(
            param.nargs == -1 for param in positional
        ):
            raise ValueError(
                "optional after AGENT must be last and cannot accompany variadic arguments"
            )


def agent_parameter(command: Command) -> AgentParameter | None:
    parameters = [
        param for param in command.params if isinstance(param, AgentParameter)
    ]
    if len(parameters) > 1:
        raise ValueError("a command can declare only one AGENT parameter")
    return next(iter(parameters), None)


def bind_prefix(ctx: Context, command: Command, raw: str) -> None:
    ctx.meta[_PREFIX] = command, raw


def usage_path(ctx: Context) -> Text:
    """Insert recommended prefix operands at their owning command level."""
    from .help import parameter_usage

    contexts = []
    current: Context | None = ctx
    while current is not None:
        contexts.append(current)
        current = current.parent
    path = Text()
    for current in reversed(contexts):
        agent = agent_parameter(current.command)
        if path:
            path.append(" ")
        if agent is not None and agent.contract.recommended == "before":
            path.append("<AGENT> " if agent.required else "[AGENT] ", style="cli.usage")
        path.append(current.info_name or "", style="cli.command.name")
        if current is not ctx:
            for param in current.command.get_params(current):
                for piece in parameter_usage(param, current):
                    path.append(f" {piece}", style="cli.usage")
    return path

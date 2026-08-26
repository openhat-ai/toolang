"""Standalone caps CLI entry point."""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar
from pathlib import Path
import os
import sys
from typing import Annotated

import click
import typer

from ...up.logging import configure_logging
from ...common.version import toolang_version
from ..common.context import CliContext, resolve_root
from ..common.output import echo_error
from ..common.routing import (
    OptionalPrefixAgentGroup,
    OptionalPrefixAgentListCommand,
    explicit_agent,
    extract_root_args,
)
from . import commands

_PREFIX_AGENT: ContextVar[str | None] = ContextVar(
    "toolang_caps_cli_prefix_agent", default=None
)
CAP_TOP_LEVEL_COMMANDS = frozenset({"list", *commands.CAP_KINDS})

app = typer.Typer(
    name="caps",
    help="Manage composable agent primitives.",
    cls=OptionalPrefixAgentGroup,
    add_completion=False,
    invoke_without_command=True,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@app.callback()
def callback(
    ctx: typer.Context,
    toolang_root: Annotated[
        Path | None,
        typer.Option("--root", "-r", help="Use a custom Toolang root."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            help="Show current version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Caps CLI."""

    del version
    try:
        configure_logging(spec=None, environ=os.environ)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.obj = CliContext(root=resolve_root(toolang_root), agent=_PREFIX_AGENT.get())


app.command(
    "list",
    help="Inspect available caps.",
    cls=OptionalPrefixAgentListCommand,
)(commands.list_caps)
_cap_apps = commands.create_cap_apps(group_cls=OptionalPrefixAgentGroup)
app.add_typer(_cap_apps["psyche"], name="psyche", no_args_is_help=True)
app.add_typer(_cap_apps["skill"], name="skill", no_args_is_help=True)
app.add_typer(_cap_apps["service"], name="service", no_args_is_help=True)
app.add_typer(_cap_apps["prompt"], name="prompt", no_args_is_help=True)


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    global_args, body = extract_root_args(raw_args)
    try:
        rewritten_body, prefix_agent = _rewrite_agent_shortcuts(body)
    except ValueError as exc:
        echo_error(str(exc))
        return 2
    token = _PREFIX_AGENT.set(prefix_agent)
    try:
        result = app(
            args=[*global_args, *rewritten_body],
            prog_name=_prog_name(sys.argv[0] if sys.argv else ""),
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        if exc.__class__.__name__ != "NoArgsIsHelpError":
            echo_error(exc)
        return exc.exit_code
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        echo_error(str(exc))
        return 1
    finally:
        _PREFIX_AGENT.reset(token)
    return result if isinstance(result, int) else 0


def _rewrite_agent_shortcuts(body: list[str]) -> tuple[list[str], str | None]:
    if not body or body[0] in CAP_TOP_LEVEL_COMMANDS or len(body) < 2:
        return body, None
    if body[1] not in CAP_TOP_LEVEL_COMMANDS:
        return body, None
    explicit = explicit_agent(body[0])
    agent = explicit or body[0]
    return [body[1], *body[2:]], agent


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"caps {_caps_version()}")
    raise typer.Exit()


def _caps_version() -> str:
    return toolang_version()


def _prog_name(argv0: str) -> str:
    text = Path(argv0).name.strip()
    return text or "caps"


if __name__ == "__main__":
    raise SystemExit(main())

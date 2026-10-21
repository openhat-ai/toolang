"""Open an interactive shell in agent home."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TextIO

from typing import Annotated

import typer

from toolang.cli.common.agent_argument import AgentArgument
from typer._click.exceptions import ClickException

from ...common.context import context_agent, prepare_agent, context_root
from ...common.output import shorten_home_path


def cd(
    ctx: typer.Context,
    agent: Annotated[
        str | None, AgentArgument("resident,visiting,roaming,before")
    ] = None,
) -> None:
    """Run the configured interactive shell from the selected directory."""

    agent = context_agent(ctx)
    _require_interactive_terminal(sys.stdin, sys.stdout)
    layout = prepare_agent(ctx) if agent else None
    directory = layout.home if layout else context_root(ctx)
    executable = os.environ.get("SHELL", "/bin/sh")
    destination = f"{layout.name}'s home" if layout else "the Toolang root"
    try:
        display_path = shorten_home_path(directory)
    except (OSError, RuntimeError):
        # A display label must not prevent the shell from opening.
        display_path = str(directory.absolute())
    typer.echo(f"Opening a shell in {destination}: {display_path}")
    typer.echo("Type exit to return.")
    try:
        result = subprocess.run([executable, "-i"], cwd=directory, check=False)
    except OSError as exc:
        raise ClickException(f"could not start shell {executable!r}: {exc}") from exc

    exit_code = result.returncode
    if exit_code < 0:
        exit_code = 128 - exit_code
    if exit_code:
        raise typer.Exit(exit_code)


def _require_interactive_terminal(input_stream: TextIO, output_stream: TextIO) -> None:
    if not input_stream.isatty() or not output_stream.isatty():
        raise ClickException("cd requires an interactive terminal")

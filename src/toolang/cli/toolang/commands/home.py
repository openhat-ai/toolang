"""Open an interactive shell in agent home."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TextIO

import typer
from typer._click.exceptions import ClickException

from ...common.context import context_agent, context_layout, context_root


def should_prepare_target(arguments: list[str]) -> bool:
    """Defer help, usage errors, and non-TTY calls to normal CLI handling."""

    if arguments not in ([], ["--"]):
        return False
    try:
        _require_interactive_terminal(sys.stdin, sys.stdout)
    except ClickException:
        return False
    return True


def home(ctx: typer.Context) -> None:
    """Run the configured interactive shell from the selected directory."""

    agent = context_agent(ctx)
    directory = context_layout(ctx).home if agent else context_root(ctx)
    _require_interactive_terminal(sys.stdin, sys.stdout)
    executable = os.environ.get("SHELL", "/bin/sh")
    destination = "agent home" if agent else "Toolang root"
    typer.echo(f"Entered {destination}. Type exit to return.")
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
        raise ClickException("home requires an interactive terminal")

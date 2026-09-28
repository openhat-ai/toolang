"""Open an interactive shell in one resident agent home."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TextIO

import typer
from typer._click.exceptions import ClickException

from ...common.context import context_layout


def shell(ctx: typer.Context) -> None:
    """Run the configured interactive shell from the selected agent home."""

    layout = context_layout(ctx)
    _require_interactive_terminal(sys.stdin, sys.stdout)
    executable = os.environ.get("SHELL", "/bin/sh")
    try:
        result = subprocess.run([executable, "-i"], cwd=layout.home, check=False)
    except OSError as exc:
        raise ClickException(f"could not start shell {executable!r}: {exc}") from exc

    exit_code = result.returncode
    if exit_code < 0:
        exit_code = 128 - exit_code
    if exit_code:
        raise typer.Exit(exit_code)


def _require_interactive_terminal(input_stream: TextIO, output_stream: TextIO) -> None:
    if not input_stream.isatty() or not output_stream.isatty():
        raise ClickException("shell requires an interactive terminal")

"""Hidden parser command for Toolang source files."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Annotated

import click
import typer


def register_parse_command(app: typer.Typer) -> None:
    app.command("parse", help="Parse a .too file and print its AST.", hidden=True, no_args_is_help=True)(
        parse_program
    )


def parse_program(
    source: Annotated[
        Path,
        typer.Argument(help="Toolang source file to parse, or '-' for stdin."),
    ],
    compact: Annotated[
        bool,
        typer.Option("--compact", help="Emit compact JSON."),
    ] = False,
    include_source_lines: Annotated[
        bool,
        typer.Option("--source-lines", help="Include parser source lines."),
    ] = False,
    stdin_filepath: Annotated[
        Path | None,
        typer.Option("--stdin-filepath", help="Path label for stdin."),
    ] = None,
) -> None:
    from ...base.error import ToolangError
    from ...lang.lower import parse, program_to_ast_data

    label, text = _read_source(source, stdin_filepath=stdin_filepath)
    try:
        program = parse(text)
    except ToolangError as exc:
        raise click.ClickException(f"{label}: {exc}") from exc
    payload = json.dumps(
        program_to_ast_data(program, include_source_lines=include_source_lines),
        ensure_ascii=False,
        indent=None if compact else 2,
        separators=(",", ":") if compact else None,
    )
    sys.stdout.write(f"{payload}\n")


def _read_source(source: Path, *, stdin_filepath: Path | None) -> tuple[Path, str]:
    if str(source) == "-":
        return stdin_filepath or Path("<stdin>"), sys.stdin.read()
    if stdin_filepath is not None:
        raise click.ClickException("--stdin-filepath can only be combined with '-'")
    candidate = source.expanduser()
    if candidate.suffix != ".too":
        raise click.ClickException(f"not a .too file: {candidate}")
    try:
        return candidate, candidate.read_text(encoding="utf-8")
    except OSError as exc:
        raise click.ClickException(f"{candidate}: {exc}") from exc

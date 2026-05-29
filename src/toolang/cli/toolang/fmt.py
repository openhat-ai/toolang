"""Hidden formatter command for Toolang source files."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sys
from typing import Annotated

import click
import typer


def register_fmt_command(app: typer.Typer) -> None:
    app.command("fmt", help="Format .too files.", hidden=True, no_args_is_help=True)(fmt)


def fmt(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="File or directory paths to format."),
    ] = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Exit non-zero if any file is not formatted."),
    ] = False,
    tab_size: Annotated[
        int,
        typer.Option("--tab-size", help="Number of spaces per indentation level."),
    ] = 2,
    stdin_filepath: Annotated[
        Path | None,
        typer.Option(
            "--stdin-filepath",
            help="Path label for stdin.",
        ),
    ] = None,
) -> None:
    from ...program_format import ToolangFormatError, format_source

    if tab_size < 1:
        raise click.ClickException("--tab-size must be at least 1")

    def format_too_source(source: str) -> str:
        return format_source(source, tab_size=tab_size)

    path_args = paths or []
    if any(str(path) == "-" for path in path_args) and len(path_args) > 1:
        raise click.ClickException("'-' cannot be combined with other path arguments")

    stdin_path_arg = _stdin_path_arg(path_args)
    if stdin_filepath is not None or stdin_path_arg is not None:
        if path_args and stdin_path_arg is None:
            raise click.ClickException("--stdin-filepath can only be combined with '-'")
        if check:
            raise click.ClickException("--check cannot be combined with stdin formatting")
        label = stdin_filepath or stdin_path_arg or Path("<stdin>")
        _format_stdin(label, format_source=format_too_source, error_type=ToolangFormatError)
        return

    source_paths = _collect_format_paths(path_args)
    changed: list[Path] = []
    for source_path in source_paths:
        try:
            source = source_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise click.ClickException(f"{source_path}: {exc}") from exc
        try:
            formatted = format_too_source(source)
        except ToolangFormatError as exc:
            raise click.ClickException(f"{source_path}: {exc}") from exc
        if formatted == source:
            continue
        changed.append(source_path)
        if check:
            continue
        try:
            source_path.write_text(formatted, encoding="utf-8")
        except OSError as exc:
            raise click.ClickException(f"{source_path}: {exc}") from exc

    if check and changed:
        for source_path in changed:
            typer.echo(f"would reformat {source_path}")
        raise typer.Exit(1)
    if changed:
        for source_path in changed:
            typer.echo(f"formatted {source_path}")


def _format_stdin(
    stdin_filepath: Path,
    *,
    format_source: Callable[[str], str],
    error_type: type[Exception],
) -> None:
    try:
        formatted = format_source(sys.stdin.read())
    except error_type as exc:
        raise click.ClickException(f"{stdin_filepath}: {exc}") from exc
    sys.stdout.write(formatted)


def _stdin_path_arg(paths: list[Path]) -> Path | None:
    if len(paths) == 1 and str(paths[0]) == "-":
        return Path("<stdin>")
    return None


def _collect_format_paths(paths: list[Path]) -> list[Path]:
    collected: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        candidate = path.expanduser()
        if candidate.is_dir():
            candidates = sorted(item for item in candidate.rglob("*.too") if item.is_file())
        elif candidate.is_file():
            if candidate.suffix != ".too":
                raise click.ClickException(f"not a .too file: {candidate}")
            candidates = [candidate]
        else:
            raise click.ClickException(f"path not found: {candidate}")
        for source_path in candidates:
            resolved = source_path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            collected.append(source_path)
    return collected

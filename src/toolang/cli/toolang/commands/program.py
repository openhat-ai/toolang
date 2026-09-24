"""Offline Toolang source developer commands."""

from __future__ import annotations

from collections.abc import Callable
import json
import os
from pathlib import Path
import sys
from typing import Annotated

import typer
from typer._click.exceptions import ClickException, UsageError

from toolang.cli.common.parameters import PathType
from ..source_output import (
    ColorOption,
    HtmlOption,
    color_enabled,
    render_source,
    write_source,
)


def fmt(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(
            metavar="PATH",
            click_type=PathType(),
            help="File or directory paths to format",
        ),
    ] = None,
    check: Annotated[
        bool,
        typer.Option("--check", help="Exit non-zero if any file is not formatted"),
    ] = False,
    tab_size: Annotated[
        int,
        typer.Option(
            "--tab-size",
            metavar="INTEGER",
            help="Number of spaces per indentation level",
        ),
    ] = 2,
    stdin_filepath: Annotated[
        Path | None,
        typer.Option("--stdin-filepath", metavar="PATH", help="Path label for stdin"),
    ] = None,
    stdout: Annotated[
        bool,
        typer.Option("--stdout", help="Print formatted source without writing files"),
    ] = False,
    highlight: Annotated[
        bool,
        typer.Option(
            "--highlight",
            help="Print highlighted formatted source without writing files",
        ),
    ] = False,
    color: ColorOption = None,
    html: HtmlOption = False,
) -> None:
    from ....lang.format import ToolangFormatError, format_source

    if tab_size < 1:
        raise ClickException("--tab-size must be at least 1")

    def format_too_source(source: str) -> str:
        return format_source(source, tab_size=tab_size)

    path_args = paths or []
    if (color is not None or html) and not highlight:
        raise UsageError("--color and --html require --highlight")
    if stdout or highlight:
        if check:
            raise UsageError("--check cannot be combined with --stdout or --highlight")
        if not path_args and stdin_filepath is not None:
            path_args = [Path("-")]
        if len(path_args) != 1:
            raise UsageError("stdout formatting requires exactly one file or '-'")
        label, source = _read_source(
            path_args[0], stdin_filepath=stdin_filepath, preserve_newlines=False
        )
        try:
            formatted = format_too_source(source)
        except ToolangFormatError as exc:
            raise ClickException(f"{label}: {exc}") from exc
        _emit_source(
            formatted,
            color=color if highlight else None,
            html=html,
            highlight=highlight,
        )
        return
    if any(str(path) == "-" for path in path_args) and len(path_args) > 1:
        raise ClickException("'-' cannot be combined with other path arguments")

    stdin_path_arg = _stdin_path_arg(path_args)
    if stdin_filepath is not None or stdin_path_arg is not None:
        if path_args and stdin_path_arg is None:
            raise ClickException("--stdin-filepath can only be combined with '-'")
        if check:
            raise ClickException("--check cannot be combined with stdin formatting")
        label = stdin_filepath or stdin_path_arg or Path("<stdin>")
        _format_stdin(
            label, format_source=format_too_source, error_type=ToolangFormatError
        )
        return

    source_paths = _collect_source_paths(path_args)
    changed: list[Path] = []
    for source_path in source_paths:
        try:
            source = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ClickException(f"{source_path}: {exc}") from exc
        try:
            formatted = format_too_source(source)
        except ToolangFormatError as exc:
            raise ClickException(f"{source_path}: {exc}") from exc
        if formatted == source:
            continue
        changed.append(source_path)
        if check:
            continue
        try:
            source_path.write_text(formatted, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ClickException(f"{source_path}: {exc}") from exc

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
        _, source = _read_source(
            Path("-"), stdin_filepath=stdin_filepath, preserve_newlines=False
        )
        formatted = format_source(source)
    except (error_type, UnicodeError) as exc:
        raise ClickException(f"{stdin_filepath}: {exc}") from exc
    write_source(formatted, sys.stdout)


def _stdin_path_arg(paths: list[Path]) -> Path | None:
    if len(paths) == 1 and str(paths[0]) == "-":
        return Path("<stdin>")
    return None


def _collect_source_paths(
    paths: list[Path],
    *,
    on_error: Callable[[Path, Exception], None] | None = None,
) -> list[Path]:
    collected: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        candidate = path.expanduser()
        try:
            if candidate.is_dir():
                candidates = sorted(
                    item for item in candidate.rglob("*.too") if item.is_file()
                )
            elif candidate.is_file():
                if candidate.suffix != ".too":
                    raise ClickException(f"not a .too file: {candidate}")
                candidates = [candidate]
            else:
                raise ClickException(f"path not found: {candidate}")
            for source_path in candidates:
                resolved = source_path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    collected.append(source_path)
        except (OSError, ClickException) as exc:
            if on_error is not None:
                on_error(candidate, exc)
            elif isinstance(exc, ClickException):
                raise
            else:
                raise ClickException(f"{candidate}: {exc}") from exc
    return collected


def _source_diagnostic(label: Path, error: Exception) -> None:
    from toolang.lang.errors import ToolangSourceError

    line = (error.line or 1) if isinstance(error, ToolangSourceError) else 1
    column = (error.column or 1) if isinstance(error, ToolangSourceError) else 1
    message = (
        str(error.__cause__)
        if isinstance(error, ClickException) and error.__cause__
        else str(error)
    )
    write_source(f"{label}:{line}:{column}: {message}\n", sys.stderr)


def _check_sources(paths: list[Path], *, stdin_filepath: Path | None) -> None:
    from toolang.lang import Program
    from toolang.common.errors import ToolangError

    failed = False

    def report(label: Path, error: Exception) -> None:
        nonlocal failed
        failed = True
        _source_diagnostic(label, error)

    sources = (
        paths if paths == [Path("-")] else _collect_source_paths(paths, on_error=report)
    )
    for source in sources:
        label = (
            stdin_filepath or Path("<stdin>")
            if str(source) == "-"
            else source.expanduser()
        )
        try:
            _, text = _read_source(
                source, stdin_filepath=stdin_filepath, preserve_newlines=False
            )
            Program.from_source(text)
        except (ToolangError, ClickException) as exc:
            report(label, exc)
    if failed:
        raise typer.Exit(1)


def parse_program(
    source: Annotated[
        list[Path],
        typer.Argument(
            metavar="SOURCE",
            click_type=PathType(),
            help="Toolang file or stdin; --check also accepts files and directories",
        ),
    ],
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Validate files or directories without printing an AST"
        ),
    ] = False,
    ast: Annotated[
        bool, typer.Option("--ast", help="Show the semantic AST (default)")
    ] = False,
    cst: Annotated[
        bool, typer.Option("--cst", help="Show the raw concrete syntax tree")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of S-expression")
    ] = False,
    compact: Annotated[
        bool, typer.Option("--compact", help="Emit compact JSON (implies --json)")
    ] = False,
    stdin_filepath: Annotated[
        Path | None,
        typer.Option("--stdin-filepath", metavar="PATH", help="Path label for stdin"),
    ] = None,
) -> None:
    from ....common.errors import ToolangError
    from ....lang import cst as concrete
    from ....lang.ast import Program, to_data
    from ..source_output import ast_sexp, cst_sexp

    if ast and cst:
        raise UsageError("--ast and --cst are mutually exclusive")
    if Path("-") in source and len(source) != 1:
        raise UsageError("'-' cannot be combined with other sources")
    if stdin_filepath is not None and source != [Path("-")]:
        raise UsageError("--stdin-filepath can only be combined with '-'")
    if check:
        if cst or json_output or compact:
            raise UsageError(
                "--check cannot be combined with --cst, --json, or --compact"
            )
        _check_sources(source, stdin_filepath=stdin_filepath)
        return
    if len(source) != 1:
        raise UsageError("tree output requires exactly one file or '-'")
    label, source_text = _read_source(
        source[0], stdin_filepath=stdin_filepath, preserve_newlines=cst
    )
    errors = []
    if cst:
        tree = concrete.parse(source_text.encode("utf-8"))
        errors = concrete.diagnostics(tree.root_node)
        output = (
            _json(concrete.to_data(tree, source_text), compact=compact)
            if json_output or compact
            else cst_sexp(tree.root_node)
        )
    else:
        try:
            program = Program.from_source(source_text)
        except ToolangError as exc:
            _source_diagnostic(label, exc)
            raise typer.Exit(1) from exc
        output = (
            _json(to_data(program), compact=compact)
            if json_output or compact
            else ast_sexp(program)
        )
    write_source(output, sys.stdout)
    for error in errors:
        point = error["start_point"]
        typer.echo(
            f"{label}:{point['row'] + 1}:{point['column'] + 1}: {error['message']}",
            err=True,
        )
    if errors:
        raise typer.Exit(1)


def highlight_source(
    source: Annotated[
        Path,
        typer.Argument(
            metavar="SOURCE",
            click_type=PathType(),
            help="Toolang file to highlight, or '-' for stdin",
        ),
    ],
    color: ColorOption = None,
    html: HtmlOption = False,
    stdin_filepath: Annotated[
        Path | None,
        typer.Option("--stdin-filepath", metavar="PATH", help="Path label for stdin"),
    ] = None,
) -> None:
    _label, text = _read_source(
        source, stdin_filepath=stdin_filepath, preserve_newlines=True
    )
    _emit_source(text, color=color, html=html, highlight=True)


def _emit_source(
    source: str, *, color: ColorOption, html: bool, highlight: bool
) -> None:
    enabled = highlight and color_enabled(
        color, environ=os.environ, terminal=sys.stdout.isatty()
    )
    try:
        output = render_source(source, color=enabled, html=html)
    except (ValueError, RuntimeError) as exc:
        raise ClickException(f"Could not highlight source: {exc}") from exc
    write_source(output, sys.stdout)


def _json(value: object, *, compact: bool) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
        )
        + "\n"
    )


def _read_source(
    source: Path, *, stdin_filepath: Path | None, preserve_newlines: bool
) -> tuple[Path, str]:
    if str(source) == "-":
        label = stdin_filepath or Path("<stdin>")
    else:
        if stdin_filepath is not None:
            raise UsageError("--stdin-filepath can only be combined with '-'")
        label = source.expanduser()
        if label.is_dir():
            raise UsageError(f"expected one .too file, not a directory: {label}")
        if label.suffix != ".too":
            raise ClickException(f"not a .too file: {label}")
    try:
        if str(source) == "-":
            buffer = getattr(sys.stdin, "buffer", None)
            text = (
                buffer.read().decode("utf-8")
                if buffer is not None
                else sys.stdin.read()
            )
        else:
            text = label.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ClickException(f"{label}: {exc}") from exc
    # AST and formatting retain the universal-newline behavior of read_text().
    # CST and original-source highlighting must retain every input byte instead.
    if not preserve_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return label, text

"""Standalone caps CLI."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import os
import sys
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from typing import Annotated

import click
import typer

from ...config.log import configure_logging
from .commands import CAP_KINDS, register_standalone_caps_commands
from ..utils import _OptionalPrefixAgentGroup, _toolang_root

_CLI_PREFIX_AGENT: str | None = None
CAP_TOP_LEVEL_COMMANDS = frozenset({"list", *CAP_KINDS})

app = typer.Typer(
    name="caps",
    help="Manage composable agent primitives.",
    cls=_OptionalPrefixAgentGroup,
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
    ctx.obj = {
        "toolang_root": _toolang_root(toolang_root),
        "agent": _CLI_PREFIX_AGENT,
    }


register_standalone_caps_commands(app)


def main(argv: Sequence[str] | None = None) -> int:
    global _CLI_PREFIX_AGENT
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    global_args, body = _extract_global_args(raw_args)
    rewritten_body, prefix_agent = _rewrite_agent_shortcuts(body)
    previous_prefix_agent = _CLI_PREFIX_AGENT
    _CLI_PREFIX_AGENT = prefix_agent
    try:
        app(
            args=[*global_args, *rewritten_body],
            prog_name=_prog_name(sys.argv[0] if sys.argv else ""),
            standalone_mode=True,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        typer.echo(f"caps error: {exc}", err=True)
        return 1
    finally:
        _CLI_PREFIX_AGENT = previous_prefix_agent
    return 0


def _extract_global_args(argv: list[str]) -> tuple[list[str], list[str]]:
    global_args: list[str] = []
    body: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        consumed = _consume_global_arg(token, argv, index)
        if consumed is None:
            body.append(token)
            index += 1
            continue
        extracted, step = consumed
        global_args.extend(extracted)
        index += step
    return global_args, body


def _consume_global_arg(token: str, argv: list[str], index: int) -> tuple[list[str], int] | None:
    if token in {"--root", "-r"}:
        if index + 1 >= len(argv):
            return ([token], 1)
        return ([token, argv[index + 1]], 2)
    if token.startswith("--root="):
        return (["--root", token.removeprefix("--root=")], 1)
    return None


def _rewrite_agent_shortcuts(body: list[str]) -> tuple[list[str], str | None]:
    if (
        len(body) >= 2
        and _looks_like_agent_name(body[0])
        and body[1] in CAP_TOP_LEVEL_COMMANDS
    ):
        return [body[1], *body[2:]], body[0]
    return body, None


def _looks_like_agent_name(token: str) -> bool:
    return bool(token) and not token.startswith("-") and token not in CAP_TOP_LEVEL_COMMANDS


def _version_callback(value: bool) -> None:
    if not value:
        return
    typer.echo(f"caps {_caps_version()}")
    raise typer.Exit()


def _caps_version() -> str:
    try:
        return package_version("toolang")
    except PackageNotFoundError:
        pyproject_path = Path(__file__).resolve().parents[3] / "pyproject.toml"
        try:
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return "unknown"
        project = data.get("project")
        if not isinstance(project, dict):
            return "unknown"
        version = project.get("version")
        return version if isinstance(version, str) else "unknown"


def _prog_name(argv0: str) -> str:
    text = Path(argv0).name.strip()
    return text or "caps"


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated

import click
import typer
from dotenv import load_dotenv

from toolang import __version__
from toolang.agent_refs import ResolvedAgentRef, resolve_agent_ref
from toolang.errors import ToolangError
from toolang.layout import resolve_toolang_root
from toolang.runtime import execute_thunk
from toolang.sync import ensure_agent_synced, sync_agent


def _version_callback(value: bool | None) -> None:
    if value:
        typer.echo(f"toolang {__version__}")
        raise typer.Exit()


app = typer.Typer(
    help="Toolang CLI",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
)


@app.callback()
def callback(
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = None,
) -> None:
    """Toolang CLI."""


@app.command()
def run(
    agent: Annotated[str, typer.Argument(help="Agent reference, path, or URI")],
    thunk: Annotated[str | None, typer.Option(help="Thunk name to run")] = None,
    user_input: Annotated[
        str | None,
        typer.Option("--input", help="User input for a thunk(user) entrypoint"),
    ] = None,
    model: Annotated[str | None, typer.Option(help="Override model selection")] = None,
) -> None:
    agent_ref = _resolve_cli_agent(agent)
    program_path = _resolve_program_path(agent_ref)
    program = ensure_agent_synced(agent_ref).to_program()
    selected_thunk = program.get_thunk(thunk)

    if selected_thunk.input_name and user_input is None and not sys.stdin.isatty():
        user_input = sys.stdin.read()

    result = execute_thunk(
        program,
        selected_thunk,
        program_path,
        user_input=user_input,
        model=model,
    )
    typer.echo(result)


@app.command()
def sync(
    agent: Annotated[str, typer.Argument(help="Agent reference, path, or URI")],
) -> None:
    sync_agent(_resolve_cli_agent(agent))
    typer.echo("synced")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    try:
        app(
            args=list(argv) if argv is not None else None,
            prog_name="toolang",
            standalone_mode=False,
        )
    except click.exceptions.Exit as exc:
        return exc.exit_code
    except click.ClickException as exc:
        exc.show(file=sys.stderr)
        return exc.exit_code
    except (FileNotFoundError, ToolangError) as exc:
        typer.echo(f"toolang error: {exc}", err=True)
        return 1
    return 0


def _resolve_program_path(agent: ResolvedAgentRef) -> Path:
    program_path = agent.source_path
    if not program_path.exists():
        if agent.agent_kind == "visiting":
            raise FileNotFoundError(
                f"Visiting agent is not materialized locally: {agent.agent_uri} -> {program_path}"
            )
        raise FileNotFoundError(f"Agent source not found: {program_path}")
    return program_path


def _resolve_cli_agent(raw: str) -> ResolvedAgentRef:
    toolang_root = resolve_toolang_root(os.environ.get("TOOLANG_ROOT", "~/.toolang"))
    guest_base_url = os.environ.get("TOOLANG_GUEST_BASE_URL", "").strip()
    guest_resolver = None
    if guest_base_url:
        base = guest_base_url.rstrip("/")
        guest_resolver = lambda name: f"{base}/{name.lstrip('/')}"
    return resolve_agent_ref(
        raw,
        cwd=Path.cwd(),
        toolang_root=toolang_root,
        guest_resolver=guest_resolver,
    )

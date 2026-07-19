"""Invocation context shared by Toolang CLI entry points."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import click
import typer

from ...common.error import ToolangError
from ...catalog.error import CatalogError


@dataclass(slots=True)
class CliContext:
    root: Path
    agent: str | None = None


def cli_context(ctx: typer.Context) -> CliContext:
    if not isinstance(ctx.obj, CliContext):
        raise TypeError("missing CLI context")
    return ctx.obj


def context_root(ctx: typer.Context) -> Path:
    return cli_context(ctx).root


def context_agent(ctx: typer.Context) -> str | None:
    return cli_context(ctx).agent


def require_prefix_agent(ctx: typer.Context) -> str:
    if agent := context_agent(ctx):
        return agent
    typer.echo(ctx.get_help())
    raise typer.Exit()


def require_runtime_agent(ctx: typer.Context, agent: str | None) -> str:
    if agent:
        return agent
    typer.echo(ctx.get_help())
    raise typer.Exit()


def resolve_root(
    explicit: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if explicit is not None:
        return explicit
    values = os.environ if environ is None else environ
    return Path(values.get("TOOLANG_ROOT", str(Path.home() / ".toolang")))


def ui_base_url(*, environ: Mapping[str, str] | None = None) -> str:
    from ...config.web import resolve_ui_base_url

    values = os.environ if environ is None else environ
    return resolve_ui_base_url(resolve_root(None, environ=values), environ=values)


def runtime_environ(
    ctx: typer.Context,
    agent_name: str,
    *,
    root: Path | None = None,
) -> dict[str, str]:
    from ...config.env import load_runtime_environ

    return load_runtime_environ(
        root or context_root(ctx),
        agent_name,
        base_environ=os.environ,
    )


def user_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except (
        CatalogError,
        FileExistsError,
        FileNotFoundError,
        ToolangError,
        ValueError,
    ) as exc:
        raise click.ClickException(str(exc)) from exc

"""Invocation context shared by Toolang CLI entry points."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import click
import typer
from dotenv import dotenv_values

from ...common.errors import ToolangError
from ...common.config import resolve_ui_base_url
from ...common.layout import AgentLayout
from ..config import load_config
from ...catalog.errors import CatalogError


@dataclass(slots=True)
class CliContext:
    root: Path
    agent: str | None = None
    layout: AgentLayout | None = None


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


def context_layout(ctx: typer.Context) -> AgentLayout:
    """Return the explicitly selected layout or one resident layout."""

    state = cli_context(ctx)
    if state.layout is not None:
        return state.layout
    return AgentLayout.resident(state.root, require_prefix_agent(ctx))


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
    values = os.environ if environ is None else environ
    root = resolve_root(None, environ=values)
    config = load_config(root / "config.toml")
    return resolve_ui_base_url(config, environ=values)


def load_runtime_environ(
    layout: AgentLayout,
    *,
    base_environ: Mapping[str, str],
) -> dict[str, str]:
    """Load root and agent dotenv defaults below explicit process values."""

    merged = _load_dotenv(layout.root_env)
    merged.update(_load_dotenv(layout.env))
    merged.update(base_environ)
    return merged


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {
        key: value
        for key, value in dotenv_values(path).items()
        if isinstance(key, str) and isinstance(value, str)
    }


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

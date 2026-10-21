"""Invocation context shared by Toolang CLI entry points."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from dotenv import dotenv_values
import typer
from typer._click import Context
from typer._click.exceptions import ClickException

from ...catalog.agent import LocalAgents
from ...common.errors import ToolangError
from ...common.config import resolve_ui_base_url
from ...common.layout import AgentLayout
from ..config import load_config
from ...catalog.errors import CatalogError
from .help import show_help

if TYPE_CHECKING:
    from toolang.common.progress import ProgressSink
    from toolang.up.source import AgentTarget


@dataclass(slots=True)
class CliContext:
    root: Path
    agent: str | None = None
    layout: AgentLayout | None = None
    source: Path | None = None
    launch_id: str | None = None
    selector: str | None = None
    prepared: bool = False
    instance_selected: bool = False


def parse_target(text: str) -> AgentTarget | Path:
    """Delegate selector syntax to its owner without reading or fetching a source."""
    from toolang.up.source import parse_agent_target

    if ":" not in text.split("/", 1)[0] and (
        text.endswith(".too") or text.startswith(("./", "../", "/", "~/"))
    ):
        path = Path(text).expanduser().resolve()
        if path.suffix != ".too":
            raise ValueError(f"agent program must point to a .too file: {text}")
        return path
    return parse_agent_target(text)


def target_context(root: Path, target: AgentTarget | Path) -> CliContext:
    """Create invocation-local binding data; source preparation belongs to execution."""
    from toolang.up.source import visiting_layout

    if isinstance(target, Path):
        layout = replace(AgentLayout.roaming(target), toolang_root=root)
        return CliContext(
            root, agent=layout.name, layout=layout, source=target, selector=str(target)
        )
    if target.form == "name":
        return CliContext(root, agent=target.name, selector=target.text)
    layout = visiting_layout(target.text, toolang_root=root)
    return CliContext(root, agent=layout.name, layout=layout, selector=target.text)


ModelCatalogOption = Annotated[
    Path | None,
    typer.Option("--catalog", metavar="PATH", help="Use this model catalog"),
]


def cli_context(ctx: Context) -> CliContext:
    if not isinstance(ctx.obj, CliContext):
        raise TypeError("missing CLI context")
    return ctx.obj


def context_root(ctx: Context) -> Path:
    return cli_context(ctx).root


def resolve_model_catalog_option(value: Path | None) -> Path | None:
    """Resolve one command-level model catalog override."""

    return value.expanduser().resolve(strict=False) if value is not None else None


def context_agent(ctx: Context) -> str | None:
    state = cli_context(ctx)
    agent = state.agent
    if (
        agent is not None
        and state.layout is None
        and LocalAgents(state.root / "agents").get(agent) is None
    ):
        raise ClickException(f"Agent {agent} not found")
    return agent


def require_prefix_agent(ctx: Context) -> str:
    if agent := context_agent(ctx):
        return agent
    show_help(ctx)


def context_layout(ctx: Context) -> AgentLayout:
    """Return the explicitly selected layout or one resident layout."""

    state = cli_context(ctx)
    if state.layout is not None:
        return state.layout
    return AgentLayout.resident(state.root, state.agent or require_prefix_agent(ctx))


def require_runtime_agent(ctx: Context, agent: str | None) -> str:
    selected = agent or context_agent(ctx)
    if selected:
        return selected
    show_help(ctx)


def select_agent_instance(ctx: Context) -> AgentLayout:
    """Resolve an existing retained instance when an executable callback needs it."""
    state = cli_context(ctx)
    layout = context_layout(ctx)
    if state.instance_selected:
        return layout
    if layout.placement == "resident" and not layout.home.is_dir():
        raise ClickException(f"Agent {layout.name} not found")
    instance = ctx.params.get("instance")
    # The native option callback has already checked canonical ID syntax.
    if instance is not None and not (
        instance.startswith("r")
        if state.source is not None
        else instance == layout.instance_id
    ):
        raise typer.BadParameter(
            "instance does not match the selected target",
            param_hint="--instance",
            ctx=ctx,
        )
    if state.source is not None:
        from toolang.up.identity import select_roaming

        layout = user_call(select_roaming, state.source, instance)
    from toolang.up.instance import read_instance

    entry = read_instance(layout.runtime_status)
    state.layout = layout
    state.launch_id = entry["launch_id"] if entry else None
    state.instance_selected = True
    return layout


def prepare_agent(ctx: Context, *, progress: ProgressSink | None = None) -> AgentLayout:
    """Materialize source inputs after native argument validation succeeds."""
    state = cli_context(ctx)
    layout = context_layout(ctx)
    if state.prepared or not (
        state.source is not None or layout.placement == "visiting" and state.selector
    ):
        return layout
    from toolang.up import source
    from .progress import make_cli_progress

    if progress is None:
        report = make_cli_progress(preparation=True)
        try:
            with report:
                layout = prepare_agent(ctx, progress=report.sink)
                report.complete_preparation()
                return layout
        except (OSError, RuntimeError, ToolangError, ValueError) as exc:
            message = report.failure_message(exc) if report.failure_stage else str(exc)
            raise ClickException(message) from exc
    state.layout = replace(
        source.materialize_roaming_program(state.source)
        if state.source is not None
        else source.resolve_visiting_layout(
            state.selector or "", toolang_root=state.root, progress=progress
        ),
        toolang_root=state.root,
    )
    state.prepared = True
    return state.layout


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

    merged = {} if layout.project_inputs else _load_dotenv(layout.root_env)
    if not layout.project_inputs:
        merged.update(_load_dotenv(layout.env))
    merged.update(base_environ)
    if merged.get("TOOLANG_MODEL_CATALOG"):
        merged["TOOLANG_MODEL_CATALOG"] = str(
            Path(merged["TOOLANG_MODEL_CATALOG"]).expanduser().resolve()
        )
    return merged


def load_root_environ(root: Path, *, base_environ: Mapping[str, str]) -> dict[str, str]:
    """Load root-only presentation defaults below the process environment."""
    return {**_load_dotenv(root / ".env"), **base_environ}


def _load_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return {
        key: value
        for key, value in dotenv_values(path, interpolate=False).items()
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
        raise ClickException(str(exc)) from exc

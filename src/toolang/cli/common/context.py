"""Invocation context shared by Toolang CLI entry points."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from contextlib import contextmanager
from contextvars import ContextVar
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from typer._click import Context
from typer._click.exceptions import ClickException

from ...catalog.agent import LocalAgents
from ...common.errors import ToolangError
from ...common.config import resolve_ui_base_url
from ...common.layout import AgentLayout
from ...common.environment import (
    ResolvedEnvironment,
    current_environment,
    environment_identity,
    environment_scope,
    resolve_agent_environment,
    resolve_root_environment,
)
from ..config import load_config
from ...catalog.errors import CatalogError
from .help import show_help

if TYPE_CHECKING:
    from toolang.up.addresses import AgentAddress
    from toolang.common.progress import ProgressSink
    from toolang.up.source import AgentTarget


# Controller settings are parsed by CLI call sites, never added to a workload.
_STARTUP_NAMES = frozenset(
    {
        "TOOLANG_MODEL_CATALOG",
        "TOOLANG_AGENT_PORT",
        "PY_LOG",
        "TOOLANG_UI_BASE_URL",
        "TOOLANG_WEBUI_BASE_URL",
        "TOOLANG_CORS_ALLOWED_ORIGINS",
        "TOOLANG_CORS_ORIGINS",
        "TOOLANG_COMPACT_MODEL",
        "TOOLANG_DEFAULT_MODEL",
        "TOOLANG_PROGRESS_MAX_WIDTH",
        "TOOLANG_INPUTBOX_MAX_WIDTH",
        "TOOLANG_COLOR_SCHEME",
        "TOOLANG_TMUX",
        "TOOLANG_TMUX_DEBUG",
        "TMUX",
        "TMUX_PANE",
        "COLUMNS",
        "LINES",
        *(
            f"TOOLANG_ALLOW_{name}"
            for name in ("MODELS", "TOOLS", "PSYCHES", "SKILLS", "SERVICES", "PROMPTS")
        ),
        *(
            f"TOOLANG_LIMIT_{name}"
            for name in (
                "AGIC_MODEL_CALLS",
                "AGIC_TOOL_CALLS",
                "TOKENS",
                "COST",
                "TIME",
            )
        ),
        *(
            f"TOOLANG_TITLE_PREFIX_{name}"
            for name in ("CHAT", "TALK", "TOP", "SERVE", "RUN", "RERUN", "RETRY")
        ),
    }
)
_STARTUP: ContextVar[Mapping[str, str]] = ContextVar("toolang_startup", default={})


def startup_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """Read captured controller inputs only while resolving concrete CLI settings."""
    return {**_STARTUP.get(), **environ}


def get_terminal_size(fallback: tuple[int, int] = (120, 24)) -> os.terminal_size:
    """Keep controller terminal geometry out of the workload environment."""
    detected = shutil.get_terminal_size(fallback=fallback)
    settings = startup_environ(os.environ)
    dimensions = []
    for name, default in zip(("COLUMNS", "LINES"), detected, strict=True):
        try:
            value = int(settings.get(name, "0"))
        except ValueError:
            value = 0
        dimensions.append(value if value > 0 else default)
    return os.terminal_size(dimensions)


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
    address: AgentAddress | None = None
    root_environment: ResolvedEnvironment | None = field(default=None, repr=False)
    shell_environment: Mapping[str, str] | None = field(default=None, repr=False)


def target_context(root: Path, target: AgentTarget | Path | AgentAddress) -> CliContext:
    """Create invocation-local binding data; source preparation belongs to execution."""
    from toolang.up.source import visiting_layout
    from toolang.up.addresses import AgentAddress

    if isinstance(target, AgentAddress):
        return CliContext(root, agent=target.name, source=target.source, address=target)

    if isinstance(target, Path):
        layout = AgentLayout.roaming(target, root=root)
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

    if value is None and (configured := _STARTUP.get().get("TOOLANG_MODEL_CATALOG")):
        value = Path(configured)
    return value.expanduser().resolve(strict=False) if value is not None else None


def context_agent(ctx: Context) -> str | None:
    state = cli_context(ctx)
    agent = state.agent
    if (
        agent is not None
        and state.layout is None
        and state.address is None
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
    if state.address is not None and not state.instance_selected:
        from toolang.up.addresses import resolve_address

        layout, launch_id = user_call(resolve_address, state.root, state.address)
        state.layout = layout
        state.source = layout.source_path
        state.launch_id = launch_id
        state.prepared = state.instance_selected = True
    if state.layout is not None:
        return state.layout
    return AgentLayout.resident(state.root, state.agent or require_prefix_agent(ctx))


def require_runtime_agent(ctx: Context, agent: str | None) -> str:
    selected = agent or context_agent(ctx)
    if selected:
        return selected
    show_help(ctx)


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
        source.materialize_roaming_program(state.source, toolang_root=state.root)
        if state.source is not None
        else source.resolve_visiting_layout(
            state.selector or "", toolang_root=state.root, progress=progress
        ),
        root=state.root,
    )
    state.prepared = True
    return state.layout


def resolve_root(
    explicit: Path | None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Path:
    if explicit is not None:
        return explicit.expanduser().resolve()
    active = current_environment()
    if environ is None and active is not None:
        return active.root
    values = os.environ if environ is None else environ
    return (
        Path(values.get("TOOLANG_ROOT", str(Path.home() / ".toolang")))
        .expanduser()
        .resolve()
    )


def ui_base_url(*, environ: Mapping[str, str] | None = None) -> str:
    values = startup_environ(os.environ if environ is None else environ)
    root = resolve_root(None) if environ is None else resolve_root(None, environ=values)
    config = load_config(root / "config.toml")
    return resolve_ui_base_url(config, environ=values)


def load_runtime_environment(
    layout: AgentLayout,
    *,
    base_environ: Mapping[str, str],
) -> ResolvedEnvironment:
    """Resolve once at the command boundary; reuse an active startup snapshot."""
    active = current_environment()
    root = layout.root or (
        layout.place
        if layout.placement == "resident"
        else resolve_root(None, environ=base_environ)
    )
    if (
        active is not None
        and active.root == root
        and active.identity == environment_identity(layout)
    ):
        return active
    parent = (
        active
        if active is not None and active.root == root and active.identity is None
        else resolve_root_environment(root, base_environ)
    )
    return resolve_agent_environment(parent, layout)


def load_runtime_environ(
    layout: AgentLayout,
    *,
    base_environ: Mapping[str, str],
) -> dict[str, str]:
    """Read the host mapping from the invocation's resolved snapshot."""
    merged = dict(load_runtime_environment(layout, base_environ=base_environ).values)
    if merged.get("TOOLANG_MODEL_CATALOG"):
        merged["TOOLANG_MODEL_CATALOG"] = str(
            Path(merged["TOOLANG_MODEL_CATALOG"]).expanduser().resolve()
        )
    return merged


def load_root_environ(root: Path, *, base_environ: Mapping[str, str]) -> dict[str, str]:
    """Resolve root defaults without selecting an agent or provider."""
    active = current_environment()
    if active is not None and active.root == root:
        return dict(active.values)
    return dict(resolve_root_environment(root, base_environ).values)


@contextmanager
def command_environment(ctx: Context):
    """Install the selected command's environment before invoking user plugins."""
    state = ctx.obj
    if not isinstance(state, CliContext) or ctx.info_name == "_serve":
        yield
        return
    try:
        root = state.root_environment or resolve_root_environment(
            state.root,
            state.shell_environment
            if state.shell_environment is not None
            else os.environ,
        )
        state.root_environment = root
        selected = root
        if (
            state.agent is not None
            or state.layout is not None
            or state.address is not None
        ):
            selected = resolve_agent_environment(root, context_layout(ctx))
    except (OSError, ValueError) as exc:
        raise ClickException(str(exc)) from exc
    settings = {
        name: root.values[name] for name in _STARTUP_NAMES if name in root.values
    }
    settings.update(
        {
            name: selected.values[name]
            for name in _STARTUP_NAMES
            if name in selected.values
        }
    )
    token = _STARTUP.set(settings)
    try:
        with environment_scope(selected):
            yield
    finally:
        _STARTUP.reset(token)


def user_call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except (
        CatalogError,
        OSError,
        ToolangError,
        ValueError,
    ) as exc:
        raise ClickException(str(exc)) from exc

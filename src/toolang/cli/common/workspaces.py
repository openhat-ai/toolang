"""Resolve invocation-only workspace grants at CLI boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from collections.abc import Mapping, Sequence
from typing import Annotated

import typer

from toolang.base.utils.workspace_paths import (
    parse_cwd,
    workspace_uri,
    authorize_workspace_path,
)
from toolang.common.layout import AgentLayout
from toolang.state.config import (
    ConfiguredWorkspaces,
    normalize_workspace_name,
    validate_workspace_name,
)

WorkspaceOptions = Annotated[
    list[str] | None,
    typer.Option(
        "--workspace",
        "-w",
        metavar="[NAME=]PATH",
        help="Add a temporary workspace (repeatable)",
    ),
]
WorkdirOption = Annotated[
    list[str] | None,
    typer.Option(
        "--workdir",
        "-d",
        metavar="PATH|URI",
        help="Set workdir; a path also adds a workspace",
    ),
]
NoAutoWorkspaceOption = Annotated[
    bool,
    typer.Option(
        "--no-auto-workspace",
        help="Skip the automatic source workspace",
    ),
]
_URI = re.compile(r"^[^/=]+://")


@dataclass(frozen=True, slots=True)
class InvocationWorkspaces:
    additions: Mapping[str, str]
    workdir: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "additions", MappingProxyType(dict(self.additions)))


def resolve_workspaces(
    layout: AgentLayout,
    *,
    procdir: Path,
    paths: Sequence[str] = (),
    workdir: str | Sequence[str] | None = None,
    srcdir: Path | None = None,
    no_auto: bool = False,
    existing: Mapping[str, str] | None = None,
) -> InvocationWorkspaces:
    """Validate the whole invocation before preparing state or starting a runtime."""
    workdir = single_workdir(workdir)
    configured = ConfiguredWorkspaces(layout.config).list()
    configured.pop("lab", None)  # The implicit grant owns this reserved name.
    grants = {"lab": str(layout.home / "lab"), **configured}
    origins = {name: f"configuration ({path})" for name, path in grants.items()}
    # Existing bindings are a selection context, not new declarations. The server
    # acquisition boundary checks compatibility with explicit invocation grants.
    grants.update(existing or {})
    additions: dict[str, str] = {}

    def add(value: str, origin: str) -> str:
        name, separator, path = value.partition("=")
        if not separator:
            name, path = "", value
        if not path:
            raise ValueError(f"workspace path must not be empty: {origin}")
        candidate = Path(path).expanduser()
        resolved = (
            candidate if candidate.is_absolute() else procdir / candidate
        ).resolve(strict=True)
        if not resolved.is_dir():
            raise ValueError(f"workspace path is not a directory: {resolved}")
        if name:
            validate_workspace_name(name)
        else:
            name = normalize_workspace_name(
                resolved.name if candidate.name in {"", ".", ".."} else candidate.name
            )
        if name in origins:
            raise ValueError(
                f"workspace name {name!r} conflicts: {origins[name]} and {origin}; use NAME=PATH"
            )
        additions[name] = str(resolved)
        grants[name] = str(resolved)
        origins[name] = origin
        return workspace_uri(name)

    selected = None
    for value in paths:
        selected = add(value, f"-w {value}")
    if workdir is not None:
        if _URI.match(workdir):
            name, relative = parse_cwd(workdir)
            if name not in grants:
                raise ValueError(f"workspace is not available: {name}")
            root = Path(grants[name]).resolve()
            target = authorize_workspace_path(root / relative, root)
            # lab is created by state preparation, after this preflight.
            if not target.is_dir() and not (name == "lab" and not relative):
                raise ValueError(f"workdir is not a directory: {target}")
            selected = workdir
        else:
            selected = add(workdir, f"--workdir {workdir}")
    elif not paths and srcdir is not None and not no_auto:
        selected = add(f"={srcdir}", "automatic source workspace")
    return InvocationWorkspaces(additions, selected)


def inspect_workspaces(
    ctx: typer.Context,
    paths: Sequence[str] | None,
    workdir: str | Sequence[str] | None,
    *,
    no_auto: bool = False,
) -> InvocationWorkspaces | None:
    """Validate optional inspection grants without changing global inspection."""
    from .context import context_agent, context_layout, user_call

    if context_agent(ctx) is None and not paths and workdir is None:
        return None
    if context_agent(ctx) is None:
        raise typer.BadParameter("workspace options require an agent target")
    layout = context_layout(ctx)
    return user_call(
        resolve_workspaces,
        layout,
        procdir=Path.cwd(),
        paths=paths or (),
        workdir=workdir,
        srcdir=layout.program.resolve().parent
        if layout.placement == "roaming"
        else None,
        no_auto=no_auto,
        existing=running_workspaces(layout) if workdir else None,
    )


def single_workdir(values: str | Sequence[str] | None) -> str | None:
    if values is None or isinstance(values, str):
        return values
    if len(values) > 1:
        raise typer.BadParameter(
            "--workdir may only be specified once", param_hint="--workdir"
        )
    return values[0] if values else None


def running_workspaces(layout: AgentLayout) -> Mapping[str, str]:
    """Expose captured temporary grants only while their server is running."""
    from toolang.up.process import AgentProcess

    process = AgentProcess(layout)
    status = process.status(ui_base_url="")
    if status is None or status.status != "running":
        return {}
    captured = (process.state() or {}).get("workspace_additions", {})
    if not isinstance(captured, dict):
        return {}
    return {
        str(name): str(path)
        for name, path in captured.items()
        if isinstance(name, str) and isinstance(path, str)
    }

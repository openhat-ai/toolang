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
)
from toolang.common.layout import AgentLayout
from toolang.state.schemas import WorkspaceInspection
from toolang.state.config import (
    normalize_workspace_name,
    validate_workspace_name,
)

WorkspaceOptions = Annotated[
    list[str] | None,
    typer.Option(
        "--workspace",
        "-w",
        metavar="[NAME=]<DIR>",
        help="Add a temporary workspace (repeatable)",
    ),
]
WorkdirOption = Annotated[
    list[str] | None,
    typer.Option(
        "--workdir",
        "-d",
        metavar="[NAME=]<DIR>|<URI>",
        help="Set workdir; a path also adds a workspace",
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
) -> InvocationWorkspaces:
    """Parse local grants; defer named workspaces to the selected runtime's State."""
    workdir = single_workdir(workdir)
    origins = {"lab": "implicit workspace"}
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
            if origins[name] == "automatic source workspace" and additions[name] == str(
                resolved
            ):
                return workspace_uri(name)
            raise ValueError(
                f"workspace name {name!r} conflicts: {origins[name]} and {origin}; use NAME=PATH"
            )
        additions[name] = str(resolved)
        origins[name] = origin
        return workspace_uri(name)

    selected = None
    for value in paths:
        selected = add(value, f"-w {value}")
    if not paths and srcdir is not None:
        selected = add(f"={srcdir}", "automatic source workspace")
    if workdir is not None:
        if _URI.match(workdir):
            parse_cwd(workdir)
            # Host, embedded, and guest runtimes validate against their own
            # captured Setup/State, including mounts and last-good publications.
            selected = workdir
        else:
            selected = add(workdir, f"--workdir {workdir}")
    return InvocationWorkspaces(additions, selected)


def single_workdir(values: str | Sequence[str] | None) -> str | None:
    if values is None or isinstance(values, str):
        return values
    if len(values) > 1:
        raise typer.BadParameter(
            "--workdir may only be specified once", param_hint="--workdir"
        )
    return values[0] if values else None


def running_workspace_inspection(
    layout: AgentLayout, *, workdir: str | None = None
) -> WorkspaceInspection | None:
    """Read workspace availability in the running agent's filesystem."""
    from urllib.parse import urlencode

    from toolang.up.process import AgentProcess
    from .client import RuntimeClient

    status = AgentProcess(layout).status(ui_base_url="")
    if status is None or status.status != "running":
        return None
    if status.endpoint is None:
        raise ValueError("running agent has no endpoint")
    query = "?" + urlencode({"workdir": workdir}) if workdir else ""
    return WorkspaceInspection.model_validate(
        RuntimeClient(status.endpoint).get("/api/v1/workspaces" + query)
    )

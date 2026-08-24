"""Package-neutral external workspace configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import cast


_WORKSPACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HOSTED_WORKSPACES_ENV = "TOOLANG_ACTIVE_WORKSPACES"


@dataclass(frozen=True, slots=True)
class Workspace:
    """One named canonical directory granted to an agent."""

    name: str
    path: Path

    def __post_init__(self) -> None:
        validate_workspace_name(self.name)
        expanded = self.path.expanduser()
        if not expanded.is_absolute():
            raise ValueError(f"workspace path must be absolute: {self.path}")
        path = expanded.resolve()
        if not path.is_dir():
            raise ValueError(f"workspace path is not an existing directory: {path}")
        object.__setattr__(self, "path", path)


def parse_workspaces(config: Mapping[str, object]) -> tuple[Workspace, ...]:
    """Parse the authoritative agent-home workspace table."""

    raw = workspace_config_values(config)
    result = tuple(
        Workspace(raw_name, Path(raw_path))
        for raw_name, raw_path in sorted(raw.items())
    )
    for index, workspace in enumerate(result):
        for other in result[index + 1 :]:
            if workspace_paths_overlap(workspace.path, other.path):
                raise ValueError(
                    f"workspace paths overlap: {workspace.name}, {other.name}"
                )
    return result


def resolve_active_workspaces(
    config: Mapping[str, object],
    *,
    environ: Mapping[str, str],
) -> tuple[Workspace, ...]:
    """Resolve local grants or the hosted mapping published at launch."""

    sandbox = environ.get("TOOLANG_SANDBOX", "none").partition(":")[0]
    hosted = environ.get(_HOSTED_WORKSPACES_ENV) if sandbox == "docker" else None
    if hosted is None:
        return parse_workspaces(config)
    try:
        payload = json.loads(hosted)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid active workspace mapping") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("active workspace mapping must be an object")
    configured = workspace_config_values(config)
    result: list[Workspace] = []
    for raw_name, raw_publication in sorted(
        payload.items(), key=lambda item: str(item[0])
    ):
        if not isinstance(raw_name, str) or not isinstance(raw_publication, Mapping):
            raise ValueError("active workspace entry is invalid")
        publication = cast(Mapping[str, object], raw_publication)
        configured_path = publication.get("configured_path")
        active_path = publication.get("active_path")
        if not isinstance(configured_path, str) or not isinstance(active_path, str):
            raise ValueError(
                "active workspace entry requires configured and active paths"
            )
        if configured.get(raw_name) != configured_path:
            continue
        active = Path(active_path)
        if not active.is_absolute():
            raise ValueError("active workspace path must be absolute")
        if not active.is_dir():
            continue
        result.append(Workspace(raw_name, active))
    return tuple(result)


def hosted_workspaces_env(
    workspaces: Mapping[str, tuple[str, Path]],
) -> tuple[str, str]:
    """Encode the internal hosted mapping for one launched runtime."""

    return (
        _HOSTED_WORKSPACES_ENV,
        json.dumps(
            {
                name: {
                    "configured_path": configured_path,
                    "active_path": str(active_path),
                }
                for name, (configured_path, active_path) in sorted(workspaces.items())
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )


def workspace_config_values(config: Mapping[str, object]) -> dict[str, str]:
    """Return validated authored workspace path strings without resolving them."""

    raw = config.get("workspaces")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("workspaces config must be a table")
    result: dict[str, str] = {}
    for raw_name, raw_path in raw.items():
        if not isinstance(raw_name, str) or not isinstance(raw_path, str):
            raise ValueError("workspace entries must map names to paths")
        validate_workspace_name(raw_name)
        result[raw_name] = raw_path
    return result


def validate_workspace_name(name: str) -> None:
    if not _WORKSPACE_NAME_RE.fullmatch(name):
        raise ValueError("workspace name must be a 1-64 character ASCII identifier")


def workspace_paths_overlap(first: Path, second: Path) -> bool:
    return (
        first == second or first.is_relative_to(second) or second.is_relative_to(first)
    )

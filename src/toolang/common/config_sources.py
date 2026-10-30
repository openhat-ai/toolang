"""Origin-aware TOML inputs and bounded script companion discovery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import tomllib
from typing import TYPE_CHECKING, Literal, cast

import tomlkit

from .config_runtime import (
    ConfigOrigins,
    RuntimeConfig,
    parse_source_metadata,
    resolve_runtime_config,
)

if TYPE_CHECKING:
    from .layout import AgentLayout


@dataclass(frozen=True, slots=True)
class ConfigSource:
    path: Path
    config: dict[str, object]
    scope: Literal["root", "home"] | None = None


def read_config(
    path: Path,
    *,
    include_workspaces: bool = True,
    origin: Path | None = None,
    context: ConfigOrigins | None = None,
) -> dict[str, object]:
    """Read one optional TOML file, preserving its discovered path as origin."""
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        return rebase_config(
            tomllib.loads(path.read_text(encoding="utf-8")),
            (origin or path).parent,
            include_workspaces=include_workspaces,
            context=context,
        )
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read configuration {path}: {exc}") from exc


def rebase_config(
    config: Mapping[str, object],
    directory: Path,
    *,
    include_workspaces: bool = True,
    context: ConfigOrigins | None = None,
) -> dict[str, object]:
    """Resolve known host paths; plugin commands and guest paths remain opaque."""
    result = deepcopy(dict(config))
    if not include_workspaces:
        result.pop("workspaces", None)
    workspaces = result.get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = cast(dict[str, object], workspaces)
        for name, path in workspaces.items():
            if isinstance(path, str) and path:
                workspaces[name] = str(resolve_path(path, directory, context=context))
    catalog = catalog_config(result)
    if catalog is not None and isinstance(catalog.get("path"), str) and catalog["path"]:
        catalog["path"] = str(
            resolve_path(str(catalog["path"]), directory, context=context)
        )
    return result


def rebase_config_content(
    content: bytes,
    directory: Path,
    *,
    include_workspaces: bool = True,
    context: ConfigOrigins | None = None,
) -> bytes:
    """Bind relative paths into captured bytes without changing unrelated files."""
    parsed = tomllib.loads(content.decode("utf-8"))
    resolved = rebase_config(
        parsed, directory, include_workspaces=include_workspaces, context=context
    )
    return content if parsed == resolved else tomlkit.dumps(resolved).encode("utf-8")


def resolve_path(
    value: str, directory: Path, *, context: ConfigOrigins | None = None
) -> Path:
    if context is not None:
        return context.resolve_path(value, directory)
    path = Path(value).expanduser()
    return (path if path.is_absolute() else directory / path).resolve()


def catalog_config(config: Mapping[str, object]) -> dict[str, object] | None:
    value: object = config
    for key in ("plugin", "model_catalog", "models_dev"):
        value = value.get(key) if isinstance(value, dict) else None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def script_directories(source: Path) -> tuple[Path, ...]:
    """Return source-first directories through the nearest Git worktree boundary."""
    srcdir = source.resolve(strict=True).parent
    for directory in (srcdir, *srcdir.parents):
        marker = directory / ".git"
        if not marker.exists() and not marker.is_symlink():
            continue
        environ = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            env=environ,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode or Path(result.stdout.strip()).resolve() != directory:
            raise ValueError(f"invalid Git worktree boundary: {marker}")
        return tuple(
            parent
            for parent in (srcdir, *srcdir.parents)
            if parent == directory or parent.is_relative_to(directory)
        )
    return (srcdir,)


def script_sources(source: Path) -> tuple[ConfigSource, ...]:
    """Select one nearest TOML and retain companion catalog candidates."""
    directories = script_directories(source)
    selected = next(
        (
            directory / "toolang.toml"
            for directory in directories
            if (directory / "toolang.toml").exists()
            or (directory / "toolang.toml").is_symlink()
        ),
        None,
    )
    result = []
    for directory in reversed(directories):
        path = directory / "toolang.toml"
        if path != selected and not (directory / "toolang.catalog.json").exists():
            continue
        config = (
            read_config(path, include_workspaces=directory == directories[0])
            if path == selected
            else {}
        )
        _reject_local_environment(config, path)
        result.append(ConfigSource(path, config))
    return tuple(result)


def source_metadata(layout: AgentLayout) -> dict[str, object]:
    if not layout.root_source_receipt.is_file():
        return {}
    data = json.loads(layout.root_source_receipt.read_bytes()).get("launch", {})
    parse_source_metadata(data)
    return cast(dict[str, object], data)


def config_origins(layout: AgentLayout) -> ConfigOrigins:
    """Load placement-local provenance without opening upstream source files."""
    launch = source_metadata(layout)
    context = parse_source_metadata(launch)
    origins = dict(context.origins)
    snapshots = dict(context.snapshots)
    launch_captured = bool(launch)
    for path in (layout.source_receipt, layout.root_source_receipt):
        if not path.is_file():
            continue
        receipt = json.loads(path.read_bytes())
        captured = receipt.get("origins", {})
        if not isinstance(captured, dict) or any(
            key not in {"root", "root/catalog.json", "program"}
            or not isinstance(value, str)
            or not Path(value).is_absolute()
            for key, value in captured.items()
        ):
            raise ValueError(f"invalid source origins: {path}")
        if path == layout.root_source_receipt:
            origins.pop("root/catalog.json", None)
            if "root" not in captured and "program" in origins:
                origins["root"] = origins["program"].parent / "toolang.toml"
        origins.update({key: Path(value) for key, value in captured.items()})
    return ConfigOrigins(origins, captured=launch_captured, snapshots=snapshots)


def config_sources(
    layout: AgentLayout, *, include_agent: bool = True
) -> tuple[ConfigSource, ...]:
    """Load the same placement-local root/home configuration in every mode."""
    runtime = layout.runtime_config or resolve_runtime_config() or RuntimeConfig()
    context = config_origins(layout)
    paths = (
        ("root", layout.root_config),
        *(((("home", layout.config),)) if include_agent else ()),
    )
    sources = tuple(
        ConfigSource(
            context.origin(scope, path),
            runtime.apply(
                read_config(
                    path,
                    include_workspaces=scope == "home",
                    origin=context.origin(scope, path),
                    context=context,
                ),
            ),
            scope,
        )
        for scope, path in paths
    )
    if human := source_metadata(layout).get("human"):
        root_config = sources[0].config
        teaming = root_config.setdefault("teaming", {})
        if isinstance(teaming, dict):
            cast(dict[str, object], teaming).setdefault("human", human)
    for source in sources:
        if source.scope != "root" or layout.placement != "resident":
            _reject_local_environment(source.config, source.path)
    return sources


def _reject_local_environment(config: Mapping[str, object], path: Path) -> None:
    if "env" in config:
        raise ValueError(f"{path}: env is supported only in the Toolang root config")


def merge_mappings(layers: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Merge mapping fields for inspection and State, not Setup policy resolution."""
    result: dict[str, object] = {}
    for layer in layers:
        for key, value in layer.items():
            previous = result.get(key)
            result[key] = (
                merge_mappings(
                    (cast(dict[str, object], previous), cast(dict[str, object], value))
                )
                if isinstance(previous, dict) and isinstance(value, dict)
                else deepcopy(value)
            )
    return result


def source_catalog(
    sources: Sequence[ConfigSource], *, roaming: bool, validate: bool = True
) -> Path | None:
    """Pick the nearest authored path or companion without merging catalogs."""
    for source in reversed(sources):
        config = catalog_config(source.config)
        if config is not None and "path" in config:
            path = config["path"]
            if not isinstance(path, str) or not path:
                raise ValueError(f"model catalog path must be nonempty: {source.path}")
            selected = Path(path)
        else:
            selected = source.path.with_name(
                "toolang.catalog.json"
                if roaming and source.scope != "home"
                else "catalog.json"
            )
            if not selected.exists() and not selected.is_symlink():
                continue
        if validate and not selected.is_file():
            raise ValueError(f"model catalog is not a file: {selected}")
        return selected
    return None

"""Agent-owned hosted filesystem assembly."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping, Sequence
import getpass
import json
import os
import tomllib

from toolang.base.types.sandbox import SandboxMount
from toolang.catalog.types import CAP_DIRECTORY_NAMES
from toolang.common.layout import (
    AgentLayout,
    IMPLICIT_WORKSPACE_NAME,
    ensure_scratch_workspace,
)
from toolang.state.source import observe_home_source, observe_root_source
from toolang.common.config_sources import PROJECTION_KEY, catalog_config, resolve_path
from toolang.common.config_runtime import CONFIG_PATCH_ENV


def mapped_path(path: Path, mounts: Sequence[SandboxMount]) -> Path | None:
    """Reuse the most specific existing file or directory mapping."""
    path = path.resolve()
    matches = [
        mount
        for mount in mounts
        if path == mount.local_path.resolve()
        or (
            mount.local_path.is_dir()
            and path.is_relative_to(mount.local_path.resolve())
        )
    ]
    if not matches:
        return None
    mount = max(matches, key=lambda item: len(item.local_path.resolve().parts))
    return mount.hosted_path / path.relative_to(mount.local_path.resolve())


def prepare_catalog_mount(
    path: Path, hosted_place: Path
) -> tuple[tuple[SandboxMount, ...], Path]:
    """Keep selected catalogs in their owning scope, mapping external files once."""
    target = hosted_place / ".inputs" / "catalog.json"
    return (SandboxMount(path.resolve(), target, read_only=True),), target


def prepare_place_mounts(
    local_place: Path,
    hosted_place: Path,
) -> tuple[SandboxMount, ...]:
    """Prepare authored place inputs and their explicit hosted mounts."""

    config_path = local_place / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists() and not config_path.is_symlink():
        config_path.touch(exist_ok=True)

    mounts = [
        SandboxMount(
            local_path=config_path,
            hosted_path=hosted_place / "config.toml",
            read_only=True,
        )
    ]
    catalog_path = local_place / "catalog.json"
    if catalog_path.is_file():
        mounts.append(
            SandboxMount(
                local_path=catalog_path.resolve(strict=True),
                hosted_path=hosted_place / "catalog.json",
                read_only=True,
            )
        )
    for directory_name in (".state", *CAP_DIRECTORY_NAMES):
        local_path = local_place / directory_name
        local_path.mkdir(parents=True, exist_ok=True)
        mounts.append(
            SandboxMount(
                local_path=local_path,
                hosted_path=hosted_place / directory_name,
            )
        )
    return tuple(mounts)


def prepare_linked_state_source_mounts(
    local_place: Path,
    agent_name: str,
    hosted_place: Path,
    *,
    include_home: bool = True,
) -> tuple[SandboxMount, ...]:
    """Mount every symbolic-linked State source at its logical guest path."""

    hosted_home = hosted_place / "agents" / agent_name
    observations = ((observe_root_source(local_place), hosted_place),)
    if include_home:
        observations += ((observe_home_source(local_place, agent_name), hosted_home),)
    return tuple(
        SandboxMount(
            local_path=item.source.resolve(strict=True),
            hosted_path=hosted_base / item.path,
            read_only=True,
        )
        for observation, hosted_base in observations
        for item in observation.files
        if item.source.is_symlink()
    )


def prepare_workspace_mounts(
    local_home: Path,
    hosted_workspaces: Path,
    *,
    workspaces: Mapping[str, str],
) -> tuple[tuple[SandboxMount, ...], dict[str, tuple[str, str]]]:
    """Capture mounted grants at sandbox startup; never mount later State additions."""
    scratch_root = ensure_scratch_workspace(local_home)
    grants = dict(
        (name, source)
        for name, source in workspaces.items()
        if name != IMPLICIT_WORKSPACE_NAME
    )
    available = sorted(
        (
            (name, source, Path(source).resolve())
            for name, source in grants.items()
            if Path(source).is_dir()
        ),
        key=lambda item: (len(item[2].parts), item[0]),
    )
    guest_lab = hosted_workspaces / IMPLICIT_WORKSPACE_NAME
    mounts = [SandboxMount(scratch_root, guest_lab)]
    mapping = {IMPLICIT_WORKSPACE_NAME: (str(scratch_root), str(guest_lab))}
    for name, source, root in available:
        guest = hosted_workspaces / name
        mounts.append(SandboxMount(local_path=root, hosted_path=guest))
        mapping[name] = (source, str(guest))
    return tuple(mounts), mapping


def prepare_guest_environment(
    layout: AgentLayout,
    *,
    mapping: Mapping[str, tuple[str, str]],
) -> dict[str, str]:
    """Capture source origins and execution corrections without rewriting sources."""
    origins = {"root": str(layout.place_config), "home": str(layout.config)}
    overlays: dict[str, object] = {}
    for scope, path in (("root", layout.place_config), ("home", layout.config)):
        if not path.is_file():
            continue
        config = tomllib.loads(path.read_text())
        workspaces = config.get("workspaces", {})
        if scope == "root" and PROJECTION_KEY not in config:
            workspaces = workspaces.get(layout.config_key, {})
        corrected = {}
        for name, value in workspaces.items():
            if not isinstance(value, str) or not value:
                continue
            canonical = resolve_path(value, path.parent)
            reference = os.path.normpath(str(path.parent / value))
            origins[reference] = str(canonical)
            mounted = mapping.get(name)
            if mounted is not None and mounted[0] == str(canonical):
                corrected[name] = mounted[1]
        catalog = catalog_config(config)
        if catalog and isinstance(catalog.get("path"), str) and catalog["path"]:
            value = str(catalog["path"])
            origins[os.path.normpath(str(path.parent / value))] = str(
                resolve_path(value, path.parent)
            )
        if corrected:
            overlays[scope] = {
                "workspaces": {layout.config_key: corrected}
                if scope == "root" and PROJECTION_KEY not in config
                else corrected
            }
    return {
        CONFIG_PATCH_ENV: json.dumps(
            {
                "origins": origins,
                "defaults": {"root": {"teaming": {"human": getpass.getuser()}}},
                **overlays,
            }
        ),
    }


def prepare_source_mounts(
    local_place: Path,
    agent_name: str,
    hosted_place: Path,
    *,
    include_home: bool = True,
) -> tuple[SandboxMount, ...]:
    """Mount original State inputs without environment-specific byte changes."""
    mounts = {
        mount.hosted_path: mount
        for mount in (
            *prepare_place_mounts(local_place, hosted_place),
            *prepare_linked_state_source_mounts(
                local_place, agent_name, hosted_place, include_home=include_home
            ),
        )
    }
    return tuple(mounts.values())

"""Agent-owned hosted filesystem assembly."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping, Sequence
import json
import os
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from toolang.plugin.catalogs.models_dev.path import ModelCatalogSelection

from toolang.base.types.sandbox import SandboxMount
from toolang.state.source import observe_root_source, observe_home_source
from toolang.catalog.types import CAP_DIRECTORY_NAMES
from toolang.common.accounts import current_username
from toolang.common.layout import (
    AgentLayout,
    IMPLICIT_WORKSPACE_NAME,
    ensure_scratch_workspace,
)
from toolang.state.config import parse_config, root_workspace_declarations
from toolang.common.config_sources import config_origins, catalog_config, resolve_path
from toolang.common.config_runtime import CONFIG_PATCH_ENV


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
    """Mount mutable root inputs by directory, keeping their anchors stable."""
    mounts = []
    for name in ("config", ".state/root", *CAP_DIRECTORY_NAMES):
        source = local_place / name
        source.mkdir(parents=True, exist_ok=True)
        mounts.append(SandboxMount(source, hosted_place / name))
    return tuple(mounts)


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


def validate_source_mounts(layout: AgentLayout, mounts: Sequence[SandboxMount]) -> None:
    """Reject source links whose targets would disappear in the guest view."""
    directory_mounts = sorted(
        (mount for mount in mounts if mount.local_path.is_dir()),
        key=lambda mount: len(mount.hosted_path.parts),
        reverse=True,
    )
    sources = (
        *observe_root_source(layout.agent_root).files,
        *observe_home_source(layout.agent_root, layout.name).files,
    )
    for entry in sources:
        source = entry.source
        if not source.is_symlink():
            continue
        owner = max(
            (
                mount
                for mount in mounts
                if mount.local_path.is_dir() and source.is_relative_to(mount.local_path)
            ),
            key=lambda mount: len(mount.local_path.parts),
            default=None,
        )
        if owner is None:
            raise ValueError(f"source symlink is outside guest mounts: {source}")
        hosted = owner.hosted_path / source.relative_to(owner.local_path)
        target = Path(os.readlink(source))
        target = Path(
            os.path.normpath(
                str(target if target.is_absolute() else hosted.parent / target)
            )
        )
        mounted = next(
            (
                mount
                for mount in directory_mounts
                if target.is_relative_to(mount.hosted_path)
            ),
            None,
        )
        if (
            mounted is None
            or (mounted.local_path / target.relative_to(mounted.hosted_path)).resolve()
            != source.resolve()
        ):
            raise ValueError(f"source symlink target is unavailable in guest: {source}")


def prepare_guest_environment(
    layout: AgentLayout,
    *,
    mapping: Mapping[str, tuple[str, str]],
    catalog: ModelCatalogSelection | None = None,
) -> dict[str, str]:
    """Capture source origins and execution corrections without rewriting sources."""
    context = config_origins(layout)
    origins = {
        **{key: str(value) for key, value in context.origins.items()},
        "root": str(context.origin("root", layout.place_config)),
        "home": str(layout.config),
        "toolang_root": str(layout.host_root),
    }
    if catalog is not None and catalog.scope in {"root", "home"}:
        origins[f"{catalog.scope}/catalog.json"] = str(catalog.path)
    overlays: dict[str, object] = {}
    for scope, path in (("root", layout.place_config), ("home", layout.config)):
        if not path.is_file():
            continue
        directory = context.origin(scope, path).parent
        config = parse_config(path.read_bytes())
        workspaces = (
            root_workspace_declarations(config, layout.config_key)
            if scope == "root"
            else config.get("workspaces", {})
        )
        if not isinstance(workspaces, Mapping):
            raise ValueError("workspaces config must be a table")
        corrected = {}
        for name, value in cast(Mapping[str, object], workspaces).items():
            if not isinstance(value, str) or not value:
                continue
            canonical = resolve_path(value, directory, context=context)
            reference = os.path.normpath(str(directory / value))
            origins[reference] = str(canonical)
            mounted = mapping.get(name)
            if mounted is not None and mounted[0] == str(canonical):
                corrected[name] = mounted[1]
        configured_catalog = catalog_config(config)
        if (
            configured_catalog
            and isinstance(configured_catalog.get("path"), str)
            and configured_catalog["path"]
        ):
            value = str(configured_catalog["path"])
            origins[os.path.normpath(str(directory / value))] = str(
                resolve_path(value, directory, context=context)
            )
        if corrected:
            overlays[scope] = {
                "workspaces": {layout.config_key: corrected}
                if scope == "root" and layout.config_key != ""
                else corrected
            }
    return {
        CONFIG_PATCH_ENV: json.dumps(
            {
                "origins": origins,
                "defaults": {"root": {"teaming": {"human": current_username()}}},
                **overlays,
            }
        ),
    }

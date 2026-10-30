"""Agent-owned hosted filesystem assembly."""

from __future__ import annotations

from pathlib import Path
from collections import deque
from collections.abc import Mapping, Sequence
import json
import os
from hashlib import sha256
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
from toolang.state.config import (
    parse_config,
    root_workspace_declarations,
    root_workspace_overrides,
)
from toolang.common.config_sources import config_origins, catalog_config, resolve_path
from toolang.common.config_runtime import CONFIG_PATCH_ENV


def prepare_catalog_mount(
    catalog: ModelCatalogSelection,
    hosted_place: Path,
    mounts: Sequence[SandboxMount] = (),
) -> tuple[tuple[SandboxMount, ...], Path | None]:
    """Reuse covered inputs; mount an external catalog independently."""
    if catalog.scope == "packaged":
        return (), None
    path = catalog.path.resolve()
    for mount in mounts:
        local = mount.local_path.resolve()
        if path != local and not (
            mount.local_path.is_dir() and path.is_relative_to(local)
        ):
            continue
        hosted = mount.hosted_path / path.relative_to(local)
        exposed = _mounted_directory_path(hosted, mounts)
        if exposed is not None and exposed.resolve() == path:
            return (), hosted if catalog.scope == "custom" else None
    target = hosted_place / ".inputs" / "catalog.json"
    return (SandboxMount(path, target, read_only=True),), target


def _mounted_directory_path(
    hosted: Path, mounts: Sequence[SandboxMount]
) -> Path | None:
    owner = max(
        (
            mount
            for mount in mounts
            if hosted == mount.hosted_path
            or mount.local_path.is_dir()
            and hosted.is_relative_to(mount.hosted_path)
        ),
        key=lambda mount: len(mount.hosted_path.parts),
        default=None,
    )
    return (
        owner.local_path / hosted.relative_to(owner.hosted_path)
        if owner is not None
        else None
    )


def prepare_place_mounts(
    local_place: Path,
    hosted_place: Path,
) -> tuple[SandboxMount, ...]:
    """Bind root config/catalog snapshots and mutable State/capability directories."""
    mounts = [
        SandboxMount(local_place / name, hosted_place / name, read_only=True)
        for name in ("config.toml", "catalog.json", "source.json")
        if (local_place / name).is_file()
    ]
    for name in (".state/root", *CAP_DIRECTORY_NAMES):
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


def validate_source_mounts(
    layout: AgentLayout,
    mounts: Sequence[SandboxMount],
    *,
    catalog: ModelCatalogSelection | None = None,
) -> None:
    """Reject source links whose targets would disappear in the guest view."""
    directory_mounts = tuple(mount for mount in mounts if mount.local_path.is_dir())

    def resolve_guest(hosted: Path) -> Path | None:
        pending = deque(hosted.parts[1:])
        current = Path("/")
        followed = 0
        while pending:
            part = pending.popleft()
            if part == "..":
                current = current.parent
                continue
            candidate = current / part
            local = _mounted_directory_path(candidate, directory_mounts)
            # Bind mounts expose a directory, even when its host name is a link.
            anchor = any(candidate == mount.hosted_path for mount in directory_mounts)
            if local is not None and not anchor and local.is_symlink():
                followed += 1
                if followed > 40:
                    return None
                target = Path(os.readlink(local))
                if target.is_absolute():
                    current = Path("/")
                pending.extendleft(
                    reversed(target.parts[1:] if target.is_absolute() else target.parts)
                )
            else:
                current = candidate
        return _mounted_directory_path(current, directory_mounts)

    sources = (
        *observe_root_source(layout.agent_root).files,
        *observe_home_source(layout.agent_root, layout.name).files,
    )
    for source in (
        *(entry.source for entry in sources),
        *(
            ()
            if catalog is not None and catalog.scope == "custom"
            else (layout.root_catalog, layout.agent_home / "catalog.json")
        ),
        layout.root_source_receipt,
        layout.source_receipt,
    ):
        if not source.is_symlink():
            continue
        if any(mount.local_path == source and source.is_file() for mount in mounts):
            # A file bind exposes the selected target inode at its guest alias.
            continue
        anchor = (
            layout.agent_home
            if source.is_relative_to(layout.agent_home)
            else layout.agent_root / source.relative_to(layout.agent_root).parts[0]
        )
        owner = next(
            (mount for mount in mounts if mount.local_path == anchor),
            None,
        )
        if owner is None:
            raise ValueError(f"source symlink is outside guest mounts: {source}")
        hosted = owner.hosted_path / source.relative_to(owner.local_path)
        target = resolve_guest(hosted)
        if target is None or target.resolve() != source.resolve():
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
        "agent_root": str(layout.agent_root),
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
        workspaces = config.get("workspaces", {})
        if scope == "root":
            workspaces = (
                root_workspace_declarations(config, layout.config_key)
                if root_workspace_overrides(
                    config, layout.config_key, directory, context=context
                )
                else {}
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
                "snapshots": {
                    "config.toml": sha256(layout.root_config.read_bytes()).hexdigest()
                    if layout.root_config.is_file()
                    else None,
                },
                "defaults": {"root": {"teaming": {"human": current_username()}}},
                **overlays,
            }
        ),
    }

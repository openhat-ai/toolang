"""Agent-owned hosted filesystem assembly."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Mapping, Sequence
import getpass
from hashlib import sha256

from toolang.base.types.sandbox import SandboxMount
from toolang.catalog.types import CAP_DIRECTORY_NAMES
from toolang.common.layout import (
    IMPLICIT_WORKSPACE_NAME,
    ensure_scratch_workspace,
)
from toolang.state.source import observe_home_source, observe_root_source
from toolang.common.config_sources import read_config
from toolang.teaming.config import root_config as parse_teaming_root


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
    path: Path, hosted_place: Path, *, mounts: Sequence[SandboxMount]
) -> tuple[tuple[SandboxMount, ...], Path]:
    """Keep selected catalogs in their owning scope, mapping external files once."""
    covered = mapped_path(path, mounts)
    if covered is not None:
        return (), covered
    identity = sha256(str(path.resolve()).encode()).hexdigest()[:16]
    target = hosted_place / ".inputs" / "models" / identity / path.name
    return (SandboxMount(path.resolve(), target, read_only=True),), target


def prepare_place_mounts(
    local_place: Path,
    hosted_place: Path,
) -> tuple[SandboxMount, ...]:
    """Prepare authored place inputs and their explicit hosted mounts."""

    config_path = local_place / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.touch(exist_ok=True)

    mounts = [
        SandboxMount(
            local_path=config_path,
            hosted_path=hosted_place / "config.toml",
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


def prepare_guest_config(
    local_place: Path,
    root_config: Path,
    key: str | None,
    *,
    workspaces: Mapping[str, str],
    mapping: Mapping[str, tuple[str, str]],
    temporary_names: Sequence[str] = (),
) -> str:
    """Project one guest root without modifying either authored configuration."""
    import tomlkit

    local_config = local_place / "config.toml"
    config = read_config(local_config, include_workspaces=False)
    root = (
        config
        if local_config == root_config
        else read_config(root_config, include_workspaces=False)
    )
    if "env" in root:
        config["env"] = root["env"]
    client = parse_teaming_root(
        root, source=root_config, default_human=getpass.getuser()
    )
    config["teaming"] = {
        "human": client.human.removeprefix("human:"),
        "enabled": client.enabled,
        "hub": client.hub,
    }
    config.pop("hub", None)
    if key is not None:
        config["workspaces"] = {
            key: {
                name: mapping[name][1] if name in mapping else source
                for name, source in workspaces.items()
                if name not in temporary_names and name != IMPLICIT_WORKSPACE_NAME
            }
        }
    return tomlkit.dumps(config)


def prepare_source_mounts(
    local_place: Path,
    agent_name: str,
    hosted_place: Path,
    *,
    capture_home: Path | None = None,
    include_home: bool = True,
) -> tuple[SandboxMount, ...]:
    """Mount authored inputs with host-relative configuration paths already bound."""
    from toolang.common.config_sources import rebase_config_content
    from toolang.common.files import atomic_write_text

    mounts = {
        mount.hosted_path: mount
        for mount in (
            *prepare_place_mounts(local_place, hosted_place),
            *prepare_linked_state_source_mounts(
                local_place, agent_name, hosted_place, include_home=include_home
            ),
        )
    }
    local_home = local_place / "agents" / agent_name
    hosted_home = hosted_place / "agents" / agent_name
    for scope, local, hosted in (
        ("root", local_place, hosted_place),
        ("home", local_home, hosted_home),
    ):
        if scope == "home" and not include_home:
            continue
        config = local / "config.toml"
        if not config.is_file():
            continue
        original = config.read_bytes()
        resolved = rebase_config_content(
            original, local, include_workspaces=scope == "home"
        )
        if resolved == original:
            continue
        identity = sha256(str(config).encode()).hexdigest()[:16]
        captured = (
            (capture_home or local_home)
            / ".runtime"
            / "config-inputs"
            / identity
            / config.name
        )
        atomic_write_text(captured, resolved.decode("utf-8"))
        target = hosted / "config.toml"
        mounts[target] = SandboxMount(captured, target, read_only=True)
    return tuple(mounts.values())

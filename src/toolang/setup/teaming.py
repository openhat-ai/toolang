"""Resolve root/home teaming independently at the setup boundary."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from toolang.common.accounts import current_username
from toolang.common.layout import root_config_path
from toolang.common.config_sources import ConfigSource, read_config, merge_mappings
from toolang.teaming.config import (
    TeamingRootConfig,
    root_config,
    home_config,
)
from toolang.teaming.errors import TeamingError
from toolang.teaming.schemas import HubConnection


@dataclass(frozen=True)
class TeamingSetup:
    root: TeamingRootConfig
    enabled: bool
    host_root: str

    def connection(self, *, gateway: str | None = None) -> HubConnection:
        return self.root.connection(root=self.host_root, gateway=gateway)

    def toolset_config(self, *, gateway: str | None = None) -> dict[str, object]:
        if not self.enabled:
            return {}
        return {
            "root": self.host_root,
            "hub": self.connection(gateway=gateway).endpoint,
        }


def resolve_teaming_setup(
    sources: Sequence[ConfigSource],
    *,
    root: Path,
    home: Path,
    host_root: Path,
    default_human: str | None = None,
) -> TeamingSetup:
    root_layers: list[Mapping[str, object]] = []
    root_raw: Mapping[str, object] | None = None
    home_raw: Mapping[str, object] = {}
    for source in sources:
        if source.scope == "root" or source.scope is None and source.path == root:
            root_layers.append(source.config)
        elif source.scope == "home" or source.scope is None and source.path == home:
            home_raw = source.config
        elif "teaming" in source.config:
            raise TeamingError(
                f"{source.path}: teaming requires explicit root/home scope"
            )
    if root_layers:
        root_raw = merge_mappings(root_layers)
    if root_raw is None:
        root_raw = read_config(root, include_workspaces=False)
    client = root_config(
        root_raw,
        source=root,
        default_human=default_human
        if default_human is not None
        else current_username(),
    )
    local = home_config(home_raw, source=home)
    return TeamingSetup(
        client, enabled=client.enabled and local.enabled, host_root=str(host_root)
    )


def load_teaming_root(root: Path) -> TeamingRootConfig:
    path = root_config_path(root)
    return root_config(
        read_config(path, include_workspaces=False),
        source=path,
        default_human=current_username(),
    )

"""Resolve root/home teaming independently at the setup boundary."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import getpass
from pathlib import Path

from toolang.common.config_sources import ConfigSource, read_config
from toolang.teaming.config import (
    TeamingHomeConfig,
    TeamingRootConfig,
    root_config,
    home_config,
)
from toolang.teaming.errors import TeamingError


@dataclass(frozen=True)
class TeamingSetup:
    root: TeamingRootConfig
    home: TeamingHomeConfig

    def toolset_config(self) -> dict[str, object]:
        return {"url": self.root.backend.url} if self.home.enabled else {}


def resolve_teaming_setup(
    sources: Sequence[ConfigSource],
    *,
    root: Path,
    home: Path,
    default_human: str | None = None,
) -> TeamingSetup:
    root_raw: Mapping[str, object] = {}
    home_raw: Mapping[str, object] = {}
    for source in sources:
        if source.path == root:
            root_raw = source.config
        elif source.path == home:
            home_raw = source.config
        elif "teaming" in source.config:
            raise TeamingError(
                f"{source.path}: teaming requires explicit root/home scope"
            )
        if "messaging" in source.config or "human" in source.config:
            raise TeamingError(
                f"{source.path}: experimental messaging/human configuration is replaced by teaming"
            )
    return TeamingSetup(
        root_config(
            root_raw,
            source=root,
            default_human=default_human
            if default_human is not None
            else getpass.getuser(),
        ),
        home_config(home_raw, source=home),
    )


def load_teaming_root(root: Path) -> TeamingRootConfig:
    path = root / "config.toml"
    value = read_config(path, include_workspaces=False)
    return resolve_teaming_setup(
        (ConfigSource(path, value),),
        root=path,
        home=root / "agents" / "_unused" / "config.toml",
    ).root

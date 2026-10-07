"""Resolve messaging defaults and identity once at the setup boundary."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import getpass
from pathlib import Path
from typing import cast

from toolang.common.config_sources import merge_mappings, read_config
from toolang.messaging.config import MessagingConfig
from toolang.messaging.errors import MessagingError
from toolang.messaging.schemas import component

DEFAULT_MESSAGING_URL = "redis://localhost:6379/0"


@dataclass(frozen=True)
class MessagingSetup:
    config: MessagingConfig | None
    human: str

    def toolset_config(self) -> dict[str, object]:
        """Pass concrete values through the existing plugin factory contract."""
        return (
            {"url": self.config.url, "groups": list(self.config.groups)}
            if self.config
            else {}
        )


def resolve_messaging_setup(
    configs: Sequence[Mapping[str, object]],
    *,
    human_config: Mapping[str, object],
    default_human: str | None = None,
) -> MessagingSetup:
    raw = merge_mappings(configs).get("messaging", {})
    if not isinstance(raw, Mapping):
        raise MessagingError("messaging must be a table")
    raw = cast(Mapping[str, object], raw)
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise MessagingError("messaging.enabled must be a boolean")
    human = human_config.get("human", {})
    if not isinstance(human, Mapping):
        raise MessagingError("human must be a table")
    human = cast(Mapping[str, object], human)
    name = human.get(
        "name", default_human if default_human is not None else getpass.getuser()
    )
    if not isinstance(name, str):
        raise MessagingError("human.name must be text")
    component(name)
    config = None
    if enabled:
        url, groups = raw.get("url", DEFAULT_MESSAGING_URL), raw.get("groups", [])
        if not isinstance(url, str):
            raise MessagingError("messaging.url must name an external Valkey server")
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise MessagingError(
                "messaging.groups must be a list of gc_ conversation IDs"
            )
        config = MessagingConfig(url, tuple(dict.fromkeys(cast(list[str], groups))))
    return MessagingSetup(config, name)


def load_messaging_setup(
    root: Path, *, default_human: str | None = None
) -> MessagingSetup:
    """Load root messaging settings without model or tool materialization."""
    config = read_config(root / "config.toml", include_workspaces=False)
    return resolve_messaging_setup(
        (config,), human_config=config, default_human=default_human
    )

"""Persistent config for runtime channel bindings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ._toml import load_toml, write_toml


class ChannelBinding(BaseModel):
    """One named channel binding stored in ``channels.toml``."""

    plugin: str
    config: dict[str, Any] = Field(default_factory=dict)


class ChannelsConfig(BaseModel):
    """Named channel bindings for one agent home."""

    channels: dict[str, ChannelBinding] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ChannelsConfig":
        """Load one channels config document from disk."""

        data = load_toml(path)
        raw_channels = data.get("channels")
        bindings = raw_channels if isinstance(raw_channels, dict) else data
        return cls(
            channels={
                name: ChannelBinding.model_validate(value)
                for name, value in bindings.items()
                if isinstance(value, dict)
            }
        )

    def save(self, path: Path) -> None:
        """Write this channels config document to disk."""

        write_toml(path, self.to_toml())

    def to_toml(self) -> dict[str, Any]:
        """Render this channels config document to TOML-compatible data."""

        return {
            "channels": {
                name: self.channels[name].model_dump(mode="python", exclude_none=True)
                for name in sorted(self.channels)
            }
        }

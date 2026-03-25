"""Persistent config for runtime tool providers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ._toml import load_toml, write_toml


class ToolBinding(BaseModel):
    """One configured runtime tool provider binding."""

    provider: str = "default"
    config: dict[str, Any] = Field(default_factory=dict)


class ToolsConfig(BaseModel):
    """Named tool-family bindings for one agent home."""

    tools: dict[str, ToolBinding] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "ToolsConfig":
        """Load one tools config document from disk."""

        data = load_toml(path)
        raw_tools = data.get("tools")
        bindings = raw_tools if isinstance(raw_tools, dict) else data
        return cls(
            tools={
                name: ToolBinding.model_validate(value)
                for name, value in bindings.items()
                if isinstance(value, dict)
            }
        )

    def save(self, path: Path) -> None:
        """Write this tools config document to disk."""

        write_toml(path, self.to_toml())

    def to_toml(self) -> dict[str, Any]:
        """Render this tools config document to TOML-compatible data."""

        return {
            "tools": {
                name: self.tools[name].model_dump(mode="python", exclude_none=True)
                for name in sorted(self.tools)
            }
        }

"""Persistent config for runtime hook bindings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ._toml import load_toml, write_toml


class HookBinding(BaseModel):
    """One named hook binding stored in ``hooks.toml``."""

    path: str
    plugin: str
    method: str = "POST"
    idempotency_header: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("method")
    @classmethod
    def normalize_method(cls, value: str) -> str:
        """Normalize one configured HTTP method."""

        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("hook method may not be empty")
        return normalized


class HooksConfig(BaseModel):
    """Named hook bindings for one agent home."""

    hooks: dict[str, HookBinding] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "HooksConfig":
        """Load one hooks config document from disk."""

        data = load_toml(path)
        raw_hooks = data.get("hooks")
        bindings = raw_hooks if isinstance(raw_hooks, dict) else data
        return cls(
            hooks={
                name: HookBinding.model_validate(value)
                for name, value in bindings.items()
                if isinstance(value, dict)
            }
        )

    def save(self, path: Path) -> None:
        """Write this hooks config document to disk."""

        write_toml(path, self.to_toml())

    def to_toml(self) -> dict[str, Any]:
        """Render this hooks config document to TOML-compatible data."""

        return {
            "hooks": {
                name: self.hooks[name].model_dump(mode="python", exclude_none=True)
                for name in sorted(self.hooks)
            }
        }

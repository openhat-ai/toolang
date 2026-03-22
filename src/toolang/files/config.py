"""Persistent authored configuration for shared caps and model aliases."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from toolang_caps.models import CapEntry

from toolang.files._toml import load_toml, write_toml


class ModelEntry(BaseModel):
    """One named model definition stored in a config file."""

    provider: str
    model: str
    base_url: str | None = None
    api_key_env: str | None = None


class ModelsSection(BaseModel):
    """Model configuration section persisted in a Toolang config file."""

    default: list[str] = Field(default_factory=list)
    named: dict[str, ModelEntry] = Field(default_factory=dict)

    @classmethod
    def from_toml(cls, data: dict[str, Any]) -> "ModelsSection":
        """Build the models section from parsed TOML data."""

        defaults = data.get("default", [])
        named = {
            key: ModelEntry.model_validate(value)
            for key, value in data.items()
            if key != "default" and isinstance(value, dict)
        }
        return cls(default=list(defaults), named=named)

    def to_toml(self) -> dict[str, Any]:
        """Render this models section to TOML-compatible data."""

        data: dict[str, Any] = {}
        if self.default:
            data["default"] = list(self.default)
        for name in sorted(self.named):
            data[name] = self.named[name].model_dump(mode="python", exclude_none=True)
        return data


class ToolangConfig(BaseModel):
    """Shared config document for locally managed caps and model aliases."""

    skills: dict[str, CapEntry] = Field(default_factory=dict)
    services: dict[str, CapEntry] = Field(default_factory=dict)
    prompts: dict[str, CapEntry] = Field(default_factory=dict)
    psyches: dict[str, CapEntry] = Field(default_factory=dict)
    models: ModelsSection = Field(default_factory=ModelsSection)

    @classmethod
    def empty(cls) -> "ToolangConfig":
        """Return an empty config document."""

        return cls()

    @classmethod
    def load(cls, path: Path) -> "ToolangConfig":
        """Load a config document from disk."""

        data = load_toml(path)
        return cls(
            skills=data.get("skills", {}) or {},
            services=data.get("services", {}) or {},
            prompts=data.get("prompts", {}) or {},
            psyches=data.get("psyches", {}) or {},
            models=ModelsSection.from_toml(data.get("models", {}) or {}),
        )

    def save(self, path: Path) -> None:
        """Write this config document to disk."""

        write_toml(path, self.to_toml())

    def to_toml(self) -> dict[str, Any]:
        """Render this config document to TOML-compatible data."""

        data: dict[str, Any] = {}
        for section in ("skills", "services", "prompts", "psyches"):
            entries = getattr(self, section)
            if entries:
                data[section] = {
                    name: entries[name].model_dump(mode="python", exclude_none=True)
                    for name in sorted(entries)
                }
        models = self.models.to_toml()
        if models:
            data["models"] = models
        return data

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from toolang.files._toml import load_toml, write_toml


class LockEntry(BaseModel):
    ref: str
    resolved: str


class ToolangLock(BaseModel):
    version: int = 1
    skills: dict[str, LockEntry] = Field(default_factory=dict)
    services: dict[str, LockEntry] = Field(default_factory=dict)
    prompts: dict[str, LockEntry] = Field(default_factory=dict)
    psyches: dict[str, LockEntry] = Field(default_factory=dict)

    @classmethod
    def empty(cls) -> "ToolangLock":
        return cls()

    @classmethod
    def load(cls, path: Path) -> "ToolangLock":
        data = load_toml(path)
        return cls(
            version=int(data.get("version", 1)),
            skills=data.get("skills", {}) or {},
            services=data.get("services", {}) or {},
            prompts=data.get("prompts", {}) or {},
            psyches=data.get("psyches", {}) or {},
        )

    def save(self, path: Path) -> None:
        write_toml(path, self.to_toml())

    def to_toml(self) -> dict[str, object]:
        data: dict[str, object] = {"version": self.version}
        for section in ("skills", "services", "prompts", "psyches"):
            entries = getattr(self, section)
            if entries:
                data[section] = {
                    name: entries[name].model_dump(mode="python")
                    for name in sorted(entries)
                }
        return data

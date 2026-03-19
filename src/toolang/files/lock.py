from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from toolang.files._toml import load_toml, write_toml


class LockEntry(BaseModel):
    ref: str
    repo: str
    path: str
    rev: str


class LockedAgentRefs(BaseModel):
    skills: dict[str, LockEntry] = Field(default_factory=dict)
    services: dict[str, LockEntry] = Field(default_factory=dict)
    prompts: dict[str, LockEntry] = Field(default_factory=dict)
    psyches: dict[str, LockEntry] = Field(default_factory=dict)

    def to_toml(self) -> dict[str, object]:
        data: dict[str, object] = {}
        for section in ("skills", "services", "prompts", "psyches"):
            entries = getattr(self, section)
            if entries:
                data[section] = {
                    name: entries[name].model_dump(mode="python")
                    for name in sorted(entries)
                }
        return data


class ToolangLock(BaseModel):
    version: int = 1
    agents: dict[str, LockedAgentRefs] = Field(default_factory=dict)

    @classmethod
    def empty(cls) -> "ToolangLock":
        return cls()

    @classmethod
    def load(cls, path: Path) -> "ToolangLock":
        data = load_toml(path)
        return cls(
            version=int(data.get("version", 1)),
            agents=data.get("agents", {}) or {},
        )

    def save(self, path: Path) -> None:
        write_toml(path, self.to_toml())

    def to_toml(self) -> dict[str, object]:
        data: dict[str, object] = {"version": self.version}
        if self.agents:
            data["agents"] = {
                agent_name: self.agents[agent_name].to_toml()
                for agent_name in sorted(self.agents)
            }
        return data

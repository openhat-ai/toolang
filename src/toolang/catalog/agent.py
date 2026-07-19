"""Authored resident agent homes."""

from __future__ import annotations

from pathlib import Path
import shutil


class LocalAgents:
    """CRUD for agent homes below one explicitly supplied directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def list(self) -> tuple[str, ...]:
        if not self.directory.is_dir():
            return ()
        return tuple(
            path.name
            for path in sorted(self.directory.iterdir())
            if path.is_dir() and (path / "agent.too").is_file()
        )

    def get(self, name: str) -> Path | None:
        home = self.path(name)
        return home if home.is_dir() and (home / "agent.too").is_file() else None

    def create(
        self,
        name: str,
        *,
        content: str,
    ) -> Path:
        home = self.path(name)
        if home.exists():
            raise FileExistsError(f"agent already exists: {home}")
        home.mkdir(parents=True)
        (home / "agent.too").write_text(content, encoding="utf-8")
        return home

    def rename(self, name: str, new_name: str) -> Path:
        home = self.get(name)
        if home is None:
            raise FileNotFoundError(f"agent not found: {self.path(name)}")
        target = self.path(new_name)
        if target.exists():
            raise FileExistsError(f"agent already exists: {target}")
        home.replace(target)
        return target

    def remove(self, name: str) -> Path:
        home = self.get(name)
        if home is None:
            raise FileNotFoundError(f"agent not found: {self.path(name)}")
        shutil.rmtree(home)
        return home

    def path(self, name: str) -> Path:
        _validate_name(name)
        return self.directory / name

def _validate_name(name: str) -> None:
    if not name.strip() or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError(f"invalid agent name: {name!r}")

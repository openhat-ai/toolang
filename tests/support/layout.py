"""Create root-owned configuration fixtures in their dedicated directory."""

from pathlib import Path


def root_file(root: Path, name: str) -> Path:
    directory = root / "config"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name

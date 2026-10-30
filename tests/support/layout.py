"""Create root-owned configuration fixtures at their root."""

from pathlib import Path


def root_file(root: Path, name: str) -> Path:
    directory = root
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name

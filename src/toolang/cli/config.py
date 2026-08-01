"""CLI-owned configuration file loading."""

from __future__ import annotations

from pathlib import Path
import tomllib


def load_config(path: Path) -> dict[str, object]:
    """Load one TOML configuration file when it exists."""

    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8"))


def load_config_layers(
    root: Path, agent_name: str = ""
) -> tuple[dict[str, object], dict[str, object]]:
    """Load root and optional agent configuration layers."""

    return (
        load_config(root / "config.toml"),
        load_config(root / "agents" / agent_name / "config.toml") if agent_name else {},
    )

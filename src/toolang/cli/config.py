"""CLI-owned configuration file loading."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import tomllib
from typing import cast

from toolang.common.config_sources import ConfigSource


TITLE_KINDS = frozenset({"chat", "talk", "top", "serve", "run", "rerun", "retry"})


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


def resolve_title_prefix(
    kind: str,
    *,
    sources: Sequence[ConfigSource],
    environment: Mapping[str, str],
    option: str | None,
) -> str:
    """Validate each layer, then merge the selected type by presence."""
    if kind not in TITLE_KINDS:
        raise ValueError(f"unknown terminal title kind: {kind}")
    prefix = ""
    for source in sources:
        table: Mapping[str, object] = source.config
        for index, key in enumerate(("terminal", "title", "prefix")):
            if key not in table:
                table = {}
                break
            value = table[key]
            path = ".".join(("terminal", "title", "prefix")[: index + 1])
            if not isinstance(value, dict):
                raise ValueError(f"{source.path}: {path} must be a table")
            table = cast(dict[str, object], value)
        for key, value in table.items():
            if key not in TITLE_KINDS:
                raise ValueError(f"{source.path}: unknown terminal.title.prefix.{key}")
            if not isinstance(value, str):
                raise ValueError(
                    f"{source.path}: terminal.title.prefix.{key} must be a string"
                )
            if key == kind:
                prefix = value
    return (
        option
        if option is not None
        else environment.get(f"TOOLANG_TITLE_PREFIX_{kind.upper()}", prefix)
    )

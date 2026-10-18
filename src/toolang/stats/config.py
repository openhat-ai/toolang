"""Home-scoped configuration for independent statistics archival."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from toolang.common.config_sources import ConfigSource


@dataclass(frozen=True, slots=True)
class StatsConfig:
    archive_after_seconds: int = 86400

    def __post_init__(self) -> None:
        if (
            type(self.archive_after_seconds) is not int
            or self.archive_after_seconds < 60
        ):
            raise ValueError("stats.archive_after_seconds must be an integer >=60")


def stats_config(sources: Sequence[ConfigSource], *, home: Path) -> StatsConfig:
    result = StatsConfig()
    for source in sources:
        if "stats" not in source.config:
            continue
        if source.path != home:
            raise ValueError(f"{source.path}: stats requires agent home scope")
        raw = source.config["stats"]
        if not isinstance(raw, dict) or set(raw) - {"archive_after_seconds"}:
            raise ValueError(f"{source.path}: stats only accepts archive_after_seconds")
        value = cast(dict[str, object], raw).get(
            "archive_after_seconds", result.archive_after_seconds
        )
        if type(value) is not int:
            raise ValueError(
                f"{source.path}: stats.archive_after_seconds must be an integer >=60"
            )
        result = StatsConfig(archive_after_seconds=value)
    return result

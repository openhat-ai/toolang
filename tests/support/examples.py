"""Discover authored examples without including generated agent state."""

from pathlib import Path


def example_sources(root: Path) -> tuple[Path, ...]:
    """Include direct scripts and direct flow modules only."""

    return tuple(
        sorted(
            path for pattern in ("*.too", "flows/*.too") for path in root.glob(pattern)
        )
    )

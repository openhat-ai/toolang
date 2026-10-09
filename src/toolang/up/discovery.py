"""Discover materialized agents at the host boundary."""

from pathlib import Path

from toolang.common.layout import AgentLayout


def agent_layouts(root: Path) -> tuple[AgentLayout, ...]:
    directory = root / "agents"
    try:
        return tuple(
            AgentLayout.resident(root, home.name)
            for home in sorted(directory.iterdir())
            if home.is_dir()
        )
    except FileNotFoundError:
        return ()


def agent_roster(root: Path) -> set[str]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    return {f"agent:{layout.name}" for layout in agent_layouts(root)}

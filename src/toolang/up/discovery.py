"""Discover materialized agents at the host boundary."""

from pathlib import Path
from stat import S_ISDIR

from toolang.common.layout import AgentLayout, is_agent_name


def agent_layouts(root: Path, *, agent: str | None = None) -> tuple[AgentLayout, ...]:
    if agent is not None:
        layout = AgentLayout.resident(root, agent)
        try:
            return (layout,) if S_ISDIR(layout.home.stat().st_mode) else ()
        except FileNotFoundError:
            return ()
    directory = root / "agents"
    try:
        return tuple(
            AgentLayout.resident(root, home.name)
            for home in sorted(directory.iterdir())
            if is_agent_name(home.name) and home.is_dir()
        )
    except FileNotFoundError:
        return ()


def agent_roster(root: Path) -> set[str]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    return {f"agent:{layout.name}" for layout in agent_layouts(root)}

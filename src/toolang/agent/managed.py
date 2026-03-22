"""Managed local-agent file operations."""

from __future__ import annotations

import shutil
from pathlib import Path

from toolang.errors import ToolangError
from toolang.concepts.identity import AgentRef
from toolang.concepts.layout import AgentHome


def create_managed_agent(agent: AgentRef) -> None:
    """Create the local source file for one managed agent."""

    source_path = agent.source
    if source_path.exists():
        raise ToolangError(f"Managed agent source already exists: {source_path}")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(_default_agent_source(agent.name), encoding="utf-8")


def clone_managed_agent(agent: AgentRef, *, source_text: str) -> None:
    """Clone one managed agent source file from existing text."""

    source_path = agent.source
    if source_path.exists():
        raise ToolangError(f"Managed agent source already exists: {source_path}")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source_text, encoding="utf-8")


def remove_managed_agent(agent: AgentRef) -> bool:
    """Remove one managed agent source file and local runtime state."""

    changed = False
    home = AgentHome.resolve(agent.home)
    room = home.room(agent.name)

    if agent.source.exists():
        agent.source.unlink()
        changed = True

    state_path = home.sync_state_path(agent.name)
    if state_path.exists():
        state_path.unlink()
        changed = True

    if room.path.exists():
        shutil.rmtree(room.path)
        changed = True

    _prune_empty_tree(home.shared_caps_root)
    _prune_empty_tree(home.path)
    return changed


def _default_agent_source(agent_name: str) -> str:
    title = agent_name.replace("-", " ")
    return (
        f"# {title.title()}\n\n"
        "thunk chat(user):\n"
        "    model = gpt-5\n\n"
        "    Respond helpfully, clearly, and directly to the user's message.\n"
    )


def _prune_empty_tree(root: Path) -> None:
    if not root.exists() or not root.is_dir():
        return

    for child in sorted(root.rglob("*"), reverse=True):
        if child.is_dir() and not any(child.iterdir()):
            child.rmdir()

    if root.exists() and not any(root.iterdir()):
        root.rmdir()

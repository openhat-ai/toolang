from __future__ import annotations

import shutil
from pathlib import Path

from toolang.errors import ToolangError
from toolang.layout import agent_room, agent_source_path, agent_sync_path


def create_resident_agent(source_path: Path, *, agent_name: str) -> None:
    if source_path.exists():
        raise ToolangError(f"Resident agent source already exists: {source_path}")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(_default_agent_source(agent_name), encoding="utf-8")


def clone_resident_agent(source_path: Path, *, source_text: str) -> None:
    if source_path.exists():
        raise ToolangError(f"Resident agent source already exists: {source_path}")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source_text, encoding="utf-8")


def remove_resident_agent(agent_home: Path, *, agent_name: str) -> bool:
    changed = False

    source_path = agent_source_path(agent_home, agent_name)
    if source_path.exists():
        source_path.unlink()
        changed = True

    state_path = agent_sync_path(agent_home, agent_name)
    if state_path.exists():
        state_path.unlink()
        changed = True

    room_path = agent_room(agent_home, agent_name)
    if room_path.exists():
        shutil.rmtree(room_path)
        changed = True

    _prune_empty_tree(agent_home / ".toolang")
    _prune_empty_tree(agent_home)
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

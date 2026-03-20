from __future__ import annotations

import shutil
from pathlib import Path

from toolang_caps.models import TEXT_CAP_KINDS, section_name


def remove_stale_sync_root_entries(sync_root: Path) -> None:
    expected_dirs = {section_name(kind) for kind in ("skill", *TEXT_CAP_KINDS)}
    for path in sync_root.iterdir():
        if path.is_dir():
            if path.name not in expected_dirs:
                shutil.rmtree(path)
            continue
        if path.suffixes != [".state", ".json"]:
            path.unlink()


def remove_legacy_lock_files(agent_home: Path) -> None:
    for filename in ("agent.lock", "toolang.lock"):
        path = agent_home / filename
        if path.exists():
            path.unlink()


def remove_legacy_agent_programs(agent_home: Path) -> None:
    agent_root = agent_home / ".toolang" / "agents"
    if not agent_root.exists():
        return
    for room in agent_root.iterdir():
        if not room.is_dir():
            continue
        for filename in ("program.json", "sync.json"):
            path = room / filename
            if path.exists():
                path.unlink()

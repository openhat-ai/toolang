"""Capture immutable runspace access for root runs."""

from __future__ import annotations

from pathlib import Path

from toolang.catalog.agent import materialize_agent_runspaces
from toolang.setup import AgentSetup

from .types import RunAccess, RunSpace, RunWorkspace

MAX_MEMO_CHARS = 32_000
_TRUNCATED_MARKER = "\n\n[Runspace notes truncated by Toolang.]"


def capture_run_access(setup: AgentSetup, space: RunSpace) -> RunAccess:
    """Read current notes and active grants into one immutable snapshot."""

    layout = setup.layout
    materialize_agent_runspaces(layout)
    if space == "collab":
        working_directory = layout.collab
        memo_file = layout.collab_memo
        workspaces = tuple(
            RunWorkspace(name=name, path=path)
            for name, path in sorted(setup.workspaces.items())
        )
    elif space == "lab":
        working_directory = layout.lab
        memo_file = layout.lab_memo
        workspaces = ()
    else:
        raise ValueError(f"invalid run space: {space!r}")
    memo, truncated = _read_memo(memo_file)
    return RunAccess(
        space=space,
        working_directory=working_directory.resolve(),
        memo_file=memo_file.resolve(),
        memo=memo,
        memo_truncated=truncated,
        workspaces=workspaces,
    )


def _read_memo(path: Path) -> tuple[str, bool]:
    with path.open("r", encoding="utf-8") as stream:
        text = stream.read(MAX_MEMO_CHARS + 1)
    if len(text) <= MAX_MEMO_CHARS:
        return text, False
    body_limit = MAX_MEMO_CHARS - len(_TRUNCATED_MARKER)
    return f"{text[:body_limit]}{_TRUNCATED_MARKER}", True

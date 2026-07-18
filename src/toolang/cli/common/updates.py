"""Durable agent update writes initiated by CLI commands."""

from __future__ import annotations

from pathlib import Path

from ...execution.records import UpdateKind
from ...execution.store import RunStore, run_store_path


def append_agent_update(
    root: Path,
    agent: str,
    kind: UpdateKind,
    payload: dict[str, object] | None = None,
) -> None:
    store = RunStore(run_store_path(root, agent))
    try:
        store.append_update(kind=kind, payload=payload or {})
    finally:
        store.close()

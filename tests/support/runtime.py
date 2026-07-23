"""Runtime snapshot helpers used by integration tests."""

from __future__ import annotations

from dataclasses import asdict
from typing import cast

from toolang.api.app import ApiContext
from toolang.state.source import read_authored_source
from toolang.state.state import PreparedCap, PreparedVisibility, list_entries


def snapshot_context(context: ApiContext) -> dict[str, object]:
    """Return one internal runtime snapshot for integration assertions."""

    durable = read_authored_source(context.root, context.name)
    agent_state = context.state_watcher.current()
    runs = context.executor.store.list_runs(limit=None)
    operational_facts: dict[str, object] = {
        "active_runs": sum(run.status in {"pending", "running"} for run in runs),
        "completed_runs": sum(
            run.status in {"finished", "failed", "canceled"} for run in runs
        ),
    }
    recent_runs = context.executor.store.list_runs(limit=20)
    recent_threads = tuple(
        dict.fromkeys(
            run.thread for run in sorted(recent_runs, key=lambda item: item.created_at)
        )
    )
    return {
        "durable": {
            "toolang_root": str(durable.toolang_root),
            "agent_name": durable.agent_name,
            "fingerprint": durable.fingerprint,
            "scanned_at": durable.scanned_at,
            "definitions": {
                "program_source": durable.program_path,
                "config_paths": list(durable.config_paths),
                "shared_entries": [
                    entry.to_snapshot()
                    for entry in _authored_entries(context, visibility="shared")
                ],
                "private_entries": [
                    entry.to_snapshot()
                    for entry in _authored_entries(context, visibility="private")
                ],
            },
            "operational_facts": {
                "prepared_fingerprint": agent_state.fingerprint,
                **operational_facts,
            },
        },
        "prepared": {
            "fingerprint": agent_state.fingerprint,
            "root_version": agent_state.root_version.hex(),
            "home_version": agent_state.home_version.hex(),
        },
        "state": {
            **agent_state.to_snapshot(),
            **operational_facts,
        },
        "channels": [],
        "execution": {
            "recent_runs": [asdict(item) for item in recent_runs],
            "recent_messages": [
                message.to_data()
                for thread_id in recent_threads
                for message in context.executor.store.recent_conversation_messages(
                    thread_id=thread_id
                )
            ],
        },
    }


def _authored_entries(
    context: ApiContext, *, visibility: str
) -> tuple[PreparedCap, ...]:
    if visibility not in {"shared", "private"}:
        raise ValueError(f"unsupported visibility: {visibility}")
    return list_entries(
        context.root,
        context.name,
        visibility=cast(PreparedVisibility, visibility),
    )

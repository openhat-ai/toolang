"""Runtime snapshot helpers used by integration tests."""

from __future__ import annotations

from dataclasses import asdict
from typing import cast

from pydantic import TypeAdapter

from toolang.api.app import ApiContext
from toolang.execution.projection import run_message_data
from toolang.execution.schemas import MessageData
from toolang.state.source import read_authored_source
from toolang.state.state import PreparedCap, PreparedVisibility, list_entries


_MESSAGES = TypeAdapter(list[MessageData])


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
    recent_steps = context.executor.store.list_steps_for_runs(
        run_ids=tuple(item.run_id for item in recent_runs)
    )
    recent_commands = {
        run.run_id: context.executor.store.list_commands(run_id=run.run_id)
        for run in recent_runs
    }
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
            "recent_updates": [
                asdict(item) for item in context.executor.store.list_updates(limit=20)
            ],
            "recent_runs": [asdict(item) for item in recent_runs],
            "recent_messages": _MESSAGES.dump_python(
                [
                    item
                    for run in sorted(recent_runs, key=lambda item: item.created_at)
                    for item in run_message_data(
                        run,
                        inputs=recent_commands.get(run.run_id, ()),
                        steps=recent_steps.get(run.run_id, ()),
                    )
                ],
                mode="json",
            ),
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

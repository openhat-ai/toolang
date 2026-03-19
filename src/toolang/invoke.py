from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from toolang.ast import Thunk
from toolang.bus.db import BusStore
from toolang.bus.events import RunFailed, RunFinished, RunOrigin, RunStarted, utc_now
from toolang.prepared import PreparedAgent
from toolang.runtime import execute_thunk


@dataclass(frozen=True, slots=True)
class InvokeResult:
    run_id: str
    output: str


def invoke_prepared_agent(
    prepared: PreparedAgent,
    thunk: Thunk,
    *,
    bus_db_path: Path,
    user_input: str | None,
    model: str | None = None,
    origin: RunOrigin = "invoke",
    thread_id: str | None = None,
) -> InvokeResult:
    bus = BusStore(bus_db_path)
    run_id = uuid.uuid4().hex
    summary = _summary(prepared.ref.agent_name, thunk)
    now = utc_now()
    bus.append(
        RunStarted(
            at=now,
            agent_uri=prepared.ref.agent_uri,
            agent_id=prepared.ref.agent_id[:12],
            run_id=run_id,
            run_type="turn",
            origin=origin,
            summary=summary,
            thunk_name=thunk.name,
            thread_id=thread_id,
        )
    )
    try:
        output = execute_thunk(
            prepared.program,
            thunk,
            prepared.source_path,
            user_input=user_input,
            model=model,
        )
    except Exception as exc:
        bus.append(
            RunFailed(
                at=utc_now(),
                agent_uri=prepared.ref.agent_uri,
                agent_id=prepared.ref.agent_id[:12],
                run_id=run_id,
                run_type="turn",
                origin=origin,
                error=str(exc),
                thunk_name=thunk.name,
                thread_id=thread_id,
            )
        )
        bus.close()
        raise

    bus.append(
        RunFinished(
            at=utc_now(),
            agent_uri=prepared.ref.agent_uri,
            agent_id=prepared.ref.agent_id[:12],
            run_id=run_id,
            run_type="turn",
            origin=origin,
            summary=summary,
            thunk_name=thunk.name,
            thread_id=thread_id,
        )
    )
    bus.close()
    return InvokeResult(run_id=run_id, output=output)


def _summary(agent_name: str, thunk: Thunk) -> str:
    thunk_name = thunk.name or "default"
    return f"{agent_name}:{thunk_name}"

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import uuid

from toolang.ast import Thunk
from toolang.bus.db import BusStore
from toolang.bus.events import RunFailed, RunFinished, RunOrigin, RunStarted, utc_now
from toolang.chats import ChatMessage, ChatStore
from toolang.messages import Message
from toolang.prepared import PreparedAgent
from toolang.runtime import execute_chat_thunk, execute_thunk


@dataclass(frozen=True, slots=True)
class InvokeResult:
    run_id: str
    output: str


@dataclass(frozen=True, slots=True)
class ChatResult:
    run_id: str
    output: str
    assistant: ChatMessage


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
    output = _tracked_turn(
        prepared=prepared,
        thunk=thunk,
        bus_db_path=bus_db_path,
        origin=origin,
        thread_id=thread_id,
        model=model,
        execute=lambda: execute_thunk(
            prepared.program,
            thunk,
            prepared.source_path,
            user_input=user_input,
            model=model,
        ),
    )
    return InvokeResult(run_id=output.run_id, output=output.output)


def chat_prepared_agent(
    prepared: PreparedAgent,
    thunk: Thunk,
    *,
    bus_db_path: Path,
    chat_store: ChatStore,
    message: Message,
    model: str | None = None,
) -> ChatResult:
    run_id = uuid.uuid4().hex

    chat_store.append_message(
        agent_uri=prepared.ref.agent_uri,
        agent_id=prepared.ref.agent_id[:12],
        agent_name=prepared.ref.agent_name,
        thread_id=message.thread_id,
        turn_id=run_id,
        role="user",
        origin=message.origin,
        channel=message.channel,
        sender=message.sender,
        text=message.text,
        meta=dict(message.meta),
    )

    def execute() -> str:
        return execute_chat_thunk(
            prepared.program,
            thunk,
            prepared.source_path,
            history_messages=chat_store.recent_openai_messages(thread_id=message.thread_id, limit=20),
            message=message,
            model=model,
        )

    tracked = _tracked_turn(
        prepared=prepared,
        thunk=thunk,
        bus_db_path=bus_db_path,
        origin="chat",
        thread_id=message.thread_id,
        model=model,
        run_id=run_id,
        execute=execute,
    )
    assistant_message = chat_store.append_message(
        agent_uri=prepared.ref.agent_uri,
        agent_id=prepared.ref.agent_id[:12],
        agent_name=prepared.ref.agent_name,
        thread_id=message.thread_id,
        turn_id=tracked.run_id,
        role="assistant",
        origin=message.origin,
        channel=message.channel,
        sender="self",
        text=tracked.output,
        meta={},
    )
    return ChatResult(
        run_id=tracked.run_id,
        output=tracked.output,
        assistant=assistant_message,
    )


@dataclass(frozen=True, slots=True)
class _TrackedTurnResult:
    run_id: str
    output: str


def _tracked_turn(
    *,
    prepared: PreparedAgent,
    thunk: Thunk,
    bus_db_path: Path,
    origin: RunOrigin,
    thread_id: str | None,
    model: str | None,
    run_id: str | None = None,
    execute,
) -> _TrackedTurnResult:
    bus = BusStore(bus_db_path)
    resolved_run_id = run_id or uuid.uuid4().hex
    summary = _summary(prepared.ref.agent_name, thunk)
    now = utc_now()
    bus.append(
        RunStarted(
            at=now,
            agent_uri=prepared.ref.agent_uri,
            agent_id=prepared.ref.agent_id[:12],
            run_id=resolved_run_id,
            run_type="turn",
            origin=origin,
            summary=summary,
            thunk_name=thunk.name,
            thread_id=thread_id,
        )
    )
    try:
        output = execute()
    except Exception as exc:
        bus.append(
            RunFailed(
                at=utc_now(),
                agent_uri=prepared.ref.agent_uri,
                agent_id=prepared.ref.agent_id[:12],
                run_id=resolved_run_id,
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
            run_id=resolved_run_id,
            run_type="turn",
            origin=origin,
            summary=summary,
            thunk_name=thunk.name,
            thread_id=thread_id,
        )
    )
    bus.close()
    return _TrackedTurnResult(run_id=resolved_run_id, output=output)


def _summary(agent_name: str, thunk: Thunk) -> str:
    thunk_name = thunk.name or "default"
    return f"{agent_name}:{thunk_name}"

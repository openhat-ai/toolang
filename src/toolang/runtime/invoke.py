from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import uuid

from toolang.agent.prepared import PreparedAgent
from toolang.bus.db import BusStore
from toolang.bus.events import RunFailed, RunFinished, RunOrigin, RunStarted, utc_now
from toolang.layout import agent_run_prompt_path
from toolang.syntax import Thunk
from toolang.concepts.persisted.prompt_trace import PromptTrace

from . import execute_prompt_build
from .build import (
    PromptBuild,
    build_chat_prompt,
    build_invoke_prompt,
    build_prompt_error_trace_data,
)
from .chats import ChatMessage, ChatStore
from .messages import Message


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
    sandbox: str = "host",
) -> InvokeResult:
    output = _tracked_turn(
        prepared=prepared,
        thunk=thunk,
        bus_db_path=bus_db_path,
        origin=origin,
        thread_id=thread_id,
        model=model,
        sandbox=sandbox,
        raw_input=user_input,
        build_prompt=lambda: build_invoke_prompt(
            prepared,
            thunk,
            user_input=user_input,
            model=model,
            origin=origin,
            thread_id=thread_id,
            sandbox=sandbox,
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
    sandbox: str = "host",
) -> ChatResult:
    run_id = uuid.uuid4().hex

    chat_store.append_message(
        agent_uri=prepared.ref.uri,
        agent_id=prepared.ref.id[:12],
        agent_name=prepared.ref.name,
        thread_id=message.thread_id,
        turn_id=run_id,
        role="user",
        origin=message.origin,
        channel=message.channel,
        sender=message.sender,
        text=message.text,
        meta=dict(message.meta),
    )
    history_messages = chat_store.recent_openai_messages(thread_id=message.thread_id, limit=20)

    tracked = _tracked_turn(
        prepared=prepared,
        thunk=thunk,
        bus_db_path=bus_db_path,
        origin="chat",
        thread_id=message.thread_id,
        model=model,
        run_id=run_id,
        sandbox=sandbox,
        raw_input=message.text,
        message=message,
        build_prompt=lambda: build_chat_prompt(
            prepared,
            thunk,
            history_messages=history_messages,
            message=message,
            model=model,
            sandbox=sandbox,
        ),
    )
    assistant_message = chat_store.append_message(
        agent_uri=prepared.ref.uri,
        agent_id=prepared.ref.id[:12],
        agent_name=prepared.ref.name,
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
    sandbox: str,
    raw_input: str | None,
    build_prompt,
    run_id: str | None = None,
    message: Message | None = None,
) -> _TrackedTurnResult:
    bus = BusStore(bus_db_path)
    resolved_run_id = run_id or uuid.uuid4().hex
    summary = _summary(prepared.ref.name, thunk)
    now = utc_now()
    trace_path = agent_run_prompt_path(
        prepared.ref.home,
        prepared.ref.name,
        resolved_run_id,
    )
    bus.append(
        RunStarted(
            at=now,
            agent_uri=prepared.ref.uri,
            agent_id=prepared.ref.id[:12],
            run_id=resolved_run_id,
            run_type="turn",
            origin=origin,
            summary=summary,
            thunk_name=thunk.name,
            thread_id=thread_id,
        )
    )

    prompt_trace: PromptTrace | None = None
    try:
        prompt_build = build_prompt()
        prompt_trace = _prompt_trace(
            prepared,
            run_id=resolved_run_id,
            thunk=thunk,
            origin=origin,
            thread_id=thread_id,
            sandbox=sandbox,
            build=prompt_build,
        )
        prompt_trace.save(trace_path)
        output = execute_prompt_build(prompt_build)
    except Exception as exc:
        if prompt_trace is None:
            prompt_trace = _prompt_trace(
                prepared,
                run_id=resolved_run_id,
                thunk=thunk,
                origin=origin,
                thread_id=thread_id,
                sandbox=sandbox,
                build=None,
                model=model,
                raw_input=raw_input,
                message=message,
            )
        prompt_trace.error = str(exc)
        prompt_trace.save(trace_path)
        bus.append(
            RunFailed(
                at=utc_now(),
                agent_uri=prepared.ref.uri,
                agent_id=prepared.ref.id[:12],
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

    prompt_trace.response_text = output
    prompt_trace.save(trace_path)
    bus.append(
        RunFinished(
            at=utc_now(),
            agent_uri=prepared.ref.uri,
            agent_id=prepared.ref.id[:12],
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


def _prompt_trace(
    prepared: PreparedAgent,
    *,
    run_id: str,
    thunk: Thunk,
    origin: RunOrigin,
    thread_id: str | None,
    sandbox: str,
    build: PromptBuild | None,
    model: str | None = None,
    raw_input: str | None = None,
    message: Message | None = None,
) -> PromptTrace:
    if build is not None:
        prompt_model = build.model
        trace_raw_input = build.raw_input
        trace_expanded_input = build.expanded_input
        trace_message_context = build.message_context
        trace_runtime_context = build.runtime_context
        trace_developer_message = build.developer_message
        trace_messages = build.messages
        trace_source_text = build.source_text
    else:
        prompt_data = build_prompt_error_trace_data(
            prepared,
            thunk,
            origin=origin,
            thread_id=thread_id,
            sandbox=sandbox,
            model=model,
            raw_input=raw_input,
            message=message,
        )
        prompt_model = str(prompt_data["model"])
        trace_raw_input = prompt_data["raw_input"]
        trace_expanded_input = prompt_data["expanded_input"]
        trace_message_context = prompt_data["message_context"]
        trace_runtime_context = dict(prompt_data["runtime_context"])
        trace_developer_message = str(prompt_data["developer_message"])
        trace_messages = list(prompt_data["messages"])
        trace_source_text = str(prompt_data["source_text"])
    return PromptTrace(
        run_id=run_id,
        created_at=datetime.now(timezone.utc),
        agent_uri=prepared.ref.uri,
        agent_id=prepared.ref.id,
        agent_name=prepared.ref.name,
        source_file=str(prepared.source_path),
        working_directory=str(prepared.ref.home),
        thunk_name=thunk.name,
        origin=origin,
        thread_id=thread_id,
        sandbox=sandbox,
        cap_scopes=list(prepared.cap_scopes.labels()),
        model=prompt_model,
        raw_input=trace_raw_input,
        expanded_input=trace_expanded_input,
        message_context=trace_message_context,
        runtime_context=trace_runtime_context,
        developer_message=trace_developer_message,
        messages=trace_messages,
        source_text=trace_source_text,
    )

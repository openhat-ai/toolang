from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import uuid

from toolang.agent.prepared import PreparedAgent
from toolang.bus.db import BusStore
from toolang.bus.events import RunFailed, RunFinished, RunOrigin, RunStarted, utc_now
from toolang.concepts.execution import MessageSender, RunKind, thread_group_for_origin
from toolang.concepts.layout import AgentHome
from toolang.concepts.messages import TextPart, TurnMessage
from toolang.concepts.persisted import ToolsConfig
from toolang.concepts.persisted.prompt_trace import PromptTrace
from toolang.concepts.tools import ToolCallResult
from toolang.program.ast import Thunk
from toolang.tools import ToolRuntime, create_tool_runtime

from .build import (
    PromptBuild,
    build_chat_prompt,
    build_invoke_prompt,
    build_prompt_error_trace_data,
)
from .chat_protocol import TurnMessageBuilder, build_assistant_turn_message
from .chats import ChatMessage, ChatStore
from .execution_store import ExecutionStore
from .messages import Message
from .model_exec import (
    ModelExecutionEventHandler,
    execute_prompt_build,
    execute_prompt_build_stream,
)


@dataclass(frozen=True, slots=True)
class InvokeResult:
    run_id: str
    output: str


@dataclass(frozen=True, slots=True)
class ChatResult:
    run_id: str
    output: str
    assistant: ChatMessage


@dataclass(frozen=True, slots=True)
class RuntimeExecutionContext:
    """Execution truth-layer context for turns running inside one long-lived run."""

    store: ExecutionStore
    run_id: str


def invoke_prepared_agent(
    prepared: PreparedAgent,
    thunk: Thunk,
    *,
    bus_db_path: Path,
    user_input: str | None,
    model: str | None = None,
    origin: RunOrigin = "invoke",
    thread_id: str | None = None,
    sender: MessageSender = "owner",
    sandbox: str = "host",
    execution_store: ExecutionStore | None = None,
    process_run_id: str | None = None,
    input_meta: dict[str, object] | None = None,
) -> InvokeResult:
    output = _tracked_turn(
        prepared=prepared,
        thunk=thunk,
        bus_db_path=bus_db_path,
        origin=origin,
        thread_id=thread_id,
        sender=sender,
        model=model,
        sandbox=sandbox,
        raw_input=user_input,
        run_kind="invoke" if execution_store is None else None,
        execution_context=(
            RuntimeExecutionContext(store=execution_store, run_id=process_run_id)
            if execution_store is not None and process_run_id is not None
            else None
        ),
        build_prompt=lambda tool_runtime: build_invoke_prompt(
            prepared,
            thunk,
            user_input=user_input,
            model=model,
            origin=origin,
            thread_id=thread_id,
            sandbox=sandbox,
            input_meta=input_meta,
            tool_runtime=tool_runtime,
        ),
        input_meta=input_meta,
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
    execution_store: ExecutionStore | None = None,
    process_run_id: str | None = None,
    run_id: str | None = None,
    stream_event: ModelExecutionEventHandler | None = None,
) -> ChatResult:
    turn_run_id = run_id or uuid.uuid4().hex
    user_message_id = f"{turn_run_id}:user"
    assistant_message_id = f"{turn_run_id}:assistant"

    chat_store.append_message(
        agent_uri=prepared.ref.uri,
        agent_id=prepared.ref.id[:12],
        agent_name=prepared.ref.name,
        thread_id=message.thread_id,
        turn_id=turn_run_id,
        role="user",
        origin=message.origin,
        channel=message.channel,
        sender=message.sender,
        text=message.text,
        message=TurnMessage(
            id=user_message_id,
            role="user",
            parts=(TextPart(id=f"{user_message_id}:text:1", text=message.text),),
            created_at=utc_now(),
            metadata=dict(message.meta),
        ),
        meta=dict(message.meta),
    )
    history_messages = chat_store.recent_openai_messages(thread_id=message.thread_id, limit=20)

    assistant_builder = (
        TurnMessageBuilder(
            message_id=assistant_message_id,
        )
        if stream_event is not None
        else None
    )

    def _stream_and_build(event) -> None:
        if assistant_builder is not None:
            assistant_builder.apply_model_event(event)
        if stream_event is not None:
            stream_event(event)

    tracked = _tracked_turn(
        prepared=prepared,
        thunk=thunk,
        bus_db_path=bus_db_path,
        origin="chat",
        thread_id=message.thread_id,
        sender=message.sender,
        model=model,
        run_id=turn_run_id,
        sandbox=sandbox,
        raw_input=message.text,
        message=message,
        execution_context=(
            RuntimeExecutionContext(store=execution_store, run_id=process_run_id)
            if execution_store is not None and process_run_id is not None
            else None
        ),
        build_prompt=lambda tool_runtime: build_chat_prompt(
            prepared,
            thunk,
            history_messages=history_messages,
            message=message,
            model=model,
            sandbox=sandbox,
            tool_runtime=tool_runtime,
        ),
        stream_event=_stream_and_build if stream_event is not None else None,
    )
    assistant_turn_message = (
        assistant_builder.build()
        if assistant_builder is not None
        else build_assistant_turn_message(
            message_id=assistant_message_id,
            output_text=tracked.output,
            tool_calls=tracked.tool_calls,
        )
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
        message=assistant_turn_message,
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
    tool_calls: list[ToolCallResult]


def _tracked_turn(
    *,
    prepared: PreparedAgent,
    thunk: Thunk,
    bus_db_path: Path,
    origin: RunOrigin,
    thread_id: str | None,
    sender: MessageSender,
    model: str | None,
    sandbox: str,
    raw_input: str | None,
    build_prompt,
    run_id: str | None = None,
    message: Message | None = None,
    run_kind: RunKind | None = None,
    execution_context: RuntimeExecutionContext | None = None,
    input_meta: dict[str, object] | None = None,
    stream_event: ModelExecutionEventHandler | None = None,
) -> _TrackedTurnResult:
    bus = BusStore(bus_db_path)
    resolved_run_id = run_id or uuid.uuid4().hex
    effective_thread_id = thread_id or f"{origin}:{resolved_run_id}"
    process_run_id = (
        execution_context.run_id
        if execution_context is not None
        else uuid.uuid4().hex if run_kind is not None else None
    )
    summary = _summary(prepared.ref.name, thunk)
    now = utc_now()
    execution = execution_context.store if execution_context is not None else (
        ExecutionStore(AgentHome.resolve(prepared.ref.home).room(prepared.ref.name).execution_db_path)
        if run_kind is not None
        else None
    )
    home = AgentHome.resolve(prepared.ref.home)
    room = home.room(prepared.ref.name)
    trace_path = room.prompt_trace_path(resolved_run_id)
    tools_config = (
        ToolsConfig.load(home.tools_config_path)
        if home.tools_config_path.exists()
        else ToolsConfig()
    )
    tool_runtime = create_tool_runtime(
        prepared.ref,
        sandbox=sandbox,
        tools_config=tools_config,
        working_directory=prepared.ref.home,
    )
    prompt_trace: PromptTrace | None = None
    try:
        if execution is not None:
            assert process_run_id is not None
            if execution_context is None:
                assert run_kind is not None
                execution.begin_run(
                    agent=prepared.ref,
                    run_id=process_run_id,
                    run_kind=run_kind,
                    sandbox=sandbox,
                    cap_scopes=prepared.cap_scopes.labels(),
                )
            execution.ensure_thread(
                agent=prepared.ref,
                thread_id=effective_thread_id,
                thread_group=thread_group_for_origin(origin),
                title=message.text if message is not None else raw_input,
                at=now,
            )
            execution.start_turn(
                turn_id=resolved_run_id,
                run_id=process_run_id,
                thread_id=effective_thread_id,
                origin=origin,
                channel=message.channel if message is not None else None,
                sender=message.sender if message is not None else sender,
                execution_strategy="direct",
                input_text=raw_input,
                at=now,
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

        prompt_build = build_prompt(tool_runtime)
        if execution is not None:
            execution.append_step(
                turn_id=resolved_run_id,
                step_kind="prompt_build",
                status="finished",
                input_json={
                    "thunk_name": thunk.name,
                    "origin": origin,
                    "thread_id": effective_thread_id,
                    "raw_input": raw_input,
                },
                output_json={
                    "model": prompt_build.model,
                    "message_count": len(prompt_build.messages),
                },
            )
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
        execution_result = (
            execute_prompt_build_stream(prompt_build, on_event=stream_event)
            if stream_event is not None
            else execute_prompt_build(prompt_build)
        )
        if isinstance(execution_result, str):
            output = execution_result
            tool_calls = []
        else:
            output = execution_result.output_text
            tool_calls = execution_result.tool_calls
        if execution is not None:
            for tool_call in tool_calls:
                execution.append_step(
                    turn_id=resolved_run_id,
                    step_kind="tool_call",
                    status="failed" if tool_call.error is not None else "finished",
                    input_json={
                        "family": tool_call.family,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                    output_json=tool_call.output,
                    error=tool_call.error,
                )
            execution.append_step(
                turn_id=resolved_run_id,
                step_kind="model_call",
                status="finished",
                input_json={
                    "model": prompt_build.model,
                    "message_count": len(prompt_build.messages),
                },
                output_json={
                    "output_length": len(output),
                    "tool_call_count": len(tool_calls),
                },
            )

        prompt_trace.tool_calls = [
            {
                "family": item.family,
                "name": item.name,
                "arguments": item.arguments,
                "output": item.output,
                "error": item.error,
            }
            for item in tool_calls
        ]
        prompt_trace.response_text = output
        prompt_trace.save(trace_path)
        if execution is not None:
            assert process_run_id is not None
            execution.finish_turn(turn_id=resolved_run_id, output_text=output)
            if execution_context is None:
                execution.finish_run(run_id=process_run_id, status="finished")
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
        return _TrackedTurnResult(
            run_id=resolved_run_id,
            output=output,
            tool_calls=tool_calls,
        )
    except Exception as exc:
        if execution is not None:
            step_kind = "prompt_build" if prompt_trace is None else "model_call"
            execution.append_step(
                turn_id=resolved_run_id,
                step_kind=step_kind,
                status="failed",
                input_json={
                    "thunk_name": thunk.name,
                    "origin": origin,
                    "thread_id": effective_thread_id,
                    "raw_input": raw_input,
                }
                if step_kind == "prompt_build"
                else {
                    "model": model or "",
                    "thread_id": effective_thread_id,
                },
                output_json={},
                error=str(exc),
            )
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
                input_meta=input_meta,
                tool_runtime=tool_runtime,
            )
        prompt_trace.error = str(exc)
        prompt_trace.save(trace_path)
        if execution is not None:
            assert process_run_id is not None
            execution.fail_turn(turn_id=resolved_run_id, error=str(exc))
            if execution_context is None:
                execution.finish_run(run_id=process_run_id, status="failed")
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
        raise
    finally:
        if execution is not None and execution_context is None:
            execution.close()
        bus.close()


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
    input_meta: dict[str, object] | None = None,
    tool_runtime: ToolRuntime | None = None,
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
        trace_tool_calls = []
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
            input_meta=input_meta,
            tool_runtime=tool_runtime,
        )
        prompt_model = str(prompt_data["model"])
        trace_raw_input = prompt_data["raw_input"]
        trace_expanded_input = prompt_data["expanded_input"]
        trace_message_context = prompt_data["message_context"]
        trace_runtime_context = dict(prompt_data["runtime_context"])
        trace_developer_message = str(prompt_data["developer_message"])
        trace_messages = list(prompt_data["messages"])
        trace_source_text = str(prompt_data["source_text"])
        trace_tool_calls = []
    return PromptTrace(
        run_id=run_id,
        created_at=datetime.now(timezone.utc),
        agent_uri=prepared.ref.uri,
        agent_id=prepared.ref.id,
        agent_name=prepared.ref.name,
        source_file=str(prepared.ref.source),
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
        tool_calls=trace_tool_calls,
    )

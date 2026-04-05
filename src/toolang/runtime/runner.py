"""Run execution pipeline for one prepared snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import uuid

from toolang.agent.prepared import PreparedAgent
from toolang.bus.db import BusStore
from toolang.bus.events import RunFailed, RunFinished, RunOrigin, RunStarted, utc_now
from toolang.caps import load_prepared_caps
from toolang.concepts.execution import (
    ActivationKind,
    MessageSender,
    RunMessageRecord,
    thread_group_for_origin,
)
from toolang.concepts.layout import AgentHome
from toolang.concepts.messages import TextPart, TurnMessage
from toolang.concepts.persisted.prompt_trace import PromptTrace
from toolang.concepts.tools import ToolCallResult
from toolang.program.ast import Thunk
from toolang.tools import ToolRuntime, create_tool_runtime

from .assembly import (
    PromptBundle,
    assemble_chat_prompt,
    assemble_invoke_prompt,
    assemble_prompt_error_trace_data,
)
from .chat_protocol import TurnMessageBuilder, build_assistant_turn_message
from .execution_store import ExecutionStore
from .messages import Message
from .model_exec import (
    ModelExecutionEventHandler,
    execute_prompt_bundle,
    execute_prompt_bundle_stream,
)


@dataclass(frozen=True, slots=True)
class InvokeResult:
    run_id: str
    output: str


@dataclass(frozen=True, slots=True)
class ChatResult:
    run_id: str
    output: str
    assistant: RunMessageRecord


@dataclass(frozen=True, slots=True)
class RuntimeExecutionContext:
    """Execution truth-layer context for runs inside one long-lived activation."""

    store: ExecutionStore
    activation_id: str


@dataclass(frozen=True, slots=True)
class _TrackedRunResult:
    run_id: str
    output: str
    tool_calls: list[ToolCallResult]


class Runner:
    """Run pipeline executor for one prepared snapshot."""

    def __init__(
        self,
        prepared: PreparedAgent,
        *,
        bus_db_path: Path,
        sandbox: str,
        execution_store: ExecutionStore | None = None,
        activation_id: str | None = None,
    ) -> None:
        if execution_store is not None and activation_id is None:
            raise RuntimeError("Runner requires activation_id with execution_store")
        self.prepared = prepared
        self.bus_db_path = bus_db_path
        self.sandbox = sandbox
        self.execution_context = (
            RuntimeExecutionContext(store=execution_store, activation_id=activation_id)
            if execution_store is not None and activation_id is not None
            else None
        )

    def invoke(
        self,
        thunk: Thunk,
        *,
        user_input: str | None,
        model: str | None = None,
        origin: RunOrigin = "invoke",
        thread_id: str | None = None,
        sender: MessageSender = "owner",
        input_meta: dict[str, object] | None = None,
    ) -> InvokeResult:
        """Execute one non-chat run."""

        resolved_run_id = uuid.uuid4().hex
        effective_thread_id = thread_id or f"{origin}:{resolved_run_id}"
        user_created_at = utc_now()
        transcript_store, owns_transcript_store = self._transcript_store()
        try:
            tracked = self._tracked_run(
                thunk=thunk,
                origin=origin,
                thread_id=effective_thread_id,
                sender=sender,
                model=model,
                run_id=resolved_run_id,
                raw_input=user_input,
                activation_kind="invoke" if self.execution_context is None else None,
                build_prompt=lambda tool_runtime: assemble_invoke_prompt(
                    self.prepared,
                    thunk,
                    user_input=user_input,
                    model=model,
                    origin=origin,
                    thread_id=effective_thread_id,
                    sandbox=self.sandbox,
                    input_meta=input_meta,
                    tool_runtime=tool_runtime,
                ),
                input_meta=input_meta,
            )
        except Exception:
            if (
                user_input is not None
                and transcript_store.get_run(run_id=resolved_run_id) is not None
            ):
                _append_user_transcript(
                    transcript_store,
                    thread_id=effective_thread_id,
                    run_id=resolved_run_id,
                    origin=origin,
                    channel=None,
                    sender=sender,
                    text=user_input,
                    meta=input_meta,
                    at=user_created_at,
                )
            if owns_transcript_store:
                transcript_store.close()
            raise

        if user_input is not None:
            _append_user_transcript(
                transcript_store,
                thread_id=effective_thread_id,
                run_id=resolved_run_id,
                origin=origin,
                channel=None,
                sender=sender,
                text=user_input,
                meta=input_meta,
                at=user_created_at,
            )
        if tracked.output or tracked.tool_calls:
            _append_assistant_transcript(
                transcript_store,
                thread_id=effective_thread_id,
                run_id=resolved_run_id,
                origin=origin,
                channel=None,
                text=tracked.output,
                message=build_assistant_turn_message(
                    message_id=f"{resolved_run_id}:assistant",
                    output_text=tracked.output,
                    tool_calls=tracked.tool_calls,
                    created_at=utc_now(),
                ),
                at=utc_now(),
            )
        if owns_transcript_store:
            transcript_store.close()
        return InvokeResult(run_id=tracked.run_id, output=tracked.output)

    def chat(
        self,
        thunk: Thunk,
        *,
        message: Message,
        model: str | None = None,
        run_id: str | None = None,
        stream_event: ModelExecutionEventHandler | None = None,
    ) -> ChatResult:
        """Execute one chat run."""

        if self.execution_context is None:
            raise RuntimeError("Runner.chat requires execution context")
        resolved_run_id = run_id or uuid.uuid4().hex
        assistant_message_id = f"{resolved_run_id}:assistant"
        user_created_at = utc_now()
        history_messages = [
            *self.execution_context.store.recent_openai_messages(
                thread_id=message.thread_id,
                limit=19,
            ),
            {"role": "user", "content": message.text},
        ]
        assistant_builder = (
            TurnMessageBuilder(message_id=assistant_message_id)
            if stream_event is not None
            else None
        )

        def _stream_and_build(event) -> None:
            if assistant_builder is not None:
                assistant_builder.apply_model_event(event)
            if stream_event is not None:
                stream_event(event)

        try:
            tracked = self._tracked_run(
                thunk=thunk,
                origin="chat",
                thread_id=message.thread_id,
                sender=message.sender,
                model=model,
                run_id=resolved_run_id,
                raw_input=message.text,
                message=message,
                build_prompt=lambda tool_runtime: assemble_chat_prompt(
                    self.prepared,
                    thunk,
                    history_messages=history_messages,
                    message=message,
                    model=model,
                    sandbox=self.sandbox,
                    tool_runtime=tool_runtime,
                ),
                stream_event=_stream_and_build if stream_event is not None else None,
            )
        except Exception:
            if self.execution_context.store.get_run(run_id=resolved_run_id) is not None:
                _append_user_transcript(
                    self.execution_context.store,
                    thread_id=message.thread_id,
                    run_id=resolved_run_id,
                    origin=message.origin,
                    channel=message.channel,
                    sender=message.sender,
                    text=message.text,
                    meta=message.meta,
                    at=user_created_at,
                )
            raise

        _append_user_transcript(
            self.execution_context.store,
            thread_id=message.thread_id,
            run_id=resolved_run_id,
            origin=message.origin,
            channel=message.channel,
            sender=message.sender,
            text=message.text,
            meta=message.meta,
            at=user_created_at,
        )
        assistant_turn_message = (
            assistant_builder.build()
            if assistant_builder is not None
            else build_assistant_turn_message(
                message_id=assistant_message_id,
                output_text=tracked.output,
                tool_calls=tracked.tool_calls,
                created_at=utc_now(),
            )
        )
        assistant_message = _append_assistant_transcript(
            self.execution_context.store,
            thread_id=message.thread_id,
            run_id=tracked.run_id,
            origin=message.origin,
            channel=message.channel,
            text=tracked.output,
            message=assistant_turn_message,
            at=utc_now(),
        )
        return ChatResult(
            run_id=tracked.run_id,
            output=tracked.output,
            assistant=assistant_message,
        )

    def _tracked_run(
        self,
        *,
        thunk: Thunk,
        origin: RunOrigin,
        thread_id: str | None,
        sender: MessageSender,
        model: str | None,
        run_id: str | None,
        raw_input: str | None,
        build_prompt,
        message: Message | None = None,
        activation_kind: ActivationKind | None = None,
        input_meta: dict[str, object] | None = None,
        stream_event: ModelExecutionEventHandler | None = None,
    ) -> _TrackedRunResult:
        bus = BusStore(self.bus_db_path)
        resolved_run_id = run_id or uuid.uuid4().hex
        effective_thread_id = thread_id or f"{origin}:{resolved_run_id}"
        activation_id = (
            self.execution_context.activation_id
            if self.execution_context is not None
            else uuid.uuid4().hex if activation_kind is not None else None
        )
        summary = _summary(self.prepared.ref.name, thunk)
        now = utc_now()
        execution = (
            self.execution_context.store
            if self.execution_context is not None
            else (
                ExecutionStore(
                    AgentHome.resolve(self.prepared.ref.home)
                    .room(self.prepared.ref.name)
                    .execution_db_path
                )
                if activation_kind is not None
                else None
            )
        )
        room = AgentHome.resolve(self.prepared.ref.home).room(self.prepared.ref.name)
        trace_path = room.prompt_trace_path(resolved_run_id)
        visible_caps = load_prepared_caps(self.prepared)
        tool_runtime = create_tool_runtime(
            self.prepared.ref,
            sandbox=self.sandbox,
            working_directory=self.prepared.ref.home,
            visible_services=[
                item.service_catalog_item() for item in visible_caps.services
            ],
        )
        prompt_trace: PromptTrace | None = None
        try:
            if execution is not None:
                assert activation_id is not None
                if self.execution_context is None:
                    assert activation_kind is not None
                    execution.begin_activation(
                        agent=self.prepared.ref,
                        activation_id=activation_id,
                        activation_kind=activation_kind,
                        sandbox=self.sandbox,
                        cap_scopes=self.prepared.cap_scopes.labels(),
                    )
                execution.ensure_thread(
                    agent=self.prepared.ref,
                    thread_id=effective_thread_id,
                    thread_group=thread_group_for_origin(origin),
                    title=message.text if message is not None else raw_input,
                    at=now,
                )
                execution.start_run(
                    run_id=resolved_run_id,
                    activation_id=activation_id,
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
                    agent_uri=self.prepared.ref.uri,
                    agent_id=self.prepared.ref.id[:12],
                    run_id=resolved_run_id,
                    run_type="run",
                    origin=origin,
                    summary=summary,
                    thunk_name=thunk.name,
                    thread_id=effective_thread_id,
                )
            )

            prompt_bundle = build_prompt(tool_runtime)
            if execution is not None:
                execution.append_step(
                    run_id=resolved_run_id,
                    step_kind="prompt_build",
                    status="finished",
                    input_json={
                        "thunk_name": thunk.name,
                        "origin": origin,
                        "thread_id": effective_thread_id,
                        "raw_input": raw_input,
                    },
                    output_json={
                        "model": prompt_bundle.model,
                        "message_count": len(prompt_bundle.messages),
                    },
                )
            prompt_trace = _prompt_trace(
                self.prepared,
                run_id=resolved_run_id,
                thunk=thunk,
                origin=origin,
                thread_id=effective_thread_id,
                sandbox=self.sandbox,
                bundle=prompt_bundle,
            )
            prompt_trace.save(trace_path)
            execution_result = (
                execute_prompt_bundle_stream(prompt_bundle, on_event=stream_event)
                if stream_event is not None
                else execute_prompt_bundle(prompt_bundle)
            )
            output = execution_result.output_text
            tool_calls = execution_result.tool_calls
            if execution is not None:
                for tool_call in tool_calls:
                    execution.append_step(
                        run_id=resolved_run_id,
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
                    run_id=resolved_run_id,
                    step_kind="model_call",
                    status="finished",
                    input_json={
                        "model": prompt_bundle.model,
                        "message_count": len(prompt_bundle.messages),
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
                assert activation_id is not None
                execution.finish_run(run_id=resolved_run_id, output_text=output)
                if self.execution_context is None:
                    execution.finish_activation(
                        activation_id=activation_id,
                        status="finished",
                    )
            bus.append(
                RunFinished(
                    at=utc_now(),
                    agent_uri=self.prepared.ref.uri,
                    agent_id=self.prepared.ref.id[:12],
                    run_id=resolved_run_id,
                    run_type="run",
                    origin=origin,
                    summary=summary,
                    thunk_name=thunk.name,
                    thread_id=effective_thread_id,
                )
            )
            return _TrackedRunResult(
                run_id=resolved_run_id,
                output=output,
                tool_calls=tool_calls,
            )
        except Exception as exc:
            if execution is not None:
                step_kind = "prompt_build" if prompt_trace is None else "model_call"
                execution.append_step(
                    run_id=resolved_run_id,
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
                    self.prepared,
                    run_id=resolved_run_id,
                    thunk=thunk,
                    origin=origin,
                    thread_id=effective_thread_id,
                    sandbox=self.sandbox,
                    bundle=None,
                    model=model,
                    raw_input=raw_input,
                    message=message,
                    input_meta=input_meta,
                    tool_runtime=tool_runtime,
                )
            prompt_trace.error = str(exc)
            prompt_trace.save(trace_path)
            if execution is not None:
                assert activation_id is not None
                execution.fail_run(run_id=resolved_run_id, error=str(exc))
                if self.execution_context is None:
                    execution.finish_activation(
                        activation_id=activation_id,
                        status="failed",
                    )
            bus.append(
                RunFailed(
                    at=utc_now(),
                    agent_uri=self.prepared.ref.uri,
                    agent_id=self.prepared.ref.id[:12],
                    run_id=resolved_run_id,
                    run_type="run",
                    origin=origin,
                    error=str(exc),
                    thunk_name=thunk.name,
                    thread_id=effective_thread_id,
                )
            )
            raise
        finally:
            if execution is not None and self.execution_context is None:
                execution.close()
            bus.close()

    def _transcript_store(self) -> tuple[ExecutionStore, bool]:
        if self.execution_context is not None:
            return self.execution_context.store, False
        room = AgentHome.resolve(self.prepared.ref.home).room(self.prepared.ref.name)
        return ExecutionStore(room.execution_db_path), True


def _append_user_transcript(
    execution: ExecutionStore,
    *,
    thread_id: str,
    run_id: str,
    origin: RunOrigin,
    channel: str | None,
    sender: MessageSender,
    text: str,
    meta: dict[str, object] | None,
    at: str,
) -> RunMessageRecord:
    user_message_id = f"{run_id}:user"
    return execution.append_message(
        thread_id=thread_id,
        run_id=run_id,
        role="user",
        origin=origin,
        channel=channel,
        sender=sender,
        text=text,
        message=TurnMessage(
            id=user_message_id,
            role="user",
            parts=(TextPart(id=f"{user_message_id}:text:1", text=text),),
            created_at=at,
            metadata=dict(meta or {}),
        ),
        meta=dict(meta or {}),
        at=at,
    )


def _append_assistant_transcript(
    execution: ExecutionStore,
    *,
    thread_id: str,
    run_id: str,
    origin: RunOrigin,
    channel: str | None,
    text: str,
    message: TurnMessage,
    at: str,
) -> RunMessageRecord:
    return execution.append_message(
        thread_id=thread_id,
        run_id=run_id,
        role="assistant",
        origin=origin,
        channel=channel,
        sender="self",
        text=text,
        message=message,
        meta=dict(message.metadata),
        at=at,
    )


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
    bundle: PromptBundle | None,
    model: str | None = None,
    raw_input: str | None = None,
    message: Message | None = None,
    input_meta: dict[str, object] | None = None,
    tool_runtime: ToolRuntime | None = None,
) -> PromptTrace:
    if bundle is not None:
        prompt_model = bundle.model
        trace_raw_input = bundle.raw_input
        trace_expanded_input = bundle.expanded_input
        trace_message_context = bundle.message_context
        trace_runtime_context = bundle.runtime_context
        trace_developer_message = bundle.developer_message
        trace_messages = bundle.messages
        trace_source_text = bundle.source_text
    else:
        prompt_data = assemble_prompt_error_trace_data(
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
        tool_calls=[],
    )

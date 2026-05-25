"""Run-context state and execution operations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import logging
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

from .. import agents
from toolang.base.error import ToolangError
from toolang.base.protocols.model_adapter import ModelAdapter
from toolang.base.protocols.tool import AgentTool
from toolang.base.types.message import (
    Message,
    Part,
    PartType,
    TextDelta,
    TextPart,
    ToolCallDelta,
    ToolCallPart,
    ToolResultPart,
    message_text,
)
from toolang.base.types.model import ModelTarget
from toolang.base.types.run import (
    ModelCall,
    ModelCallResult,
    ModelPartDeltaEvent,
    ModelPartEndEvent,
    ModelPartStartEvent,
    RunResult,
    ToolCall,
    ToolCallResult,
)
from toolang.base.types.tool import ToolContext
from .events import (
    PartDelta,
    PartEnd,
    PartStart,
    StepEnd,
    StepStart,
    TraceEvent,
    TraceEventHandler,
)
from .records import (
    InputRecord,
    ModelCallStepPayload,
    RunInputRef,
    StepInputItem,
    StepOutputRef,
    ToolCallStepPayload,
)
from .snapshot import RunSnapshot

if TYPE_CHECKING:
    from .input import RunInput

_MODEL_LOGGER = logging.getLogger("toolang.run.model")
_TOOL_LOGGER = logging.getLogger("toolang.run.tool")
_LOG_PREVIEW_LIMIT = 2_000


class RunContext:
    """One mutable run context used by one run loop."""

    def __init__(
        self,
        run_input: RunInput,
        model: ModelTarget,
        adapter: ModelAdapter,
        *,
        on_event: TraceEventHandler | None = None,
        consume_inputs: Callable[[str], Sequence[InputRecord]] | None = None,
        stream: bool = False,
    ) -> None:
        self._input = run_input
        self._model = model
        self._adapter = adapter
        self._on_event = on_event
        self._consume_inputs = consume_inputs
        self._stream = stream
        self._snapshot = run_input.snapshot
        self._messages = list(run_input.messages())
        self._state: dict[str, Any] | None = None
        self._output_text = ""
        self._round = 0
        self._step_index = 0
        self._last_step_index: int | None = None
        self._active_model_step_index: int | None = None
        self._active_part_count = 0
        self._text_part_index: int | None = None
        self._tool_part_indexes: dict[str, int] = {}
        self._started_part_indexes: set[int] = set()
        self._tool_call_sources: dict[str, tuple[int, int]] = {}
        self._tool_definitions = tuple(
            tool.definition()
            for tool in sorted(run_input.tools().values(), key=lambda item: item.name)
        ) if model.tools else ()

    @property
    def model(self) -> ModelTarget:
        return self._model

    @property
    def instructions(self) -> str:
        return self._input.instructions()

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    @property
    def on_event(self) -> TraceEventHandler | None:
        return self._on_event

    @property
    def tools(self) -> Mapping[str, AgentTool]:
        return self._input.tools()

    def call_model(self) -> ModelCallResult:
        """Perform one model call and update run state."""

        step_index = self._next_step_index()
        started_at = _utc_now()
        consumed_inputs = self._consume_pending_inputs()
        step_input = (*self._model_step_input(), *(RunInputRef(index=item.index) for item in consumed_inputs))
        self._start_model_step(step_index)
        self._emit(
            StepStart(
                run_id=self._input.run.run_id,
                thread_id=self._input.run.thread_id,
                step_index=step_index,
                kind="model_call",
                input=step_input,
                started_at=started_at,
                instructions=self._input.instructions(),
            )
        )
        request = ModelCall(
            instructions=self._input.instructions(),
            messages=list(self._messages),
            tools=self._tool_definitions,
            state=self._state,
        )
        _MODEL_LOGGER.info(
            "model call target ref=%s provider=%s model=%s adapter=%s%s",
            self._model.ref,
            self._model.provider,
            self._model.model,
            self._model.adapter,
            f" base_url={self._model.base_url}" if self._model.base_url else "",
        )
        _log_model_request(request)
        if self._stream and self._on_event is not None and self._model.streaming:
            current = self._adapter.stream(
                self._model,
                request,
                on_event=self._handle_model_event,
            )
        else:
            current = self._adapter.invoke(self._model, request)
        return self._apply_model_response(
            current,
            step_index=step_index,
            started_at=started_at,
        )

    def call_tool(self, call: ToolCall) -> ToolCallResult:
        """Perform one tool call and update run state."""

        step_index = self._next_step_index()
        started_at = _utc_now()
        source = self._tool_call_sources.get(call.tool_call_id)
        step_input: tuple[StepInputItem, ...]
        if source is not None:
            step_input = (
                StepOutputRef(
                    step_index=source[0],
                    part_index=source[1],
                ),
            )
        elif self._last_step_index is not None:
            step_input = (StepOutputRef(step_index=self._last_step_index),)
        else:
            step_input = (RunInputRef(),)
        _log_tool_call_input(call)
        self._emit(
            StepStart(
                run_id=self._input.run.run_id,
                thread_id=self._input.run.thread_id,
                step_index=step_index,
                kind="tool_call",
                input=step_input,
                started_at=started_at,
            )
        )
        record = _invoke_tool_call(
            run_id=self._input.run.run_id,
            tools=self._input.tools(),
            snapshot=self._snapshot,
            call=call,
        )
        part = ToolResultPart(
            tool_call_id=record.tool_call_id,
            call_id=record.call_id,
            tool_name=record.name,
            tool_family=record.name,
            output=dict(record.output),
        )
        self._emit(
            PartStart(
                run_id=self._input.run.run_id,
                thread_id=self._input.run.thread_id,
                step_index=step_index,
                part_index=0,
                kind=part.type,
            )
        )
        self._emit(
            PartEnd(
                run_id=self._input.run.run_id,
                thread_id=self._input.run.thread_id,
                step_index=step_index,
                part_index=0,
                data=part,
            )
        )
        status = "failed" if record.error else "finished"
        _log_tool_call_output(record)
        self._emit(
            StepEnd(
                run_id=self._input.run.run_id,
                thread_id=self._input.run.thread_id,
                step_index=step_index,
                kind="tool_call",
                status=status,
                output=(part,),
                payload=ToolCallStepPayload(),
                started_at=started_at,
                finished_at=_utc_now(),
                error=record.error,
            )
        )
        self._messages.append(_tool_followup_message(record))
        self._last_step_index = step_index
        return record

    def call_tools(self, calls: Sequence[ToolCall]) -> tuple[ToolCallResult, ...]:
        """Perform a batch of tool calls in sequence."""

        return tuple(self.call_tool(call) for call in calls)

    def has_pending_inputs(self) -> bool:
        """Return whether unconsumed steering inputs are waiting for this run."""

        return bool(self._pending_inputs())

    def finish(self) -> RunResult:
        """Finalize one run result from accumulated state."""

        message = Message.assistant(self._output_text) if self._output_text else None
        return RunResult(message=message, output_text=self._output_text)

    def _apply_model_response(
        self,
        current: ModelCallResult,
        *,
        step_index: int,
        started_at: str,
    ) -> ModelCallResult:
        parsed_calls = tuple(current.tool_calls)
        current_text = message_text(current.message.parts) if current.message is not None else ""
        self._output_text = current_text
        _log_model_result(current)
        output_parts = self._model_output_parts(current=current, tool_calls=parsed_calls)
        for part_index, part in output_parts:
            self._emit_part_start(step_index=step_index, part_index=part_index, kind=part.type)
            self._emit(
                PartEnd(
                    run_id=self._input.run.run_id,
                    thread_id=self._input.run.thread_id,
                    step_index=step_index,
                    part_index=part_index,
                    data=part,
                )
            )
            if isinstance(part, ToolCallPart):
                self._tool_call_sources[part.tool_call_id] = (step_index, part_index)
        output = tuple(part for _, part in sorted(output_parts, key=lambda item: item[0]))
        if current.message is not None:
            self._messages.append(current.message)
        self._state = current.state
        self._round += 1
        self._emit(
            StepEnd(
                run_id=self._input.run.run_id,
                thread_id=self._input.run.thread_id,
                step_index=step_index,
                kind="model_call",
                status="finished",
                output=output,
                payload=ModelCallStepPayload(
                    model_ref=self._model.ref,
                    input_tokens=current.usage.input_tokens if current.usage is not None else 0,
                    output_tokens=current.usage.output_tokens if current.usage is not None else 0,
                    provider=self._model.provider,
                    model=self._model.model,
                    adapter=self._model.adapter,
                    base_url=self._model.base_url,
                ),
                started_at=started_at,
                finished_at=_utc_now(),
            )
        )
        self._last_step_index = step_index
        self._active_model_step_index = None
        return current

    def _handle_model_event(self, event: object) -> None:
        step_index = self._active_model_step_index
        if step_index is None:
            return
        if isinstance(event, ModelPartStartEvent):
            if event.kind == "text":
                self._emit_part_start(
                    step_index=step_index,
                    part_index=self._ensure_text_part_index(),
                    kind="text",
                )
            return
        if isinstance(event, ModelPartDeltaEvent):
            if isinstance(event.delta, TextDelta):
                part_index = self._ensure_text_part_index()
                self._emit_part_start(step_index=step_index, part_index=part_index, kind="text")
                if event.delta.text:
                    self._emit(
                        PartDelta(
                            run_id=self._input.run.run_id,
                            thread_id=self._input.run.thread_id,
                            step_index=step_index,
                            part_index=part_index,
                            delta=event.delta,
                        )
                    )
                return
            if isinstance(event.delta, ToolCallDelta):
                part_index = self._ensure_tool_part_index(event.delta.tool_call_id)
                self._emit_part_start(
                    step_index=step_index,
                    part_index=part_index,
                    kind="tool_call",
                )
                if event.delta.text:
                    self._emit(
                        PartDelta(
                            run_id=self._input.run.run_id,
                            thread_id=self._input.run.thread_id,
                            step_index=step_index,
                            part_index=part_index,
                            delta=event.delta,
                        )
                    )
                return
        if isinstance(event, ModelPartEndEvent):
            return

    def _model_output_parts(
        self,
        *,
        current: ModelCallResult,
        tool_calls: Sequence[ToolCall],
    ) -> list[tuple[int, Part]]:
        items: list[tuple[int, Part]] = []
        seen_tool_calls: set[str] = set()
        saw_text = False
        message = current.message
        if message is not None and message.role == "assistant":
            for part in message.parts:
                if isinstance(part, TextPart):
                    part_index = self._ensure_text_part_index()
                    items.append((part_index, part))
                    saw_text = True
                    continue
                if isinstance(part, ToolCallPart):
                    part_index = self._ensure_tool_part_index(part.tool_call_id)
                    items.append((part_index, part))
                    seen_tool_calls.add(part.tool_call_id)
        current_text = message_text(message.parts) if message is not None else ""
        if not saw_text and current_text:
            part_index = self._ensure_text_part_index()
            items.append((part_index, TextPart(text=current_text)))
        for call in tool_calls:
            if call.tool_call_id in seen_tool_calls:
                continue
            part_index = self._ensure_tool_part_index(call.tool_call_id)
            items.append(
                (
                    part_index,
                    ToolCallPart(
                        tool_call_id=call.tool_call_id,
                        call_id=call.call_id,
                        tool_name=call.name,
                        tool_family=call.name,
                        input=dict(call.input),
                    ),
                )
            )
        return items

    def _model_step_input(self) -> tuple[StepInputItem, ...]:
        if self._last_step_index is None:
            return (RunInputRef(),)
        return (StepOutputRef(step_index=self._last_step_index),)

    def _consume_pending_inputs(self) -> tuple[InputRecord, ...]:
        inputs = self._pending_inputs()
        for input in inputs:
            if input.action != "steer" or input.message is None:
                continue
            self._messages.append(input.message)
        return inputs

    def _pending_inputs(self) -> tuple[InputRecord, ...]:
        if self._consume_inputs is None:
            return ()
        return tuple(self._consume_inputs(self._input.run.run_id))

    def _start_model_step(self, step_index: int) -> None:
        self._active_model_step_index = step_index
        self._active_part_count = 0
        self._text_part_index = None
        self._tool_part_indexes = {}
        self._started_part_indexes = set()

    def _ensure_text_part_index(self) -> int:
        if self._text_part_index is None:
            self._text_part_index = self._active_part_count
            self._active_part_count += 1
        return self._text_part_index

    def _ensure_tool_part_index(self, tool_call_id: str) -> int:
        part_index = self._tool_part_indexes.get(tool_call_id)
        if part_index is None:
            part_index = self._active_part_count
            self._active_part_count += 1
            self._tool_part_indexes[tool_call_id] = part_index
        return part_index

    def _emit_part_start(self, *, step_index: int, part_index: int, kind: PartType) -> None:
        if part_index in self._started_part_indexes:
            return
        self._started_part_indexes.add(part_index)
        self._emit(
            PartStart(
                run_id=self._input.run.run_id,
                thread_id=self._input.run.thread_id,
                step_index=step_index,
                part_index=part_index,
                kind=kind,
            )
        )

    def _emit(self, event: TraceEvent) -> None:
        if self._on_event is None:
            return
        self._on_event(event)

    def _next_step_index(self) -> int:
        self._step_index += 1
        return self._step_index


def _invoke_tool_call(
    *,
    run_id: str,
    tools: Mapping[str, AgentTool],
    snapshot: RunSnapshot,
    call: ToolCall,
) -> ToolCallResult:
    name = call.name
    tool = tools.get(name)
    if tool is None:
        raise ToolangError(f"unknown tool call: {name or '<empty>'}")
    arguments = dict(call.input)
    try:
        output = tool.invoke(
            arguments,
            _tool_context(
                run_id=run_id,
                snapshot=snapshot,
                tool_name=name,
                tools=tools,
            ),
        )
        error = None
    except Exception as exc:
        output = {}
        error = str(exc)
    return ToolCallResult(
        tool_call_id=call.tool_call_id,
        call_id=call.call_id,
        name=name,
        input=arguments,
        output=output,
        error=error,
    )


def _tool_context(
    *,
    run_id: str,
    snapshot: RunSnapshot,
    tool_name: str,
    tools: Mapping[str, AgentTool],
) -> ToolContext:
    if not snapshot.agent.root:
        raise ToolangError("run snapshot has no agent root")
    if not snapshot.agent.name:
        raise ToolangError("run snapshot has no agent name")
    root = Path(snapshot.agent.root)
    home = (
        Path(snapshot.agent.home)
        if snapshot.agent.home
        else agents.agent_home(root, snapshot.agent.name)
    )
    tool = tools.get(tool_name)
    plugin_name = getattr(tool, "plugin_name", None)
    if not isinstance(plugin_name, str) or not plugin_name:
        raise ToolangError(f"unknown tool plugin for tool: {tool_name}")
    return ToolContext(
        run_id=run_id,
        home=home,
        room=agents.tool_room(root, snapshot.agent.name, plugin_name),
        wd=home,
    )


def _log_model_request(request: ModelCall) -> None:
    if not _MODEL_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _MODEL_LOGGER.debug(
        "model call input instructions=%s messages=%s tools=%s state=%s",
        _preview_text(request.instructions),
        _preview_data([message.to_data() for message in request.messages]),
        _preview_data([tool.name for tool in request.tools]),
        _preview_data(request.state),
    )


def _log_model_result(result: ModelCallResult) -> None:
    if not _MODEL_LOGGER.isEnabledFor(logging.DEBUG):
        return
    usage = None
    if result.usage is not None:
        usage = {
            "input_tokens": result.usage.input_tokens,
            "output_tokens": result.usage.output_tokens,
        }
    _MODEL_LOGGER.debug(
        "model call output message=%s tool_calls=%s usage=%s state=%s",
        _preview_data(result.message.to_data() if result.message is not None else None),
        _preview_data(
            [
                {
                    "tool_call_id": call.tool_call_id,
                    "call_id": call.call_id,
                    "name": call.name,
                    "input": call.input,
                }
                for call in result.tool_calls
            ]
        ),
        _preview_data(usage),
        _preview_data(result.state),
    )


def _log_tool_call_input(call: ToolCall) -> None:
    if not _TOOL_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _TOOL_LOGGER.debug(
        "tool call input name=%s tool_call_id=%s call_id=%s arguments=%s",
        call.name,
        call.tool_call_id,
        call.call_id,
        _preview_data(call.input),
    )


def _log_tool_call_output(result: ToolCallResult) -> None:
    if not _TOOL_LOGGER.isEnabledFor(logging.DEBUG):
        return
    _TOOL_LOGGER.debug(
        "tool call output name=%s tool_call_id=%s call_id=%s output=%s error=%s",
        result.name,
        result.tool_call_id,
        result.call_id,
        _preview_data(result.output),
        result.error or "-",
    )


def _preview_text(value: str) -> str:
    compact = " ".join(value.split())
    if len(compact) <= _LOG_PREVIEW_LIMIT:
        return compact
    return compact[: _LOG_PREVIEW_LIMIT - 3] + "..."


def _preview_data(value: object) -> str:
    if value is None:
        return "-"
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        text = repr(value)
    if len(text) <= _LOG_PREVIEW_LIMIT:
        return text
    return text[: _LOG_PREVIEW_LIMIT - 3] + "..."


def _tool_followup_message(tool_call: ToolCallResult) -> Message:
    meta: dict[str, Any] = {}
    if tool_call.error is not None:
        meta["error"] = tool_call.error
    return Message(
        role="tool",
        parts=(
            ToolResultPart(
                tool_call_id=tool_call.tool_call_id,
                call_id=tool_call.call_id,
                tool_name=tool_call.name,
                tool_family=tool_call.name,
                output=dict(tool_call.output),
            ),
        ),
        meta=meta,
    )


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

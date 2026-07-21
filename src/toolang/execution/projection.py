"""Project durable execution state into caller-facing schemas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import fields
from typing import Any

from toolang.base.types.message import (
    AudioPart,
    FilePart,
    ImagePart,
    Message,
    MessageRole,
    Part,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    message_summary,
)
from .records import (
    CommandRecord,
    InputRef,
    OutputRef,
    RunRecord,
    RunStatus,
    StepKind,
    StepPath,
    StepRecord,
    ThreadPeer,
    ThreadRecord,
    trace_index,
    trace_run,
)
from .schemas import (
    AudioPartData,
    CommandData,
    CommandInfo,
    FailureDetail,
    FilePartData,
    ImagePartData,
    InputDetail,
    InputRefData,
    MessageData,
    MessagePartData,
    MessagePayload,
    OutputRefData,
    RunDetail,
    RunInfo,
    RunOutput,
    StepData,
    StepDetail,
    StepInputData,
    TextPartData,
    ThreadDetail,
    ThreadInfo,
    ThreadPeerInfo,
    ThreadRunInfo,
    ToolCallPartData,
    ToolResultPartData,
)
from .store import RunStore


class ExecutionProjector:
    """Read durable execution truth through one typed projection boundary."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def list_threads(
        self,
        *,
        limit: int | None = 50,
        origin: str | None = None,
        channel: str | None = None,
        status: str | None = None,
    ) -> list[ThreadInfo]:
        """Return filtered thread summaries in most-recently-updated order."""

        runs = self.store.list_runs(limit=None)
        grouped_runs: dict[str, list[RunRecord]] = {}
        for thread_id in sorted({run.thread_id for run in runs}):
            grouped_runs[thread_id] = list(
                self.store.list_thread_runs_chronological(thread_id=thread_id)
            )
        commands_by_run = {
            run.run_id: self.store.list_commands(run_id=run.run_id)
            for thread_runs in grouped_runs.values()
            for run in thread_runs
        }
        thread_records = {item.thread_id: item for item in self.store.list_threads()}
        items = [
            thread_info_from_runs(
                thread_id,
                thread_runs,
                commands_by_run=commands_by_run,
                thread=thread_records.get(thread_id),
            )
            for thread_id, thread_runs in grouped_runs.items()
        ]
        items.extend(
            thread_info_from_record(thread)
            for thread_id, thread in thread_records.items()
            if thread_id not in grouped_runs
        )
        filtered = [
            item
            for item in items
            if (origin is None or item.origin == origin)
            and (channel is None or item.channel == channel)
            and (status is None or item.status == status)
        ]
        ordered = sorted(filtered, key=lambda item: item.updated_at, reverse=True)
        return ordered if limit is None else ordered[:limit]

    def thread_info(self, thread_id: str) -> ThreadInfo | None:
        """Return one thread summary when durable thread truth exists."""

        runs = self.store.list_thread_runs_chronological(thread_id=thread_id)
        thread = self.store.get_thread(thread_id=thread_id)
        if not runs:
            return thread_info_from_record(thread) if thread is not None else None
        commands_by_run = {
            run.run_id: self.store.list_commands(run_id=run.run_id) for run in runs
        }
        return thread_info_from_runs(
            thread_id,
            runs,
            commands_by_run=commands_by_run,
            thread=thread,
        )

    def thread_detail(
        self, thread_id: str, *, limit: int | None = 50
    ) -> ThreadDetail | None:
        """Return one thread and its most recent run details."""

        info = self.thread_info(thread_id)
        if info is None:
            return None
        runs = list(
            self.store.list_thread_runs_chronological(
                thread_id=thread_id,
                limit=None,
            )
        )
        visible_runs = runs if limit is None else runs[-limit:]
        return ThreadDetail(
            **{item.name: getattr(info, item.name) for item in fields(ThreadInfo)},
            runs=[self._run_detail(run, event_cursor=None) for run in visible_runs],
            event_cursor=self.store.latest_event_cursor(
                domain="thread", domain_id=thread_id
            ),
        )

    def list_runs(
        self,
        *,
        limit: int | None = 50,
        thread_id: str | None = None,
        status: RunStatus | None = None,
    ) -> list[RunInfo]:
        """Return run information from durable truth."""

        runs = self.store.list_runs(limit=limit, thread_id=thread_id, status=status)
        steps_by_run = self.store.list_steps_for_runs(
            run_ids=tuple(item.run_id for item in runs)
        )
        return [
            run_info_from_record(
                run,
                inputs=self.store.list_commands(run_id=run.run_id),
                steps=steps_by_run.get(run.run_id, ()),
            )
            for run in runs
        ]

    def run_info(self, run: RunRecord) -> RunInfo:
        """Return one caller-facing run projection."""

        return run_info_from_record(
            run,
            inputs=self.store.list_commands(run_id=run.run_id),
            steps=self.store.list_steps(run_id=run.run_id),
        )

    def run_detail(self, run_id: str) -> RunDetail | None:
        """Return one complete run detail when it exists."""

        run = self.store.get_run(run_id=run_id)
        if run is None:
            return None
        return self._run_detail(
            run,
            event_cursor=self.store.latest_event_cursor(domain="run", domain_id=run_id),
        )

    def run_messages(self, run_id: str) -> list[MessageData] | None:
        """Return one run's caller-facing transcript messages."""

        run = self.store.get_run(run_id=run_id)
        if run is None:
            return None
        return run_message_data(
            run,
            inputs=self.store.list_commands(run_id=run_id),
            steps=self.store.list_steps(run_id=run_id),
        )

    def _run_detail(self, run: RunRecord, *, event_cursor: int | None) -> RunDetail:
        steps = self.store.list_steps(run_id=run.run_id)
        return run_detail_from_record(
            run,
            steps=steps,
            inputs=self.store.list_commands(run_id=run.run_id),
            prompts=_prompt_bodies(self.store, steps),
            event_cursor=event_cursor,
        )


def run_input_message_data(run: RunRecord, input: CommandRecord) -> MessageData:
    """Return one durable run input message projection."""

    if input.input is None:
        raise ValueError(f"run input has no message: {run.id}:{input.index}")
    message = input.input
    meta = dict(message.meta)
    meta.update({"kind": input.kind, "command_index": input.index})
    meta.update(dict(input.context))
    return MessageData(
        id=f"{run.id}:command:{input.index}",
        thread_id=run.thread,
        run_id=run.id,
        step_index=input.index,
        role=message.role,
        parts=[message_part_data(part) for part in message.parts],
        created_at=input.created_at,
        meta=meta,
    )


def run_input_record_message_data(
    run: RunRecord, input: CommandRecord
) -> MessageData | None:
    """Return the caller-facing message for one run input."""

    if input.input is None:
        return None
    return run_input_message_data(run, input)


def step_message_data(run: RunRecord, step: StepRecord) -> MessageData | None:
    """Build one caller-facing message from one durable step."""

    return message_data_for_step(
        step=step.path,
        thread=run.thread,
        kind=step.kind,
        output=step.output,
        created_at=step.finished_at or step.started_at,
        error=step.error,
    )


def run_output_message_data(
    *, run: RunRecord, steps: Sequence[StepRecord]
) -> MessageData | None:
    """Return the final assistant message for one run when present."""

    for step in reversed(steps):
        if step.kind == "model":
            return step_message_data(run, step)
    return None


def run_message_data(
    run: RunRecord,
    *,
    inputs: Sequence[CommandRecord],
    steps: Sequence[StepRecord],
) -> list[MessageData]:
    """Return the derived run transcript projection."""

    messages = [
        message
        for input in inputs
        if (message := run_input_record_message_data(run, input)) is not None
    ]
    for step in steps:
        message = step_message_data(run, step)
        if message is not None:
            messages.append(message)
    return sorted(messages, key=lambda item: item.created_at)


def replay_message(message: MessageData) -> Message:
    """Return one model-history message reconstructed from one projection."""

    return Message(
        role=message.role,
        parts=tuple(message_part_value(part) for part in message.parts),
        meta=dict(message.meta),
    )


def message_payload_data(message: Message) -> MessagePayload:
    """Project one canonical message without runtime identity."""

    return MessagePayload(
        role=message.role,
        parts=[message_part_data(part) for part in message.parts],
        meta=dict(message.meta),
    )


def message_part_data(part: Part) -> MessagePartData:
    """Project one canonical message part into its protocol schema."""

    if isinstance(part, TextPart):
        return TextPartData(text=part.text)
    if isinstance(part, ImagePart):
        return ImagePartData(
            image_url=part.image_url,
            file_id=part.file_id,
            detail=part.detail,
            filename=part.filename,
            media_type=part.media_type,
        )
    if isinstance(part, AudioPart):
        return AudioPartData(
            data=part.data,
            format=part.format,
            filename=part.filename,
            media_type=part.media_type,
        )
    if isinstance(part, FilePart):
        return FilePartData(
            file_data=part.file_data,
            file_url=part.file_url,
            file_id=part.file_id,
            filename=part.filename,
            media_type=part.media_type,
        )
    if isinstance(part, ToolCallPart):
        return ToolCallPartData(
            tool_call_id=part.tool_call_id,
            tool_name=part.tool_name,
            tool_family=part.tool_family,
            input=dict(part.input),
            call_id=part.call_id,
        )
    return ToolResultPartData(
        tool_call_id=part.tool_call_id,
        tool_name=part.tool_name,
        tool_family=part.tool_family,
        output=dict(part.output),
        call_id=part.call_id,
    )


def message_part_value(part: MessagePartData) -> Part:
    """Reconstruct one canonical message part from its protocol schema."""

    if isinstance(part, TextPartData):
        return TextPart(text=part.text)
    if isinstance(part, ImagePartData):
        return ImagePart(
            image_url=part.image_url,
            file_id=part.file_id,
            detail=part.detail,
            filename=part.filename,
            media_type=part.media_type,
        )
    if isinstance(part, AudioPartData):
        return AudioPart(
            data=part.data,
            format=part.format,
            filename=part.filename,
            media_type=part.media_type,
        )
    if isinstance(part, FilePartData):
        return FilePart(
            file_data=part.file_data,
            file_url=part.file_url,
            file_id=part.file_id,
            filename=part.filename,
            media_type=part.media_type,
        )
    if isinstance(part, ToolCallPartData):
        return ToolCallPart(
            tool_call_id=part.tool_call_id,
            tool_name=part.tool_name,
            tool_family=part.tool_family,
            input=dict(part.input),
            call_id=part.call_id,
        )
    return ToolResultPart(
        tool_call_id=part.tool_call_id,
        tool_name=part.tool_name,
        tool_family=part.tool_family,
        output=dict(part.output),
        call_id=part.call_id,
    )


def thread_peer_info(peer: ThreadPeer) -> ThreadPeerInfo:
    """Project one durable thread peer into its protocol schema."""

    return ThreadPeerInfo(type=peer.type, name=peer.name, thread=peer.thread)


def step_data_from_record(step: StepRecord) -> StepData:
    """Project one durable step record into its protocol schema."""

    return StepData(
        parent=step.parent,
        index=step.index,
        kind=step.kind,
        input=[step_input_data(item) for item in step.input],
        output=[message_part_data(part) for part in step.output],
        context=dict(step.context),
        detail=dict(step.detail),
        status=step.status,
        error=step.error,
        created_at=step.created_at,
        started_at=step.started_at,
        finished_at=step.finished_at,
    )


def command_data_from_record(command: CommandRecord) -> CommandData:
    """Project one durable command record into its protocol schema."""

    return CommandData(
        run=command.run,
        index=command.index,
        kind=command.kind,
        apply=command.apply,
        input=(
            message_payload_data(command.input) if command.input is not None else None
        ),
        context=dict(command.context),
        status=command.status,
        error=command.error,
        created_at=command.created_at,
        finished_at=command.finished_at,
    )


def step_input_data(item: InputRef | OutputRef | Message) -> StepInputData:
    """Project one durable step input into its protocol schema."""

    if isinstance(item, InputRef):
        return InputRefData(cmd=item.cmd, part=item.part)
    if isinstance(item, OutputRef):
        return OutputRefData(step=item.step, part=item.part)
    return message_payload_data(item)


def message_data_for_step(
    *,
    step: StepPath,
    thread: str,
    kind: StepKind,
    output: Sequence[Part],
    created_at: str,
    error: str | None = None,
) -> MessageData | None:
    """Build one caller-facing message from one step output."""

    if not output:
        return None
    role = _role_for_step(kind)
    if role is None:
        return None
    meta: dict[str, Any] = {}
    if error is not None:
        meta["error"] = error
    return MessageData(
        id=f"{step}:message",
        thread_id=thread,
        run_id=trace_run(step),
        step_index=trace_index(step) or 0,
        role=role,
        parts=[message_part_data(part) for part in output],
        created_at=created_at,
        meta=meta,
    )


def thread_info_from_runs(
    thread_id: str,
    runs: Sequence[RunRecord],
    *,
    commands_by_run: Mapping[str, Sequence[CommandRecord]],
    thread: ThreadRecord | None = None,
) -> ThreadInfo:
    """Build one thread summary from ordered run records."""

    first = runs[0]
    last = runs[-1]
    active = next((run for run in reversed(runs) if run.status == "running"), None)
    first_input = run_input_message_data(
        first, _start_input(commands_by_run.get(first.run_id, ()))
    )
    title = _message_parts_summary(first_input.parts) or first.origin
    updated_at = last.finished_at or last.started_at
    if thread is not None:
        updated_at = max(updated_at, thread.updated_at)
    return ThreadInfo(
        id=thread_id,
        title=title,
        created_at=thread.created_at if thread is not None else first.created_at,
        origin=last.origin,
        channel=_thread_channel(thread_id, last.origin),
        status=_thread_status(active),
        updated_at=updated_at,
        peer=thread_peer_info(thread.peer if thread is not None else ThreadPeer()),
        parent=thread.parent if thread is not None else None,
        run_count=len(runs),
        latest_run=thread_run_info_from_record(last),
        active_run=thread_run_info_from_record(active) if active is not None else None,
    )


def thread_info_from_record(thread: ThreadRecord) -> ThreadInfo:
    """Build one thread summary from metadata when no runs exist."""

    title = thread.peer.name if thread.peer.type == "agent" else thread.origin
    return ThreadInfo(
        id=thread.thread_id,
        title=title,
        created_at=thread.created_at,
        origin=thread.origin,
        channel=_thread_channel(thread.thread_id, thread.origin),
        status="idle",
        updated_at=thread.updated_at,
        peer=thread_peer_info(thread.peer),
        parent=thread.parent,
        run_count=0,
        latest_run=None,
        active_run=None,
    )


def thread_run_info_from_record(run: RunRecord) -> ThreadRunInfo:
    """Build one compact run summary for thread list projections."""

    return ThreadRunInfo(
        id=run.run_id,
        origin=run.origin,
        status=run.status,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        updated_at=run.finished_at or run.started_at,
    )


def run_info_from_record(
    run: RunRecord,
    *,
    inputs: Sequence[CommandRecord],
    steps: Sequence[StepRecord],
) -> RunInfo:
    """Build one run information projection from durable truth."""

    input_message = run_input_from_records(run, inputs=inputs)
    input_text = (
        _message_parts_summary(input_message.parts) if input_message is not None else ""
    )
    last_step_message = next(
        (
            message
            for step in reversed(steps)
            if (message := step_message_data(run, step)) is not None
        ),
        None,
    )
    summary = (
        _message_parts_summary(last_step_message.parts)
        if last_step_message is not None
        else input_text
    )
    if run.status == "failed" and run.error and (not summary or summary == input_text):
        summary = run.error
    return RunInfo(
        id=run.run_id,
        parent=run.parent,
        origin=run.origin,
        thread_id=run.thread_id,
        root_run_id=run.root_run_id,
        executable_kind=run.executable_kind,
        executable_name=run.executable_name,
        call_kind=run.call_kind,
        metadata=dict(run.metadata),
        input_text=input_text,
        summary=summary,
        status=run.status,
        error=run.error,
        superseded=run.superseded,
        failure=failure_from_steps(status=run.status, error=run.error, steps=steps),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        updated_at=run.finished_at or run.started_at,
    )


def command_info_from_record(run: RunRecord, command: CommandRecord) -> CommandInfo:
    """Build one accepted command projection from durable truth."""

    return CommandInfo(
        run_id=run.run_id,
        index=command.index,
        kind=command.kind,
        apply=command.apply,
        status=command.status,
        message=run_input_record_message_data(run, command),
        error=command.error,
        created_at=command.created_at,
        finished_at=command.finished_at,
    )


def run_input_from_records(
    run: RunRecord, *, inputs: Sequence[CommandRecord]
) -> MessageData | None:
    """Build the start input projection from durable input records."""

    start = _start_input(inputs)
    return run_input_message_data(run, start)


def run_inputs_from_records(
    run: RunRecord, *, inputs: Sequence[CommandRecord]
) -> list[InputDetail]:
    """Build run input details from durable input records."""

    return [
        InputDetail(
            record=command_data_from_record(input),
            message=run_input_record_message_data(run, input),
        )
        for input in inputs
    ]


def run_output_from_steps(
    run: RunRecord,
    *,
    steps: Sequence[StepRecord],
) -> RunOutput:
    """Build one run output projection from durable truth."""

    projected_steps = list(steps)
    virtual_failure = _virtual_runtime_failure_step(run, steps=steps)
    if virtual_failure is not None:
        projected_steps.append(virtual_failure)
    step_details = [
        StepDetail(
            record=step_data_from_record(step),
            message=(
                None if step is virtual_failure else step_message_data(run, step)
            ),
            virtual=step is virtual_failure,
        )
        for step in projected_steps
    ]
    return RunOutput(
        status=run.status,
        error=run.error,
        failure=failure_from_steps(
            status=run.status,
            error=run.error,
            steps=projected_steps,
        ),
        steps=step_details,
    )


def run_detail_from_record(
    run: RunRecord,
    *,
    steps: Sequence[StepRecord],
    inputs: Sequence[CommandRecord] = (),
    prompts: Mapping[str, str] | None = None,
    event_cursor: int | None = None,
) -> RunDetail:
    """Build one complete run detail projection from durable truth."""

    info = run_info_from_record(run, inputs=inputs, steps=steps)
    return RunDetail(
        **{item.name: getattr(info, item.name) for item in fields(RunInfo)},
        input=run_input_from_records(run, inputs=inputs),
        inputs=run_inputs_from_records(run, inputs=inputs),
        output=run_output_from_steps(run, steps=steps),
        prompts=dict(prompts or {}),
        event_cursor=event_cursor,
    )


def failure_from_steps(
    *, status: str, error: str | None, steps: Sequence[StepRecord]
) -> FailureDetail | None:
    """Build the normalized failure projection for a run or run summary."""

    if status != "failed" and error is None:
        return None
    failed_step = next(
        (item for item in reversed(steps) if item.status == "failed"), None
    )
    step_error = failed_step.error if failed_step is not None else None
    return FailureDetail(
        reason=error or step_error or "Run failed.",
        step_index=failed_step.step_index if failed_step is not None else None,
        step_kind=failed_step.kind if failed_step is not None else None,
        step_error=step_error,
    )


def _thread_channel(thread_id: str, origin: str) -> str:
    if origin != "chat":
        return ""
    if thread_id.startswith("web_"):
        return "web"
    if thread_id.startswith("script_tg_"):
        return "tg"
    return "terminal"


def _message_parts_summary(parts: Sequence[MessagePartData]) -> str:
    return message_summary(tuple(message_part_value(part) for part in parts))


def _thread_status(active: RunRecord | None) -> str:
    return "running" if active is not None else "idle"


def _start_input(inputs: Sequence[CommandRecord]) -> CommandRecord:
    for input in inputs:
        if input.index == 0 and input.kind == "start":
            return input
    raise ValueError("run start input not found")


def _virtual_runtime_failure_step(
    run: RunRecord, *, steps: Sequence[StepRecord]
) -> StepRecord | None:
    error = run.error
    if run.status != "failed" or error is None:
        return None
    if any(
        item.kind == "system" and item.status == "failed" and item.error == error
        for item in steps
    ):
        return None
    step_index = max((item.step_index for item in steps), default=0) + 1
    timestamp = run.finished_at or run.started_at
    return StepRecord(
        parent=run.run_id,
        index=step_index,
        kind="system",
        status="failed",
        input=(),
        output=(TextPart(text=error),),
        started_at=timestamp,
        finished_at=timestamp,
        detail={"message": error},
        error=error,
    )


def _prompt_bodies(store: RunStore, steps: Sequence[StepRecord]) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for prompt_hash in _prompt_hashes(steps):
        body = store.get_prompt(prompt_hash=prompt_hash)
        if body is not None:
            prompts[prompt_hash] = body
    return prompts


def _prompt_hashes(steps: Sequence[StepRecord]) -> tuple[str, ...]:
    hashes: list[str] = []
    for step in steps:
        for payload, keys in (
            (step.context, ("instruct", "prompt_context")),
            (step.detail, ("instruct", "context")),
        ):
            for key in keys:
                value = payload.get(key)
                if isinstance(value, str) and value and value not in hashes:
                    hashes.append(value)
    return tuple(hashes)


def _role_for_step(kind: StepKind) -> MessageRole | None:
    if kind == "model":
        return "assistant"
    if kind == "tool":
        return "tool"
    return None

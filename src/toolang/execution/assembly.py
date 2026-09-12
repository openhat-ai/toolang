"""Control-driven message assembly shared by live execution and replay."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import cast

from toolang.base.types.message import (
    Message,
    MessageRole,
    ToolCallPart,
    ToolResultPart,
)

from .message_delta import literal_delta, render_delta
from .recall import recall_revisions
from .prompting import control_message
from .records import (
    CompactControlPayload,
    ControlRecord,
    ExecuteControlPayload,
    RunControlPayload,
    RunRecord,
    StepRecord,
    StoredModelStepGiven,
)
from .types import (
    ControlRef,
    FieldRef,
    Local,
    MessageDelta,
    MessageTemplate,
    RunRef,
    RecallTarget,
    ToolStepGiven,
    TypedRef,
)
from .values import parts_from_local
from .tool_results import workspace_reply_from_step


def active_steps(
    steps: Sequence[StepRecord], controls: Mapping[ControlRef, ControlRecord]
) -> tuple[StepRecord, ...]:
    start = 0
    for index, step in enumerate(steps):
        if starts_sequence(step, controls):
            start = index
    return tuple(steps[start:])


def tail_delta(
    run: RunRecord,
    steps: Sequence[StepRecord],
    controls: Mapping[ControlRef, ControlRecord],
    resolve: Callable[[object], object],
) -> MessageDelta:
    """Record the still-unrecorded terminal exchange, not a second transcript."""

    models = [
        i
        for i, step in enumerate(steps)
        if isinstance(step.given, StoredModelStepGiven)
    ]
    tail = steps[models[-1] :] if models else ()
    messages: list[MessageTemplate] = []
    if not models:
        entry = next(
            (
                control
                for control in reversed(tuple(controls.values()))
                if isinstance(
                    control.payload, RunControlPayload | ExecuteControlPayload
                )
            ),
            None,
        )
        if entry is not None and isinstance(
            entry.payload, RunControlPayload | ExecuteControlPayload
        ):
            if "_" in entry.payload.input:
                messages.append(
                    _local_message(
                        "user",
                        FieldRef.from_path(entry.ref, "payload", "input", "_"),
                        Local(entry.payload.input["_"]),
                        resolve,
                    )
                )
    deferred: dict[ControlRef, ControlRecord] = {}
    replies = {
        step.ref: reply
        for step in tail
        if (reply := workspace_reply_from_step(step, resolve)) is not None
    }
    result_ids = {reply.tool_call_id for reply in replies.values()} | {
        step.output.local.value.tool_call_id
        for step in tail
        if step.output is not None
        and isinstance(step.output.local.value, ToolResultPart)
        and isinstance(step.given, ToolStepGiven)
        and step.given.trigger == "model"
    }
    calls: set[str] = set()
    for step in tail:
        if step.output is not None and step.kind == "model":
            parts = parts_from_local(step.output.local)
            segments = []
            for index, part in enumerate(parts):
                if isinstance(part, ToolCallPart):
                    if part.tool_call_id not in result_ids:
                        continue
                    calls.add(part.tool_call_id)
                segments.append(
                    TypedRef(
                        FieldRef.from_path(step.ref, "output", "local", "value", index),
                        "Part",
                    )
                )
            if segments:
                messages.append(MessageTemplate("assistant", tuple(segments)))
        elif step.ref in replies:
            reply = replies[step.ref]
            if reply.tool_call_id in calls:
                messages.append(MessageTemplate("tool", (reply,)))
        elif (
            step.output is not None
            and isinstance(step.output.local.value, ToolResultPart)
            and isinstance(step.given, ToolStepGiven)
            and step.given.trigger == "model"
        ):
            if step.output.local.value.tool_call_id in calls:
                messages.append(
                    MessageTemplate(
                        "tool",
                        (
                            TypedRef(
                                FieldRef.from_path(
                                    step.ref, "output", "local", "value"
                                ),
                                "ToolResultPart",
                            ),
                        ),
                    )
                )
        for ref in (
            *step.preceded_by,
            *((step.aborted_by,) if step.aborted_by else ()),
        ):
            control = controls.get(ref)
            if (
                control is not None
                and control.kind == "cancel"
                and control.status == "applied"
            ):
                deferred[ref] = control
    if not models and run.output is not None:
        # A non-model root contributes its public output, never child internals.
        messages.append(
            _local_message(
                "assistant",
                FieldRef.from_path(RunRef(run.id), "output", "local", "value"),
                run.output.local,
                resolve,
            )
        )
    if run.status == "canceled":
        attempt = max(
            (c.index for c in controls.values() if c.kind in {"run", "retry"}),
            default=0,
        )
        for control in controls.values():
            if (
                control.kind == "cancel"
                and control.status == "applied"
                and control.index > attempt
            ):
                deferred[control.ref] = control
    for control in deferred.values():
        if template := control_message(control):
            messages.append(template)
    return MessageDelta(messages=tuple(messages))


def _local_message(
    role: MessageRole, ref: FieldRef, value: Local, resolve: Callable[[object], object]
) -> MessageTemplate:
    if value.type in {"Text", "Part", "Part[]"}:
        return MessageTemplate(role, (TypedRef(ref, value.type),))
    local = replace(value, value=resolve(value.value))
    return literal_delta((Message(role, parts_from_local(local)),)).messages[0]


def recall_sources(values: Sequence[str] = ()) -> tuple[str, ...]:
    """Resolve auto once; persist the resulting selection, not authored source."""

    return ("far", "near") if not values or "auto" in values else tuple(values)


def assemble_messages(
    far: str,
    near: Sequence[Message],
    now: Sequence[Message],
    recall: Sequence[str],
) -> list[Message]:
    return [
        *([Message.user(far)] if far and "far" in recall else []),
        *(near if "near" in recall else ()),
        *now,
    ]


def adopted_horizon(
    horizon: FieldRef | None, controls: Sequence[ControlRecord], run: RunRef
) -> FieldRef | None:
    """Only controls targeting this Run can replace its horizon."""

    for control in controls:
        if control.target != run:
            continue
        if isinstance(control.payload, RunControlPayload | CompactControlPayload):
            horizon = control.payload.horizon
    return horizon


def starts_sequence(
    step: StepRecord, controls: Mapping[ControlRef, ControlRecord]
) -> bool:
    return any(
        ref.target == step.ref.run
        and ref in controls
        and controls[ref].kind in {"run", "retry", "execute"}
        for ref in step.preceded_by
    )


class MessageHistory:
    """A fixed historical prefix, with lazily resolved, reusable messages.

    The loader supplies records and a value resolver. No State or Store policy
    participates in selection or rendering. Tail templates become the next
    recording Model Step's delta; they never amend the preceding Run.
    """

    def __init__(
        self,
        thread: str,
        roots: Sequence[RunRef],
        load: Callable[[Sequence[RunRef]], Mapping[RunRef, Sequence[MessageDelta]]],
        tail: Callable[[Sequence[RunRef]], MessageDelta],
        resolve: Callable[[TypedRef], object],
        control: Callable[[ControlRef], ControlRecord],
    ) -> None:
        self.thread = thread
        self.roots = tuple(roots)
        self._load = load
        self._load_tail = tail
        self._resolve = resolve
        self._control = control
        self._messages: dict[RunRef, tuple[Message, ...]] = {}
        self._recalls: dict[RunRef, dict[RecallTarget, str]] = {}
        self._selected_recalls: dict[FieldRef | None, dict[RecallTarget, str]] = {}
        self._recorded: set[RunRef] = set()
        self._selections: dict[FieldRef | None, tuple[str, tuple[Message, ...]]] = {}
        self._ranges: dict[FieldRef | None, tuple[RunRef, ...]] = {}
        self._tails: dict[
            tuple[RunRef, ...], tuple[MessageDelta, tuple[Message, ...]]
        ] = {}

    def select(self, horizon: FieldRef | None) -> tuple[str, tuple[Message, ...]]:
        if horizon not in self._selections:
            summary = ""
            begin = 0
            if horizon is not None:
                output = self._resolve(
                    TypedRef(horizon.select("local", "value"), "Json")
                )
                if not isinstance(output, Mapping):
                    raise ValueError("compact output must be an object")
                value = cast(Mapping[str, object], output)
                if value.get("thread") != self.thread:
                    raise ValueError("compact output targets another Thread")
                if not self.roots or value.get("begin") not in {
                    None,
                    str(self.roots[0]),
                }:
                    raise ValueError("compact output must cover the complete prefix")
                end = value.get("end")
                root_ids = tuple(str(root) for root in self.roots)
                if not isinstance(end, str) or end not in root_ids:
                    raise ValueError(
                        "compact end must retain a historical root in near"
                    )
                begin = root_ids.index(end)
                summary = value.get("summary")
                if not isinstance(summary, str):
                    raise ValueError("compact summary must be text")
            messages: list[Message] = []
            selected = self.roots[begin:]
            missing = tuple(root for root in selected if root not in self._messages)
            if missing:
                for root, deltas in self._load(missing).items():
                    if deltas:
                        self._recorded.add(root)
                    self._messages[root] = tuple(
                        message
                        for delta in deltas
                        for message in render_delta(delta, self._resolve)
                    )
                    self._recalls[root] = recall_revisions(deltas, self._control)
            revisions: dict[RecallTarget, str] = {}
            for root in selected:
                messages.extend(self._messages[root])
                revisions.update(self._recalls[root])
            self._selections[horizon] = summary, tuple(messages)
            self._selected_recalls[horizon] = revisions
            self._ranges[horizon] = selected
        return self._selections[horizon]

    def recalls(self, horizon: FieldRef | None) -> Mapping[RecallTarget, str]:
        """Return recalled revisions in the selected near, never its far summary."""

        self.select(horizon)
        return self._selected_recalls[horizon]

    def near_roots(
        self, horizon: FieldRef | None
    ) -> tuple[tuple[RunRef, tuple[Message, ...]], ...]:
        """Return the cached root boundaries of the selected near."""

        self.select(horizon)
        return tuple((root, self._messages[root]) for root in self._ranges[horizon])

    def tail(
        self, horizon: FieldRef | None
    ) -> tuple[MessageDelta, tuple[Message, ...]]:
        """Include every unrecorded trailing root, bounded by the selected near."""

        self.select(horizon)
        roots = self._ranges[horizon]
        start = max(
            (index for index, root in enumerate(roots) if root in self._recorded),
            default=0,
        )
        pending = roots[start:]
        if pending not in self._tails:
            delta = self._load_tail(pending)
            self._tails[pending] = delta, render_delta(delta, self._resolve)
        return self._tails[pending]

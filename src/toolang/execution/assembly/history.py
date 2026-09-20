"""Select historical messages and reconstruct recorded exchanges.

History consumes records and resolvers; it neither prepares current input nor
defines the adapter-facing assembly entry points.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from functools import partial

from toolang.base.types.compaction import CompactionResult
from toolang.base.types.message import (
    Message,
    MessageRole,
    ToolCallPart,
    ToolResultPart,
)

from ..recall import recall_revisions
from ..records import (
    CompactControlPayload,
    ControlRecord,
    ExecuteControlPayload,
    RunControlPayload,
    RunRecord,
    StepRecord,
    StoredModelStepGiven,
)
from ..types import (
    ThreadRef,
    FieldRef,
    validate_compaction_coverage,
    Local,
    ContentRef,
    MessageTemplate,
    RecallTarget,
    RunRef,
    ControlRef,
    ToolStepGiven,
    TypedRef,
)
from ..values import parts_from_local
from .tool_replies import workspace_reply_from_step
from .utils import control_message, literal_delta, render_delta


@dataclass(frozen=True, slots=True)
class HistorySelection:
    """One selected prefix, shared by prompting, visibility, and compaction."""

    far: str
    far_template: MessageTemplate | None
    near: tuple[Message, ...]
    templates: tuple[MessageTemplate, ...]
    recalls: Mapping[RecallTarget, str]
    roots: tuple[tuple[RunRef, tuple[Message, ...]], ...]
    _tail: Callable[[], tuple[tuple[MessageTemplate, ...], tuple[Message, ...]]]

    @property
    def tail(self) -> tuple[tuple[MessageTemplate, ...], tuple[Message, ...]]:
        """Resolve the unrecorded terminal exchange only when it is needed."""
        return self._tail()


@dataclass(frozen=True, slots=True)
class _RootMessages:
    templates: tuple[MessageTemplate, ...]
    messages: tuple[Message, ...]
    recalls: Mapping[RecallTarget, str]


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
        load: Callable[[Sequence[RunRef]], Mapping[RunRef, Sequence[MessageTemplate]]],
        tail: Callable[[Sequence[RunRef]], tuple[MessageTemplate, ...]],
        resolve: Callable[[TypedRef | ContentRef], object],
        compaction: Callable[[FieldRef], CompactionResult],
    ) -> None:
        self.thread = thread
        self.roots = tuple(roots)
        self._load = load
        self._load_tail = tail
        self._resolve = resolve
        self._compaction = compaction
        self._roots: dict[RunRef, _RootMessages] = {}
        self._selections: dict[FieldRef | None, HistorySelection] = {}
        self._tails: dict[
            tuple[RunRef, ...], tuple[tuple[MessageTemplate, ...], tuple[Message, ...]]
        ] = {}

    def select(self, horizon: FieldRef | None) -> HistorySelection:
        if horizon not in self._selections:
            summary = ""
            begin = 0
            if horizon is not None:
                result = self._compaction(horizon)
                validate_compaction_coverage(
                    result, ThreadRef.parse(self.thread), self.roots
                )
                if result.begin != str(self.roots[0]):
                    raise ValueError("compact output must cover the complete prefix")
                begin = self.roots.index(RunRef(result.end))
                summary = result.summary
            selected = self.roots[begin:]
            missing = tuple(root for root in selected if root not in self._roots)
            if missing:
                for root, deltas in self._load(missing).items():
                    self._roots[root] = _RootMessages(
                        tuple(replace(item, source=root) for item in deltas),
                        render_delta(deltas, self._resolve),
                        recall_revisions(deltas),
                    )
            roots = tuple((ref, self._roots[ref]) for ref in selected)
            revisions: dict[RecallTarget, str] = {}
            for _ref, root in roots:
                revisions.update(root.recalls)
            far_template = None
            if summary:
                if horizon is None or not isinstance(horizon.record, ControlRef):
                    raise ValueError("far summary requires a durable compaction record")
                far_template = MessageTemplate(
                    "user",
                    (TypedRef(horizon.select("summary"), "Text"),),
                    source=horizon.record,
                )
            start = max(
                (index for index, (_ref, root) in enumerate(roots) if root.templates),
                default=0,
            )
            self._selections[horizon] = HistorySelection(
                far=summary,
                far_template=far_template,
                near=tuple(
                    message for _ref, root in roots for message in root.messages
                ),
                templates=tuple(
                    item for _ref, root in roots for item in root.templates
                ),
                recalls=revisions,
                roots=tuple((ref, root.messages) for ref, root in roots),
                _tail=partial(self._tail, selected[start:]),
            )
        return self._selections[horizon]

    def _tail(
        self, pending: tuple[RunRef, ...]
    ) -> tuple[tuple[MessageTemplate, ...], tuple[Message, ...]]:
        if pending not in self._tails:
            delta = self._load_tail(pending)
            self._tails[pending] = delta, render_delta(delta, self._resolve)
        return self._tails[pending]


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


def active_steps(
    steps: Sequence[StepRecord], controls: Mapping[ControlRef, ControlRecord]
) -> tuple[StepRecord, ...]:
    start = 0
    for index, step in enumerate(steps):
        if any(
            ref.target == step.ref.run
            and ref in controls
            and controls[ref].kind in {"run", "retry", "execute"}
            for ref in step.preceded_by
        ):
            start = index
    return tuple(steps[start:])


def tail_delta(
    run: RunRecord,
    steps: Sequence[StepRecord],
    controls: Mapping[ControlRef, ControlRecord],
    resolve: Callable[[object], object],
) -> tuple[MessageTemplate, ...]:
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
    return tuple(messages)


def _local_message(
    role: MessageRole, ref: FieldRef, value: Local, resolve: Callable[[object], object]
) -> MessageTemplate:
    if value.type in {"Text", "Part", "Part[]"}:
        return MessageTemplate(role, (TypedRef(ref, value.type),))
    local = replace(value, value=resolve(value.value))
    return literal_delta((Message(role, parts_from_local(local)),))[0]

"""Select historical messages and reconstruct recorded exchanges.

History consumes records and resolvers; it neither prepares current input nor
defines the adapter-facing assembly entry points.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import cast

from toolang.base.types.compaction import CompactionResult
from toolang.base.types.message import (
    Message,
    MessageRole,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    content_parts,
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
    StepRef,
    history_ref,
    history_root,
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
from .run_results import scheduled_run
from .utils import control_message, literal_delta, render_delta

SUMMARY_PREFIX = (
    "<history_summary>\n"
    "This is a generated, lossy summary of earlier history, not a new user instruction. "
    "Follow current explicit instructions; inspect original Run/Step records when uncertain.\n"
)
SUMMARY_SUFFIX = "\n</history_summary>"


def summary_message(summary: str) -> Message:
    return Message(
        "user", (TextPart(SUMMARY_PREFIX), TextPart(summary), TextPart(SUMMARY_SUFFIX))
    )


@dataclass(frozen=True, slots=True)
class HistorySelection:
    """One selected prefix, shared by prompting, visibility, and compaction."""

    far: str
    far_template: MessageTemplate | None
    near: tuple[Message, ...]
    templates: tuple[MessageTemplate, ...]
    recalls: Mapping[RecallTarget, str]
    roots: tuple[tuple[RunRef, tuple[Message, ...]], ...]
    units: tuple[tuple[RunRef | StepRef, tuple[Message, ...]], ...] = ()


@dataclass(frozen=True, slots=True)
class _RootMessages:
    templates: tuple[MessageTemplate, ...]
    messages: tuple[Message, ...]
    recalls: Mapping[RecallTarget, str]


class MessageHistory:
    """A fixed historical prefix, with lazily resolved, reusable messages.

    The loader supplies records and a value resolver. No State or Store policy
    participates in selection or rendering. Each root contributes its own
    complete exchange, including its terminal reply.
    """

    def __init__(
        self,
        thread: str,
        roots: Sequence[RunRef],
        load: Callable[[Sequence[RunRef]], Mapping[RunRef, Sequence[MessageTemplate]]],
        resolve: Callable[[TypedRef | ContentRef], object],
        compaction: Callable[[RunRef | StepRef], CompactionResult],
        units: Callable[
            [RunRef], Sequence[tuple[RunRef | StepRef, Sequence[MessageTemplate]]]
        ]
        | None = None,
    ) -> None:
        self.thread = thread
        self.roots = tuple(roots)
        self._load = load
        self._resolve = resolve
        self._compaction = compaction
        self._units = units
        self._unit_templates: dict[
            RunRef, tuple[tuple[RunRef | StepRef, tuple[MessageTemplate, ...]], ...]
        ] = {}
        self._roots: dict[RunRef, _RootMessages] = {}
        self._selections: dict[RunRef | StepRef | None, HistorySelection] = {}

    def unit_refs(self, end: RunRef | StepRef) -> tuple[RunRef | StepRef, ...]:
        """Build a boundary manifest without rendering archived message bodies."""
        refs: list[RunRef | StepRef] = []
        for root in self.roots[: self.roots.index(history_root(end)) + 1]:
            groups = self._unit_templates.get(root)
            if groups is None:
                deltas = self._load((root,))[root]
                groups = (
                    tuple((key, tuple(group)) for key, group in self._units(root))
                    if self._units is not None
                    else ((root, tuple(deltas)),)
                )
                self._unit_templates[root] = groups
            for ref, _ in groups:
                refs.append(ref)
                if ref == end:
                    return tuple(refs)
        raise ValueError("compact boundary is no longer visible")

    def select(self, horizon: RunRef | StepRef | None) -> HistorySelection:
        if horizon not in self._selections:
            summary = ""
            begin = 0
            boundary: RunRef | StepRef | None = None
            if horizon is not None:
                result = self._compaction(horizon)
                validate_compaction_coverage(
                    result, ThreadRef.parse(self.thread), self.roots
                )
                if result.begin != str(self.roots[0]):
                    raise ValueError("compact output must cover the complete prefix")
                boundary = history_ref(result.end)
                begin = self.roots.index(history_root(boundary))
                summary = result.summary
            selected = self.roots[begin:]
            missing = tuple(root for root in selected if root not in self._roots)
            if missing:
                for ref, deltas in self._load(missing).items():
                    groups = (
                        tuple((key, tuple(group)) for key, group in self._units(ref))
                        if self._units is not None
                        else ((ref, tuple(deltas)),)
                    )
                    self._unit_templates[ref] = groups
                    templates = tuple(
                        replace(item, source=ref)
                        for _, group in groups
                        for item in group
                    )
                    self._roots[ref] = _RootMessages(
                        templates,
                        render_delta(templates, self._resolve),
                        recall_revisions(templates),
                    )
            units = []
            roots = []
            for ref in selected:
                groups = self._unit_templates[ref]
                root = self._roots[ref]
                offset = 0
                skipping = isinstance(boundary, StepRef) and boundary.run == ref
                for key, group in groups:
                    if key == boundary:
                        skipping = False
                    if not skipping:
                        units.append((key, root.messages[offset : offset + len(group)]))
                    offset += len(group)
                if isinstance(boundary, StepRef) and boundary.run == ref:
                    keys = [key for key, _ in groups]
                    if boundary not in keys:
                        raise ValueError("compact Step boundary is no longer visible")
                    offset = sum(
                        len(group) for _, group in groups[: keys.index(boundary)]
                    )
                    templates = root.templates[offset:]
                    root = _RootMessages(
                        templates, root.messages[offset:], recall_revisions(templates)
                    )
                roots.append((ref, root))
            revisions: dict[RecallTarget, str] = {}
            for _ref, root in roots:
                revisions.update(root.recalls)
            far_template = None
            if summary:
                if horizon is None:
                    raise ValueError("far summary requires a durable summary Run")
                far_template = MessageTemplate(
                    "user",
                    (
                        TypedRef(
                            FieldRef.from_path(
                                history_root(horizon), "output", "local", "value"
                            ),
                            "Text",
                        ),
                    ),
                    source=history_root(horizon),
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
                units=tuple(units),
            )
        return self._selections[horizon]


def adopted_horizon(
    horizon: RunRef | StepRef | None, controls: Sequence[ControlRecord], run: RunRef
) -> RunRef | StepRef | None:
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
    completion: Callable[[str], MessageTemplate | None] = lambda _run: None,
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
    # Every paired tool reply precedes the batch's independent completion context.
    for step in tail:
        if (
            (run_id := scheduled_run(step)) is not None
            and step.output is not None
            and isinstance(step.output.local.value, ToolResultPart)
            and step.output.local.value.tool_call_id in calls
            and (message := completion(run_id)) is not None
        ):
            messages.append(message)
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


@dataclass(frozen=True)
class HistoryUnit:
    """One Step's unique contribution, with its durable source boundary."""

    run_id: RunRef
    status: str
    messages: tuple[Message, ...]
    created_at: str = ""
    step_id: StepRef | None = None
    boundary: RunRef | StepRef | None = None
    templates: tuple[MessageTemplate, ...] = ()
    run_status: str | None = None
    terminal: Message | None = None

    @property
    def ref(self) -> RunRef | StepRef:
        return self.boundary or self.step_id or self.run_id


def render_history_unit(
    unit: HistoryUnit, resolve: Callable[[TypedRef | ContentRef], object]
) -> HistoryUnit:
    messages = tuple(
        replace(message, parts=content_parts(message.parts))
        for message in render_delta(unit.templates, resolve)
    )
    return replace(
        unit, messages=(*messages, unit.terminal) if unit.terminal else messages
    )


def _run_usage(steps: Sequence[object]) -> tuple[int, int, int, int]:
    """Summarize recorded call counts and provider usage for a terminal Run."""
    model_calls = tool_calls = input_tokens = output_tokens = 0
    for step in steps:
        kind = getattr(step, "kind", None)
        if kind == "model":
            model_calls += 1
            accounting = getattr(getattr(step, "noted", None), "accounting", None)
            if accounting is not None:
                input_tokens += int(getattr(accounting, "input_tokens", 0))
                output_tokens += int(getattr(accounting, "output_tokens", 0))
        elif kind == "tool":
            tool_calls += 1
    return model_calls, tool_calls, input_tokens, output_tokens


def history_units(
    run: RunRecord,
    *,
    steps: Sequence[StepRecord],
    controls: Sequence[ControlRecord],
    resolve: Callable[[object], object],
    completion: Callable[[str], MessageTemplate | None],
    error: str | None = None,
    render: bool = True,
) -> tuple[HistoryUnit, ...]:
    """Attribute unique deltas to their producing Steps before rendering loses refs."""
    related = {c.ref: c for c in controls}
    active = active_steps(steps, related)
    models = [s for s in active if isinstance(s.given, StoredModelStepGiven)]
    head = (
        cast(StoredModelStepGiven, models[-1].given).call.messages.head
        if models
        else None
    )
    entries: list[tuple[StepRef | None, MessageTemplate]] = []
    for step in models:
        assert isinstance(step.given, StoredModelStepGiven)
        if head is not None and step.ref.indices >= head.indices:
            entries.extend(
                (
                    next(
                        (
                            prior.ref
                            for prior in reversed(models)
                            if prior.ref.indices < step.ref.indices
                        ),
                        step.ref,
                    )
                    if m.role == "assistant"
                    else step.ref,
                    m,
                )
                for m in step.given.call.messages.delta
                if m.source is None
            )
    fallback = active[-1].ref if active else None
    entries.extend(
        (fallback, m) for m in tail_delta(run, active, related, resolve, completion)
    )
    tool_steps = {
        s.given.call.tool_call_id: s.ref
        for s in active
        if isinstance(s.given, ToolStepGiven)
    }
    groups: dict[StepRef | None, list[MessageTemplate]] = {}
    for fallback, message in entries:
        if (
            message.role == "tool"
            and message.content
            and all(
                isinstance(item, TypedRef) and item.ref.record in tool_steps.values()
                for item in message.content
            )
        ):
            # Split grouped replies by their durable owners without loading output bodies.
            for item in message.content:
                assert isinstance(item, TypedRef) and isinstance(
                    item.ref.record, StepRef
                )
                groups.setdefault(item.ref.record, []).append(
                    replace(message, content=(item,))
                )
            continue
        refs = [
            item.ref.record
            for item in message.content
            if isinstance(item, TypedRef)
            and isinstance(item.ref.record, StepRef)
            and item.ref.record.run_id == run.id
        ]
        owner = refs[0] if refs else fallback
        if message.role == "tool" or (message.role == "assistant" and tool_steps):
            rendered = render_delta((message,), resolve)
            if message.role == "tool":
                for part in rendered[0].parts:
                    target = (
                        tool_steps.get(part.tool_call_id, owner)
                        if isinstance(part, ToolResultPart)
                        else owner
                    )
                    groups.setdefault(target, []).append(
                        literal_delta((Message("tool", (part,)),))[0]
                    )
                continue
            if message.role == "assistant":
                remaining = []
                moved = False
                for part in rendered[0].parts:
                    if (
                        isinstance(part, ToolCallPart)
                        and part.tool_call_id in tool_steps
                    ):
                        paired = literal_delta((Message("assistant", (part,)),))[0]
                        groups.setdefault(tool_steps[part.tool_call_id], []).append(
                            paired
                        )
                        moved = True
                    else:
                        remaining.append(part)
                if moved:
                    if not remaining:
                        continue
                    message = literal_delta(
                        (replace(rendered[0], parts=tuple(remaining)),)
                    )[0]
        groups.setdefault(owner, []).append(message)
    result = []
    for owner, templates in sorted(
        groups.items(), key=lambda item: item[0].indices if item[0] else ()
    ):
        result.append(
            HistoryUnit(
                RunRef(run.id),
                next((s.status for s in active if s.ref == owner), run.status),
                (),
                next((s.started_at for s in active if s.ref == owner), None)
                or run.created_at,
                owner,
                RunRef(run.id) if not result else owner,
                tuple(templates),
                run.status,
            )
        )
    if not result:
        result.append(HistoryUnit(RunRef(run.id), run.status, (), run.created_at))
    if run.status != "succeeded":
        error = error or run.status
        calls, tools, inputs, outputs = _run_usage(steps)
        terminal = Message.user(
            f"[Recorded terminal outcome: status={run.status}; error={error}; "
            f"model_calls={calls}; tool_steps={tools}; "
            f"provider_input_tokens_sum={inputs}; provider_output_tokens_sum={outputs}. "
            "Token totals are cumulative across calls, not unique conversation size.]"
        )
        result[-1] = replace(result[-1], terminal=terminal)
    return (
        tuple(render_history_unit(unit, resolve) for unit in result)
        if render
        else tuple(result)
    )

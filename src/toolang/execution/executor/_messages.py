"""Execution-local messages and their pending durable templates."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from toolang.base.types.message import Message, MessageRole

from ..message_delta import literal_delta, render_delta
from ..control_messages import control_message
from ..recall import recall_revisions
from ..records import ControlRecord, RecallControlPayload, SteerControlPayload
from ..types import (
    FieldRef,
    Local,
    MessageDelta,
    MessageTemplate,
    RecallTarget,
    TypedRef,
)


class _MessageBuffer:
    """Render additions once, retaining the resolved prefix until execution ends."""

    def __init__(self, messages: Sequence[Message] = ()) -> None:
        self.messages: list[Message] = []
        self.pending: list[MessageTemplate] = []
        self.recalls: dict[RecallTarget, str] = {}
        self.started = False
        self.initialize(messages)

    def copy(self) -> _MessageBuffer:
        """Stage a call boundary without rerendering the adopted prefix."""

        other = _MessageBuffer()
        other.messages = list(self.messages)
        other.pending = list(self.pending)
        other.recalls = dict(self.recalls)
        other.started = self.started
        return other

    def initialize(self, messages: Sequence[Message]) -> None:
        if not self.started:
            self.messages.clear()
            self.pending.clear()
            self.recalls.clear()
            for template in literal_delta(messages).messages:
                self._append(template)

    def append(self, message: Message) -> None:
        """Append authored or runtime-rendered literal content."""

        self._append(literal_delta((message,)).messages[0])

    def append_control(self, control: ControlRecord) -> None:
        """Keep recalled revisions alongside the exact templates being appended."""

        payload = control.payload
        if not isinstance(payload, RecallControlPayload | SteerControlPayload):
            return
        template = control_message(control)
        if template is None:
            return
        if isinstance(payload, RecallControlPayload):
            content: object = payload.content
        else:
            content = next(
                (item.value for item in payload.input if item.name == "_"), None
            )
        self.append_template(template, lambda _ref: content)
        self.recalls.update(
            recall_revisions(
                (MessageDelta(messages=(template,)),), lambda _ref: control
            )
        )

    def prepend(self, delta: MessageDelta, messages: Sequence[Message]) -> None:
        """Record a newly consumed historical tail before this sequence's input."""

        self.pending[:0] = delta.messages
        self.messages[:0] = messages

    def append_ref(self, role: MessageRole, ref: FieldRef, value: Local) -> None:
        """Use the same field value online that the owning record will persist."""

        template = MessageTemplate(role, (TypedRef(ref, value.type),))
        self.append_template(template, lambda _ref: value.value)

    def append_template(
        self, template: MessageTemplate, resolve: Callable[[TypedRef], object]
    ) -> None:
        self.messages.extend(render_delta(MessageDelta(messages=(template,)), resolve))
        self.pending.append(template)

    def _append(self, template: MessageTemplate) -> None:
        def no_ref(ref: TypedRef) -> object:
            raise ValueError(f"literal message contains a reference: {ref}")

        self.messages.extend(render_delta(MessageDelta(messages=(template,)), no_ref))
        self.pending.append(template)

    def take_delta(self) -> MessageDelta:
        """Advance the recording boundary when a Model Step is established."""

        delta = MessageDelta(messages=tuple(self.pending))
        self.pending.clear()
        self.started = True
        return delta

    def group_tools(self, start: int) -> None:
        """Combine an unsaved tool batch without changing its Part boundaries."""

        count = len(self.pending) - start
        if not count:
            return
        self.pending[start:] = [
            MessageTemplate(
                "tool",
                tuple(
                    segment
                    for item in self.pending[start:]
                    for segment in item.segments
                ),
            )
        ]
        self.messages[-count:] = [
            Message(
                "tool",
                tuple(part for item in self.messages[-count:] for part in item.parts),
            )
        ]

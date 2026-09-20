"""Execution-local messages and staged additions to a durable call sequence."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy

from toolang.base.types.message import Message, MessageRole

from .utils import control_message, literal_delta, render_delta
from ..recall import recall_revisions
from ..records import ControlRecord, RecallControlPayload, SteerControlPayload
from ..types import (
    ContentRef,
    FieldRef,
    Local,
    MessageTemplate,
    ModelMessages,
    RecallTarget,
    StepRef,
    TypedRef,
)


class MessageBuffer:
    """Keep resolved messages beside their immutable recording descriptions."""

    def __init__(self, messages: Sequence[Message] = ()) -> None:
        self.messages: list[Message] = []
        self.templates: list[MessageTemplate] = []
        self.pending: list[MessageTemplate] = []
        self.recalls: dict[RecallTarget, str] = {}
        self.head: StepRef | None = None
        self.initialize(messages)

    @property
    def started(self) -> bool:
        return self.head is not None

    def copy(self) -> MessageBuffer:
        """Prepare a candidate without consuming the committed prefix."""

        other = MessageBuffer()
        other.messages = list(self.messages)
        other.templates = list(self.templates)
        other.pending = list(self.pending)
        other.recalls = dict(self.recalls)
        other.head = self.head
        return other

    def initialize(self, messages: Sequence[Message]) -> None:
        if not self.started:
            self.messages.clear()
            self.templates.clear()
            self.pending.clear()
            self.recalls.clear()
            for message in messages:
                self.append(message)

    def append(self, message: Message) -> None:
        self._append(literal_delta((message,))[0], deepcopy(message))

    def append_control(self, control: ControlRecord) -> None:
        payload = control.payload
        if not isinstance(payload, RecallControlPayload | SteerControlPayload):
            return
        template = control_message(control)
        if template is None:
            return
        content = (
            payload.content
            if isinstance(payload, RecallControlPayload)
            else payload.input.get("_")
        )
        self.append_template(template, lambda _ref: content)

    def append_ref(self, role: MessageRole, ref: FieldRef, value: Local) -> None:
        self.append_template(
            MessageTemplate(role, (TypedRef(ref, value.type),)),
            lambda _ref: value.value,
        )

    def append_template(
        self,
        template: MessageTemplate,
        resolve: Callable[[TypedRef | ContentRef], object],
    ) -> None:
        self._append(template, render_delta((template,), resolve)[0])

    def _append(self, template: MessageTemplate, message: Message) -> None:
        self.messages.append(message)
        self.templates.append(template)
        self.pending.append(template)
        self.recalls.update(recall_revisions((template,)))

    def take_delta(
        self,
        step: StepRef,
        *,
        reset: bool = False,
    ) -> ModelMessages:
        """Establish a new head or append additions in the staged candidate."""

        if self.head is None or reset:
            self.head = step
            delta = tuple(self.templates)
        else:
            delta = tuple(self.pending)
        self.pending.clear()
        return ModelMessages(self.head, delta)

    def group_tools(self, start: int) -> None:
        """Group an unsaved batch without changing tool Part boundaries."""

        count = len(self.pending) - start
        if not count:
            return
        template = MessageTemplate(
            "tool",
            tuple(segment for item in self.pending[start:] for segment in item.content),
        )
        self.pending[start:] = [template]
        self.templates[-count:] = [template]
        self.messages[-count:] = [
            Message(
                "tool",
                tuple(part for item in self.messages[-count:] for part in item.parts),
            )
        ]

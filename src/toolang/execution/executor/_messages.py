"""Execution-local messages and their pending durable templates."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from toolang.base.types.message import Message, MessageRole, TextPart, message_text

from ..message_delta import literal_delta, render_delta
from ..types import FieldRef, Local, MessageDelta, MessageTemplate, TypedRef


class _MessageBuffer:
    """Render additions once, retaining the resolved prefix until execution ends."""

    def __init__(self, messages: Sequence[Message] = ()) -> None:
        self.messages: list[Message] = []
        self.pending: list[MessageTemplate] = []
        self.started = False
        self.context = ""
        self.initialize(messages)

    def copy(self) -> _MessageBuffer:
        """Stage a call boundary without rerendering the adopted prefix."""

        other = _MessageBuffer()
        other.messages = list(self.messages)
        other.pending = list(self.pending)
        other.started = self.started
        other.context = self.context
        return other

    def initialize(
        self,
        messages: Sequence[Message],
        *,
        context: str = "",
        visible: Sequence[Message] = (),
    ) -> None:
        previous_context = _last_context(visible)
        if not self.started:
            self.messages.clear()
            self.pending.clear()
            if context and context == previous_context:
                messages = _without_context(messages, context)
                context = ""
            for template in literal_delta(messages).messages:
                self._append(template)
        elif context and context != self.context:
            if self.context or context != previous_context:
                self.append(Message.user(context))
            else:
                context = ""
        self.context = context

    def append(self, message: Message) -> None:
        """Append authored or runtime-rendered literal content."""

        self._append(literal_delta((message,)).messages[0])

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


def _last_context(messages: Sequence[Message]) -> str:
    """Recognize only the runtime context envelope, not arbitrary user text."""

    for message in reversed(messages):
        if message.role != "user":
            continue
        text = message_text(message.parts)
        if text.startswith("<context>") and "</context>" in text:
            return text[: text.index("</context>") + len("</context>")]
    return ""


def _without_context(messages: Sequence[Message], context: str) -> tuple[Message, ...]:
    result = []
    removed = False
    for message in messages:
        parts = message.parts
        if (
            not removed
            and message.role == "user"
            and parts
            and isinstance(parts[0], TextPart)
        ):
            text = parts[0].text
            if text == context or text.startswith(context + "\n\n"):
                text = text[len(context) :].removeprefix("\n\n")
                parts = (*((TextPart(text),) if text else ()), *parts[1:])
                removed = True
        if parts:
            result.append(Message(message.role, parts))
    return tuple(result)

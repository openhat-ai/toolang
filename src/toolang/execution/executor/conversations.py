"""Resolve live conversation guidance at execution boundaries, outside assembly."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from toolang.common.layout import AgentLayout
from toolang.teaming.agent_client import AgentClient
from toolang.teaming.errors import MessagingError
from toolang.teaming.schemas import ConversationRules, convo_target

from ..records import RecallControlPayload
from ..types import ConversationScope, ConvoRulesRecallTarget
from .rules import _HonorRequired

if TYPE_CHECKING:
    from .runs.agic import _AgicState


@dataclass(frozen=True, slots=True)
class ConversationAccess:
    scope: ConversationScope
    document: ConversationRules
    participants: tuple[str, ...] = ()

    @property
    def recall(self) -> RecallControlPayload:
        return RecallControlPayload(
            ConvoRulesRecallTarget(self.scope.backend, self.scope.conversation),
            self.document.revision,
            self.document.content,
        )


async def load_source(
    layout: AgentLayout, scope: ConversationScope
) -> RecallControlPayload:
    async with AgentClient(layout.place, actor=f"agent:{layout.name}") as client:
        with client.session():
            if client.config.identity != scope.backend:
                raise MessagingError("Source conversation backend changed")
            document = await client.get_rules(scope.conversation)
    return RecallControlPayload(
        ConvoRulesRecallTarget(scope.backend, scope.conversation),
        document.revision,
        document.content,
    )


async def load_target(
    layout: AgentLayout, target: str, *, pending: bool = False
) -> ConversationAccess:
    destination = convo_target(target, layout.name)
    async with AgentClient(layout.place, actor=f"agent:{layout.name}") as client:
        with client.session():
            resolved = await client.resolve(destination)
            if not resolved.exists and not pending:
                raise MessagingError("No conversation exists with this agent")
            document = (
                await client.get_rules(resolved.conversation)
                if resolved.exists
                else ConversationRules.create(resolved.conversation)
            )
            return ConversationAccess(
                ConversationScope(client.config.identity, resolved.conversation),
                document,
                resolved.participants if not resolved.exists else (),
            )


async def preflight(state: _AgicState, name: str, target: str) -> ConversationAccess:
    access = await load_target(state.layout, target, pending=name == "convo__write")
    if name == "convo__get_rules":
        return access
    payload = access.recall
    assert state.execution is not None
    pending = state.execution.runtime_controls(state.prepared.run.run_id)
    if state.visible_recalls.get(payload.target, "0") != payload.revision or any(
        isinstance(control.payload, RecallControlPayload)
        and control.payload.target == payload.target
        and payload.revision != "0"
        for control in pending
    ):
        raise _HonorRequired(conversations=(target,))
    return access

"""Agent-owned message polling and serial batch handling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import json
import logging
from typing import Any

from toolang.base.types.message import TextPart
from toolang.base.types.policy import RunBindings
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.runnables import resolve_runnable_reference, runnable_fallback
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import resolve_runnable_input
from toolang.teaming.types import MessageReceiver
from toolang.teaming.errors import (
    BackendUnavailable,
    ConversationAccessDenied,
    MessagingError,
)
from toolang.teaming.schemas import Message, stream_id
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

logger = logging.getLogger(__name__)
_CONTEXT = 20
_CURSOR_SAVE_SECONDS = 2
_INSTRUCTIONS = """Handle this message batch as the receiving agent.
Use convo__team and convo__list to find agents and conversations; never inspect other agents'
threads, files, configuration, or shell sessions to discover how to communicate.
Unless explicitly asked to contact someone elsewhere, reply using convo__write
with target set to the source conversation ID from this batch, message set to
your reply text, and reply_to set to the source message's id. Never use stream_id
as reply_to. convo__peek inspects history without consuming messages. The tool
owns the envelope and delivery. Never resend a confirmed write or retry uncertain
delivery. Your final output is a brief handling summary, not a chat message or a
replies JSON object.
Use convo__peek when you need earlier discussion. Do not re-handle your own messages,
acknowledgements, completed discussions, or requests addressed only to others.
If no action is needed, simply note that. Do not send acknowledgements of acknowledgements.
Delegate substantial work to an available spawn target, preserving source context;
tell the worker to send its result via convo__write. Do not await it in this handler.
Batch:
"""


class MessagingLoop:
    def __init__(
        self,
        *,
        layout: AgentLayout,
        owner: str,
        executor: RunExecutor,
        threads: ThreadManager,
        get_agent_setup: Callable[[], AgentSetup],
        get_agent_state: Callable[[], AgentState],
        client: MessageReceiver,
        endpoint: str = "",
    ):
        self.agent, self.owner = f"agent:{layout.name}", owner
        self.client = client
        self.endpoint = endpoint
        self.executor, self.threads = executor, threads
        self.get_setup, self.get_state = get_agent_setup, get_agent_state
        self.identity = ""
        self.pending: tuple[str, str] | None = None
        self.last_conversation = ""
        self.gaps: dict[str, str | None] = {}

    async def consume(self, stop: asyncio.Event) -> None:
        delay = 0.5
        while not stop.is_set():
            try:
                await self.poll()
            except BackendUnavailable:
                logger.warning("Talk cursor transport unavailable; retrying")
            delay = min(5, delay * 2) if self.pending is not None else 0.5
            await self._wait(stop, delay)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    async def poll(self) -> None:
        # Pin the entire batch and its cursor write to one Hub/backend identity.
        with self.client.session() as identity:
            if identity != self.identity:
                self.identity = identity
                self.pending = None
                self.gaps, self.last_conversation = {}, ""
            if self.pending is not None:
                await self._save_cursor(reconcile=True)
                return
            await self._poll()

    async def _save_cursor(self, *, reconcile: bool = False) -> None:
        if self.pending is None:
            return
        conversation, cursor = self.pending
        try:
            if reconcile:
                saved = await self.client.talk_cursor(conversation)
                if stream_id(saved) >= stream_id(cursor):
                    self.pending = None
                    return
            await self.client.advance_talk_cursor(conversation, cursor)
        except ConversationAccessDenied:
            # Departure after handling must not block the remaining conversations.
            logger.warning("Talk cursor not saved after leaving %s", conversation)
        self.pending = None

    async def _poll(self) -> None:
        conversations = await self.client.contacts()
        # Round-robin independently of membership changes.
        conversations.sort(
            key=lambda g: (
                g["conversation"] <= self.last_conversation,
                g["conversation"],
            )
        )
        for info in conversations:
            conversation = info["conversation"]
            try:
                cursor = await self.client.talk_cursor(conversation)
                gap = await self.client.check_cursor(conversation, cursor)
                entries = await self.client.read(
                    conversation, after=cursor, count=_CONTEXT
                )
            except ConversationAccessDenied:
                # Membership can change after the directory snapshot.
                continue
            if gap and gap != self.gaps.get(conversation):
                logger.warning("%s: %s", conversation, gap)
            self.gaps[conversation] = gap
            if not entries:
                continue
            self.last_conversation = conversation
            messages = []
            for sid, fields in entries:
                try:
                    message = Message.decode(fields["data"])
                except (MessagingError, KeyError):
                    logger.warning(
                        "Skipping malformed message: %s %s", conversation, sid
                    )
                    continue
                messages.append({**message.data(), "stream_id": sid})
            cancelled = False
            try:
                if any(m["sender"] != self.agent for m in messages):
                    await self.handle(
                        {
                            "agent": self.agent,
                            "available_conversations": conversations,
                            "conversations": [
                                {
                                    "conversation": conversation,
                                    "messages": messages,
                                }
                            ],
                        }
                    )
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception:
                logger.exception("Messaging batch failed; skipping %s", conversation)
            finally:
                self.pending = conversation, entries[-1][0]
                try:
                    async with asyncio.timeout(_CURSOR_SAVE_SECONDS):
                        await self._save_cursor()
                except (BackendUnavailable, TimeoutError):
                    logger.warning("Talk cursor save interrupted; retaining progress")
                except Exception:
                    if not cancelled:
                        raise
                    logger.warning(
                        "Talk cursor save failed during cancellation; retaining progress",
                        exc_info=True,
                    )
            return

    async def handle(self, batch: dict[str, Any]) -> None:
        setup, state = self.get_setup(), self.get_state()
        preferred = "talk"
        custom = preferred in state.agics or preferred in state.flows
        runnable = (
            runnable_fallback(state, preferred=preferred)
            if custom
            else setup.defaults.runnable or runnable_fallback(state, preferred="talk")
        )
        resolved = resolve_runnable_reference(state, runnable)
        text = ("" if custom else _INSTRUCTIONS) + json.dumps(batch, ensure_ascii=False)
        spec = RunSpec(
            setup=setup,
            state=state,
            thread=self.threads.create(prefix=ThreadPrefix.TALK),
            workdir=self.executor.resolve_workdir(
                setup, state, workdir=None, inherit_thread_workdir=False
            ),
            bindings=RunBindings(
                runnable=runnable,
                model=setup.defaults.model.ref if setup.defaults.model else None,
            ),
            model_request=setup.defaults.model,
            limits=setup.limits,
            input=resolve_runnable_input(
                resolved.executable,
                {"_": (TextPart(text),)},
                structs={s.name: s for s in state.modules[resolved.module].structs},
            ),
        )
        handle = self.executor.run(spec)
        try:
            record = await handle
        except asyncio.CancelledError:
            handle.cancel(reason="Messaging handler stopped")
            await handle
            raise
        if record.status != "succeeded":
            raise MessagingError(f"Message handler {record.id}: {record.status}")

"""Agent-owned message polling and serial batch handling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import json
import logging
from typing import Any, cast

from toolang.base.types.message import TextPart
from toolang.base.types.policy import RunBindings
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ConversationScope
from toolang.execution.talk import TalkBatch
from toolang.teaming.ids import talk_thread_id
from toolang.lang.input import CallInput
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
        self.layout = layout
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
        conversations.sort(
            key=lambda g: (
                g["conversation"] <= self.last_conversation,
                g["conversation"],
            )
        )
        for info in conversations:
            conversation = info["conversation"]
            batch: dict[str, Any]
            scope = ConversationScope(self.identity, conversation)
            try:
                cursor = await self.client.talk_cursor(conversation)
                receipt = self.executor.store.talk.get(scope, cursor)
                if receipt is not None:
                    batch = json.loads(receipt.batch.payload)
                    through = receipt.batch.through
                    if (
                        batch.get("agent") != self.agent
                        or batch["conversations"][0]["conversation"] != conversation
                        or batch["cursor"] != {"after": cursor, "through": through}
                    ):
                        raise MessagingError(
                            "Stored talk batch differs from its source binding"
                        )
                else:
                    gap = await self.client.check_cursor(conversation, cursor)
                    entries = await self.client.read(
                        conversation, after=cursor, count=_CONTEXT
                    )
                    if gap and gap != self.gaps.get(conversation):
                        logger.warning("%s: %s", conversation, gap)
                    self.gaps[conversation] = gap
                    if not entries:
                        continue
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
                    through = entries[-1][0]
                    batch = {
                        "agent": self.agent,
                        "available_conversations": conversations,
                        "cursor": {"after": cursor, "through": through},
                        "conversations": [
                            {"conversation": conversation, "messages": messages}
                        ],
                    }
            except ConversationAccessDenied:
                continue
            self.last_conversation = conversation
            batch_messages = cast(
                list[dict[str, Any]], batch["conversations"][0]["messages"]
            )
            incoming = [
                item
                for item in batch_messages
                if item["sender"] != self.agent and not item.get("handler_error", False)
            ]
            cancelled = False
            try:
                if incoming:
                    await self.handle(batch)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                logger.exception("Messaging batch failed or deferred: %s", conversation)
                admitted = self.executor.store.talk.get(scope, cursor)
                if admitted is not None and self.executor.store.talk.attempt_notice(
                    admitted.run
                ):
                    await self._send_failure_reply(conversation, incoming, exc)
            finally:
                admitted = self.executor.store.talk.get(scope, cursor)
                if not incoming or admitted is not None:
                    run = (
                        self.executor.store.get_run(run_id=admitted.run)
                        if admitted is not None
                        else None
                    )
                    if admitted is not None and run is None:
                        raise MessagingError("Stored talk receipt has no Run")
                    if run is None or run.status not in {"pending", "running"}:
                        self.pending = conversation, through
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

    async def _send_failure_reply(
        self,
        conversation: str,
        messages: list[dict[str, Any]],
        error: Exception,
    ) -> None:
        """Reply to the latest external message when its handler fails."""

        source = next(
            (
                message
                for message in reversed(messages)
                if message["sender"] != self.agent
            ),
            None,
        )
        if source is None:
            return
        try:
            await self.client.send(
                conversation,
                body=f"Message handling failed: {error}"[:2048],
                in_reply_to=source["id"],
                handler_error=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Unable to send message-handler failure reply: %s", conversation
            )

    async def handle(self, batch: dict[str, Any]) -> None:
        conversation = batch["conversations"][0]["conversation"]
        scope = ConversationScope(self.identity, conversation)
        after, through = batch["cursor"]["after"], batch["cursor"]["through"]
        receipt = self.executor.store.talk.get(scope, after)
        if receipt is not None:
            record = self.executor.store.get_run(run_id=receipt.run)
            if record is None:
                raise MessagingError("Stored talk receipt has no Run")
        else:
            await self.client.get_rules(conversation)
            setup, state = self.get_setup(), self.get_state()
            admitted = TalkBatch(
                scope, after, through, json.dumps(batch, ensure_ascii=False)
            )
            spec = RunSpec(
                setup=setup,
                state=state,
                thread=self.threads.ensure_talk(
                    scope,
                    thread_id=talk_thread_id(conversation, agent_name=self.layout.name),
                ),
                workdir=self.executor.resolve_workdir(
                    setup, state, workdir=None, inherit_thread_workdir=False
                ),
                bindings=RunBindings(),
                driver="talk",
                limits=setup.limits,
                conversation=scope,
                talk_batch=admitted,
                input=CallInput({"_": (TextPart(admitted.payload),)}),
            )
            handle = self.executor.run(spec, request_id=admitted.request_id)
            try:
                record = await handle
            except asyncio.CancelledError:
                handle.cancel(reason="Messaging handler stopped")
                await handle
                raise
        if record.status in {"pending", "running"}:
            return
        if record.status != "succeeded":
            detail = (
                self.executor.store.resolve_error(record.error)
                if record.error is not None
                else record.status
            )
            raise MessagingError(f"Message handler {record.id}: {detail}")

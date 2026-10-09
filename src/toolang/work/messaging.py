"""Agent-owned message polling and serial batch handling."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
import json
import logging
from typing import Any

from toolang.base.types.message import TextPart, ToolResultPart
from toolang.base.types.policy import RunBindings
from toolang.common.files import atomic_write_text
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.runnables import resolve_runnable_reference, runnable_fallback
from toolang.execution.threads import ThreadManager
from toolang.execution.types import ThreadPrefix
from toolang.lang.input import resolve_runnable_input
from toolang.teaming.types import MessageReceiver
from toolang.teaming.errors import MessagingError
from toolang.teaming.schemas import Message, stream_id
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

logger = logging.getLogger(__name__)
_CONTEXT = 20
_INSTRUCTIONS = """Handle this message batch as the receiving agent.
Use msg/targets to find agents and conversations; never inspect other agents'
threads, files, configuration, or shell sessions to discover how to communicate.
Reply using msg/send to the source group, with in_reply_to set to the source
message ID, unless explicitly asked to contact someone elsewhere. The tool owns
the envelope and delivery. Never resend a successful tool send. Your final output
is a brief handling summary, not a chat message or a replies JSON object.
Previous messages/results are context only. Do not re-handle your own messages,
acknowledgements, completed discussions, or requests addressed only to others.
If no action is needed, simply note that. Do not send acknowledgements of acknowledgements.
Delegate substantial work to an available spawn target, preserving source context;
tell the worker to send its result via msg/send. Do not await it in this handler.
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
        self.room = layout.channel_room("messaging")
        self.path = self.room / "unselected.json"
        self.saved: dict[str, Any] = {}
        self.last_group = ""
        self.gaps: dict[str, str | None] = {}

    def load(self) -> None:
        with self.client.session() as identity:
            path = self.room / f"v1-{identity}.json"
        saved = {}
        try:
            if path.exists():
                saved = json.loads(path.read_text())
                if not isinstance(saved, dict):
                    raise ValueError("expected object")
                for value in saved.values():
                    stream_id(value["cursor"])
                    if not isinstance(value["messages"], list):
                        raise ValueError("expected message context")
        except (OSError, ValueError, KeyError, TypeError, MessagingError) as exc:
            raise MessagingError(f"Invalid messaging checkpoint: {path}") from exc
        self.path, self.saved = path, saved
        self.gaps, self.last_group = {}, ""

    async def consume(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.poll()
            await self._wait(stop, 0.5)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    async def poll(self) -> None:
        # Pin discovery for the whole batch. A Hub/backend switch fails this
        # attempt instead of mixing data or credentials across checkpoints.
        with self.client.session() as identity:
            path = self.room / f"v1-{identity}.json"
            if path != self.path:
                self.load()
            await self._poll()

    async def _poll(self) -> None:
        groups = await self.client.contacts()
        # Round-robin independently of membership changes.
        groups.sort(key=lambda g: (g["group"] <= self.last_group, g["group"]))
        for info in groups:
            group = info["group"]
            previous = self.saved.get(
                group, {"cursor": "0-0", "messages": [], "result": None}
            )
            gap = await self.client.check_cursor(group, previous["cursor"])
            if gap and gap != self.gaps.get(group):
                logger.warning("%s: %s", group, gap)
            self.gaps[group] = gap
            entries = await self.client.read(
                group, after=previous["cursor"], count=_CONTEXT
            )
            if not entries:
                continue
            self.last_group = group
            messages = []
            for sid, fields in entries:
                try:
                    message = Message.decode(fields["data"])
                except (MessagingError, KeyError):
                    logger.warning("Skipping malformed message: %s %s", group, sid)
                    continue
                messages.append({**message.data(), "stream_id": sid})
            outcome = previous["result"]
            try:
                if any(m["sender"] != self.agent for m in messages):
                    outcome = {"status": "failed", "replies": []}
                    await self.handle(
                        {
                            "agent": self.agent,
                            "available_groups": groups,
                            "groups": [
                                {
                                    "group": group,
                                    "messages": messages,
                                    "previous": previous,
                                }
                            ],
                        },
                        outcome,
                    )
            except asyncio.CancelledError:
                if outcome is not None:
                    outcome["status"] = "cancelled"
                raise
            except Exception as exc:
                logger.exception("Messaging batch failed; skipping %s", group)
                if outcome is not None:
                    outcome["error"] = str(exc)
            finally:
                saved = {
                    **self.saved,
                    group: {
                        "cursor": entries[-1][0],
                        "messages": (previous["messages"] + messages)[-_CONTEXT:],
                        "result": outcome,
                    },
                }
                atomic_write_text(self.path, json.dumps(saved, ensure_ascii=False))
                self.saved = saved
            return

    async def handle(self, batch: dict[str, Any], outcome: dict[str, Any]) -> None:
        setup, state = self.get_setup(), self.get_state()
        custom = "msg" in state.agics
        runnable = (
            "agic:msg"
            if custom
            else setup.defaults.runnable or runnable_fallback(state, preferred="msg")
        )
        resolved = resolve_runnable_reference(state, runnable)
        if resolved.executable.kind != "agic":
            raise MessagingError("Messaging requires agic:msg or a default agic")
        text = ("" if custom else _INSTRUCTIONS) + json.dumps(batch, ensure_ascii=False)
        spec = RunSpec(
            setup=setup,
            state=state,
            thread=self.threads.create(prefix=ThreadPrefix.SCRIPT),
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
        outcome["run"] = handle.run_id
        try:
            record = await handle
        except asyncio.CancelledError:
            handle.cancel(reason="Messaging handler stopped")
            await handle
            raise
        finally:
            for step in self.executor.store.list_steps(run_id=handle.run_id):
                result = step.output.value if step.output is not None else None
                if (
                    isinstance(result, ToolResultPart)
                    and result.tool_name == "msg__send"
                    and result.error is None
                ):
                    outcome["replies"].append(result.output)
        if record.status != "succeeded":
            raise MessagingError(f"Message handler {record.id}: {record.status}")
        outcome.update(
            status="succeeded",
            noted=self.executor.store.run_output_text(run_id=record.id),
        )

"""Agent-owned messaging lifecycle and serial batch handling."""

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
from toolang.messaging.client import MessagingClient, RENEW_SECONDS, host_token
from toolang.messaging.config import MessagingConfig
from toolang.messaging.errors import MessagingError
from toolang.messaging.schemas import Message, stream_id
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

logger = logging.getLogger(__name__)
_CONTEXT = 20
_INSTRUCTIONS = """Handle this message batch as the receiving agent.
Use coop/contacts to find agents and conversations; never inspect other agents'
threads, files, configuration, or shell sessions to discover how to communicate.
Reply using coop/send to the source group, with in_reply_to set to the source
message ID, unless explicitly asked to contact someone elsewhere. The tool owns
the envelope and delivery. Never resend a successful tool send. Your final output
is a brief handling summary, not a chat message or a replies JSON object.
Previous messages/results are context only. Do not re-handle your own messages,
acknowledgements, completed discussions, or requests addressed only to others.
If no action is needed, simply note that. Do not send acknowledgements of acknowledgements.
Delegate substantial work to an available spawn target, preserving source context;
tell the worker to send its result via coop/send. Do not await it in this handler.
Batch:
"""


class MessagingLoop:
    def __init__(
        self,
        *,
        layout: AgentLayout,
        owner: str,
        config: MessagingConfig,
        executor: RunExecutor,
        threads: ThreadManager,
        get_agent_setup: Callable[[], AgentSetup],
        get_agent_state: Callable[[], AgentState],
        client: MessagingClient | None = None,
    ):
        self.agent, self.owner = layout.name, owner
        self.client = client or MessagingClient(config)
        self.token = host_token()
        self.executor, self.threads = executor, threads
        self.get_setup, self.get_state = get_agent_setup, get_agent_state
        self.path = layout.channel_room("messaging") / f"v1-{config.identity}.json"
        self.saved: dict[str, Any] = {}
        self.last_group = ""
        self.gaps: dict[str, str | None] = {}

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text())
            if not isinstance(saved, dict):
                raise ValueError("expected object")
            for value in saved.values():
                stream_id(value["cursor"])
                if not isinstance(value["messages"], list):
                    raise ValueError("expected message context")
            self.saved = saved
        except (OSError, ValueError, KeyError, TypeError, MessagingError) as exc:
            raise MessagingError(f"Invalid messaging checkpoint: {self.path}") from exc

    async def run(self, stop: asyncio.Event) -> None:
        delay = 0.5
        try:
            self.load()
            while not stop.is_set():
                try:
                    await self.client.register(self.agent, self.owner, self.token)
                    async with asyncio.TaskGroup() as tasks:
                        tasks.create_task(self._heartbeat(stop))
                        tasks.create_task(self._consume(stop))
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Messaging disconnected; reconnecting")
                    await self._wait(stop, delay)
                    delay = min(delay * 2, 5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Messaging stopped; check configuration and checkpoint")
        finally:
            with suppress(Exception):
                await self.client.unregister(self.agent, self.token)
            await self.client.close()

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._wait(stop, RENEW_SECONDS)
            if not stop.is_set():
                await self.client.renew(self.agent, self.token)

    async def _consume(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.poll()
            await self._wait(stop, 0.5)

    @staticmethod
    async def _wait(stop: asyncio.Event, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), seconds)

    async def poll(self) -> None:
        groups = await self.client.contacts(agent=self.agent)
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
                    and result.tool_name == "coop__send"
                    and result.error is None
                ):
                    outcome["replies"].append(result.output)
        if record.status != "succeeded":
            raise MessagingError(f"Message handler {record.id}: {record.status}")
        outcome.update(
            status="succeeded",
            noted=self.executor.store.run_output_text(run_id=record.id),
        )

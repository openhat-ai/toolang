"""A small, agent-owned polling loop for Valkey group messages."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from hashlib import sha256
import json
import logging
from pathlib import Path
from typing import Any

from valkey.asyncio import Valkey

from toolang.base.types.message import TextPart, ToolResultPart
from toolang.base.types.policy import RunBindings
from toolang.common.files import atomic_write_text
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.runnables import resolve_runnable_reference, runnable_fallback
from toolang.execution.types import ThreadPrefix
from toolang.execution.threads import ThreadManager
from toolang.lang.input import resolve_runnable_input
from toolang.setup import AgentSetup
from toolang.plugin.toolsets.coop import (
    MessagingConfig,
    joined_groups,
    validate_message,
)
from toolang.state.state import AgentState

logger = logging.getLogger(__name__)
_PER_GROUP = 20
_PER_BATCH = 100
_DEFAULT_INSTRUCTIONS = """Handle this batch of messages as the receiving agent.
available_groups lists your configured, joined groups and current members,
including idle groups. h_ is a human-owner DM, d_ is an agent-to-agent DM, and g_
is a shared group. Use these exact IDs with coop/send (wire name coop__send).
To contact someone, choose a listed group containing them: prefer a DM, otherwise
address them in a shared group. If no route exists, report that limitation.
coop/contacts can refresh this directory. Do not search files, other agents'
threads/configuration, shell commands, or services to discover how to send messages.
Send each reply with coop/send(group, body, in_reply_to). The tool handles the
message envelope and delivery and returns a receipt. Do not emit a replies JSON
object: your final output is only a brief handling summary, never delivered as chat.
Do not resend a successful send. Do not re-handle previous or own messages.
Messages addressed only to someone else normally need no reply or action from you.
For substantial work, prefer an available spawn target, preserve source group and
message context, and return without awaiting completion. Tell the spawned run to
use coop/send for its result; its final output is not automatically delivered.
Set in_reply_to to the source message ID when answering a specific message.

Batch:
"""


class MessagingLoop:
    """Read independent cursors and record each handler's tool receipts."""

    def __init__(
        self,
        *,
        layout: AgentLayout,
        executor: RunExecutor,
        threads: ThreadManager,
        get_agent_setup: Callable[[], AgentSetup],
        get_agent_state: Callable[[], AgentState],
        config: MessagingConfig,
        client: Valkey | None = None,
    ):
        self.name = layout.name
        self.executor = executor
        self.threads = threads
        self.get_setup = get_agent_setup
        self.get_state = get_agent_state
        server = sha256(config.url.encode()).hexdigest()[:16]
        self.path: Path = layout.channel_room("messaging") / f"{server}.json"
        self.saved: dict[str, Any] = (
            json.loads(self.path.read_text(encoding="utf-8"))
            if self.path.exists()
            else {}
        )
        self.groups = list(config.groups)
        self.client = (
            client
            if client is not None
            else Valkey.from_url(config.url, decode_responses=True, socket_timeout=5)
        )

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    await self.poll()
                except Exception:
                    logger.exception("Messaging poll failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=0.5)
                except TimeoutError:
                    pass
        finally:
            await self.client.aclose()

    async def poll(self) -> None:
        batches: list[dict[str, Any]] = []
        available_groups = await joined_groups(self.client, self.name, self.groups)
        joined = {item["group"] for item in available_groups}
        boundaries: dict[str, str] = {}
        remaining = _PER_BATCH
        # Rotate the first reader so a busy group cannot starve later groups.
        groups = self.groups[:]
        if groups:
            self.groups = groups[1:] + groups[:1]
        for group in groups:
            if remaining == 0:
                break
            if group not in joined:
                continue
            previous = self.saved.get(group, {})
            streams = await self.client.xread(
                {f"too:group:{group}:msg": previous.get("cursor", "0-0")},
                count=min(_PER_GROUP, remaining),
            )
            messages = []
            for _, entries in streams:
                for stream_id, fields in entries:
                    boundaries[group] = stream_id
                    remaining -= 1
                    try:
                        message = json.loads(fields["data"])
                        validate_message(message)
                    except (KeyError, TypeError, ValueError):
                        logger.warning(
                            "Skipping malformed message in %s at %s", group, stream_id
                        )
                        continue
                    messages.append({**message, "stream_id": stream_id})
            if group in boundaries:
                batches.append(
                    {
                        "group": group,
                        "messages": messages,
                        "previous": {
                            "messages": previous.get("messages", []),
                            "result": previous.get("result"),
                        },
                    }
                )
        if not batches:
            return
        outcome: dict[str, Any] | None = None
        try:
            if any(
                message["sender"] != self.name
                for batch in batches
                for message in batch["messages"]
            ):
                outcome = {"status": "failed", "replies": []}
                await self.handle(
                    {
                        "agent": self.name,
                        "available_groups": available_groups,
                        "groups": batches,
                    },
                    outcome,
                )
        except asyncio.CancelledError:
            if outcome is not None:
                outcome["status"] = "cancelled"
            raise
        except Exception as exc:
            if outcome is not None:
                outcome.update(status="failed", error=str(exc))
            logger.exception("Messaging batch failed; skipping it")
        finally:
            saved = dict(self.saved)
            for batch in batches:
                group = batch["group"]
                result = batch["previous"]["result"]
                if outcome is not None and any(
                    message["sender"] != self.name for message in batch["messages"]
                ):
                    result = {
                        **outcome,
                        "noted": outcome.get("noted", {}).get(group),
                        "replies": [
                            reply
                            for reply in outcome["replies"]
                            if reply["group"] == group
                        ],
                    }
                saved[group] = {
                    "cursor": boundaries[group],
                    "messages": (batch["previous"]["messages"] + batch["messages"])[
                        -_PER_GROUP:
                    ],
                    "result": result,
                }
            atomic_write_text(self.path, json.dumps(saved, ensure_ascii=False))
            self.saved = saved

    async def handle(self, batch: dict[str, Any], outcome: dict[str, Any]) -> None:
        state, setup = self.get_state(), self.get_setup()
        custom = "msg" in state.agics
        runnable = (
            "agic:msg"
            if custom
            else (setup.defaults.runnable or runnable_fallback(state, preferred="msg"))
        )
        resolved = resolve_runnable_reference(state, runnable)
        if resolved.executable.kind != "agic":
            raise ValueError("Messaging requires agic:msg or a default agic entry")
        text = json.dumps(batch, ensure_ascii=False)
        if not custom:
            text = _DEFAULT_INSTRUCTIONS + text
        thread = self.threads.create(prefix=ThreadPrefix.SCRIPT)
        spec = RunSpec(
            setup=setup,
            state=state,
            thread=thread,
            bindings=RunBindings(
                runnable=runnable,
                model=setup.defaults.model.ref if setup.defaults.model else None,
            ),
            model_request=setup.defaults.model,
            limits=setup.limits,
            input=resolve_runnable_input(
                resolved.executable,
                {"_": (TextPart(text),)},
                structs={
                    item.name: item for item in state.modules[resolved.module].structs
                },
            ),
        )
        handle = self.executor.run(spec)
        outcome["run"] = handle.run_id
        try:
            record = await handle
        finally:
            # Delivery already happened in the tools. Preserve receipts even when
            # later model work fails or is cancelled; never replay those sends.
            for step in self.executor.store.list_steps(run_id=handle.run_id):
                result = step.output.value if step.output is not None else None
                if (
                    isinstance(result, ToolResultPart)
                    and result.tool_name == "coop__send"
                    and result.error is None
                ):
                    outcome["replies"].append(
                        {"group": result.output["group"], **result.output["message"]}
                    )
        if record.status != "succeeded":
            raise ValueError(f"Message run {record.id} ended with {record.status}")
        # The final text is a run summary, not a delivery protocol. Only attach it
        # to a single source group; multi-group summaries stay in the run record.
        if len(batch["groups"]) == 1:
            outcome["noted"] = {
                batch["groups"][0]["group"]: self.executor.store.run_output_text(
                    run_id=record.id
                )
            }
        outcome["status"] = "succeeded"

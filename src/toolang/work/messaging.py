"""A small, agent-owned polling loop for Valkey group messages."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
from typing import Any, cast
from uuid import uuid4

from valkey.asyncio import Valkey
from valkey.asyncio.connection import parse_url

from toolang.base.types.message import TextPart
from toolang.base.types.policy import RunBindings
from toolang.common.files import atomic_write_text
from toolang.common.layout import AgentLayout
from toolang.execution.executor import RunExecutor, RunSpec
from toolang.execution.runnables import resolve_runnable_reference, runnable_fallback
from toolang.execution.types import ThreadPrefix
from toolang.execution.threads import ThreadManager
from toolang.lang.input import resolve_runnable_input
from toolang.setup import AgentSetup
from toolang.state.state import AgentState

logger = logging.getLogger(__name__)
_COMPONENT = r"(?:[A-Za-z0-9-]|%[0-9A-F]{2})+"
_GROUP = re.compile(rf"(?:d_{_COMPONENT}_{_COMPONENT}|[hg]_{_COMPONENT})\Z")
_PER_GROUP = 20
_PER_BATCH = 100
_DEFAULT_INSTRUCTIONS = """Handle this batch of group messages as the receiving agent.
Each group contains new messages and previous messages/handling results for context.
Do not re-handle previous messages or your own messages. Use existing run/spawn
capabilities when needed, preserving the source group and original message context.
Return only a JSON object: {"replies": [{"group": "group ID", "body": "reply text",
"in_reply_to": null}], "noted": {"group ID": "brief handling summary"}}.
An empty replies list means no reply is needed. Replies are sent directly to their
target groups after this run ends. Do not promise that spawned runs automatically
send replies; only this batch result is delivered by the messaging loop.

Batch:
"""


@dataclass(frozen=True, slots=True)
class MessagingConfig:
    url: str
    groups: tuple[str, ...]

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> MessagingConfig | None:
        value = config.get("messaging")
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("messaging must be a table")
        value = cast(Mapping[str, object], value)
        url, groups = value.get("url"), value.get("groups")
        if not isinstance(url, str) or not url.strip():
            raise ValueError("messaging.url must name an external Valkey server")
        parse_url(url)
        if not isinstance(groups, list) or not all(
            isinstance(group, str) and _GROUP.fullmatch(group) for group in groups
        ):
            raise ValueError("messaging.groups must contain d_, h_, or g_ group IDs")
        return cls(url=url, groups=tuple(dict.fromkeys(cast(list[str], groups))))


class MessagingLoop:
    """Read independent cursors, run one batch at a time, and deliver its replies."""

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
        boundaries: dict[str, str] = {}
        remaining = _PER_BATCH
        # Rotate the first reader so a busy group cannot starve later groups.
        groups = self.groups[:]
        if groups:
            self.groups = groups[1:] + groups[:1]
        for group in groups:
            if remaining == 0:
                break
            if not await self.client.execute_command(
                "SISMEMBER", f"too:group:{group}:members", self.name
            ):
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
                        _validate_message(message)
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
                await self.handle({"agent": self.name, "groups": batches}, outcome)
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
        record = await handle
        if record.status != "succeeded":
            raise ValueError(f"Message run {record.id} ended with {record.status}")
        output = json.loads(self.executor.store.run_output_text(run_id=record.id))
        if not isinstance(output, dict) or not isinstance(output.get("replies"), list):
            raise ValueError(
                "Message handler must return a JSON object with a replies list"
            )
        replies = output["replies"]
        notes = output.get("noted", {})
        if not isinstance(notes, dict) or not all(
            isinstance(note, str) for note in notes.values()
        ):
            raise ValueError("noted must map group IDs to handling summaries")
        for reply in replies:
            if not isinstance(reply, dict) or not isinstance(reply.get("group"), str):
                raise ValueError("Each reply requires a target group")
            _validate_message({"id": "reply", "sender": self.name, **reply})
            if not _GROUP.fullmatch(
                reply["group"]
            ) or not await self.client.execute_command(
                "SISMEMBER",
                f"too:group:{reply['group']}:members",
                self.name,
            ):
                raise ValueError(
                    f"Agent is not a member of reply group {reply['group']}"
                )
        outcome["noted"] = notes
        for reply in replies:
            message = {
                "id": str(uuid4()),
                "sender": self.name,
                "body": reply["body"],
                "in_reply_to": reply.get("in_reply_to"),
                "origin": {"agent": self.name, "run": record.id},
            }
            await self.client.xadd(
                f"too:group:{reply['group']}:msg",
                {"data": json.dumps(message, ensure_ascii=False)},
                maxlen=10000,
                approximate=True,
            )
            outcome["replies"].append({"group": reply["group"], **message})
        outcome["status"] = "succeeded"


def _validate_message(value: Any) -> None:
    if not isinstance(value, dict) or not all(
        isinstance(value.get(field), str) and value[field]
        for field in ("id", "sender", "body")
    ):
        raise ValueError("Messages require nonempty id, sender, and body strings")
    if value.get("in_reply_to") is not None and not isinstance(
        value["in_reply_to"], str
    ):
        raise ValueError("in_reply_to must be a message ID or null")

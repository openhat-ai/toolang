"""Send group messages through an externally managed Valkey server."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import re
from typing import Any, cast
from uuid import uuid4

from valkey.asyncio import Valkey
from valkey.asyncio.connection import parse_url

from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import CoopToolContext, ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool

_COMPONENT = r"(?:[A-Za-z0-9-]|%[0-9A-F]{2})+"
_GROUP = re.compile(rf"(?:d_{_COMPONENT}_{_COMPONENT}|[hg]_{_COMPONENT})\Z")


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


async def joined_groups(
    client: Valkey, agent: str, groups: tuple[str, ...] | list[str]
) -> list[dict[str, Any]]:
    """Describe configured groups the agent currently belongs to."""
    result = []
    for group in sorted(groups):
        members = await client.execute_command("SMEMBERS", f"too:group:{group}:members")
        if agent in members:
            result.append({"group": group, "members": sorted(members)})
    return result


class CoopToolset:
    name = "coop"
    description = "Find contacts and send messages to joined groups."

    def __init__(self, config: Mapping[str, Any], *, client: Valkey | None = None):
        self.config = (
            MessagingConfig.from_config({"messaging": config}) if config else None
        )
        self.client = client

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Valkey]:
        if self.config is None:
            raise ValueError("Group messaging is not configured")
        if self.client is not None:
            yield self.client
        else:
            async with Valkey.from_url(
                self.config.url,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            ) as client:
                yield client

    def tools(self) -> Mapping[str, Tool]:
        @tool(
            description="List your configured, joined groups and their member names, including idle DMs."
        )
        async def contacts(context: ToolContext | None = None) -> dict[str, Any]:
            assert context is not None
            async with self.connection() as client:
                assert self.config is not None
                return {
                    "groups": await joined_groups(
                        client, context.home.name, self.config.groups
                    )
                }

        @tool(
            description="Send one message now to a joined group. Supply text, not a JSON envelope. Returns the delivered message ID; use in_reply_to to answer a specific source message."
        )
        async def send(
            group: str,
            body: str,
            in_reply_to: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            if not _GROUP.fullmatch(group):
                raise ValueError("Invalid group ID")
            agent = context.home.name
            message = {
                "id": str(uuid4()),
                "sender": agent,
                "body": body,
                "in_reply_to": in_reply_to,
                "origin": {
                    "agent": agent,
                    "run": context.run_id
                    if isinstance(context, CoopToolContext)
                    else None,
                },
            }
            validate_message(message)
            async with self.connection() as client:
                if not await client.execute_command(
                    "SISMEMBER", f"too:group:{group}:members", agent
                ):
                    raise ValueError(f"Agent is not a member of reply group {group}")
                stream_id = await client.xadd(
                    f"too:group:{group}:msg",
                    {"data": json.dumps(message, ensure_ascii=False)},
                    maxlen=10000,
                    approximate=True,
                )
            return {"group": group, "stream_id": stream_id, "message": message}

        return {func.__name__: create_function_tool(func) for func in (contacts, send)}


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    return CoopToolset(config)


def validate_message(value: Any) -> None:
    if not isinstance(value, dict) or not all(
        isinstance(value.get(field), str) and value[field]
        for field in ("id", "sender", "body")
    ):
        raise ValueError("Messages require nonempty id, sender, and body strings")
    if value.get("in_reply_to") is not None and not isinstance(
        value["in_reply_to"], str
    ):
        raise ValueError("in_reply_to must be a message ID or null")

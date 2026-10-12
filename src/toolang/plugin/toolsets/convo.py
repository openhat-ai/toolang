"""Conversation tools from the current agent's perspective."""

from collections.abc import Mapping
from dataclasses import asdict
from functools import partial
from pathlib import Path
from typing import Any


from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import ConvoToolContext, ToolContext, ToolResult
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.base.utils.tool_descriptions import action_summary
from toolang.teaming.agent_client import AgentClient
from toolang.teaming.errors import MessagingError
from toolang.teaming.schemas import (
    Message,
    conversation_id,
    conversation_name,
    identifier,
    participant,
)


class ConvoToolset:
    name = "convo"
    description = (
        "Discover team members and conversations, peek at history, and write messages."
    )

    def __init__(self, config: Mapping[str, Any]):
        self.root = Path(config["root"]) if config else None

    def connection(self, context: ToolContext) -> AgentClient:
        if self.root is None:
            raise MessagingError("Teaming is disabled for this agent")
        return AgentClient(self.root, actor=f"agent:{context.home.name}")

    def tools(self) -> Mapping[str, Tool]:
        @tool(
            description="List team members as id, name, and online. Agent names are write targets; humans require a conversation ID. Human online status is null.",
            summary=partial(_summary, "team"),
        )
        async def team(context: ToolContext | None = None) -> list[dict[str, Any]]:
            assert context is not None
            async with self.connection(context) as client:
                return [
                    {
                        "id": row["member"],
                        "name": participant(row["member"]).name,
                        "online": row.get("online"),
                    }
                    for row in await client.team()
                ]

        @tool(
            name="list",
            description="List accessible conversations as id, name, and members. Name matches exactly. Members accepts agent names or agent:/human: IDs and matches all supplied members; exact requires the complete set, including yourself. Combine filters with AND. No filters lists all.",
            summary=partial(_summary, "list"),
        )
        async def list_conversations(
            name: str | None = None,
            members: list[str] | None = None,
            exact: bool = False,
            context: ToolContext | None = None,
        ) -> list[dict[str, Any]]:
            assert context is not None
            conversation_name(name)
            if members == [] or (exact and members is None):
                raise MessagingError(
                    "Member filtering requires a nonempty members array"
                )
            selected = (
                {
                    participant(value if ":" in value else f"agent:{value}").id
                    for value in members
                }
                if members is not None
                else None
            )
            async with self.connection(context) as client:
                rows = await client.contacts()
                return [
                    {
                        "id": row["conversation"],
                        "name": row["name"],
                        "members": row["participants"],
                    }
                    for row in rows
                    if (name is None or row["name"] == name)
                    and (
                        selected is None
                        or (
                            selected == set(row["participants"])
                            if exact
                            else selected <= set(row["participants"])
                        )
                    )
                ]

        @tool(
            description="Peek at an accessible conversation by ID or your existing DM with an agent name. Return metadata and the latest limit messages in chronological order; 0 returns metadata only. Does not consume messages or affect talk processing. Never creates a DM.",
            summary=partial(_summary, "peek"),
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 1000,
                        "default": 50,
                    },
                },
                "required": ["target"],
                "additionalProperties": False,
            },
        )
        async def peek(
            target: str,
            limit: int = 50,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            if not 0 <= limit <= 1000:
                raise MessagingError("limit must be between 0 and 1000")
            destination = _target(target, context)
            async with self.connection(context) as client:
                if destination.startswith("agent:"):
                    resolved = await client.resolve(destination)
                    if not resolved.exists:
                        raise MessagingError("No conversation exists with this agent")
                    destination = resolved.conversation
                info = await client.conversation(destination)
                rows = await client.history(destination, count=limit) if limit else []
                messages = []
                for _stream_id, fields in rows:
                    if "data" not in fields:
                        raise MessagingError("Malformed stored message")
                    messages.append(_message(Message.decode(fields["data"])))
                return {"conversation": asdict(info), "messages": messages}

        @tool(
            description="Write plain text to an agent name (your implicit DM) or an exact dm_/gc_ conversation ID. Names of conversations and typed participant targets are not accepted. A first DM write creates it atomically. Do not resend a confirmed write or automatically retry uncertain delivery.",
            summary=partial(_summary, "write"),
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "message": {"type": "string"},
                    "reply_to": {
                        "type": ["string", "null"],
                        "default": None,
                        "description": "ID of the message being replied to. Omit for a new message.",
                    },
                },
                "required": ["target", "message"],
                "additionalProperties": False,
            },
        )
        async def write(
            target: str,
            message: str,
            reply_to: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            destination = _target(target, context)
            async with self.connection(context) as client:
                receipt = await client.send(
                    destination,
                    body=message,
                    in_reply_to=reply_to,
                    run=context.run_id
                    if isinstance(context, ConvoToolContext)
                    else None,
                    thread=context.thread_id
                    if isinstance(context, ConvoToolContext)
                    else None,
                )
                return {
                    "conversation": receipt["conversation"],
                    "message": _message(Message(**receipt["message"])),
                }

        @tool(
            description="Create a group containing you, or create/reuse a DM with exactly two typed participants including you. Its optional name is editable and need not be unique.",
            summary=partial(_summary, "create"),
        )
        async def create(
            name: str | None = None,
            participants: list[str] | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return asdict(
                    await client.create_conversation(name, participants=participants)
                )

        @tool(
            description="Rename a conversation you participate in using its current revision from peek. Null clears the name. Returns the updated metadata and previous_name; a revision conflict requires a fresh peek.",
            summary=partial(_summary, "rename"),
        )
        async def rename(
            conversation: str,
            name: str | None,
            revision: int,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                previous = await client.conversation(conversation)
                if previous.revision != revision:
                    raise MessagingError(
                        "Conversation revision conflict; reload before renaming"
                    )
                updated = await client.rename_conversation(
                    conversation, name, revision=revision
                )
                return {**asdict(updated), "previous_name": previous.name}

        @tool(
            description="Join a custom conversation by its canonical gc_ ID. Direct and system conversations cannot be edited.",
            summary=partial(_summary, "join"),
        )
        async def join(
            conversation: str, context: ToolContext | None = None
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.join_conversation(conversation)

        @tool(
            description="Leave a custom conversation by its canonical gc_ ID. Preserve the conversation and its messages.",
            summary=partial(_summary, "leave"),
        )
        async def leave(
            conversation: str, context: ToolContext | None = None
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.leave_conversation(conversation)

        leaves = (
            create_function_tool(function)
            for function in (
                team,
                list_conversations,
                peek,
                write,
                create,
                rename,
                join,
                leave,
            )
        )
        return {leaf.name: leaf for leaf in leaves}


def _target(value: str, context: ToolContext) -> str:
    try:
        return conversation_id(value)
    except MessagingError:
        name = identifier(value)
    if name == context.home.name:
        raise MessagingError("Cannot write to or peek at a DM with yourself")
    return f"agent:{name}"


def _message(message: Message) -> dict[str, Any]:
    return {
        "id": message.id,
        "sender": message.sender,
        "message": message.body,
        "reply_to": message.in_reply_to,
    }


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _name(value: object) -> str:
    return (
        "unnamed" if value is None else f'"{value}"' if isinstance(value, str) else ""
    )


def _short_name(value: object) -> object:
    """Bound the old name without losing the new name or conversation ID."""
    return value[:17] + "..." if isinstance(value, str) and len(value) > 20 else value


def _summary(
    operation: str, args: Mapping[str, Any], result: ToolResult | None = None
) -> str:
    verbs = {
        "team": ("list", "Listing", "Listed"),
        "list": ("list", "Listing", "Listed"),
        "peek": ("read", "Reading", "Read"),
        "write": ("write", "Writing", "Wrote"),
        "create": ("create", "Creating", "Created"),
        "rename": ("rename", "Renaming", "Renamed"),
        "join": ("join", "Joining", "Joined"),
        "leave": ("leave", "Leaving", "Left"),
    }[operation]
    label = "conversation"
    target = _text(args.get("conversation"))
    if operation == "team":
        label, target = "team members", ""
    elif operation == "list":
        label, target = "conversations", _text(args.get("name"))
        members = args.get("members")
        if (
            isinstance(members, list)
            and members
            and all(isinstance(m, str) for m in members)
        ):
            match = "only" if args.get("exact") is True else "with"
            target += ("; " if target else "") + match + " " + ", ".join(members)
    elif operation in {"peek", "write"}:
        label = (
            "message"
            if operation == "write"
            else "conversation"
            if args.get("limit") == 0
            else "conversation messages"
        )
        target = _text(args.get("target"))
    elif operation == "create":
        target = _text(args.get("name"))
        members = args.get("participants")
        if (
            isinstance(members, list)
            and members
            and all(isinstance(m, str) for m in members)
        ):
            target += (" with " if target else "with ") + ", ".join(members)
        if result is not None and result.error is None:
            ref = _text(result.output.get("id"))
            if ref:
                target = f"{target} ({ref})" if target else ref
    elif operation == "rename" and "name" in args:
        name = _name(args["name"])
        if target and name:
            new_target = f"{target} → {name}"
            if (
                result is not None
                and result.error is None
                and "previous_name" in result.output
            ):
                old = _name(_short_name(result.output["previous_name"]))
                if old:
                    new_target = f"{old} → {name} ({target})"
            target = new_target
    return action_summary(result, verbs, label + (f": {target}" if target else ""))


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    return ConvoToolset(config)

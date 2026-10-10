"""Model-facing messaging tools backed by the shared teaming service."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import MsgToolContext, ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.teaming.errors import MessagingError
from toolang.teaming.agent_client import AgentClient


class MsgToolset:
    name = "msg"
    description = (
        "Discover message targets, send messages, and manage your conversations."
    )

    def __init__(self, config: Mapping[str, Any]):
        self.root = Path(config["root"]) if config else None

    def connection(self, context: ToolContext) -> AgentClient:
        if self.root is None:
            raise MessagingError("Teaming is disabled for this agent")
        return AgentClient(self.root, actor=f"agent:{context.home.name}")

    def tools(self) -> Mapping[str, Tool]:
        @tool(
            description="List registered agents/humans and accessible conversations with canonical targets, membership, presence, and the metadata revision required for renaming."
        )
        async def targets(context: ToolContext | None = None) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.targets()

        @tool(
            description="Send plain text to a canonical agent:, human:, dm_, or gc_ target. Reply in the source conversation using in_reply_to when appropriate; never resend a confirmed send."
        )
        async def send(
            target: str,
            body: str,
            in_reply_to: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.send(
                    target,
                    body=body,
                    in_reply_to=in_reply_to,
                    run=context.run_id if isinstance(context, MsgToolContext) else None,
                    thread=context.thread_id
                    if isinstance(context, MsgToolContext)
                    else None,
                )

        @tool(
            description="Create a GC containing you, or create/reuse a DM with exactly two typed participants including you. Its optional name is editable and need not be unique."
        )
        async def create_conversation(
            name: str | None = None,
            participants: list[str] | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.create_conversation(name, participants=participants)

        @tool(
            description="Join a custom conversation by its canonical gc_ ID. Direct and system conversations cannot be edited."
        )
        async def join_conversation(
            conversation: str, context: ToolContext | None = None
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.join_conversation(conversation)

        @tool(
            description="Leave a custom conversation by its canonical gc_ ID. This preserves the conversation and its messages."
        )
        async def leave_conversation(
            conversation: str, context: ToolContext | None = None
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.leave_conversation(conversation)

        @tool(
            description="Rename a conversation you participate in, using its current metadata revision. Null clears the name."
        )
        async def rename_conversation(
            conversation: str,
            name: str | None,
            revision: int,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.rename_conversation(
                    conversation, name, revision=revision
                )

        @tool(
            description="Resolve a participant, canonical conversation ID, or explicit conversation name without creating anything."
        )
        async def resolve(
            target: str, by_name: bool = False, context: ToolContext | None = None
        ) -> dict[str, Any]:
            from dataclasses import asdict

            assert context is not None
            async with self.connection(context) as client:
                return asdict(
                    await client.resolve(target, kind="name" if by_name else None)
                )

        return {
            function.__name__: create_function_tool(function)
            for function in (
                targets,
                send,
                create_conversation,
                rename_conversation,
                join_conversation,
                leave_conversation,
                resolve,
            )
        }


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    return MsgToolset(config)

"""Model-facing messaging tools backed by the shared teaming service."""

from collections.abc import Mapping
from typing import Any

from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import MsgToolContext, ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.teaming.config import BackendConfig
from toolang.teaming.errors import MessagingError
from toolang.teaming.messaging import MessagingClient
from toolang.teaming.schemas import target as parse_target


class MsgToolset:
    name = "msg"
    description = "Discover message targets, send messages, and manage your groups."

    def __init__(self, config: Mapping[str, Any]):
        self.config = BackendConfig(config["url"]) if config else None

    def connection(self, context: ToolContext) -> MessagingClient:
        if self.config is None:
            raise MessagingError("Teaming is disabled for this agent")
        return MessagingClient(self.config, actor=f"agent:{context.home.name}")

    def tools(self) -> Mapping[str, Tool]:
        @tool(
            description="List registered agents/humans and accessible groups with canonical targets, membership, and presence."
        )
        async def targets(context: ToolContext | None = None) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.targets()

        @tool(
            description="Send plain text to a canonical agent:, human:, or group: target. Reply in the source group using in_reply_to when appropriate; never resend a confirmed send."
        )
        async def send(
            target: str,
            body: str,
            in_reply_to: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            parse_target(target)
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
            description="Create a custom group containing you. Supply its readable ID, without the group: prefix."
        )
        async def create_group(
            name: str, context: ToolContext | None = None
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.create_group(name)

        @tool(
            description="Join a custom group by its canonical group: target. Direct and system groups cannot be edited."
        )
        async def join_group(
            group: str, context: ToolContext | None = None
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.join_group(group)

        @tool(
            description="Leave a custom group by its canonical group: target. This preserves the group and its messages."
        )
        async def leave_group(
            group: str, context: ToolContext | None = None
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection(context) as client:
                return await client.leave_group(group)

        return {
            function.__name__: create_function_tool(function)
            for function in (targets, send, create_group, join_group, leave_group)
        }


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    return MsgToolset(config)

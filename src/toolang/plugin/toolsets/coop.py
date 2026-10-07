"""Model-facing messaging tools. The transport owns the wire protocol."""

from collections.abc import Mapping
from typing import Any

from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import CoopToolContext, ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.messaging.client import MessagingClient
from toolang.messaging.config import MessagingConfig
from toolang.messaging.errors import MessagingError
from toolang.messaging.schemas import agent_dm


class CoopToolset:
    name = "coop"
    description = "Discover contacts and send messages to agents and humans."

    def __init__(self, config: Mapping[str, Any]):
        self.config = (
            MessagingConfig.from_config({"messaging": config}) if config else None
        )

    def connection(self) -> MessagingClient:
        if self.config is None:
            raise MessagingError("Messaging is not configured")
        return MessagingClient(self.config)

    def tools(self) -> Mapping[str, Tool]:
        @tool(
            description="List registered agents and joined conversations. For an agent DM use the listed dm group ID; owner DMs belong to the named agent."
        )
        async def contacts(context: ToolContext | None = None) -> dict[str, Any]:
            assert context is not None
            async with self.connection() as client:
                agents = await client.agents()
                return {
                    "agents": [
                        {"name": name, "dm": agent_dm(context.home.name, name)}
                        for name in sorted(agents)
                        if name != context.home.name
                    ],
                    "groups": await client.contacts(agent=context.home.name),
                }

        @tool(
            description="Send a message now. Supply a canonical group ID and plain body text; the tool creates the JSON envelope. Use in_reply_to for a specific reply. Prefer replying in the source group unless asked to contact someone elsewhere."
        )
        async def send(
            group: str,
            body: str,
            in_reply_to: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None
            async with self.connection() as client:
                return await client.send(
                    group,
                    sender=context.home.name,
                    body=body,
                    agent=True,
                    run=context.run_id
                    if isinstance(context, CoopToolContext)
                    else None,
                    in_reply_to=in_reply_to,
                )

        return {
            function.__name__: create_function_tool(function)
            for function in (contacts, send)
        }


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    return CoopToolset(config)

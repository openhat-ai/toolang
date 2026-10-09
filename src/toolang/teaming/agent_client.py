"""Agent messaging, presence, and event publication through Hub HTTP."""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from .client import HubClient
from .discovery import host_token, hub_connection
from .schemas import HubConnection, Message, target


class AgentClient(HubClient):
    def __init__(
        self,
        root: Path,
        *,
        actor: str,
        token: str | None = None,
        connection: Callable[[], HubConnection] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        target(actor, kind="agent")
        self.token = token if token is not None else host_token()
        super().__init__(
            connection or (lambda: hub_connection(root)),
            actor=actor,
            prefix="/agents/" + quote(actor, safe=""),
            lease=self.token,
            transport=transport,
        )

    async def register(self, owner: str, *, endpoint: str = "") -> None:
        # Owner is supplied by the hosting boundary; Hub derives authority from
        # its own root configuration, never a caller-provided owner field.
        await self._request("PUT", "/lease", json={"endpoint": endpoint})

    async def renew(self) -> None:
        await self._request("PATCH", "/lease")

    async def unregister(self) -> None:
        await self._request("DELETE", "/lease")

    async def send(
        self,
        destination: str,
        *,
        body: str,
        in_reply_to: str | None = None,
        run: str | None = None,
        thread: str | None = None,
    ) -> dict[str, Any]:
        message = Message.create(
            self.actor, body, in_reply_to, {"thread": thread, "run": run}
        )
        return await self._request(
            "POST",
            "/msg/messages",
            message_id=message.id,
            json={
                "id": message.id,
                "target": destination,
                "body": message.body,
                "in_reply_to": message.in_reply_to,
                "thread": thread,
                "run": run,
            },
        )


class AgentEventClient:
    """The exporter port shares the lifecycle's HTTP connection and lease."""

    def __init__(self, client: AgentClient) -> None:
        self.client = client

    async def initialize(self) -> dict[str, str]:
        return (await self.client._request("GET", "/events/state"))["meta"]

    async def capture(
        self, agent: str | None = None
    ) -> tuple[dict[str, str], dict[str, Any], dict[str, str]]:
        if agent != self.client.actor:
            raise ValueError("Publication state belongs to this agent")
        data = await self.client._request("GET", "/events/state")
        return (
            data["meta"],
            {self.client.actor: data["origin"]} if data["origin"] else {},
            {},
        )

    def _body(self, op: dict[str, Any]) -> dict[str, Any]:
        if op["agent"] != self.client.actor or op["token"] != self.client.token:
            raise ValueError("Publication identity does not match the client")
        return {
            key: value for key, value in op.items() if key not in {"agent", "token"}
        }

    async def commit(self, op: dict[str, Any]) -> str:
        return (
            await self.client._request("POST", "/events/commits", json=self._body(op))
        )["stream_id"]

    async def stage(self, op: dict[str, Any]) -> None:
        body = self._body(op)
        generation = body.pop("generation")
        await self.client._request(
            "PUT", f"/events/staging/{quote(generation, safe='')}", json=body
        )

    async def abandon(self, agent: str, generation: str, *, token: str) -> None:
        if agent != self.client.actor or token != self.client.token:
            raise ValueError("Publication identity does not match the client")
        await self.client._request(
            "DELETE", f"/events/staging/{quote(generation, safe='')}"
        )

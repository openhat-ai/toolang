"""Shared Hub HTTP messaging transport; no backend-driver dependencies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Self
from urllib.parse import quote

import httpx

from toolang.execution.errors import SnapshotLimitError
from .errors import (
    BackendUnavailable,
    HubIdentityChanged,
    MessagingError,
    SendUnconfirmed,
    EventProtocolError,
    EventRecoveryRequired,
)
from .schemas import Conversation, HubConnection, Message, stream_id, target


class HubClient:
    def __init__(
        self,
        config: HubConnection | Callable[[], HubConnection],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        actor: str | None = None,
        prefix: str = "",
        lease: str | None = None,
    ) -> None:
        self._resolve: Callable[[], HubConnection] = (
            (lambda: config) if isinstance(config, HubConnection) else config
        )
        self.actor = actor if actor is not None else self.config.human
        self._prefix, self._lease = prefix, lease
        self._http = httpx.AsyncClient(
            timeout=15,
            trust_env=False,
            transport=transport,
        )

    @property
    def config(self) -> HubConnection:
        return self._resolve()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, *, message_id: str | None = None, **kwargs: Any
    ) -> Any:
        connection = self.config
        headers = {
            "X-Toolang-Backend": connection.identity,
            "X-Toolang-Human": quote(connection.human, safe=""),
        }
        if self._lease is not None:
            headers["X-Toolang-Agent-Lease"] = self._lease
        try:
            response = await self._http.request(
                method,
                connection.endpoint + self._prefix + path,
                headers=headers,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            if message_id is not None:
                raise SendUnconfirmed(
                    f"Send unconfirmed for {message_id}; inspect history before retrying"
                ) from exc
            raise BackendUnavailable(
                "Hub is unavailable; check 'too hub status' or run 'too hub start'"
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            if message_id is not None:
                raise SendUnconfirmed(f"Send unconfirmed for {message_id}") from exc
            raise MessagingError("Invalid Hub response") from exc
        if response.is_success:
            if message_id is not None:
                try:
                    receipt = Message(**data["message"])
                    target(data["group"], kind="group")
                    if (
                        receipt.id != message_id
                        or receipt.sender != self.actor
                        or stream_id(data["stream_id"]) == (0, 0)
                    ):
                        raise ValueError("Mismatched send receipt")
                except (KeyError, TypeError, ValueError, MessagingError) as exc:
                    raise SendUnconfirmed(
                        f"Send unconfirmed for {message_id}; invalid Hub receipt"
                    ) from exc
            return data
        detail = (
            data.get("detail", "Hub request failed")
            if isinstance(data, dict)
            else "Hub request failed"
        )
        code = data.get("code") if isinstance(data, dict) else None
        if code == "recovery_required":
            raise EventRecoveryRequired(str(detail))
        if code == "protocol_error":
            raise EventProtocolError(str(detail))
        if response.status_code == 413:
            raise SnapshotLimitError(str(detail))
        if code == "send_unconfirmed":
            raise SendUnconfirmed(f"Send unconfirmed for {message_id}: {detail}")
        if code == "backend_unavailable":
            raise BackendUnavailable(str(detail))
        if message_id and response.status_code >= 500:
            raise SendUnconfirmed(f"Send unconfirmed for {message_id}: {detail}")
        if code == "hub_changed":
            if self._lease is not None:
                raise BackendUnavailable("Hub identity changed; reconnecting")
            raise HubIdentityChanged("Hub identity changed; reopen Text")
        raise MessagingError(str(detail))

    async def agents(self) -> dict[str, str]:
        return await self._request("GET", "/msg/agents")

    async def targets(self) -> dict[str, Any]:
        return await self._request("GET", "/msg/targets")

    async def contacts(self, *, include_preview: bool = False) -> list[dict[str, Any]]:
        return await self._request(
            "GET", "/msg/groups", params={"include_preview": include_preview}
        )

    async def resolve(self, value: str, *, kind: str | None = None) -> str:
        data = await self._request(
            "POST", "/msg/resolve", json={"target": value, "kind": kind}
        )
        return data["group"]

    async def conversation(self, group: str) -> Conversation:
        data = await self._request("GET", self._group(group))
        return Conversation(
            data["id"],
            data["kind"],
            tuple(data["members"]),
            data["display_name"],
            data["system"],
        )

    async def create_group(self, name: str) -> dict[str, Any]:
        return await self._request("POST", "/msg/groups", json={"name": name})

    async def join_group(self, group: str) -> dict[str, Any]:
        return await self._request("PUT", self._group(group) + "/membership")

    async def leave_group(self, group: str) -> dict[str, Any]:
        return await self._request("DELETE", self._group(group) + "/membership")

    async def send(
        self, destination: str, *, body: str, in_reply_to: str | None = None
    ) -> dict[str, Any]:
        message = Message.create(self.actor, body, in_reply_to)
        return await self._request(
            "POST",
            "/msg/messages",
            message_id=message.id,
            json={
                "id": message.id,
                "target": destination,
                "body": message.body,
                "in_reply_to": message.in_reply_to,
            },
        )

    async def read(
        self, group: str, *, after: str = "0-0", count: int = 100
    ) -> list[tuple[str, dict[str, str]]]:
        return await self._messages(group, {"after": after, "count": count})

    async def history(
        self, group: str, *, count: int = 200
    ) -> list[tuple[str, dict[str, str]]]:
        return await self._messages(group, {"count": count})

    async def _messages(
        self, group: str, params: dict[str, str | int]
    ) -> list[tuple[str, dict[str, str]]]:
        rows = await self._request(
            "GET", self._group(group) + "/messages", params=params
        )
        return [
            (row["stream_id"], {"data": row["data"]} if row["data"] is not None else {})
            for row in rows
        ]

    async def check_cursor(self, group: str, cursor: str) -> str | None:
        data = await self._request(
            "GET", self._group(group) + "/cursor", params={"after": cursor}
        )
        return data["notice"]

    @staticmethod
    def _group(group: str) -> str:
        return "/msg/groups/" + quote(group, safe="")

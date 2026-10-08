"""Hub transport used by human clients, without backend-driver dependencies."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from .errors import BackendUnavailable, MessagingError, SendUnconfirmed
from .schemas import Conversation, HubConnection, Message


class HubClient:
    def __init__(
        self,
        config: HubConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config, self.actor = config, config.human
        self._http = httpx.AsyncClient(
            base_url=config.endpoint,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=15,
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> HubClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    async def _request(
        self, method: str, path: str, *, message_id: str | None = None, **kwargs: Any
    ) -> Any:
        try:
            response = await self._http.request(method, path, **kwargs)
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
            return data
        detail = (
            data.get("detail", "Hub request failed")
            if isinstance(data, dict)
            else "Hub request failed"
        )
        code = data.get("code") if isinstance(data, dict) else None
        if code == "send_unconfirmed":
            raise SendUnconfirmed(f"Send unconfirmed for {message_id}: {detail}")
        if code == "backend_unavailable":
            raise BackendUnavailable(str(detail))
        if message_id and response.status_code >= 500:
            raise SendUnconfirmed(f"Send unconfirmed for {message_id}: {detail}")
        if response.status_code == 401:
            raise MessagingError("Hub identity changed; reopen Text")
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

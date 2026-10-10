"""Shared Hub HTTP messaging transport; no backend-driver dependencies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Self, TypeVar, overload
import time
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter

from toolang.execution.errors import SnapshotLimitError
from .errors import (
    BackendUnavailable,
    ConversationAccessDenied,
    StorageIntegrityError,
    MessagingError,
    SendUnconfirmed,
    EventProtocolError,
    EventRecoveryRequired,
)
from .schemas import (
    Conversation,
    ConversationSummary,
    ConversationStatistics,
    GlobalStatistics,
    TeamMember,
    Targets,
    HubConnection,
    Message,
    Resolution,
    conversation_id,
    stream_id,
)


T = TypeVar("T")
_TEAM = TypeAdapter(list[TeamMember])
_CONTACTS = TypeAdapter(list[ConversationSummary])
_TARGETS = TypeAdapter(Targets)
_GLOBAL_STATS = TypeAdapter(GlobalStatistics)
_CONVO_STATS = TypeAdapter(ConversationStatistics)


def _response(adapter: TypeAdapter[T], data: Any) -> T:
    try:
        return adapter.validate_python(data)
    except (ValueError, TypeError, KeyError, MessagingError) as exc:
        raise MessagingError("Invalid Hub response") from exc


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
                    conversation_id(data["conversation"])
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
        if code == "conversation_access_denied":
            raise ConversationAccessDenied(str(detail))
        if code == "storage_integrity":
            raise StorageIntegrityError(str(detail))
        if code == "backend_unavailable":
            raise BackendUnavailable(str(detail))
        if message_id and response.status_code >= 500:
            raise SendUnconfirmed(f"Send unconfirmed for {message_id}: {detail}")
        if code == "hub_changed":
            if self._lease is not None:
                raise BackendUnavailable("Hub identity changed; reconnecting")
            raise MessagingError("Hub identity changed; reopen Talk")
        raise MessagingError(str(detail))

    async def agents(self) -> dict[str, str]:
        return await self._request("GET", "/msg/agents")

    @staticmethod
    def _presence(rows: list[TeamMember]) -> list[TeamMember]:
        now = time.time() * 1000
        result = []
        for row in rows:
            local = row.copy()
            deadline = row["deadline"]
            local["online"] = (
                (deadline is not None and deadline > now)
                if row["member"].startswith("agent:")
                else None
            )
            result.append(local)
        return result

    async def team(self) -> list[TeamMember]:
        return self._presence(_response(_TEAM, await self._request("GET", "/team")))

    async def targets(self) -> Targets:
        data = _response(_TARGETS, await self._request("GET", "/msg/targets"))
        data["participants"] = self._presence(data["participants"])
        return data

    async def contacts(
        self, *, include_preview: bool = False
    ) -> list[ConversationSummary]:
        return _response(
            _CONTACTS,
            await self._request(
                "GET", "/msg/conversations", params={"include_preview": include_preview}
            ),
        )

    async def resolve(
        self, value: str, *, kind: str | None = None, create: bool = False
    ) -> Resolution:
        data = await self._request(
            "POST",
            "/msg/resolve",
            json={"target": value, "kind": kind, "create": create},
        )
        return Resolution(
            data["conversation"], tuple(data["participants"]), data["exists"]
        )

    @staticmethod
    def _decode_conversation(data: dict[str, Any]) -> Conversation:
        try:
            values: dict[str, Any] = {
                **data,
                "participants": tuple(data["participants"]),
            }
            return Conversation(**values)
        except (ValueError, TypeError, KeyError, MessagingError) as exc:
            raise MessagingError("Invalid Hub conversation response") from exc

    async def conversation(self, conversation: str) -> Conversation:
        data = await self._request("GET", self._conversation(conversation))
        return self._decode_conversation(data)

    async def create_conversation(
        self, name: str | None = None, *, participants: list[str] | None = None
    ) -> Conversation:
        data = await self._request(
            "POST",
            "/msg/conversations",
            json={"name": name, "participants": participants},
        )
        return self._decode_conversation(data)

    async def rename_conversation(
        self, conversation: str, name: str | None, *, revision: int
    ) -> Conversation:
        data = await self._request(
            "PATCH",
            self._conversation(conversation),
            json={"name": name, "revision": revision},
        )
        return self._decode_conversation(data)

    async def join_conversation(self, conversation: str) -> dict[str, Any]:
        return await self._request(
            "PUT", self._conversation(conversation) + "/participants"
        )

    async def leave_conversation(self, conversation: str) -> dict[str, Any]:
        return await self._request(
            "DELETE", self._conversation(conversation) + "/participants"
        )

    @overload
    async def statistics(self, conversation: str) -> ConversationStatistics: ...

    @overload
    async def statistics(self, conversation: None = None) -> GlobalStatistics: ...

    async def statistics(
        self, conversation: str | None = None
    ) -> GlobalStatistics | ConversationStatistics:
        data = await self._request(
            "GET",
            self._conversation(conversation) + "/stats"
            if conversation
            else "/msg/stats",
        )
        return (
            _response(_CONVO_STATS, data)
            if conversation
            else _response(_GLOBAL_STATS, data)
        )

    async def send(
        self,
        destination: str,
        *,
        body: str,
        in_reply_to: str | None = None,
        participants: list[str] | None = None,
    ) -> dict[str, Any]:
        message = Message.create(self.actor, body, in_reply_to)
        return await self._request(
            "POST",
            "/msg/messages",
            message_id=message.id,
            json={
                "id": message.id,
                "target": destination,
                "participants": participants,
                "body": message.body,
                "in_reply_to": message.in_reply_to,
            },
        )

    async def read(
        self, conversation: str, *, after: str = "0-0", count: int = 100
    ) -> list[tuple[str, dict[str, str]]]:
        return await self._messages(conversation, {"after": after, "count": count})

    async def history(
        self, conversation: str, *, count: int = 200
    ) -> list[tuple[str, dict[str, str]]]:
        return await self._messages(conversation, {"count": count})

    async def _messages(
        self, conversation: str, params: dict[str, str | int]
    ) -> list[tuple[str, dict[str, str]]]:
        rows = await self._request(
            "GET", self._conversation(conversation) + "/messages", params=params
        )
        return [
            (row["stream_id"], {"data": row["data"]} if row["data"] is not None else {})
            for row in rows
        ]

    async def check_cursor(self, conversation: str, cursor: str) -> str | None:
        data = await self._request(
            "GET",
            self._conversation(conversation) + "/cursor",
            params={"after": cursor},
        )
        return data["notice"]

    @staticmethod
    def _conversation(conversation: str) -> str:
        return "/msg/conversations/" + quote(conversation, safe="")

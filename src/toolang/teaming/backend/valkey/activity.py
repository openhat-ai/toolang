"""Lease-fenced activity cache in Valkey."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from toolang.execution.activity import ActivityQuery
from toolang.execution.schemas import ActivitySnapshot
from ...activity_cache import publication_key, select_cached
from .activity_scripts import SAVE
from .keys import TEAM, PRESENCE, activity_key

if TYPE_CHECKING:
    from .backend import ValkeyBackend


class ValkeyActivity:
    """Persist bounded absolute snapshots under the existing agent lease."""

    def __init__(self, backend: ValkeyBackend) -> None:
        self.backend = backend

    async def lease(self, agent: str) -> dict[str, str]:
        return (await self.backend.lease_info(agent))["lease"] or {}

    async def save(self, agent: str, token: str, pages: list[ActivitySnapshot]) -> bool:
        slot, query = publication_key(agent, pages)
        return bool(
            await self.backend._fenced_eval(
                agent,
                SAVE,
                [TEAM, activity_key(agent), PRESENCE],
                [
                    token,
                    slot,
                    json.dumps(
                        {
                            "query": query,
                            "pages": [page.model_dump() for page in pages],
                        },
                        separators=(",", ":"),
                    ),
                    agent,
                ],
            )
        )

    async def cached(self, agent: str, query: ActivityQuery) -> list[ActivitySnapshot]:
        rows = await self.backend._call("HGETALL", activity_key(agent))
        entries = {}
        for slot in ("query", "default"):
            if slot not in rows:
                continue
            data = json.loads(rows[slot])
            entries[slot] = (
                data["query"],
                [ActivitySnapshot.model_validate(page) for page in data["pages"]],
            )
        return select_cached(agent, query, entries)

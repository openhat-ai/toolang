"""Read-only observation of all locally registered runtime instances."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

from toolang.execution.activity import ActivityQuery, ActivityReader
from toolang.execution.schemas import ActivitySnapshot
from toolang.teaming.observation import Frame, LocalObservation, frames
from .observation import running_instances


class RuntimeObservation:
    def __init__(self, root: Path, *, home: Path | None = None) -> None:
        self.root = root
        self.home = home
        self.readers: dict[str, ActivityReader] = {}

    async def read(self, query: ActivityQuery) -> list[ActivitySnapshot]:
        instances = await asyncio.to_thread(running_instances, self.root)
        pages = []
        for instance in instances:
            if self.home is not None and instance.get("home") != str(self.home):
                continue
            agent = instance["agent"]
            reader = self.readers.get(agent)
            path = Path(instance["store"])
            if reader is None or reader.path != path:
                reader = self.readers[agent] = ActivityReader(path, agent)
            snapshot = await LocalObservation(reader, live=True).read(query)
            pages.extend(
                page.model_copy(
                    update={
                        "presence": "online",
                        "instance": instance["instance"],
                        "placement": instance["placement"],
                        "drivers": tuple(instance["drivers"]),
                    }
                )
                for page in snapshot
            )
        return pages

    async def updates(self, query: ActivityQuery) -> AsyncGenerator[Frame]:
        while True:
            for frame in frames(await self.read(query)):
                yield frame
            await asyncio.sleep(0.5)

    async def result(self, agent: str, ref: str) -> str:
        if agent not in self.readers:
            await self.read(ActivityQuery())
        reader = self.readers.get(agent)
        if reader is None:
            raise ValueError("Runtime instance is no longer available")
        return await asyncio.to_thread(reader.result, ref)

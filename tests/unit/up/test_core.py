from __future__ import annotations

import asyncio
from pathlib import Path

from toolang.common.layout import AgentLayout
from toolang.up import AgentCore


def test_agent_core_shares_store_and_ids(tmp_path: Path) -> None:
    async def run() -> None:
        layout = AgentLayout.resident(tmp_path, "alice")
        core = AgentCore(layout)
        try:
            assert core.executor.store is core.store
            assert core.executor.ids is core.ids
            assert core.threads.store is core.store
            assert core.threads.ids is core.ids
            assert core.setup.layout is layout
            assert core.state.layout is layout
        finally:
            await core.close()

    asyncio.run(run())

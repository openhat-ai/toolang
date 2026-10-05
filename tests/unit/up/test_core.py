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


def test_agent_core_sync_publishes_to_its_live_state(tmp_path: Path) -> None:
    async def run():
        layout = AgentLayout.resident(tmp_path, "alice")
        layout.home.mkdir(parents=True)
        layout.program.write_text("agic answer():\n  First.\n")
        core = AgentCore(layout)
        try:
            sync = core.executor._sync_state
            assert sync == core.state.sync
            assert sync is not None
            initial = await sync()
            layout.program.write_text("agic answer():\n  Updated.\n")
            updated = await sync()
            assert updated.error is None
            assert updated.revision != initial.revision
            assert updated.revision == core.state.current().revision
            assert updated.files == core.state.current().files
        finally:
            await core.close()

    asyncio.run(run())

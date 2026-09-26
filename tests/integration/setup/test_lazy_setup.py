"""Setup model materialization creates no persistent model cache files."""

from __future__ import annotations

import asyncio
from pathlib import Path

from toolang.common.layout import AgentLayout
from toolang.setup.watcher import load_setup


def test_setup_loads_catalog_data_in_memory_without_model_cache(tmp_path: Path) -> None:
    async def run() -> None:
        layout = AgentLayout.resident(tmp_path, "alice")
        setup = await load_setup(layout, agent_context=False, validate_defaults=False)
        cache_path = layout.root / ".setup" / "models"
        assert not cache_path.exists()
        assert setup.models()
        assert setup.providers()
        assert not cache_path.exists()

    asyncio.run(run())

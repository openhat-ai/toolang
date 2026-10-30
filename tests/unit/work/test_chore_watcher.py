import asyncio

import pytest

from toolang.common.layout import AgentLayout
from toolang.state import watcher


@pytest.mark.parametrize(
    "invalid", ["---\nschedule: invalid\n---\nBroken.", "---\ntitle: [\n---\nBroken."]
)
def test_invalid_edit_does_not_end_chore_watch(tmp_path, monkeypatch, invalid):
    layout = AgentLayout.resident(tmp_path, "alice")
    path = layout.home / "chores" / "cleanup.md"
    path.parent.mkdir(parents=True)
    path.write_text("Original.")
    reader = watcher.StateWatcher(layout)
    asyncio.run(reader.refresh())

    async def changes(*args, **kwargs):
        path.write_text(invalid)
        yield {path}
        assert reader.current().chores == ()
        assert reader.diagnostics()
        path.write_text("Corrected.")
        yield {path}

    monkeypatch.setattr(watcher, "directory_changes", changes)

    async def read():
        return [
            snapshot async for snapshot in reader.updates(stop_signal=asyncio.Event())
        ]

    snapshots = asyncio.run(read())
    assert len(snapshots) == 2
    assert snapshots[0].chores == ()
    assert snapshots[1].chores[0].body == "Corrected."
    assert path.read_text() == "Corrected."

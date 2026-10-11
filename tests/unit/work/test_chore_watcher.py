import asyncio

import pytest
from watchfiles import Change

from toolang.common.layout import AgentLayout
from toolang.work import watcher


@pytest.mark.parametrize(
    "invalid", ["---\nschedule: invalid\n---\nBroken.", "---\ntitle: [\n---\nBroken."]
)
def test_invalid_edit_does_not_end_chore_watch(tmp_path, monkeypatch, invalid):
    layout = AgentLayout.resident(tmp_path, "alice")
    path = layout.home / "chores" / "cleanup.md"
    path.parent.mkdir(parents=True)
    path.write_text("Original.")
    reader = watcher.ChoreWatcher(layout)

    async def changes(*args, **kwargs):
        path.write_text(invalid)
        yield {(Change.modified, str(path))}
        assert reader.current()[0].body == "Original."
        path.write_text("Corrected.")
        yield {(Change.modified, str(path))}

    monkeypatch.setattr(watcher, "awatch", changes)

    async def read():
        return [
            snapshot async for snapshot in reader.updates(stop_signal=asyncio.Event())
        ]

    snapshots = asyncio.run(read())
    assert len(snapshots) == 1
    assert snapshots[0][0].body == "Corrected."
    assert path.read_text() == "Corrected."

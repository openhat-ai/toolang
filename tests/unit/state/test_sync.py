"""Flat publication receipts distinguish local exclusions and fatal failures."""

import asyncio
from hashlib import sha256
import os
from unittest.mock import AsyncMock, Mock

import pytest

from toolang.common.layout import AgentLayout
from toolang.state import watcher as state_watcher
from toolang.state.watcher import StateRefresh, StateWatcher


@pytest.fixture
def watcher(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agic answer:\n  First.\n")
    return StateWatcher(layout)


def test_sync_uses_one_refresh_and_no_receipt_scan(watcher, monkeypatch):
    state = asyncio.run(watcher.refresh())
    refresh = AsyncMock(return_value=StateRefresh(state))
    scan = Mock(side_effect=AssertionError("receipt must use captured sources"))
    monkeypatch.setattr(watcher, "refresh_result", refresh)
    monkeypatch.setattr(state_watcher, "observe_home_source", scan)
    result = asyncio.run(watcher.sync())
    assert result.to_data() == {
        "revision": state.revision,
        "files": [file.to_data() for file in state.files],
    }
    refresh.assert_awaited_once_with(force=False)
    scan.assert_not_called()


@pytest.mark.parametrize("initial", [False, True])
def test_sync_publishes_valid_changes_and_excludes_invalid_resource(watcher, initial):
    layout = watcher.layout
    prompts = layout.home / "prompts"
    prompts.mkdir()
    removed = prompts / "removed.md"
    removed.write_text("Old prompt.")
    old = asyncio.run(watcher.refresh()) if initial else None
    removed.unlink()
    (prompts / "added.md").write_text("New prompt.")
    layout.program.write_text("agic (")
    result = asyncio.run(watcher.sync())
    assert result.error is None
    assert result.revision != (old.revision if old else None)
    data = result.to_data()
    assert set(data) == {"revision", "files"}
    files = {file["key"]: file for file in data["files"]}
    assert "prompts/removed.md" not in files
    assert (
        files["prompts/added.md"]["accepted_digest"]
        == sha256(b"New prompt.").hexdigest()
    )
    assert files["agent.too"]["observed_digest"] == sha256(b"agic (").hexdigest()
    assert files["agent.too"]["accepted_digest"] is None
    assert files["agent.too"]["code"] == "invalid-program"
    assert all(
        not isinstance(value, (dict, list))
        for file in data["files"]
        for value in file.values()
    )
    layout.program.write_text("agic answer:\n  Repaired.\n")
    repaired = asyncio.run(watcher.sync())
    assert repaired.error is None
    assert all(file.accepted for file in repaired.files)
    assert watcher.diagnostics() == ()


@pytest.mark.parametrize("initial", [False, True])
def test_fatal_config_rejection_keeps_state_and_returns_no_acceptance_receipt(
    watcher, initial
):
    previous = asyncio.run(watcher.refresh()) if initial else None
    watcher.layout.config.write_text("[broken")
    result = asyncio.run(watcher.sync())
    assert result.error == "state_rejected"
    assert result.to_data() == {
        "error": "state_rejected",
        "message": result.message,
        "revision": previous.revision if previous else None,
        "files": [],
    }
    if previous:
        assert watcher.current() is previous
    watcher.layout.config.unlink()
    assert asyncio.run(watcher.sync()).error is None


@pytest.mark.parametrize("initial", [False, True])
def test_fatal_io_rejection_does_not_issue_partial_receipt(
    watcher, monkeypatch, initial
):
    previous = asyncio.run(watcher.refresh()) if initial else None
    monkeypatch.setattr(
        state_watcher,
        "observe_home_source",
        Mock(side_effect=PermissionError("source is unreadable")),
    )
    result = asyncio.run(watcher.sync())
    assert result.error == "io_error"
    assert result.revision == (previous.revision if previous else None)
    assert result.to_data()["files"] == []
    assert "source is unreadable" in result.message


def test_unreadable_prepared_manifest_retains_published_state(watcher, monkeypatch):
    previous = asyncio.run(watcher.refresh())
    watcher.layout.program.write_text("agic answer:\n  Updated.\n")
    load = state_watcher.load_layer_source

    def fail_home(layout, scope, revision):
        if scope == "home":
            raise PermissionError("prepared manifest is unreadable")
        return load(layout, scope, revision)

    monkeypatch.setattr(state_watcher, "load_layer_source", fail_home)
    result = asyncio.run(watcher.sync())
    assert result.error == "io_error"
    assert result.revision == previous.revision
    assert result.files == ()
    assert watcher.current() is previous
    monkeypatch.setattr(state_watcher, "load_layer_source", load)
    assert asyncio.run(watcher.sync()).revision != previous.revision


@pytest.mark.parametrize("invalidated", [False, True])
@pytest.mark.parametrize(
    "failure,code",
    [
        (PermissionError("candidate is unreadable"), "io_error"),
        (ValueError("candidate is invalid"), "state_rejected"),
    ],
)
def test_background_skip_preserves_check_failure(
    watcher, monkeypatch, failure, code, invalidated
):
    async def scenario():
        initial = await watcher.refresh()
        watcher.layout.program.write_text("agic answer:\n  Changed.\n")
        prepare = Mock(side_effect=failure)
        with monkeypatch.context() as patch:
            patch.setattr(state_watcher, "prepare_agent_state", prepare)
            rejected = await watcher.refresh_result()
            skipped = await watcher._request_check(
                requested=False,
                invalidated_home=frozenset({"agent.too"})
                if invalidated
                else frozenset(),
            )
        assert rejected.state is skipped.state is initial
        assert rejected.error == skipped.error == code
        assert rejected.diagnostics == skipped.diagnostics == watcher.diagnostics()
        assert rejected.diagnostics[0].message == str(failure)
        prepare.assert_called_once()

        repaired = await watcher.refresh_result()
        assert repaired.state.revision != initial.revision
        assert repaired.error is None
        assert repaired.diagnostics == watcher.diagnostics() == ()

    asyncio.run(scenario())


def test_sync_checks_bytes_even_when_metadata_is_unchanged(watcher):
    first = asyncio.run(watcher.sync())
    path = watcher.layout.program
    stat = path.stat()
    content = path.read_text().replace("First", "Other")
    path.write_text(content)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    second = asyncio.run(watcher.sync())
    assert second.error is None
    assert second.revision != first.revision
    assert second.files[0].digest == sha256(content.encode()).hexdigest()


def test_concurrent_sync_and_canceled_waiter_share_serialized_checker(
    watcher, monkeypatch
):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        check = watcher._perform_check
        active = 0
        calls = 0

        async def guarded(**kwargs):
            nonlocal active, calls
            active += 1
            calls += 1
            assert active == 1
            entered.set()
            await release.wait()
            try:
                return await check(**kwargs)
            finally:
                active -= 1

        monkeypatch.setattr(watcher, "_perform_check", guarded)
        canceled = asyncio.create_task(watcher.sync())
        await entered.wait()
        others = [asyncio.create_task(watcher.sync()) for _ in range(2)]
        canceled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await canceled
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*others), 5)
        assert calls == 3
        assert results[0] == results[1]
        assert results[0].error is None
        assert results[0].revision == watcher.current().revision

    asyncio.run(scenario())

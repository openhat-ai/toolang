"""Synchronization uses one queued check and reports only unpublished inputs."""

import asyncio
from hashlib import sha256
import os
from unittest.mock import AsyncMock, Mock

import pytest

from toolang.common.layout import AgentLayout
from toolang.state import watcher as state_watcher
from toolang.state.errors import StateDiagnostic, StatePreparationError
from toolang.state.watcher import StateRefresh, StateWatcher


@pytest.fixture
def watcher(tmp_path):
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agic answer:\n  First.\n")
    return StateWatcher(layout)


def test_sync_uses_one_refresh_and_no_success_scan(watcher, monkeypatch):
    state = asyncio.run(watcher.refresh())
    refresh = AsyncMock(return_value=StateRefresh(state))
    scan = Mock(side_effect=AssertionError("success must use the published manifest"))
    monkeypatch.setattr(watcher, "refresh_result", refresh)
    monkeypatch.setattr(state_watcher, "raw_source_files", scan)
    result = asyncio.run(watcher.sync())
    assert result.to_data() == {
        "revision": state.revision,
        "files": [item.to_data() for item in state.files],
    }
    refresh.assert_awaited_once_with(force=False)
    scan.assert_not_called()


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


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("broken_config", [False, True])
def test_sync_reports_additions_changes_deletions_and_repairs(
    watcher, monkeypatch, initial, broken_config
):
    layout = watcher.layout
    prompts = layout.home / "prompts"
    prompts.mkdir()
    removed = prompts / "removed.md"
    removed.write_text("Old prompt.")
    old = asyncio.run(watcher.refresh()) if initial else None
    removed.unlink()
    (prompts / "added.md").write_text("New prompt.")
    broken = layout.home / "config.toml" if broken_config else layout.program
    broken.write_text("[broken" if broken_config else "agic (")
    scan = Mock(wraps=state_watcher.raw_source_files)
    refresh = AsyncMock(wraps=watcher.refresh_result)
    monkeypatch.setattr(state_watcher, "raw_source_files", scan)
    monkeypatch.setattr(watcher, "refresh_result", refresh)
    result = asyncio.run(watcher.sync())
    refresh.assert_awaited_once_with(force=False)
    scan.assert_called_once_with(layout.root, layout.name)
    assert result.error == "state_rejected"
    assert result.revision == (old.revision if old else None)
    assert result.files == (old.files if old else ())
    assert result.diagnostics
    if old:
        assert watcher.current() is old
        assert result.diagnostics == watcher.diagnostics()
    expected = [
        {
            "scope": "home",
            "key": "prompts/added.md",
            "disk_digest": sha256(b"New prompt.").hexdigest(),
            "state_digest": None,
        }
    ]
    if initial:
        expected.append(
            {
                "scope": "home",
                "key": "prompts/removed.md",
                "disk_digest": None,
                "state_digest": sha256(b"Old prompt.").hexdigest(),
            }
        )
    if broken_config:
        expected.append(
            {
                "scope": "home",
                "key": "config.toml",
                "disk_digest": sha256(b"[broken").hexdigest(),
                "state_digest": None,
            }
        )
    if not initial or not broken_config:
        expected.append(
            {
                "scope": "home",
                "key": "agent.too",
                "disk_digest": sha256(
                    b"agic answer:\n  First.\n" if broken_config else b"agic ("
                ).hexdigest(),
                "state_digest": sha256(b"agic answer:\n  First.\n").hexdigest()
                if initial
                else None,
            }
        )
    assert result.to_data()["differences"] == sorted(
        expected, key=lambda item: item["key"]
    )
    if broken_config:
        broken.unlink()
    else:
        broken.write_text("agic answer:\n  Repaired.\n")
    repaired = asyncio.run(watcher.sync())
    assert repaired.error is None
    assert repaired.diagnostics == ()
    assert repaired.revision == watcher.current().revision
    assert repaired.files == watcher.current().files


@pytest.mark.parametrize("initial", [False, True])
def test_sync_does_not_claim_partial_manifest_on_io_failure(
    watcher, monkeypatch, initial
):
    old = asyncio.run(watcher.refresh()) if initial else None
    failed = Mock(side_effect=PermissionError("source is unreadable"))
    monkeypatch.setattr(state_watcher, "observe_home_source", failed)
    scan = Mock(side_effect=PermissionError("manifest is unreadable"))
    monkeypatch.setattr(state_watcher, "raw_source_files", scan)
    result = asyncio.run(watcher.sync())
    assert result.error == "io_error"
    assert result.revision == (old.revision if old else None)
    assert result.files == (old.files if old else ())
    assert result.differences is None
    assert "source is unreadable" in result.message
    assert "manifest is unreadable" in result.message
    scan.assert_called_once()


def test_sync_preserves_rejection_when_reporting_scan_fails(watcher, monkeypatch):
    old = asyncio.run(watcher.refresh())
    watcher.layout.program.write_text("agic (")
    monkeypatch.setattr(
        state_watcher, "raw_source_files", Mock(side_effect=OSError("cannot inspect"))
    )
    result = asyncio.run(watcher.sync())
    assert result.error == "state_rejected"
    assert result.revision == old.revision
    assert result.differences is None
    assert result.diagnostics == watcher.diagnostics()
    assert result.diagnostics[0].code == "invalid-program"
    assert "cannot inspect" in result.message


def test_sync_uses_exact_check_result_not_later_watcher_diagnostics(
    watcher, monkeypatch
):
    state = asyncio.run(watcher.refresh())
    diagnostic = StateDiagnostic(
        layer="program",
        module_kind="agent",
        authored_path="agent.too",
        line=1,
        code="test-rejection",
        message="This check failed.",
    )

    async def refresh(*, force):
        assert force is False
        result = watcher._reject_check(StatePreparationError(diagnostic))
        watcher._failure = None
        return result

    monkeypatch.setattr(watcher, "refresh_result", refresh)
    result = asyncio.run(watcher.sync())
    assert result.diagnostics == (diagnostic,)
    assert result.differences == ()
    assert result.revision == state.revision


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


def test_sync_keeps_last_valid_state_when_prepared_manifest_cannot_be_read(
    watcher, monkeypatch
):
    previous = asyncio.run(watcher.refresh())
    watcher.layout.program.write_text("agic answer:\n  Updated.\n")
    load_source = state_watcher.load_layer_source

    def unreadable_home(layout, scope, revision):
        if scope == "home":
            raise PermissionError("prepared home manifest is unreadable")
        return load_source(layout, scope, revision)

    monkeypatch.setattr(state_watcher, "load_layer_source", unreadable_home)
    result = asyncio.run(watcher.sync())
    assert result.error == "io_error"
    assert result.revision == previous.revision
    assert result.files == previous.files
    assert watcher.current() is previous
    assert result.to_data()["differences"] == [
        {
            "scope": "home",
            "key": "agent.too",
            "disk_digest": sha256(b"agic answer:\n  Updated.\n").hexdigest(),
            "state_digest": previous.files[0].digest,
        }
    ]
    assert "prepared home manifest is unreadable" in result.message
    monkeypatch.setattr(state_watcher, "load_layer_source", load_source)
    repaired = asyncio.run(watcher.sync())
    assert repaired.error is None
    assert repaired.revision != previous.revision
    assert repaired.revision == watcher.current().revision


def test_sync_preserves_preparation_error_when_current_pointer_is_unreadable(
    watcher, monkeypatch
):
    previous = asyncio.run(watcher.refresh())
    watcher.layout.program.write_text("agic (")
    monkeypatch.setattr(
        state_watcher,
        "load_current_revision",
        Mock(side_effect=PermissionError("current pointer is unreadable")),
    )
    result = asyncio.run(watcher.sync())
    assert result.error == "state_rejected"
    assert result.revision == previous.revision
    assert result.files == previous.files
    assert result.diagnostics[0].code == "invalid-program"
    assert watcher.current() is previous

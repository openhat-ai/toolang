import asyncio
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path

import pytest

from toolang.common.files import file_write_lock
from toolang.common.layout import AgentLayout
from toolang.plugin.sandboxes import host
from toolang.up import sandbox
from toolang.up.records import SandboxState


def test_direct_registration_blocks_duplicate_and_removal(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    ref = asyncio.run(
        sandbox.register_host_server(
            layout, endpoint="http://localhost:1", launch_id=None
        )
    )
    assert ref.meta["signal_scope"] == "process"
    for action in (
        sandbox.register_host_server(
            layout, endpoint="http://localhost:1", launch_id=None
        ),
        sandbox.remove_agent(layout),
    ):
        with pytest.raises(ValueError, match="already running"):
            asyncio.run(action)
    assert layout.home.is_dir()
    state = SandboxState.load(layout.sandbox_state)
    assert state is not None and state.ref == ref


def test_managed_ack_does_not_wait_for_parent_lock(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    ref = host.process_ref(
        os.getpid(), "http://localhost:1", group=True, launch_id="launch-1"
    )
    SandboxState("host", ref).save(layout.sandbox_state)
    with ThreadPoolExecutor() as pool:
        with file_write_lock(layout.sandbox_state.with_suffix(".lock")):
            result = pool.submit(
                asyncio.run,
                sandbox.register_host_server(
                    layout, endpoint=ref.endpoint, launch_id="launch-1"
                ),
            ).result(timeout=2)
    assert result == ref


def test_managed_ack_timeout_does_not_create_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    monkeypatch.setattr(sandbox, "HOST_ACK_TIMEOUT_SEC", 0.01)
    with pytest.raises(TimeoutError, match="did not register"):
        asyncio.run(
            sandbox.register_host_server(
                layout, endpoint="http://localhost:1", launch_id="lost-parent"
            )
        )
    assert not layout.sandbox_state.exists()


def test_managed_ack_rejects_another_launch(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    ref = host.process_ref(
        os.getpid(), "http://localhost:1", group=True, launch_id="old"
    )
    SandboxState("host", ref).save(layout.sandbox_state)
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(
            sandbox.register_host_server(layout, endpoint=ref.endpoint, launch_id="new")
        )
    state = SandboxState.load(layout.sandbox_state)
    assert state is not None and state.ref == ref


def test_reused_pid_is_never_signalled(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = host.process_ref(os.getpid(), "http://localhost:1")
    stale = replace(ref, meta={**ref.meta, "created": 0.0})
    monkeypatch.setattr(
        host.os, "kill", lambda *_args: pytest.fail("must not signal reused pid")
    )
    assert not asyncio.run(host.HostSandbox({}).running(stale))
    asyncio.run(host.HostSandbox({}).stop(stale, force=True))


def test_missing_identity_does_not_authorize_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = replace(host.process_ref(os.getpid(), "http://localhost:1"), meta={})
    monkeypatch.setattr(
        host.os, "kill", lambda *_args: pytest.fail("must not signal unknown process")
    )
    with pytest.raises(ValueError, match="lacks process identity"):
        asyncio.run(host.HostSandbox({}).stop(ref, force=True))


def test_removal_retains_control_lock_and_clears_stale_ref(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    ref = host.process_ref(os.getpid(), "http://localhost:1")
    ref = replace(ref, meta={**ref.meta, "created": 0.0})
    SandboxState("host", ref).save(layout.sandbox_state)
    lock = layout.sandbox_state.with_suffix(".lock")
    inode = lock.stat().st_ino
    asyncio.run(sandbox.remove_agent(layout))
    assert not layout.home.exists() and not layout.sandbox_state.exists()
    assert lock.stat().st_ino == inode


@pytest.mark.parametrize("running", [True, False])
def test_failed_launch_cleanup_keeps_live_reference(
    tmp_path: Path, running: bool
) -> None:
    ref = host.process_ref(os.getpid(), "http://localhost:1")
    layout = AgentLayout.resident(tmp_path, "alice")
    state = SandboxState("host", ref)
    state.save(layout.sandbox_state)
    released: list[object] = []

    class FailingStop(host.HostSandbox):
        async def stop(self, ref, *, force=False):
            raise RuntimeError("stop failed")

        async def running(self, ref):
            return running

        async def release(self, ref):
            released.append(ref)

    asyncio.run(
        sandbox._recover_failed_launch(layout, FailingStop({}), ref=ref, state=state)
    )
    assert layout.sandbox_state.exists() is running
    assert released == ([] if running else [ref])


def test_unreadable_identity_is_indeterminate(monkeypatch: pytest.MonkeyPatch) -> None:
    ref = host.process_ref(os.getpid(), "http://localhost:1")

    def denied(_pid):
        raise host.psutil.AccessDenied(os.getpid())

    monkeypatch.setattr(host.psutil, "Process", denied)
    with pytest.raises(ValueError, match="cannot inspect"):
        asyncio.run(host.HostSandbox({}).running(ref))


def test_zombie_reference_is_exited(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    ref = host.process_ref(os.getpid(), "http://localhost:1")
    monkeypatch.setattr(
        host.psutil,
        "Process",
        lambda _pid: SimpleNamespace(status=lambda: host.psutil.STATUS_ZOMBIE),
    )
    assert not asyncio.run(host.HostSandbox({}).running(ref))


@pytest.mark.parametrize("status", ["running", "failed"])
def test_releasing_dead_ref_clears_active_report_but_preserves_failure(
    tmp_path: Path, status: str
) -> None:
    from toolang.up.process import AgentProcess, write_runtime_state

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    ref = host.process_ref(os.getpid(), "http://localhost:1")
    SandboxState("host", replace(ref, meta={**ref.meta, "created": 0.0})).save(
        layout.sandbox_state
    )
    write_runtime_state(
        layout,
        endpoint=ref.endpoint,
        started_at="old",
        pid=os.getpid(),
        process_created=0.0,
        status=status,
        message="startup failed" if status == "failed" else None,
    )
    asyncio.run(sandbox.release_stopped(layout))
    report = AgentProcess(layout).state()
    assert report is not None
    assert report["status"] == ("stopped" if status == "running" else "failed")
    assert report["message"] == ("startup failed" if status == "failed" else None)
    assert not layout.sandbox_state.exists()
    new = asyncio.run(
        sandbox.register_host_server(layout, endpoint=ref.endpoint, launch_id=None)
    )
    assert new.meta["created"] == ref.meta["created"]

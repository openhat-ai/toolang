from __future__ import annotations

from pathlib import Path
import os
from dataclasses import replace

from toolang.plugin.sandboxes.host import process_ref

import pytest

from toolang.base.types.sandbox import SandboxRef
from toolang.common.layout import AgentLayout
from toolang.up import process
from toolang.up.records import SandboxState


@pytest.mark.parametrize("matching", [True, False])
def test_status_uses_saved_identity_not_live_pid_in_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matching: bool
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    ref = process_ref(os.getpid(), "http://localhost:8123")
    if not matching:
        ref = replace(ref, meta={**ref.meta, "created": 0.0})
    SandboxState(sandbox="host", ref=ref, launch_id="fixture").save(
        layout.runtime_status
    )
    process.write_runtime_state(
        layout,
        endpoint="http://stale.example:9999",
        started_at="old",
        pid=os.getpid(),
        process_created=-1,
    )
    from toolang.up import sandbox

    monkeypatch.setattr(sandbox, "_health_ready", lambda _url: False)
    runtime = process.AgentProcess(layout)
    status = runtime.status(ui_base_url="")
    assert status is not None
    assert status.status == ("starting" if matching else "stopped")
    assert status.endpoint == (ref.endpoint if matching else None)
    assert runtime.state() is None
    assert process.runtime_identity_row(None, layout=layout) == (
        "PID",
        str(os.getpid()),
    )


def test_status_keeps_one_snapshot_when_a_restart_changes_the_file(
    tmp_path, monkeypatch
):
    from toolang.up.records import update_record

    layout = AgentLayout.resident(tmp_path, "alice")
    state = SandboxState(
        "host", process_ref(os.getpid(), "http://localhost:8123"), "first"
    )
    state.save(layout.runtime_status)
    process.write_runtime_state(
        layout,
        endpoint=state.ref.endpoint,
        started_at="now",
        pid=os.getpid(),
        process_created=state.ref.meta["created"],
        launch_id="first",
    )
    read = process.read_record
    reads = 0

    def restart_after_snapshot(path, instance=None):
        nonlocal reads
        entry = read(path, instance)
        reads += 1
        if reads == 1:
            replaced = replace(state.ref, meta={**state.ref.meta, "created": 0})
            update_record(
                path,
                lambda entry: entry.update(
                    launch_id="second",
                    control={"sandbox": "host", "ref": replaced.to_data()},
                    workload={
                        "status": "running",
                        "pid": os.getpid(),
                        "process_created": 0,
                    },
                ),
            )
        return entry

    monkeypatch.setattr(process, "read_record", restart_after_snapshot)
    result = process.AgentProcess(layout).status(ui_base_url="")
    assert result is not None and result.status == "running"
    assert result.endpoint == state.ref.endpoint
    assert reads == 1


def test_status_does_not_adopt_unregistered_process(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    process.write_runtime_state(
        layout, endpoint="http://localhost:8123", started_at="old", pid=os.getpid()
    )
    status = process.AgentProcess(layout).status(ui_base_url="")
    assert status is not None and status.status == "unknown"
    assert status.endpoint is None
    assert status.message is None
    assert process.runtime_identity_row({"pid": os.getpid()}, layout=layout) is None


def test_status_recovers_readiness_from_health_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolang.up import sandbox

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    ref = process_ref(os.getpid(), "http://localhost:8123/")
    SandboxState("host", ref, launch_id="fixture").save(layout.runtime_status)
    monkeypatch.setattr(
        sandbox, "_health_ready", lambda url: url == "http://localhost:8123/healthz"
    )
    status = process.AgentProcess(layout).status(ui_base_url="")
    assert status is not None and status.status == "running"


def test_runtime_identity_uses_environment_process_without_sandbox_state() -> None:
    assert process.runtime_identity_row({"pid": 1234}) == ("PID", "1234")


def test_runtime_identity_formats_named_docker_container(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef(
            runtime_id="176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb",
            endpoint="http://localhost:7001",
            runtime_kind="container",
            runtime_name="toolang-alice-launch",
        ),
        launch_id="fixture",
    ).save(layout.runtime_status)

    assert process.runtime_identity_row({"pid": 1}, layout=layout) == (
        "Container",
        "toolang-alice-launch (176191c1528b)",
    )


def test_runtime_identity_formats_legacy_docker_reference(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef(
            runtime_id=(
                "176191c1528b8e2861cc16422dee13ade59d4977c2148a9ebf5d36a06f090abb"
            ),
            endpoint="http://localhost:7001",
            meta={"container_name": "toolang-alice-legacy"},
        ),
        launch_id="fixture",
    ).save(layout.runtime_status)

    assert process.runtime_identity_row({"pid": 1}, layout=layout) == (
        "Container",
        "toolang-alice-legacy (176191c1528b)",
    )


def test_runtime_identity_preserves_unknown_opaque_id(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    SandboxState(
        sandbox="custom",
        ref=SandboxRef(
            runtime_id="opaque:identifier:that-must-not-be-shortened",
            endpoint="http://localhost:7001",
        ),
        launch_id="fixture",
    ).save(layout.runtime_status)

    assert process.runtime_identity_row({"pid": 1}, layout=layout) == (
        "Runtime",
        "workload:opaque:identifier:that-must-not-be-shortened",
    )


@pytest.mark.parametrize("instance", ["a" * 12, "b" * 12, "a", None])
def test_container_reports_match_launch_identity_not_guest_pid_or_hostname(
    tmp_path: Path, instance: str | None
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    SandboxState(
        "docker:image",
        SandboxRef("a" * 64, "http://localhost:1", runtime_kind="container"),
        launch_id="fixture",
    ).save(layout.runtime_status)
    process.write_runtime_state(
        layout,
        endpoint="http://guest:1",
        started_at="now",
        pid=1,
        sandbox="docker:image",
        sandbox_instance=instance,
    )
    report = process.AgentProcess(layout).state()
    assert report is not None and report["launch_id"] == "fixture"


def test_runtime_registration_waits_for_conditional_stop(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event, local

    from toolang.common.files import file_write_lock

    layout = AgentLayout.resident(tmp_path, "alice")
    process.write_runtime_state(layout, endpoint="http://old", started_at="old", pid=1)
    read = Event()
    finish_stop = Event()
    waiting = Event()
    from toolang.up import records

    load = records.read_document
    scope = local()

    def pause_read(path, **kwargs):
        state = load(path, **kwargs)
        if getattr(scope, "locked", False) and not read.is_set():
            read.set()
            assert finish_stop.wait(timeout=5)
        return state

    @contextmanager
    def observed_lock(path, **kwargs):
        assert path == layout.runtime_status.with_name("status.json.lock")
        kwargs.pop("on_wait", None)
        with file_write_lock(path, on_wait=waiting.set, **kwargs):
            scope.locked = True
            try:
                yield
            finally:
                scope.locked = False

    monkeypatch.setattr(records, "read_document", pause_read)
    monkeypatch.setattr(records, "file_write_lock", observed_lock)
    with ThreadPoolExecutor(max_workers=2) as pool:
        stopping = pool.submit(
            process.stop_runtime_state,
            layout,
            expected_pid=1,
            expected_started_at="old",
        )
        try:
            assert read.wait(timeout=5)
            registering = pool.submit(
                process.write_runtime_state,
                layout,
                endpoint="http://new",
                started_at="new",
                pid=2,
            )
            assert waiting.wait(timeout=2)
        finally:
            finish_stop.set()
        assert stopping.result(timeout=5)
        registering.result(timeout=5)
    state = process._load_runtime_state(layout.runtime_status)
    assert state is not None and state["pid"] == 2 and state["status"] == "running"
    assert not process.stop_runtime_state(layout, expected_pid=1)

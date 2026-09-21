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
    SandboxState(sandbox="host", ref=ref).save(layout.sandbox_state)
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


def test_status_does_not_adopt_unregistered_process(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    process.write_runtime_state(
        layout, endpoint="http://localhost:8123", started_at="old", pid=os.getpid()
    )
    status = process.AgentProcess(layout).status(ui_base_url="")
    assert status is not None and status.status == "failed"
    assert status.endpoint is None
    assert status.message == "active runtime report has no sandbox reference"
    assert process.runtime_identity_row({"pid": os.getpid()}, layout=layout) is None


def test_status_recovers_readiness_from_health_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from toolang.up import sandbox

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    ref = process_ref(os.getpid(), "http://localhost:8123/")
    SandboxState("host", ref).save(layout.sandbox_state)
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
    ).save(layout.sandbox_state)

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
    ).save(layout.sandbox_state)

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
    ).save(layout.sandbox_state)

    assert process.runtime_identity_row({"pid": 1}, layout=layout) == (
        "Runtime",
        "workload:opaque:identifier:that-must-not-be-shortened",
    )


@pytest.mark.parametrize("instance", ["a" * 12, "b" * 12, "a", None])
def test_container_reports_match_the_instance_not_guest_pid(
    tmp_path: Path, instance: str | None
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    SandboxState(
        "docker:image",
        SandboxRef("a" * 64, "http://localhost:1", runtime_kind="container"),
    ).save(layout.sandbox_state)
    process.write_runtime_state(
        layout,
        endpoint="http://guest:1",
        started_at="now",
        pid=1,
        sandbox="docker:image",
        sandbox_instance=instance,
    )
    report = process.AgentProcess(layout).state()
    assert (report is not None) is (instance == "a" * 12)

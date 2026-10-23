from __future__ import annotations

import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from typing import Any, cast

import pytest

from toolang.base.errors import SandboxLaunchError
from toolang.base.types.progress import ProgressEvent, ProgressSink
from toolang.base.types.sandbox import (
    SandboxMount,
    SandboxPlan,
    SandboxRef,
    SandboxRequest,
)
from toolang.common.layout import AgentLayout
from toolang.state import prepare as state_prepare
from toolang.state.cache import agent_current_path
from toolang.up import sandbox
from toolang.up.instance import SandboxState
from toolang.up import process as process_runtime
from toolang.up.server import ServeSpec


class FakeSandbox:
    name = "fake"
    location = "guest"

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.alive = True
        self.stop_error: Exception | None = None
        self.release_error: Exception | None = None
        self.attach_error: Exception | None = None

    def runtime_place(self, local_place: Path) -> Path:
        del local_place
        return Path("/runtime")

    def prepare(
        self,
        spec: str | None,
        request: SandboxRequest,
    ) -> SandboxPlan:
        self.calls.append(("prepare", spec, request))
        return SandboxPlan(
            sandbox=f"fake:{spec}",
            command=request.command,
            working_directory=request.working_directory,
            output=request.output,
            log_path=request.log_path,
            endpoint=request.endpoint,
        )

    async def launch(self, plan: SandboxPlan) -> SandboxRef:
        self.calls.append(("launch", plan))
        return SandboxRef("workload-1", plan.endpoint)

    async def attach(
        self,
        plan: SandboxPlan,
        ref: SandboxRef,
        *,
        progress: ProgressSink | None = None,
        progress_id: str | None = None,
    ) -> None:
        del progress, progress_id
        self.calls.append(("attach", plan, ref))
        if self.attach_error is not None:
            raise self.attach_error

    async def running(self, ref: SandboxRef) -> bool:
        self.calls.append(("running", ref))
        return self.alive

    async def wait(self, ref: SandboxRef) -> int:
        self.calls.append(("wait", ref))
        return 7

    async def stop(self, ref: SandboxRef, *, force: bool = False) -> None:
        self.calls.append(("stop", ref, force))
        if self.stop_error is not None:
            raise self.stop_error
        self.alive = False

    async def release(self, ref: SandboxRef) -> None:
        self.calls.append(("release", ref))
        if self.release_error is not None:
            raise self.release_error


class ConcurrentSandbox(FakeSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.launches = 0

    async def launch(self, plan: SandboxPlan) -> SandboxRef:
        self.launches += 1
        await asyncio.sleep(0)
        return await super().launch(plan)


class BlockingSandbox(FakeSandbox):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def wait(self, ref: SandboxRef) -> int:
        self.calls.append(("wait", ref))
        self.waiting.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking wait unexpectedly returned")


class FailingWaitSandbox(FakeSandbox):
    async def wait(self, ref: SandboxRef) -> int:
        self.calls.append(("wait", ref))
        raise RuntimeError("follow failed")


class RecoverableLaunchSandbox(FakeSandbox):
    async def launch(self, plan: SandboxPlan) -> SandboxRef:
        ref = SandboxRef("recoverable-workload", plan.endpoint)
        self.calls.append(("launch", plan))
        raise SandboxLaunchError("launch cleanup failed", ref=ref)


class OutputDetachSandbox(FakeSandbox):
    async def detach_output(self, ref: SandboxRef) -> None:
        self.calls.append(("detach_output", ref))


class GuestFailureSandbox(FakeSandbox):
    async def attach(
        self,
        plan: SandboxPlan,
        ref: SandboxRef,
        *,
        progress: ProgressSink | None = None,
        progress_id: str | None = None,
    ) -> None:
        self.calls.append(("attach", plan, ref))
        if progress is None:
            return
        progress(
            ProgressEvent(
                id=progress_id or "runtime:guest",
                kind="runtime",
                stage="create",
                label="Installing Toolang...",
                status="running",
            )
        )
        await asyncio.sleep(0.01)
        progress(
            ProgressEvent(
                id=progress_id or "runtime:guest",
                kind="runtime",
                stage="create",
                label="Failed to check Toolang",
                status="failed",
                detail="incompatible CLI",
            )
        )

    async def running(self, ref: SandboxRef) -> bool:
        del ref
        return False


class GuestStartupSandbox(FakeSandbox):
    async def attach(
        self,
        plan: SandboxPlan,
        ref: SandboxRef,
        *,
        progress: ProgressSink | None = None,
        progress_id: str | None = None,
    ) -> None:
        self.calls.append(("attach", plan, ref))
        if progress is None:
            return
        item_id = progress_id or "runtime:guest"
        progress(
            ProgressEvent(
                id=item_id,
                kind="runtime",
                stage="create",
                label="Installing Toolang...",
                status="running",
            )
        )
        progress(
            ProgressEvent(
                id=item_id,
                kind="runtime",
                stage="start",
                label="Starting agent...",
                status="running",
            )
        )


def _launch_spec(tmp_path: Path) -> sandbox.LaunchSpec:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    return sandbox.LaunchSpec(
        serve=ServeSpec(
            layout=layout,
            host="127.0.0.1",
            endpoint_host="localhost",
            port=8123,
        ),
        sandbox="fake:value:with:colons",
        config={},
        environ={},
    )


def test_resolve_selection_uses_explicit_home_root_host_precedence(
    tmp_path: Path,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)

    assert sandbox.resolve_selection(layout) == "host"

    layout.place_config.write_text(
        "[sandbox]\ndriver = 'docker'\ntarget = 'root-image'\n",
        encoding="utf-8",
    )
    assert sandbox.resolve_selection(layout) == "docker:root-image"

    layout.config.write_text(
        "[sandbox]\ndriver = 'docker'\ntarget = 'home-image'\n",
        encoding="utf-8",
    )
    assert sandbox.resolve_selection(layout) == "docker:home-image"
    assert (
        sandbox.resolve_selection(layout, explicit="docker:explicit-image")
        == "docker:explicit-image"
    )


def test_sandbox_state_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "sandbox.json"
    state = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef(
            runtime_id="container",
            endpoint="http://localhost:8123",
            meta={"image": "python:3.13-slim"},
            runtime_kind="container",
            runtime_name="toolang-alice-launch",
        ),
        launch_id="fixture",
    )

    state.save(path)

    assert SandboxState.load(path) == state
    assert '"version": 5' in path.read_text(encoding="utf-8")


def test_sandbox_state_does_not_adopt_a_version_one_reference(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sandbox.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "sandbox": "docker:python:3.13-slim",
                "ref": {
                    "runtime_id": "opaque-id",
                    "endpoint": "http://localhost:8123",
                    "meta": {},
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported runtime status version"):
        SandboxState.load(path)


def test_sandbox_state_rejects_corrupted_data(tmp_path: Path) -> None:
    path = tmp_path / "sandbox.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid runtime status"):
        SandboxState.load(path)


def test_sandbox_state_rejects_an_unversioned_payload(tmp_path: Path) -> None:
    path = tmp_path / "sandbox.json"
    path.write_text(
        '{"sandbox":"host","ref":{"runtime_id":"1","endpoint":"http://x"}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported runtime status version"):
        SandboxState.load(path)


def test_sandbox_ref_rejects_non_json_recovery_metadata(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="JSON-compatible"):
        SandboxRef(
            "workload",
            "http://localhost:1",
            meta=cast(Any, {"path": tmp_path}),
        )


def test_agent_removal_releases_control_state_through_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    implementation.alive = False
    monkeypatch.setattr(
        sandbox,
        "load_state_sandbox",
        lambda _layout, _state: implementation,
    )
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-test", "http://localhost:8123"),
        launch_id="fixture",
    ).save(layout.runtime_status)

    asyncio.run(sandbox.remove_agent(layout))

    assert ("release", SandboxRef("toolang-alice-test", "http://localhost:8123")) in (
        implementation.calls
    )
    assert SandboxState.load(layout.runtime_status) is None
    assert not layout.home.exists()


def test_release_stopped_rejects_a_workload_that_became_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    monkeypatch.setattr(
        sandbox,
        "load_state_sandbox",
        lambda _layout, _state: implementation,
    )
    layout = AgentLayout.resident(tmp_path, "alice")
    state = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-test", "http://localhost:8123"),
        launch_id="fixture",
    )
    state.save(layout.runtime_status)

    with pytest.raises(ValueError, match="already running"):
        asyncio.run(sandbox.release_stopped(layout))

    assert ("release", state.ref) not in implementation.calls
    assert SandboxState.load(layout.runtime_status) == state


def test_stop_handle_stops_only_its_exact_owned_workload(
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    layout = AgentLayout.resident(tmp_path, "alice")
    state = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-owned", "http://localhost:8123"),
        launch_id="fixture",
    )
    state.save(layout.runtime_status)
    handle = sandbox.SandboxHandle(implementation, state)
    events: list[ProgressEvent] = []

    assert (
        asyncio.run(sandbox.stop_handle(layout, handle, progress=events.append)) is True
    )

    assert ("stop", state.ref, False) in implementation.calls
    assert ("release", state.ref) in implementation.calls
    assert SandboxState.load(layout.runtime_status) is None
    assert [(event.kind, event.stage, event.status) for event in events] == [
        ("runtime", "stop", "running"),
        ("runtime", "stop", "ok"),
        ("runtime", "destroy", "running"),
        ("runtime", "destroy", "ok"),
    ]
    assert {event.id for event in events} == {"runtime:toolang-alice-owned"}


def test_stop_handle_reports_the_failed_cleanup_stage(
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    implementation.stop_error = RuntimeError("stop failed")
    layout = AgentLayout.resident(tmp_path, "alice")
    state = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-owned", "http://localhost:8123"),
        launch_id="fixture",
    )
    state.save(layout.runtime_status)
    events: list[ProgressEvent] = []

    with pytest.raises(RuntimeError, match="stop failed"):
        asyncio.run(
            sandbox.stop_handle(
                layout,
                sandbox.SandboxHandle(implementation, state),
                progress=events.append,
            )
        )

    assert [
        (event.kind, event.stage, event.status, event.detail) for event in events
    ] == [
        ("runtime", "stop", "running", "docker:python:3.13-slim"),
        ("runtime", "stop", "failed", "stop failed"),
    ]
    assert SandboxState.load(layout.runtime_status) == state


def test_stop_handle_rejects_replaced_ownership_without_stopping(
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    layout = AgentLayout.resident(tmp_path, "alice")
    owned = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-owned", "http://localhost:8123"),
        launch_id="fixture",
    )
    replacement = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-replacement", "http://localhost:8124"),
        launch_id="fixture",
    )
    replacement.save(layout.runtime_status)

    with pytest.raises(ValueError, match="ownership changed"):
        asyncio.run(
            sandbox.stop_handle(
                layout,
                sandbox.SandboxHandle(implementation, owned),
            )
        )

    assert not any(
        isinstance(call, tuple) and call[0] in {"stop", "release"}
        for call in implementation.calls
    )
    assert SandboxState.load(layout.runtime_status) == replacement


def test_stop_handle_does_not_stop_without_current_ownership(
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    layout = AgentLayout.resident(tmp_path, "alice")
    owned = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-owned", "http://localhost:8123"),
        launch_id="fixture",
    )

    assert (
        asyncio.run(
            sandbox.stop_handle(
                layout,
                sandbox.SandboxHandle(implementation, owned),
            )
        )
        is False
    )
    assert not any(
        isinstance(call, tuple) and call[0] in {"stop", "release"}
        for call in implementation.calls
    )


def test_sandbox_status_reports_plugin_recovery_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    SandboxState(
        sandbox="missing",
        ref=SandboxRef("workload-1", "http://localhost:8123"),
        launch_id="fixture",
    ).save(layout.runtime_status)

    def fail_recovery(*_args: object) -> object:
        raise ValueError("plugin is unavailable")

    monkeypatch.setattr(sandbox, "load_state_sandbox", fail_recovery)

    status = process_runtime.AgentProcess(layout).status(ui_base_url="")
    assert status is not None and status.status == "unknown"
    assert status.message == "plugin is unavailable"


def test_launch_delegates_complete_spec_and_stop_releases_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    configs: list[dict[str, object]] = []

    def create_sandbox(_name: str, config: dict[str, object]) -> FakeSandbox:
        configs.append(dict(config))
        return implementation

    monkeypatch.setattr(sandbox, "create_sandbox", create_sandbox)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)
    external_program = tmp_path / "external-agent.too"
    external_program.write_text("# Agent alice\n", encoding="utf-8")
    spec.serve.layout.program.symlink_to(external_program)
    spec.serve.layout.place_config.write_text(
        "[plugin.sandbox.fake]\ncurrent = true\n",
        encoding="utf-8",
    )

    events: list[ProgressEvent] = []
    handle = asyncio.run(sandbox.launch(spec, progress=events.append))

    prepare = implementation.calls[0]
    assert isinstance(prepare, tuple)
    assert prepare[:2] == ("prepare", "value:with:colons")
    request = cast(SandboxRequest, prepare[2])
    assert request.hosted_place == Path("/runtime")
    assert request.hosted_home == Path("/runtime/agents/alice")
    assert (
        SandboxMount(
            external_program,
            request.hosted_home / "agent.too",
            read_only=True,
        )
        in request.mounts
    )
    assert SandboxState.load(spec.serve.layout.runtime_status) == handle.state
    assert [
        (event.stage, event.status)
        for event in events
        if event.kind == "prepare" and not event.complete
    ] == [
        ("materialize", "running"),
        ("materialize", "ok"),
        ("materialize", "running"),
        ("materialize", "ok"),
    ]
    assert [
        (event.kind, event.stage, event.status)
        for event in events
        if event.kind == "runtime"
    ] == [
        ("runtime", "create", "running"),
        ("runtime", "create", "running"),
        ("runtime", "create", "running"),
        ("runtime", "create", "ok"),
        ("runtime", "start", "running"),
        ("runtime", "start", "ok"),
    ]
    assert len({event.id for event in events if event.kind == "runtime"}) == 1

    with pytest.raises(ValueError, match="owner was replaced"):
        asyncio.run(sandbox.stop(spec.serve.layout, expected_launch_id="stale"))
    assert implementation.alive
    assert asyncio.run(sandbox.stop(spec.serve.layout, force=True)) is True
    assert ("stop", handle.state.ref, True) in implementation.calls
    assert ("release", handle.state.ref) in implementation.calls
    assert SandboxState.load(spec.serve.layout.runtime_status) is None
    assert configs == [{}, {"current": True}]


def test_guest_roaming_mounts_the_shared_project_edit_guard(monkeypatch, tmp_path):
    implementation = FakeSandbox()
    monkeypatch.setattr(
        sandbox, "create_sandbox", lambda *_args, **_kwargs: implementation
    )

    async def ready(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    source = tmp_path / "aid.too"
    from toolang.common.place_codes import roaming_place_code

    layout = AgentLayout.roaming(
        source,
        place_code=roaming_place_code(str(source), 1),
        root=tmp_path / "global",
    )
    layout.home.mkdir(parents=True)
    spec = _launch_spec(tmp_path / "global")
    spec = replace(spec, serve=replace(spec.serve, layout=layout))
    handle = asyncio.run(sandbox.launch(spec))
    prepared = next(
        call
        for call in implementation.calls
        if isinstance(call, tuple) and call[0] == "prepare"
    )
    request = cast(SandboxRequest, prepared[2])
    guard = tmp_path / ".toolang" / ".state" / "root" / "edit.lock"
    assert guard.is_file()
    assert SandboxMount(guard, Path("/runtime/.state/root/edit.lock")) in request.mounts
    position = request.command.index("--source-edit-lock")
    assert request.command[position + 1] == "/runtime/.state/root/edit.lock"
    registry = tmp_path / "global" / ".runtime" / "active" / layout.address
    guest_registry = Path("/runtime/agents/aid/.runtime/registration")
    assert SandboxMount(registry, guest_registry) in request.mounts
    assert not any(mount.local_path == registry.parent for mount in request.mounts)
    assert request.command[request.command.index("--registry-directory") + 1] == str(
        guest_registry
    )
    assert request.command[request.command.index("--registry-status") + 1] == str(
        layout.runtime_status
    )
    assert asyncio.run(sandbox.stop(layout, force=True))
    assert ("release", handle.state.ref) in implementation.calls


def test_startup_renderer_failure_does_not_change_launch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    monkeypatch.setattr(
        sandbox,
        "create_sandbox",
        lambda _name, config: implementation,
    )

    async def ready(*_args: object, **_kwargs: object) -> None:
        return None

    def fail(_event: ProgressEvent) -> None:
        raise RuntimeError("renderer failed")

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)

    handle = asyncio.run(sandbox.launch(spec, progress=fail))

    assert handle.state.ref.runtime_id == "workload-1"


def test_background_launch_does_not_require_an_output_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    monkeypatch.setattr(
        sandbox,
        "create_sandbox",
        lambda _name, config: implementation,
    )

    async def ready(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    original = _launch_spec(tmp_path)
    spec = replace(
        original,
        output="file",
        log_path=original.serve.layout.runtime_log,
    )

    asyncio.run(sandbox.launch(spec))

    names = [call[0] for call in implementation.calls if isinstance(call, tuple)]
    assert names[:3] == ["prepare", "launch", "attach"]
    assert not any(
        isinstance(call, tuple) and call[0] == "detach_output"
        for call in implementation.calls
    )
    assert asyncio.run(sandbox.stop(spec.serve.layout)) is True


def test_background_launch_detaches_optional_output_after_readiness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = OutputDetachSandbox()
    monkeypatch.setattr(
        sandbox,
        "create_sandbox",
        lambda _name, config: implementation,
    )

    async def ready(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    original = _launch_spec(tmp_path)
    spec = replace(
        original,
        output="file",
        log_path=original.serve.layout.runtime_log,
    )

    handle = asyncio.run(sandbox.launch(spec))

    assert ("detach_output", handle.state.ref) in implementation.calls
    assert asyncio.run(sandbox.stop(spec.serve.layout)) is True


def test_stop_failure_preserves_sandbox_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    implementation.stop_error = RuntimeError("still running")
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)
    spec = _launch_spec(tmp_path)
    state = SandboxState(
        sandbox=spec.sandbox,
        ref=SandboxRef("workload-1", spec.serve.endpoint),
        launch_id="fixture",
    )
    state.save(spec.serve.layout.runtime_status)

    with pytest.raises(RuntimeError, match="still running"):
        asyncio.run(sandbox.stop(spec.serve.layout))

    assert SandboxState.load(spec.serve.layout.runtime_status) == state


def test_launch_failure_stops_releases_and_clears_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)

    async def fail(*_args, **_kwargs) -> None:
        await asyncio.sleep(0.01)
        raise TimeoutError("not ready")

    monkeypatch.setattr(sandbox, "_wait_ready", fail)
    spec = _launch_spec(tmp_path)

    events: list[ProgressEvent] = []
    with pytest.raises(TimeoutError, match="not ready"):
        asyncio.run(sandbox.launch(spec, progress=events.append))

    assert (
        "stop",
        SandboxRef("workload-1", spec.serve.endpoint),
        True,
    ) in implementation.calls
    assert (
        "release",
        SandboxRef("workload-1", spec.serve.endpoint),
    ) in implementation.calls
    assert SandboxState.load(spec.serve.layout.runtime_status) is None
    assert (events[-1].kind, events[-1].stage) == ("runtime", "start")
    assert events[-1].status == "failed"
    assert events[-1].detail == "not ready"


def test_guest_failure_progress_wins_the_early_exit_diagnostic_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    implementation = GuestFailureSandbox()
    monkeypatch.setattr(
        sandbox, "create_sandbox", lambda *_args, **_kwargs: implementation
    )
    spec = _launch_spec(tmp_path)
    events: list[ProgressEvent] = []

    with pytest.raises(RuntimeError, match="exited before becoming ready"):
        asyncio.run(sandbox.launch(spec, progress=events.append))

    activities = [(event.label, event.status) for event in events]
    assert ("Installing Toolang...", "running") in activities
    assert ("Failed to check Toolang", "failed") in activities
    assert not any("Waiting for the agent API" in label for label, _ in activities)
    assert activities[-1] == ("Failed to check Toolang", "failed")
    assert len({event.id for event in events if event.kind == "runtime"}) == 1


def test_readiness_cleanup_failure_preserves_sandbox_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    implementation.release_error = RuntimeError("release failed")
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)

    async def fail(*_args, **_kwargs) -> None:
        raise TimeoutError("not ready")

    monkeypatch.setattr(sandbox, "_wait_ready", fail)
    spec = _launch_spec(tmp_path)

    with pytest.raises(TimeoutError, match="not ready"):
        asyncio.run(sandbox.launch(spec))

    state = SandboxState.load(spec.serve.layout.runtime_status)
    assert state is not None
    assert state.ref == SandboxRef("workload-1", spec.serve.endpoint)


def test_attach_cleanup_failure_preserves_sandbox_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    implementation.attach_error = RuntimeError("could not attach output")
    implementation.release_error = RuntimeError("release failed")
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)
    spec = _launch_spec(tmp_path)

    with pytest.raises(RuntimeError, match="could not attach output"):
        asyncio.run(sandbox.launch(spec))

    state = SandboxState.load(spec.serve.layout.runtime_status)
    assert state is not None
    assert state.ref == SandboxRef("workload-1", spec.serve.endpoint)


def test_recoverable_launch_failure_persists_state_when_release_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = RecoverableLaunchSandbox()
    implementation.release_error = RuntimeError("release failed")
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)
    spec = _launch_spec(tmp_path)

    with pytest.raises(SandboxLaunchError, match="launch cleanup failed"):
        asyncio.run(sandbox.launch(spec))

    state = SandboxState.load(spec.serve.layout.runtime_status)
    assert state is not None
    assert state.ref.runtime_id == "recoverable-workload"


def test_foreground_run_waits_then_releases_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)
    ready_states: list[SandboxState] = []

    result = asyncio.run(sandbox.run(spec, on_ready=ready_states.append))

    ref = SandboxRef("workload-1", spec.serve.endpoint)
    assert result == 7
    assert ready_states == [
        SandboxState(sandbox=spec.sandbox, ref=ref, launch_id=ready_states[0].launch_id)
    ]
    assert ("wait", ref) in implementation.calls
    assert ("release", ref) in implementation.calls
    assert SandboxState.load(spec.serve.layout.runtime_status) is None


def test_foreground_wait_failure_stops_releases_and_clears_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FailingWaitSandbox()
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)

    with pytest.raises(RuntimeError, match="follow failed"):
        asyncio.run(sandbox.run(spec))

    ref = SandboxRef("workload-1", spec.serve.endpoint)
    assert ("stop", ref, True) in implementation.calls
    assert ("release", ref) in implementation.calls
    assert SandboxState.load(spec.serve.layout.runtime_status) is None


def test_foreground_ready_failure_stops_releases_and_clears_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)

    def fail_ready(_state: SandboxState) -> None:
        raise RuntimeError("ready output failed")

    with pytest.raises(RuntimeError, match="ready output failed"):
        asyncio.run(sandbox.run(spec, on_ready=fail_ready))

    ref = SandboxRef("workload-1", spec.serve.endpoint)
    assert ("stop", ref, True) in implementation.calls
    assert ("release", ref) in implementation.calls
    assert SandboxState.load(spec.serve.layout.runtime_status) is None


def test_legacy_guest_state_blocks_launch_stop_and_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)
    spec = _launch_spec(tmp_path)
    spec.serve.layout.legacy_sandbox_state.parent.mkdir(parents=True, exist_ok=True)
    spec.serve.layout.legacy_sandbox_state.write_text("{}\n", encoding="utf-8")

    for operation in (
        lambda: sandbox.launch(spec),
        lambda: sandbox.stop(spec.serve.layout),
        lambda: sandbox.remove_agent(spec.serve.layout),
    ):
        with pytest.raises(ValueError, match="legacy sandbox state"):
            asyncio.run(operation())

    status = process_runtime.AgentProcess(spec.serve.layout).status(ui_base_url="")
    assert status is not None
    assert status.status == "unknown"


def test_unreferenced_staging_blocks_launch_and_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)
    spec = _launch_spec(tmp_path)
    stage = spec.serve.layout.sandbox_stage / "orphaned"
    stage.mkdir(parents=True)
    (stage / "guest.env").write_text("SECRET=value\n", encoding="utf-8")

    for operation in (
        lambda: sandbox.launch(spec),
        lambda: sandbox.remove_agent(spec.serve.layout),
    ):
        with pytest.raises(ValueError, match="unreferenced sandbox staging"):
            asyncio.run(operation())

    assert stage.is_dir()


def test_concurrent_launch_accepts_only_one_workload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = ConcurrentSandbox()
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)

    async def launch_twice() -> list[object]:
        return await asyncio.gather(
            sandbox.launch(spec),
            sandbox.launch(spec),
            return_exceptions=True,
        )

    results = asyncio.run(launch_twice())

    assert implementation.launches == 1
    assert sum(isinstance(result, sandbox.SandboxHandle) for result in results) == 1
    errors = [result for result in results if isinstance(result, ValueError)]
    assert len(errors) == 1
    assert "already running" in str(errors[0])


def test_canceled_run_preserves_state_when_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = BlockingSandbox()
    implementation.stop_error = RuntimeError("still running")
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)

    async def cancel_run() -> BaseException | None:
        task = asyncio.create_task(sandbox.run(spec))
        await implementation.waiting.wait()
        task.cancel()
        try:
            await task
        except BaseException as exc:
            return exc
        return None

    error = asyncio.run(cancel_run())

    assert isinstance(error, RuntimeError)
    assert str(error) == "still running"
    assert SandboxState.load(spec.serve.layout.runtime_status) is not None
    assert not any(
        isinstance(call, tuple) and call[0] == "release"
        for call in implementation.calls
    )


def test_canceled_run_stops_releases_and_clears_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = BlockingSandbox()
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)

    async def cancel_run() -> BaseException | None:
        task = asyncio.create_task(sandbox.run(spec))
        await implementation.waiting.wait()
        task.cancel()
        try:
            await task
        except BaseException as exc:
            return exc
        return None

    error = asyncio.run(cancel_run())

    assert isinstance(error, asyncio.CancelledError)
    ref = SandboxRef("workload-1", spec.serve.endpoint)
    assert ("stop", ref, False) in implementation.calls
    assert ("release", ref) in implementation.calls
    assert SandboxState.load(spec.serve.layout.runtime_status) is None


def test_readiness_fails_when_workload_exits() -> None:
    implementation = FakeSandbox()
    implementation.alive = False

    with pytest.raises(
        RuntimeError,
        match="agent server exited before becoming ready",
    ):
        asyncio.run(
            sandbox._wait_ready(
                implementation,
                SandboxRef("workload-1", "http://localhost:1"),
                launch_id="launch",
                timeout_sec=0.1,
            )
        )


def test_launch_cannot_adopt_a_healthy_server_from_another_generation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class OtherServer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            payload = (
                {"ok": True}
                if self.path == "/healthz"
                else {"runtime": {"launch_id": "another-launch"}}
            )
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), OtherServer)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    implementation = FakeSandbox()
    probes = 0

    async def running(ref: SandboxRef) -> bool:
        nonlocal probes
        probes += 1
        return probes == 1

    monkeypatch.setattr(implementation, "running", running)
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)
    spec = _launch_spec(tmp_path)
    spec = replace(spec, serve=replace(spec.serve, port=server.server_port))
    try:
        with pytest.raises(RuntimeError, match="exited before becoming ready"):
            asyncio.run(sandbox.launch(spec))
        assert SandboxState.load(spec.serve.layout.runtime_status) is None
        assert any(
            isinstance(call, tuple) and call[0] == "release"
            for call in implementation.calls
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_resolve_dev_artifact_accepts_one_toolang_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "toolang-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    assert sandbox.resolve_dev_artifact(wheel, sandbox="docker") == wheel


def test_resolve_dev_artifact_selects_newest_wheel_recursively(
    tmp_path: Path,
) -> None:
    older = tmp_path / "toolang-1.0.0-py3-none-any.whl"
    nested = tmp_path / "nested"
    nested.mkdir()
    newer = nested / "toolang-0.9.0-py3-none-any.whl"
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "toolang"\nversion = "2.0.0"\n',
        encoding="utf-8",
    )
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    assert sandbox.resolve_dev_artifact(tmp_path, sandbox="docker") == newer


def test_resolve_dev_artifact_breaks_timestamp_ties_by_path(tmp_path: Path) -> None:
    first = tmp_path / "toolang-a.whl"
    second = tmp_path / "toolang-b.whl"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    os.utime(first, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second, ns=(1_000_000_000, 1_000_000_000))

    assert sandbox.resolve_dev_artifact(tmp_path, sandbox="docker") == first


def test_resolve_dev_artifact_rejects_invalid_paths(tmp_path: Path) -> None:
    text_file = tmp_path / "toolang.txt"
    text_file.write_text("not a wheel", encoding="utf-8")
    unrelated_wheel = tmp_path / "example-1.0.0-py3-none-any.whl"
    unrelated_wheel.write_bytes(b"wheel")
    empty = tmp_path / "empty"
    empty.mkdir()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / unrelated_wheel.name).write_bytes(b"wheel")

    with pytest.raises(FileNotFoundError, match="--dev path not found"):
        sandbox.resolve_dev_artifact(tmp_path / "missing", sandbox="docker")
    with pytest.raises(ValueError, match="--dev file is not a Toolang wheel"):
        sandbox.resolve_dev_artifact(text_file, sandbox="docker")
    with pytest.raises(ValueError, match="--dev file is not a Toolang wheel"):
        sandbox.resolve_dev_artifact(unrelated_wheel, sandbox="docker")
    with pytest.raises(FileNotFoundError, match="No Toolang wheels found"):
        sandbox.resolve_dev_artifact(empty, sandbox="docker")
    with pytest.raises(FileNotFoundError, match="No Toolang wheels found"):
        sandbox.resolve_dev_artifact(unrelated, sandbox="docker")


def test_resolve_dev_artifact_rejects_host_before_path_validation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="only applies to guest sandboxes"):
        sandbox.resolve_dev_artifact(tmp_path / "missing", sandbox="host")


def test_select_sandbox_keeps_selection_separate_from_plugin_config() -> None:
    configs = (
        {
            "sandbox": {"driver": "docker", "target": "python:3.13"},
            "plugin": {
                "sandbox": {
                    "docker": {
                        "image": "python:3.13-slim",
                    },
                    "host": {"mode": "local"},
                },
            },
        },
        {"plugin": {"sandbox": {"docker": {"image": "agent-image"}}}},
    )

    selected, config = sandbox._select_sandbox_configs(
        configs,
        explicit=None,
    )
    explicit, host_config = sandbox._select_sandbox_configs(
        configs,
        explicit="host",
    )

    assert selected == "docker:python:3.13"
    assert config == {"image": "agent-image"}
    assert explicit == "host"
    assert host_config == {"mode": "local"}

    def runtime_place(self, local_place: Path) -> Path:
        return Path("/runtime")


def test_guest_launch_snapshots_workspace_mounts_and_unavailable_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    implementation = FakeSandbox()
    monkeypatch.setattr(
        sandbox, "create_sandbox", lambda *_args, **_kwargs: implementation
    )

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    missing = tmp_path / "missing"
    spec.serve.layout.config.write_text(
        f'[workspaces]\nrepo = "{repo}"\nmissing = "{missing}"\n',
        encoding="utf-8",
    )
    handle = asyncio.run(sandbox.launch(spec))
    try:
        prepare = implementation.calls[0]
        assert isinstance(prepare, tuple)
        request = cast(SandboxRequest, prepare[2])
        hosted = Path("/runtime/agents/alice/.workspaces/repo")
        lab_root = spec.serve.layout.home / ".lab"
        hosted_lab = Path("/runtime/agents/alice/.workspaces/lab")
        assert SandboxMount(repo, hosted) in request.mounts
        assert SandboxMount(lab_root.resolve(), hosted_lab) in request.mounts
        assert not any(mount.local_path == missing for mount in request.mounts)
        assert request.envs["TOOLANG_WORKSPACE_LOCATION"] == "guest"
        assert json.loads(request.envs["TOOLANG_WORKSPACE_MOUNTS"]) == {
            "repo": [str(repo), str(hosted)],
            "lab": [str(lab_root.resolve()), str(hosted_lab)],
        }
        new = tmp_path / "new"
        new.mkdir()
        spec.serve.layout.config.write_text(
            f'[workspaces]\nrepo = "{repo}"\nnew = "{new}"\n',
            encoding="utf-8",
        )
        # This launch retains only the mounts captured before the config changed.
        assert not any(mount.local_path == new for mount in request.mounts)
        assert "new" not in json.loads(request.envs["TOOLANG_WORKSPACE_MOUNTS"])
    finally:
        asyncio.run(sandbox.stop(spec.serve.layout, force=True))
    assert handle.state.sandbox == "fake:value:with:colons"


@pytest.mark.parametrize("linked", [False, True])
def test_guest_workspace_config_retains_its_host_origin(tmp_path, monkeypatch, linked):
    import tomllib
    from toolang.common.config_sources import rebase_config

    implementation = FakeSandbox()
    monkeypatch.setattr(
        sandbox, "create_sandbox", lambda *_args, **_kwargs: implementation
    )

    async def ready(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    if linked:
        authored = tmp_path / "shared.toml"
        authored.write_text('[workspaces]\nrepo = "../../repo"\n')
        spec.serve.layout.config.symlink_to(authored)
    else:
        spec.serve.layout.config.write_text('[workspaces]\nrepo = "../../repo"\n')
    asyncio.run(sandbox.launch(spec))
    prepared = implementation.calls[0]
    assert isinstance(prepared, tuple)
    request = cast(SandboxRequest, prepared[2])
    config_mount = next(
        (
            mount
            for mount in request.mounts
            if mount.hosted_path == request.hosted_home / "config.toml"
        ),
        None,
    )
    content = (
        config_mount.local_path if config_mount else spec.serve.layout.config
    ).read_text()
    guest_config = rebase_config(tomllib.loads(content), request.hosted_home)
    assert len({mount.hosted_path for mount in request.mounts}) == len(request.mounts)
    assert config_mount is not None and config_mount.read_only
    captured = json.loads(request.envs["TOOLANG_WORKSPACE_MOUNTS"])
    assert guest_config["workspaces"] == {"repo": captured["repo"][0]}


def test_management_lock_reports_waiting_before_the_lock_is_available(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from toolang.common.files import file_write_lock

    layout = AgentLayout.resident(tmp_path, "alice")
    waiting = Event()
    events = []

    def progress(event):
        events.append(event)
        if event.status == "running":
            waiting.set()

    def acquire():
        with sandbox.management_lock(layout, progress=progress):
            assert events[-1].status == "ok"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_write_lock(layout.runtime_status.with_name("status.json.lock")):
            future = pool.submit(acquire)
            assert waiting.wait(timeout=2)
            assert [event.status for event in events] == ["running"]
            assert events[0].label == "Waiting for agent management..."
        future.result(timeout=2)
    assert [event.status for event in events] == ["running", "ok"]


@pytest.mark.parametrize("owned_handle", [False, True])
def test_shutdown_reports_management_lock_contention(tmp_path, owned_handle):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from toolang.common.files import file_write_lock

    layout = AgentLayout.resident(tmp_path, "alice")
    waiting = Event()
    events = []

    def progress(event):
        events.append(event)
        if event.status == "running":
            waiting.set()

    def stop():
        if owned_handle:
            handle = sandbox.SandboxHandle(
                FakeSandbox(),
                SandboxState(
                    "fake",
                    SandboxRef("test", "http://localhost:1"),
                    launch_id="fixture",
                ),
            )
            return asyncio.run(sandbox.stop_handle(layout, handle, progress=progress))
        return asyncio.run(sandbox.stop(layout, progress=progress))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_write_lock(layout.runtime_status.with_name("status.json.lock")):
            future = pool.submit(stop)
            assert waiting.wait(timeout=1), "Shutdown blocked without progress"
            assert not future.done()
        assert future.result(timeout=2) is False
    assert [event.status for event in events] == ["running", "ok"]
    assert {event.stage for event in events} == {"stop"}


@pytest.mark.parametrize("location", ["host", "guest"])
def test_launch_prepares_state_once_before_workload_with_state_overrides(
    tmp_path, monkeypatch, location
):
    implementation = FakeSandbox()
    implementation.location = location
    monkeypatch.setattr(
        sandbox, "create_sandbox", lambda *_args, **_kwargs: implementation
    )
    spec = _launch_spec(tmp_path)
    spec = replace(
        spec,
        serve=replace(
            spec.serve, ceiling_overrides={"models": ("*",), "skills": ("*",)}
        ),
    )
    captured = []
    original = state_prepare.prepare_agent_state

    def prepare(layout, **kwargs):
        captured.append(kwargs)
        return original(layout, **kwargs)

    monkeypatch.setattr(state_prepare, "prepare_agent_state", prepare)
    original_launch = implementation.launch

    async def launch(plan):
        assert len(captured) == 1
        assert agent_current_path(spec.serve.layout).exists()
        return await original_launch(plan)

    async def ready(*_args, **_kwargs):
        return None

    monkeypatch.setattr(implementation, "launch", launch)
    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    events = []
    asyncio.run(sandbox.launch(spec, progress=events.append))
    assert captured[0]["allow_overrides"] == {"skills": ("*",)}
    assert captured[0]["workspace_additions"] == spec.serve.workspace_additions
    complete = next(
        i for i, e in enumerate(events) if e.subject == "state" and e.complete
    )
    assert events[complete].label == "Prepared agent state"
    ready_event = next(
        i for i, e in enumerate(events) if e.kind == "runtime" and e.stage == "start"
    )
    assert complete < ready_event


def test_local_prepare_failure_never_creates_a_workload_and_allows_retry(
    tmp_path, monkeypatch
):
    implementation = FakeSandbox()
    implementation.location = "host"
    monkeypatch.setattr(
        sandbox, "create_sandbox", lambda *_args, **_kwargs: implementation
    )
    spec = _launch_spec(tmp_path)
    original = state_prepare.prepare_agent_state

    def reject(*_args, **_kwargs):
        raise OSError("cannot read State source")

    monkeypatch.setattr(state_prepare, "prepare_agent_state", reject)
    events = []
    with pytest.raises(OSError, match="cannot read State source"):
        asyncio.run(sandbox.launch(spec, progress=events.append))
    assert not any(
        isinstance(call, tuple) and call[0] == "launch" for call in implementation.calls
    )
    failure = next(e for e in events if e.subject == "state" and e.status == "failed")
    assert failure.label == "Failed to prepare agent state"
    assert not (
        spec.serve.layout.place / ".runtime" / "active" / "alice" / "alice.json"
    ).exists()
    monkeypatch.setattr(state_prepare, "prepare_agent_state", original)

    async def ready(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    asyncio.run(sandbox.launch(spec))
    assert (
        sum(
            isinstance(call, tuple) and call[0] == "launch"
            for call in implementation.calls
        )
        == 1
    )


def test_failed_workload_withdraws_discovery_before_resource_release(
    tmp_path, monkeypatch
):
    from toolang.up.instance import patch_workload
    from toolang.up.observation import runtime_inventory
    from toolang.up.registry import instance_filename

    implementation = FakeSandbox()
    monkeypatch.setattr(
        sandbox, "create_sandbox", lambda *_args, **_kwargs: implementation
    )

    async def ready(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)
    handle = asyncio.run(sandbox.launch(spec))
    patch_workload(
        spec.serve.layout.runtime_status,
        handle.state.launch_id,
        {"status": "failed", "pid": None},
    )
    reference = (
        tmp_path
        / ".runtime"
        / "active"
        / spec.serve.layout.address
        / instance_filename(spec.serve.layout)
    )
    inventory = runtime_inventory(tmp_path)
    assert not reference.exists()
    assert inventory.snapshot.instances[0].state == "failed"
    assert not inventory.snapshot.instances[0].needs_live_observation
    assert SandboxState.load(spec.serve.layout.runtime_status) == handle.state
    assert asyncio.run(sandbox.stop(spec.serve.layout))
    assert not reference.exists()
    assert spec.serve.layout.runtime_status.exists()


def test_automatic_retry_preserves_a_child_bind_failure_and_renews_generation(
    tmp_path, monkeypatch
):
    from toolang.up.instance import read_instance, patch_workload

    implementation = FakeSandbox()
    implementation.location = "host"
    generations = []
    monkeypatch.setattr(
        sandbox, "create_sandbox", lambda *_args, **_kwargs: implementation
    )
    spec = _launch_spec(tmp_path)
    spec = replace(spec, serve=replace(spec.serve, automatic_port=True))
    original = implementation.launch

    async def launch(plan):
        implementation.alive = True
        entry = read_instance(spec.serve.layout.runtime_status)
        assert entry is not None
        generations.append(entry["launch_id"])
        return await original(plan)

    async def ready(*_args, **_kwargs):
        if len(generations) == 1:
            patch_workload(
                spec.serve.layout.runtime_status,
                generations[0],
                {
                    "status": "failed",
                    "pid": None,
                    "message": "[Errno 98] Address already in use",
                },
            )
            raise RuntimeError("agent server exited before becoming ready")

    monkeypatch.setattr(implementation, "launch", launch)
    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    handle = asyncio.run(sandbox.launch(spec))
    assert len(generations) == 2 and len(set(generations)) == 2
    assert handle.state.launch_id == generations[-1]
    assert asyncio.run(sandbox.stop(spec.serve.layout))


def test_adapter_assigned_port_is_the_compatible_effective_port(tmp_path, monkeypatch):
    from toolang.up.contract import require_compatible

    implementation = FakeSandbox()
    monkeypatch.setattr(
        sandbox, "create_sandbox", lambda *_args, **_kwargs: implementation
    )
    spec = _launch_spec(tmp_path)
    spec = replace(spec, serve=replace(spec.serve, automatic_port=True))

    async def launch(_plan):
        return SandboxRef("assigned-port", "http://localhost:9184")

    async def ready(*_args, **_kwargs):
        return None

    monkeypatch.setattr(implementation, "launch", launch)
    monkeypatch.setattr(sandbox, "_wait_ready", ready)
    asyncio.run(sandbox.launch(spec))
    try:
        require_compatible(spec.serve.layout, {"port": 9184})
    finally:
        asyncio.run(sandbox.stop(spec.serve.layout))

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.base.errors import SandboxLaunchError
from toolang.base.types.progress import ProgressEvent, ProgressSink
from toolang.base.types.sandbox import (
    SandboxPlan,
    SandboxRef,
    SandboxRequest,
)
from toolang.common.layout import AgentLayout
from toolang.state.state import AgentState
from toolang.up import sandbox
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

    def runtime_root(self, local_root: Path) -> Path:
        del local_root
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
        progress_id: str,
    ) -> None:
        del progress, progress_id
        self.calls.append(("attach", plan, ref))
        if self.attach_error is not None:
            raise self.attach_error

    async def follow(self, plan: SandboxPlan, ref: SandboxRef) -> None:
        self.calls.append(("follow", plan, ref))

    async def unfollow(self, ref: SandboxRef) -> None:
        self.calls.append(("unfollow", ref))

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


class GuestFailureSandbox(FakeSandbox):
    async def attach(
        self,
        plan: SandboxPlan,
        ref: SandboxRef,
        *,
        progress: ProgressSink | None = None,
        progress_id: str,
    ) -> None:
        self.calls.append(("attach", plan, ref))
        if progress is None:
            return
        progress(
            ProgressEvent(
                id="startup:guest:install",
                kind="runtime",
                stage="create",
                label="Installing Toolang",
                status="running",
            )
        )
        await asyncio.sleep(0.01)
        progress(
            ProgressEvent(
                id="startup:guest:validate",
                kind="runtime",
                stage="create",
                label="Checking Toolang compatibility",
                status="failed",
                detail="incompatible CLI",
            )
        )

    async def running(self, ref: SandboxRef) -> bool:
        del ref
        return False


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

    layout.root_config.write_text(
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
    state = sandbox.SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef(
            runtime_id="container",
            endpoint="http://localhost:8123",
            meta={"image": "python:3.13-slim"},
            runtime_kind="container",
            runtime_name="toolang-alice-launch",
        ),
    )

    state.save(path)

    assert sandbox.SandboxState.load(path) == state
    assert '"version": 1' in path.read_text(encoding="utf-8")


def test_sandbox_state_reads_version_one_reference_without_runtime_identity(
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

    state = sandbox.SandboxState.load(path)

    assert state is not None
    assert state.ref.runtime_kind == "workload"
    assert state.ref.runtime_name is None


def test_sandbox_state_rejects_corrupted_data(tmp_path: Path) -> None:
    path = tmp_path / "sandbox.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid sandbox state"):
        sandbox.SandboxState.load(path)


def test_sandbox_state_rejects_an_unversioned_payload(tmp_path: Path) -> None:
    path = tmp_path / "sandbox.json"
    path.write_text(
        '{"sandbox":"host","ref":{"runtime_id":"1","endpoint":"http://x"}}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unsupported sandbox state version"):
        sandbox.SandboxState.load(path)


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
    sandbox.SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-test", "http://localhost:8123"),
    ).save(layout.sandbox_state)

    asyncio.run(sandbox.release_for_removal(layout))

    assert ("release", SandboxRef("toolang-alice-test", "http://localhost:8123")) in (
        implementation.calls
    )
    assert sandbox.SandboxState.load(layout.sandbox_state) is None


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
    state = sandbox.SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-test", "http://localhost:8123"),
    )
    state.save(layout.sandbox_state)

    with pytest.raises(ValueError, match="already running"):
        asyncio.run(sandbox.release_stopped(layout))

    assert ("release", state.ref) not in implementation.calls
    assert sandbox.SandboxState.load(layout.sandbox_state) == state


def test_stop_handle_stops_only_its_exact_owned_workload(
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    layout = AgentLayout.resident(tmp_path, "alice")
    state = sandbox.SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-owned", "http://localhost:8123"),
    )
    state.save(layout.sandbox_state)
    handle = sandbox.SandboxHandle(implementation, state)
    events: list[ProgressEvent] = []

    assert (
        asyncio.run(sandbox.stop_handle(layout, handle, progress=events.append)) is True
    )

    assert ("stop", state.ref, False) in implementation.calls
    assert ("release", state.ref) in implementation.calls
    assert sandbox.SandboxState.load(layout.sandbox_state) is None
    assert [(event.kind, event.stage, event.status) for event in events] == [
        ("runtime", "stop", "running"),
        ("runtime", "stop", "ok"),
        ("runtime", "destroy", "running"),
        ("runtime", "destroy", "ok"),
    ]


def test_stop_handle_reports_the_failed_cleanup_stage(
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    implementation.stop_error = RuntimeError("stop failed")
    layout = AgentLayout.resident(tmp_path, "alice")
    state = sandbox.SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-owned", "http://localhost:8123"),
    )
    state.save(layout.sandbox_state)
    events: list[ProgressEvent] = []

    with pytest.raises(RuntimeError, match="stop failed"):
        asyncio.run(
            sandbox.stop_handle(
                layout,
                sandbox.SandboxHandle(implementation, state),
                progress=events.append,
            )
        )

    assert [(event.stage, event.status, event.detail) for event in events] == [
        ("stop", "running", "docker:python:3.13-slim"),
        ("stop", "failed", "stop failed"),
    ]
    assert sandbox.SandboxState.load(layout.sandbox_state) == state


def test_stop_handle_rejects_replaced_ownership_without_stopping(
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    layout = AgentLayout.resident(tmp_path, "alice")
    owned = sandbox.SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-owned", "http://localhost:8123"),
    )
    replacement = sandbox.SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-replacement", "http://localhost:8124"),
    )
    replacement.save(layout.sandbox_state)

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
    assert sandbox.SandboxState.load(layout.sandbox_state) == replacement


def test_stop_handle_does_not_stop_without_current_ownership(
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    layout = AgentLayout.resident(tmp_path, "alice")
    owned = sandbox.SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("toolang-alice-owned", "http://localhost:8123"),
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


def test_sandbox_status_treats_plugin_recovery_failure_as_not_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    sandbox.SandboxState(
        sandbox="missing",
        ref=SandboxRef("workload-1", "http://localhost:8123"),
    ).save(layout.sandbox_state)

    def fail_recovery(*_args: object) -> object:
        raise ValueError("plugin is unavailable")

    monkeypatch.setattr(sandbox, "load_state_sandbox", fail_recovery)

    assert process_runtime._sandbox_running(layout) is False


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
    spec.serve.layout.root_config.write_text(
        "[plugin.sandbox.fake]\ncurrent = true\n",
        encoding="utf-8",
    )

    events: list[ProgressEvent] = []
    handle = asyncio.run(sandbox.launch(spec, progress=events.append))

    prepare = implementation.calls[0]
    assert isinstance(prepare, tuple)
    assert prepare[:2] == ("prepare", "value:with:colons")
    request = cast(SandboxRequest, prepare[2])
    assert request.hosted_root == Path("/runtime")
    assert request.hosted_home == Path("/runtime/agents/alice")
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) == handle.state
    assert [(event.kind, event.stage, event.status) for event in events] == [
        ("runtime", "create", "running"),
        ("runtime", "create", "running"),
        ("runtime", "create", "ok"),
        ("runtime", "start", "running"),
        ("runtime", "start", "ok"),
    ]

    assert asyncio.run(sandbox.stop(spec.serve.layout, force=True)) is True
    assert ("stop", handle.state.ref, True) in implementation.calls
    assert ("release", handle.state.ref) in implementation.calls
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) is None
    assert configs == [{}, {"current": True}]


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


def test_stop_failure_preserves_sandbox_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeSandbox()
    implementation.stop_error = RuntimeError("still running")
    monkeypatch.setattr(sandbox, "create_sandbox", lambda _name, config: implementation)
    spec = _launch_spec(tmp_path)
    state = sandbox.SandboxState(
        sandbox=spec.sandbox,
        ref=SandboxRef("workload-1", spec.serve.endpoint),
    )
    state.save(spec.serve.layout.sandbox_state)

    with pytest.raises(RuntimeError, match="still running"):
        asyncio.run(sandbox.stop(spec.serve.layout))

    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) == state


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
    cleanup_events: list[ProgressEvent] = []
    with pytest.raises(TimeoutError, match="not ready"):
        asyncio.run(
            sandbox.launch(
                spec,
                progress=events.append,
                cleanup_progress=cleanup_events.append,
            )
        )

    assert (
        "stop",
        SandboxRef("workload-1", spec.serve.endpoint),
        True,
    ) in implementation.calls
    assert (
        "release",
        SandboxRef("workload-1", spec.serve.endpoint),
    ) in implementation.calls
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) is None
    assert events[-1].kind == "runtime"
    assert events[-1].stage == "start"
    assert events[-1].status == "failed"
    assert events[-1].detail == "not ready"
    assert [(event.stage, event.status) for event in cleanup_events] == [
        ("stop", "running"),
        ("stop", "ok"),
        ("destroy", "running"),
        ("destroy", "ok"),
    ]


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

    stages = [(event.stage, event.label, event.status) for event in events]
    assert ("create", "Installing Toolang", "running") in stages
    assert ("create", "Checking Toolang compatibility", "failed") in stages
    assert ("start", "Waiting for agent API", "running") not in stages
    assert stages[-1] == ("create", "Starting workload", "failed")


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

    state = sandbox.SandboxState.load(spec.serve.layout.sandbox_state)
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

    state = sandbox.SandboxState.load(spec.serve.layout.sandbox_state)
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

    state = sandbox.SandboxState.load(spec.serve.layout.sandbox_state)
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
    ready_states: list[sandbox.SandboxState] = []

    def on_ready(state: sandbox.SandboxState) -> None:
        ready_states.append(state)
        implementation.calls.append(("ready", state.ref))

    result = asyncio.run(sandbox.run(spec, on_ready=on_ready))

    ref = SandboxRef("workload-1", spec.serve.endpoint)
    assert result == 7
    assert ready_states == [sandbox.SandboxState(sandbox=spec.sandbox, ref=ref)]
    assert ("wait", ref) in implementation.calls
    follow_call = next(
        call
        for call in implementation.calls
        if isinstance(call, tuple) and call[0] == "follow"
    )
    assert implementation.calls.index(("ready", ref)) < implementation.calls.index(
        follow_call
    )
    assert implementation.calls.index(follow_call) < (
        implementation.calls.index(("wait", ref))
    )
    assert ("release", ref) in implementation.calls
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) is None


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
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) is None


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

    def fail_ready(_state: sandbox.SandboxState) -> None:
        raise RuntimeError("ready output failed")

    with pytest.raises(RuntimeError, match="ready output failed"):
        asyncio.run(sandbox.run(spec, on_ready=fail_ready))

    ref = SandboxRef("workload-1", spec.serve.endpoint)
    assert ("stop", ref, True) in implementation.calls
    assert ("release", ref) in implementation.calls
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) is None


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
        lambda: sandbox.release_for_removal(spec.serve.layout),
    ):
        with pytest.raises(ValueError, match="legacy guest-writable sandbox state"):
            asyncio.run(operation())

    status = process_runtime.AgentProcess(spec.serve.layout).status(ui_base_url="")
    assert status is not None
    assert status.status == "failed"


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
        lambda: sandbox.release_for_removal(spec.serve.layout),
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
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) is not None
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
    cleanup_events: list[ProgressEvent] = []

    async def cancel_run() -> BaseException | None:
        task = asyncio.create_task(
            sandbox.run(spec, cleanup_progress=cleanup_events.append)
        )
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
    assert implementation.calls.index(("unfollow", ref)) < implementation.calls.index(
        ("stop", ref, False)
    )
    assert ("release", ref) in implementation.calls
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) is None
    assert [(event.stage, event.status) for event in cleanup_events] == [
        ("stop", "running"),
        ("stop", "ok"),
        ("destroy", "running"),
        ("destroy", "ok"),
    ]


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
                timeout_sec=0.1,
            )
        )


def test_resolve_dev_artifact_accepts_one_toolang_wheel(tmp_path: Path) -> None:
    wheel = tmp_path / "toolang-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel")

    assert sandbox._resolve_dev_artifact(wheel, sandbox="docker") == wheel


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

    assert sandbox._resolve_dev_artifact(tmp_path, sandbox="docker") == newer


def test_resolve_dev_artifact_breaks_timestamp_ties_by_path(tmp_path: Path) -> None:
    first = tmp_path / "toolang-a.whl"
    second = tmp_path / "toolang-b.whl"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    os.utime(first, ns=(1_000_000_000, 1_000_000_000))
    os.utime(second, ns=(1_000_000_000, 1_000_000_000))

    assert sandbox._resolve_dev_artifact(tmp_path, sandbox="docker") == first


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
        sandbox._resolve_dev_artifact(tmp_path / "missing", sandbox="docker")
    with pytest.raises(ValueError, match="--dev file is not a Toolang wheel"):
        sandbox._resolve_dev_artifact(text_file, sandbox="docker")
    with pytest.raises(ValueError, match="--dev file is not a Toolang wheel"):
        sandbox._resolve_dev_artifact(unrelated_wheel, sandbox="docker")
    with pytest.raises(FileNotFoundError, match="No Toolang wheels found"):
        sandbox._resolve_dev_artifact(empty, sandbox="docker")
    with pytest.raises(FileNotFoundError, match="No Toolang wheels found"):
        sandbox._resolve_dev_artifact(unrelated, sandbox="docker")


def test_resolve_dev_artifact_rejects_host_before_path_validation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="only applies to guest sandboxes"):
        sandbox._resolve_dev_artifact(tmp_path / "missing", sandbox="host")


def test_select_sandbox_keeps_selection_separate_from_plugin_config() -> None:
    state = cast(
        AgentState,
        SimpleNamespace(
            root_config={
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
            home_config={"plugin": {"sandbox": {"docker": {"image": "agent-image"}}}},
        ),
    )

    selected, config = sandbox._select_sandbox(
        state,
        explicit=None,
    )
    explicit, host_config = sandbox._select_sandbox(
        state,
        explicit="host",
    )

    assert selected == "docker:python:3.13"
    assert config == {"image": "agent-image"}
    assert explicit == "host"
    assert host_config == {"mode": "local"}

    def runtime_root(self, local_root: Path) -> Path:
        return Path("/runtime")

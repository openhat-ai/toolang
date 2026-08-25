from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from toolang.base.types.sandbox import (
    SandboxPlan,
    SandboxRef,
    SandboxRequest,
)
from toolang.common.layout import AgentLayout
from toolang.state.state import AgentState
from toolang.up import sandbox
from toolang.up.server import ServeSpec


class FakeSandbox:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.alive = True
        self.stop_error: Exception | None = None

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
            log_path=request.log_path,
            endpoint=request.endpoint,
        )

    async def launch(self, plan: SandboxPlan) -> SandboxRef:
        self.calls.append(("launch", plan))
        return SandboxRef("workload-1", plan.endpoint)

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


def test_sandbox_state_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "sandbox.json"
    state = sandbox.SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef(
            runtime_id="container",
            endpoint="http://localhost:8123",
            meta={"image": "python:3.13-slim"},
        ),
    )

    state.save(path)

    assert sandbox.SandboxState.load(path) == state


def test_sandbox_state_rejects_corrupted_data(tmp_path: Path) -> None:
    path = tmp_path / "sandbox.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid sandbox state"):
        sandbox.SandboxState.load(path)


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

    handle = asyncio.run(sandbox.launch(spec))

    prepare = implementation.calls[0]
    assert isinstance(prepare, tuple)
    assert prepare[:2] == ("prepare", "value:with:colons")
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) == handle.state

    assert asyncio.run(sandbox.stop(spec.serve.layout, force=True)) is True
    assert ("stop", handle.state.ref, True) in implementation.calls
    assert ("release", handle.state.ref) in implementation.calls
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) is None
    assert configs == [{}, {"current": True}]


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
        raise TimeoutError("not ready")

    monkeypatch.setattr(sandbox, "_wait_ready", fail)
    spec = _launch_spec(tmp_path)

    with pytest.raises(TimeoutError, match="not ready"):
        asyncio.run(sandbox.launch(spec))

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

    result = asyncio.run(sandbox.run(spec, on_ready=ready_states.append))

    ref = SandboxRef("workload-1", spec.serve.endpoint)
    assert result == 7
    assert ready_states == [sandbox.SandboxState(sandbox=spec.sandbox, ref=ref)]
    assert ("wait", ref) in implementation.calls
    assert ("release", ref) in implementation.calls
    assert sandbox.SandboxState.load(spec.serve.layout.sandbox_state) is None


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
                            "token_env": "SANDBOX_TOKEN",
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
        environ={"SANDBOX_TOKEN": "secret"},
    )
    explicit, host_config = sandbox._select_sandbox(
        state,
        explicit="host",
        environ={"SANDBOX_TOKEN": "secret"},
    )

    assert selected == "docker:python:3.13"
    assert config == {"image": "agent-image", "token": "secret"}
    assert explicit == "host"
    assert host_config == {"mode": "local"}

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from toolang.base.types.hosting import (
    HostingPlan,
    HostingRef,
    HostingRequest,
)
from toolang.common.layout import AgentLayout
from toolang.up import hosting
from toolang.up.server import ServeSpec


class FakeHosting:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.alive = True
        self.stop_error: Exception | None = None

    def prepare(
        self,
        spec: str | None,
        request: HostingRequest,
    ) -> HostingPlan:
        self.calls.append(("prepare", spec, request))
        return HostingPlan(
            sandbox=f"fake:{spec}",
            command=request.command,
            working_directory=request.working_directory,
            log_path=request.log_path,
            endpoint=request.endpoint,
        )

    async def launch(self, plan: HostingPlan) -> HostingRef:
        self.calls.append(("launch", plan))
        return HostingRef("workload-1", plan.endpoint)

    async def running(self, ref: HostingRef) -> bool:
        self.calls.append(("running", ref))
        return self.alive

    async def wait(self, ref: HostingRef) -> int:
        self.calls.append(("wait", ref))
        return 7

    async def stop(self, ref: HostingRef, *, force: bool = False) -> None:
        self.calls.append(("stop", ref, force))
        if self.stop_error is not None:
            raise self.stop_error
        self.alive = False

    async def release(self, ref: HostingRef) -> None:
        self.calls.append(("release", ref))


class ConcurrentHosting(FakeHosting):
    def __init__(self) -> None:
        super().__init__()
        self.launches = 0

    async def launch(self, plan: HostingPlan) -> HostingRef:
        self.launches += 1
        await asyncio.sleep(0)
        return await super().launch(plan)


class BlockingHosting(FakeHosting):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = asyncio.Event()

    async def wait(self, ref: HostingRef) -> int:
        self.calls.append(("wait", ref))
        self.waiting.set()
        await asyncio.Event().wait()
        raise AssertionError("blocking wait unexpectedly returned")


def _launch_spec(tmp_path: Path) -> hosting.LaunchSpec:
    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    return hosting.LaunchSpec(
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


def test_hosting_state_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "hosting.json"
    state = hosting.HostingState(
        sandbox="docker:python:3.13-slim",
        ref=HostingRef(
            runtime_id="container",
            endpoint="http://localhost:8123",
            meta={"image": "python:3.13-slim"},
        ),
    )

    state.save(path)

    assert hosting.HostingState.load(path) == state


def test_hosting_state_rejects_corrupted_data(tmp_path: Path) -> None:
    path = tmp_path / "hosting.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid hosting state"):
        hosting.HostingState.load(path)


def test_launch_delegates_complete_spec_and_stop_releases_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeHosting()
    monkeypatch.setattr(hosting, "load_hosting", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(hosting, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)

    handle = asyncio.run(hosting.launch(spec))

    prepare = implementation.calls[0]
    assert isinstance(prepare, tuple)
    assert prepare[:2] == ("prepare", "value:with:colons")
    assert hosting.HostingState.load(spec.serve.layout.hosting_state) == handle.state

    assert asyncio.run(hosting.stop(spec.serve.layout, force=True)) is True
    assert ("stop", handle.state.ref, True) in implementation.calls
    assert ("release", handle.state.ref) in implementation.calls
    assert hosting.HostingState.load(spec.serve.layout.hosting_state) is None


def test_stop_failure_preserves_hosting_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeHosting()
    implementation.stop_error = RuntimeError("still running")
    monkeypatch.setattr(hosting, "load_hosting", lambda _name, config: implementation)
    spec = _launch_spec(tmp_path)
    state = hosting.HostingState(
        sandbox=spec.sandbox,
        ref=HostingRef("workload-1", spec.serve.endpoint),
    )
    state.save(spec.serve.layout.hosting_state)

    with pytest.raises(RuntimeError, match="still running"):
        asyncio.run(hosting.stop(spec.serve.layout))

    assert hosting.HostingState.load(spec.serve.layout.hosting_state) == state


def test_launch_failure_stops_releases_and_clears_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeHosting()
    monkeypatch.setattr(hosting, "load_hosting", lambda _name, config: implementation)

    async def fail(*_args, **_kwargs) -> None:
        raise TimeoutError("not ready")

    monkeypatch.setattr(hosting, "_wait_ready", fail)
    spec = _launch_spec(tmp_path)

    with pytest.raises(TimeoutError, match="not ready"):
        asyncio.run(hosting.launch(spec))

    assert (
        "stop",
        HostingRef("workload-1", spec.serve.endpoint),
        True,
    ) in implementation.calls
    assert (
        "release",
        HostingRef("workload-1", spec.serve.endpoint),
    ) in implementation.calls
    assert hosting.HostingState.load(spec.serve.layout.hosting_state) is None


def test_foreground_run_waits_then_releases_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = FakeHosting()
    monkeypatch.setattr(hosting, "load_hosting", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(hosting, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)
    ready_states: list[hosting.HostingState] = []

    result = asyncio.run(hosting.run(spec, on_ready=ready_states.append))

    ref = HostingRef("workload-1", spec.serve.endpoint)
    assert result == 7
    assert ready_states == [hosting.HostingState(sandbox=spec.sandbox, ref=ref)]
    assert ("wait", ref) in implementation.calls
    assert ("release", ref) in implementation.calls
    assert hosting.HostingState.load(spec.serve.layout.hosting_state) is None


def test_concurrent_launch_accepts_only_one_workload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = ConcurrentHosting()
    monkeypatch.setattr(hosting, "load_hosting", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(hosting, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)

    async def launch_twice() -> list[object]:
        return await asyncio.gather(
            hosting.launch(spec),
            hosting.launch(spec),
            return_exceptions=True,
        )

    results = asyncio.run(launch_twice())

    assert implementation.launches == 1
    assert sum(isinstance(result, hosting.HostingHandle) for result in results) == 1
    errors = [result for result in results if isinstance(result, ValueError)]
    assert len(errors) == 1
    assert "already running" in str(errors[0])


def test_canceled_run_preserves_state_when_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    implementation = BlockingHosting()
    implementation.stop_error = RuntimeError("still running")
    monkeypatch.setattr(hosting, "load_hosting", lambda _name, config: implementation)

    async def ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(hosting, "_wait_ready", ready)
    spec = _launch_spec(tmp_path)

    async def cancel_run() -> BaseException | None:
        task = asyncio.create_task(hosting.run(spec))
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
    assert hosting.HostingState.load(spec.serve.layout.hosting_state) is not None
    assert not any(
        isinstance(call, tuple) and call[0] == "release"
        for call in implementation.calls
    )


def test_readiness_fails_when_workload_exits() -> None:
    implementation = FakeHosting()
    implementation.alive = False

    with pytest.raises(
        RuntimeError,
        match="agent server exited before becoming ready",
    ):
        asyncio.run(
            hosting._wait_ready(
                implementation,
                HostingRef("workload-1", "http://localhost:1"),
                timeout_sec=0.1,
            )
        )

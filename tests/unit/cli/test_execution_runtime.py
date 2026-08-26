"""Selection and ownership of CLI execution runtimes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.cli.common import execution_runtime as runtime
from toolang.common.layout import AgentLayout
from toolang.up.logging import LoggingPlan
from toolang.up.process import AgentStatus
from toolang.up.records import SandboxState
from toolang.base.types.sandbox import SandboxRef


class _Progress:
    current_stage = "Starting workload"
    failure_reason: str | None = None

    def __init__(self) -> None:
        self.finished = 0
        self.interrupted = 0

    def __call__(self, _event: object) -> None:
        pass

    def finish(self) -> None:
        self.finished += 1

    def interrupt(self) -> None:
        self.interrupted += 1


def _status(
    *,
    value: str,
    endpoint: str | None = None,
    sandbox: str | None = None,
) -> AgentStatus:
    return AgentStatus(
        name="alice",
        status=value,
        endpoint=endpoint,
        api_url=None,
        webui_url=None,
        sandbox=sandbox,
    )


def _set_status(
    monkeypatch: pytest.MonkeyPatch,
    layout: AgentLayout,
    status: AgentStatus | None,
) -> None:
    class Process:
        def __init__(self, selected: AgentLayout) -> None:
            assert selected == layout

        def status(self, *, ui_base_url: str) -> AgentStatus | None:
            assert ui_base_url == "https://ui.test"
            return status

    monkeypatch.setattr(runtime.agents, "AgentProcess", Process)


def test_execution_runtime_attaches_to_a_compatible_running_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(
        monkeypatch,
        layout,
        _status(
            value="running",
            endpoint="http://127.0.0.1:7001",
            sandbox="docker:python:3.13-slim",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_resolve_inactive_launch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a running agent must not resolve another launch")
        ),
    )

    with runtime.open_execution_runtime(
        layout,
        sandbox="docker",
        ui_base_url="https://ui.test",
    ) as selected:
        assert selected == runtime.ExecutionRuntime(
            sandbox="docker:python:3.13-slim",
            mode="remote",
            endpoint="http://127.0.0.1:7001",
        )


def test_execution_runtime_rejects_dev_for_an_attached_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(
        monkeypatch,
        layout,
        _status(
            value="running",
            endpoint="http://127.0.0.1:7001",
            sandbox="docker:python:3.13-slim",
        ),
    )

    with pytest.raises(runtime.ExecutionRuntimeError, match="cannot modify running"):
        with runtime.open_execution_runtime(
            layout,
            sandbox="docker",
            dev=tmp_path / "dist",
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("an attached runtime must not accept --dev")


@pytest.mark.parametrize(
    ("requested", "message"),
    (
        ("host", "does not match running sandbox"),
        ("docker:other", "does not match running sandbox"),
    ),
)
def test_execution_runtime_rejects_a_running_sandbox_mismatch(
    requested: str,
    message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(
        monkeypatch,
        layout,
        _status(
            value="running",
            endpoint="http://127.0.0.1:7001",
            sandbox="docker:python:3.13-slim",
        ),
    )

    with pytest.raises(runtime.ExecutionRuntimeError, match=message):
        with runtime.open_execution_runtime(
            layout,
            sandbox=requested,
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("a mismatched runtime must not open")


@pytest.mark.parametrize("status", ("preparing", "starting"))
def test_execution_runtime_rejects_an_unready_agent(
    status: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(
        monkeypatch,
        layout,
        _status(value=status, endpoint="http://127.0.0.1:7001", sandbox="host"),
    )

    with pytest.raises(runtime.ExecutionRuntimeError, match=status):
        with runtime.open_execution_runtime(
            layout,
            sandbox=None,
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("an unready runtime must not open")


@pytest.mark.parametrize("status", (None, "stopped", "failed"))
def test_execution_runtime_opens_embedded_host_and_releases_stopped_state(
    status: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(
        monkeypatch,
        layout,
        _status(value=status) if status is not None else None,
    )
    monkeypatch.setattr(
        runtime.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: "host",
    )
    released: list[AgentLayout] = []

    async def release_stopped(selected: AgentLayout) -> None:
        released.append(selected)

    monkeypatch.setattr(runtime.sandbox_runtime, "release_stopped", release_stopped)

    with runtime.open_execution_runtime(
        layout,
        sandbox=None,
        ui_base_url="https://ui.test",
    ) as selected:
        assert selected == runtime.ExecutionRuntime(sandbox="host", mode="embedded")

    assert released == [layout]


def test_execution_runtime_rejects_dev_for_embedded_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        runtime.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: "host",
    )

    with pytest.raises(runtime.ExecutionRuntimeError, match="does not apply"):
        with runtime.open_execution_runtime(
            layout,
            sandbox="host",
            dev=tmp_path / "dist",
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("embedded host must not accept --dev")


def test_execution_runtime_launches_and_cleans_up_a_temporary_guest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        runtime.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: explicit or "docker",
    )
    launch = SimpleNamespace(sandbox="docker")
    development = tmp_path / "dist"

    def resolve_launch(*_args: object, **kwargs: object) -> object:
        assert kwargs["dev"] == development
        return launch

    monkeypatch.setattr(runtime, "_resolve_inactive_launch", resolve_launch)
    progress = _Progress()
    shutdown_progress = _Progress()
    monkeypatch.setattr(
        runtime,
        "make_runtime_startup_progress",
        lambda *_args, **_kwargs: progress,
    )
    monkeypatch.setattr(
        runtime,
        "make_runtime_shutdown_progress",
        lambda *_args, **_kwargs: shutdown_progress,
    )
    implementation = cast(Any, SimpleNamespace())
    state = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("container-1", "http://127.0.0.1:8123"),
    )
    handle = runtime.sandbox_runtime.SandboxHandle(implementation, state)
    calls: list[object] = []

    async def launch_runtime(spec: object, *, progress: object) -> object:
        calls.append(("launch", spec, progress))
        return handle

    async def stop_handle(
        selected: AgentLayout,
        selected_handle: object,
        *,
        force: bool = False,
        progress: object | None = None,
    ) -> bool:
        calls.append(("stop", selected, selected_handle, force, progress))
        return True

    monkeypatch.setattr(runtime.sandbox_runtime, "launch", launch_runtime)
    monkeypatch.setattr(runtime.sandbox_runtime, "stop_handle", stop_handle)

    with runtime.open_execution_runtime(
        layout,
        sandbox="docker",
        dev=development,
        ui_base_url="https://ui.test",
    ) as selected:
        calls.append("body")
        assert selected == runtime.ExecutionRuntime(
            sandbox="docker:python:3.13-slim",
            mode="remote",
            endpoint="http://127.0.0.1:8123",
            owned=True,
        )

    assert calls == [
        ("launch", launch, progress),
        "body",
        ("stop", layout, handle, False, shutdown_progress),
    ]
    assert progress.finished == 1
    assert shutdown_progress.finished == 1


@pytest.mark.parametrize(
    ("development", "hint"),
    (
        (None, "pass `--dev dist`"),
        (Path("dist"), "Upgrade the Toolang package"),
    ),
)
def test_execution_runtime_startup_failure_uses_the_available_dev_hint(
    development: Path | None,
    hint: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        runtime.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: explicit or "docker",
    )
    monkeypatch.setattr(
        runtime,
        "_resolve_inactive_launch",
        lambda *_args, **_kwargs: SimpleNamespace(sandbox="docker"),
    )
    progress = _Progress()
    progress.failure_reason = (
        "Installed Toolang does not provide the required `too serve` entrypoint."
    )
    monkeypatch.setattr(
        runtime,
        "make_runtime_startup_progress",
        lambda *_args, **_kwargs: progress,
    )

    async def fail_launch(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("startup failed")

    monkeypatch.setattr(runtime.sandbox_runtime, "launch", fail_launch)

    with pytest.raises(runtime.ExecutionRuntimeError) as captured:
        with runtime.open_execution_runtime(
            layout,
            sandbox="docker",
            dev=development,
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("a failed runtime must not open")

    assert hint in str(captured.value)


def test_execution_runtime_cleanup_does_not_hide_a_body_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        runtime.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: explicit or "docker",
    )
    monkeypatch.setattr(
        runtime,
        "_resolve_inactive_launch",
        lambda *_args, **_kwargs: SimpleNamespace(sandbox="docker"),
    )
    monkeypatch.setattr(
        runtime,
        "make_runtime_startup_progress",
        lambda *_args, **_kwargs: _Progress(),
    )
    state = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("container-1", "http://127.0.0.1:8123"),
    )
    handle = runtime.sandbox_runtime.SandboxHandle(cast(Any, SimpleNamespace()), state)

    async def launch_runtime(*_args: object, **_kwargs: object) -> object:
        return handle

    async def fail_cleanup(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(runtime.sandbox_runtime, "launch", launch_runtime)
    monkeypatch.setattr(runtime.sandbox_runtime, "stop_handle", fail_cleanup)

    with pytest.raises(LookupError, match="body failed"):
        with runtime.open_execution_runtime(
            layout,
            sandbox="docker",
            ui_base_url="https://ui.test",
        ):
            raise LookupError("body failed")

    assert "could not stop temporary agent alice" in capsys.readouterr().err


def test_execution_runtime_cleans_up_a_launched_guest_with_invalid_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        runtime.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: explicit or "docker",
    )
    monkeypatch.setattr(
        runtime,
        "_resolve_inactive_launch",
        lambda *_args, **_kwargs: SimpleNamespace(sandbox="docker"),
    )
    monkeypatch.setattr(
        runtime,
        "make_runtime_startup_progress",
        lambda *_args, **_kwargs: _Progress(),
    )
    state = cast(
        Any,
        SimpleNamespace(
            sandbox="docker:python:3.13-slim",
            ref=SimpleNamespace(endpoint=""),
        ),
    )
    handle = runtime.sandbox_runtime.SandboxHandle(cast(Any, SimpleNamespace()), state)
    cleaned: list[object] = []

    async def launch_runtime(*_args: object, **_kwargs: object) -> object:
        return handle

    async def stop_handle(
        selected: AgentLayout,
        selected_handle: object,
        *,
        progress: object | None = None,
    ) -> bool:
        cleaned.append((selected, selected_handle, progress))
        return True

    monkeypatch.setattr(runtime.sandbox_runtime, "launch", launch_runtime)
    monkeypatch.setattr(runtime.sandbox_runtime, "stop_handle", stop_handle)

    with pytest.raises(runtime.ExecutionRuntimeError, match="requires an endpoint"):
        with runtime.open_execution_runtime(
            layout,
            sandbox="docker",
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("an invalid remote runtime must not open")

    assert len(cleaned) == 1
    assert cleaned[0][:2] == (layout, handle)


def test_inactive_launch_wraps_environment_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    monkeypatch.setattr(
        runtime,
        "load_runtime_environ",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("could not read dotenv")
        ),
    )

    with pytest.raises(runtime.ExecutionRuntimeError, match="could not read dotenv"):
        runtime._resolve_inactive_launch(
            layout,
            sandbox="docker",
            dev=None,
            model_catalog=None,
            base_environ={},
        )


def test_inactive_launch_uses_fresh_environment_and_file_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    catalog = tmp_path / "models.json"
    development = tmp_path / "toolang.whl"
    captured: dict[str, Any] = {}

    def load_environ(
        selected: AgentLayout,
        *,
        base_environ: object,
    ) -> dict[str, str]:
        assert selected == layout
        assert base_environ == {"PROCESS": "value"}
        return {"PROCESS": "value", "DOTENV": "agent"}

    def logging_plan(**kwargs: object) -> LoggingPlan:
        captured["logging"] = kwargs
        return LoggingPlan(
            spec="error",
            destination="agent_log",
            path=layout.runtime_log,
            environ={"LOGGED": "yes"},
        )

    async def resolve_launch(**kwargs: object) -> object:
        captured["launch"] = kwargs
        return SimpleNamespace(sandbox="docker")

    monkeypatch.setattr(runtime, "load_runtime_environ", load_environ)
    monkeypatch.setattr(runtime, "resolve_agent_logging", logging_plan)
    monkeypatch.setattr(runtime.sandbox_runtime, "resolve_launch", resolve_launch)

    result = runtime._resolve_inactive_launch(
        layout,
        sandbox="docker",
        dev=development,
        model_catalog=catalog,
        base_environ={"PROCESS": "value"},
    )

    assert result.sandbox == "docker"
    assert captured["logging"] == {
        "mode": "start",
        "environ": {
            "PROCESS": "value",
            "DOTENV": "agent",
            "TOOLANG_ROOT": str(layout.root),
            "TOOLANG_MODEL_CATALOG": str(catalog),
        },
        "agent_log_path": layout.runtime_log,
    }
    assert captured["launch"] == {
        "layout": layout,
        "sandbox": "docker",
        "dev": development,
        "output": "file",
        "log_path": layout.runtime_log,
        "log_spec": "error",
        "temporary_port": True,
        "environ": {"LOGGED": "yes"},
    }


def test_sandbox_match_accepts_driver_or_exact_spec() -> None:
    assert runtime.sandbox_matches("docker", "docker:python:3.13-slim")
    assert runtime.sandbox_matches(
        "docker:python:3.13-slim",
        "docker:python:3.13-slim",
    )
    assert not runtime.sandbox_matches("host", "docker:python:3.13-slim")
    assert not runtime.sandbox_matches("docker:other", "docker:python:3.13-slim")

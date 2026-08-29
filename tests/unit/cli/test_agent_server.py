"""AgentServer acquisition for CLI run execution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from toolang.cli.common import agent_server
from toolang.common.layout import AgentLayout
from toolang.up.logging import LoggingPlan
from toolang.up.process import AgentStatus
from toolang.up.records import SandboxState
from toolang.base.types.sandbox import SandboxRef


class _Progress:
    current_stage = "Starting workload"
    failure_reason: str | None = None
    failure_phase: str | None = None

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

    monkeypatch.setattr(agent_server.agents, "AgentProcess", Process)


def test_agent_server_attaches_to_a_compatible_running_agent(
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
        agent_server,
        "_resolve_inactive_launch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a running agent must not resolve another launch")
        ),
    )

    with agent_server.acquire_agent_server(
        layout,
        sandbox="docker",
        ui_base_url="https://ui.test",
    ) as selected:
        assert selected == agent_server.AgentServerRef(
            sandbox="docker:python:3.13-slim",
            endpoint="http://127.0.0.1:7001",
        )


def test_agent_server_rejects_dev_for_an_attached_agent(
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

    with pytest.raises(
        agent_server.AgentServerAcquisitionError,
        match="only applies when starting a new guest",
    ):
        with agent_server.acquire_agent_server(
            layout,
            sandbox="docker",
            dev=tmp_path / "dist",
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("an attached AgentServer must not accept --dev")


@pytest.mark.parametrize(
    ("requested", "message"),
    (
        ("host", "does not match running sandbox"),
        ("docker:other", "does not match running sandbox"),
    ),
)
def test_agent_server_rejects_a_running_sandbox_mismatch(
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

    with pytest.raises(agent_server.AgentServerAcquisitionError, match=message):
        with agent_server.acquire_agent_server(
            layout,
            sandbox=requested,
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("a mismatched AgentServer must not be acquired")


@pytest.mark.parametrize("status", ("preparing", "starting"))
def test_agent_server_rejects_an_unready_agent(
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

    with pytest.raises(agent_server.AgentServerAcquisitionError, match=status):
        with agent_server.acquire_agent_server(
            layout,
            sandbox=None,
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("an unready AgentServer must not be acquired")


@pytest.mark.parametrize("status", (None, "stopped", "failed"))
def test_agent_server_opens_embedded_host_and_releases_stopped_state(
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
        agent_server.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: "host",
    )
    released: list[AgentLayout] = []

    async def release_stopped(selected: AgentLayout) -> None:
        released.append(selected)

    monkeypatch.setattr(
        agent_server.sandbox_runtime, "release_stopped", release_stopped
    )

    with agent_server.acquire_agent_server(
        layout,
        sandbox=None,
        ui_base_url="https://ui.test",
    ) as selected:
        assert selected is None

    assert released == [layout]


def test_agent_server_rejects_dev_for_embedded_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        agent_server.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: "host",
    )

    with pytest.raises(
        agent_server.AgentServerAcquisitionError,
        match="only applies to guest sandboxes",
    ):
        with agent_server.acquire_agent_server(
            layout,
            sandbox="host",
            dev=tmp_path / "dist",
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("embedded host must not accept --dev")


def test_agent_server_launches_and_cleans_up_a_temporary_guest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        agent_server.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: explicit or "docker",
    )
    development = tmp_path / "dist"
    launch = SimpleNamespace(sandbox="docker", dev_artifact=development)

    def resolve_launch(*_args: object, **kwargs: object) -> object:
        assert kwargs["dev"] == development
        return launch

    monkeypatch.setattr(agent_server, "_resolve_inactive_launch", resolve_launch)
    progress = _Progress()
    shutdown_progress = _Progress()
    monkeypatch.setattr(
        agent_server,
        "make_runtime_startup_progress",
        lambda *_args, **_kwargs: progress,
    )
    monkeypatch.setattr(
        agent_server,
        "make_runtime_shutdown_progress",
        lambda *_args, **_kwargs: shutdown_progress,
    )
    implementation = cast(Any, SimpleNamespace())
    state = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("container-1", "http://127.0.0.1:8123"),
    )
    handle = agent_server.sandbox_runtime.SandboxHandle(implementation, state)
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

    monkeypatch.setattr(agent_server.sandbox_runtime, "launch", launch_runtime)
    monkeypatch.setattr(agent_server.sandbox_runtime, "stop_handle", stop_handle)

    with agent_server.acquire_agent_server(
        layout,
        sandbox="docker",
        dev=development,
        ui_base_url="https://ui.test",
    ) as selected:
        calls.append("body")
        assert selected == agent_server.AgentServerRef(
            sandbox="docker:python:3.13-slim",
            endpoint="http://127.0.0.1:8123",
        )

    assert calls == [
        ("launch", launch, progress),
        "body",
        ("stop", layout, handle, False, shutdown_progress),
    ]
    assert progress.finished == 1
    assert shutdown_progress.finished == 1


def test_agent_server_warns_when_a_development_cli_uses_the_package_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        agent_server.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: explicit or "docker",
    )
    launch = SimpleNamespace(sandbox="docker", dev_artifact=None)
    monkeypatch.setattr(
        agent_server,
        "_resolve_inactive_launch",
        lambda *_args, **_kwargs: launch,
    )
    monkeypatch.setattr(
        agent_server,
        "development_source",
        lambda: (True, tmp_path),
    )
    progress = _Progress()
    shutdown_progress = _Progress()
    monkeypatch.setattr(
        agent_server,
        "make_runtime_startup_progress",
        lambda *_args, **_kwargs: progress,
    )
    monkeypatch.setattr(
        agent_server,
        "make_runtime_shutdown_progress",
        lambda *_args, **_kwargs: shutdown_progress,
    )
    handle = agent_server.sandbox_runtime.SandboxHandle(
        cast(Any, SimpleNamespace()),
        SandboxState(
            sandbox="docker:python:3.13-slim",
            ref=SandboxRef("container-1", "http://127.0.0.1:8123"),
        ),
    )

    async def launch_runtime(_spec: object, *, progress: object) -> object:
        return handle

    async def stop_handle(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(agent_server.sandbox_runtime, "launch", launch_runtime)
    monkeypatch.setattr(agent_server.sandbox_runtime, "stop_handle", stop_handle)

    with agent_server.acquire_agent_server(
        layout,
        sandbox="docker",
        ui_base_url="https://ui.test",
    ):
        pass

    assert capsys.readouterr().err.splitlines() == [
        "Warning: the new docker guest will install Toolang from the package index, "
        f"not from {tmp_path}.",
        "Build the current source with `uv build --wheel`, then run this command "
        "again with `--dev dist`.",
    ]


@pytest.mark.parametrize(
    ("development", "reason", "fix"),
    (
        (
            None,
            "Toolang package installed in the guest cannot start",
            "again with `--dev dist`",
        ),
        (
            Path("dist/toolang-0.3.0-py3-none-any.whl"),
            "selected Toolang wheel cannot start",
            "again with `--dev PATH`",
        ),
    ),
)
def test_agent_server_startup_failure_uses_structured_package_guidance(
    development: Path | None,
    reason: str,
    fix: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        agent_server.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: explicit or "docker",
    )
    monkeypatch.setattr(
        agent_server,
        "_resolve_inactive_launch",
        lambda *_args, **_kwargs: SimpleNamespace(
            sandbox="docker",
            dev_artifact=development,
        ),
    )
    progress = _Progress()
    progress.failure_phase = "startup.validate"
    progress.failure_reason = "guest compatibility check failed"
    monkeypatch.setattr(
        agent_server,
        "make_runtime_startup_progress",
        lambda *_args, **_kwargs: progress,
    )
    monkeypatch.setattr(agent_server, "development_source", lambda: (True, tmp_path))

    async def fail_launch(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("startup failed")

    monkeypatch.setattr(agent_server.sandbox_runtime, "launch", fail_launch)

    with pytest.raises(agent_server.AgentServerAcquisitionError) as captured:
        with agent_server.acquire_agent_server(
            layout,
            sandbox="docker",
            dev=development,
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("a failed AgentServer must not be acquired")

    message = str(captured.value)
    assert f"Reason: The {reason}" in message
    assert "Fix: " in message
    assert fix in message


def test_agent_server_cleanup_does_not_hide_a_body_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        agent_server.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: explicit or "docker",
    )
    monkeypatch.setattr(
        agent_server,
        "_resolve_inactive_launch",
        lambda *_args, **_kwargs: SimpleNamespace(
            sandbox="docker",
            dev_artifact=None,
        ),
    )
    monkeypatch.setattr(
        agent_server,
        "make_runtime_startup_progress",
        lambda *_args, **_kwargs: _Progress(),
    )
    state = SandboxState(
        sandbox="docker:python:3.13-slim",
        ref=SandboxRef("container-1", "http://127.0.0.1:8123"),
    )
    handle = agent_server.sandbox_runtime.SandboxHandle(
        cast(Any, SimpleNamespace()), state
    )

    async def launch_runtime(*_args: object, **_kwargs: object) -> object:
        return handle

    async def fail_cleanup(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(agent_server.sandbox_runtime, "launch", launch_runtime)
    monkeypatch.setattr(agent_server.sandbox_runtime, "stop_handle", fail_cleanup)

    with pytest.raises(LookupError, match="body failed"):
        with agent_server.acquire_agent_server(
            layout,
            sandbox="docker",
            ui_base_url="https://ui.test",
        ):
            raise LookupError("body failed")

    assert "could not stop temporary agent alice" in capsys.readouterr().err


def test_agent_server_cleans_up_a_launched_guest_with_invalid_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        agent_server.sandbox_runtime,
        "resolve_selection",
        lambda _layout, *, explicit: explicit or "docker",
    )
    monkeypatch.setattr(
        agent_server,
        "_resolve_inactive_launch",
        lambda *_args, **_kwargs: SimpleNamespace(
            sandbox="docker",
            dev_artifact=None,
        ),
    )
    monkeypatch.setattr(
        agent_server,
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
    handle = agent_server.sandbox_runtime.SandboxHandle(
        cast(Any, SimpleNamespace()), state
    )
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

    monkeypatch.setattr(agent_server.sandbox_runtime, "launch", launch_runtime)
    monkeypatch.setattr(agent_server.sandbox_runtime, "stop_handle", stop_handle)

    with pytest.raises(
        agent_server.AgentServerAcquisitionError, match="requires an endpoint"
    ):
        with agent_server.acquire_agent_server(
            layout,
            sandbox="docker",
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("an invalid AgentServer must not be acquired")

    assert len(cleaned) == 1
    assert cleaned[0][:2] == (layout, handle)


def test_inactive_launch_wraps_environment_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    monkeypatch.setattr(
        agent_server,
        "load_runtime_environ",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("could not read dotenv")
        ),
    )

    with pytest.raises(
        agent_server.AgentServerAcquisitionError, match="could not read dotenv"
    ):
        agent_server._resolve_inactive_launch(
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

    monkeypatch.setattr(agent_server, "load_runtime_environ", load_environ)
    monkeypatch.setattr(agent_server, "resolve_agent_logging", logging_plan)
    monkeypatch.setattr(agent_server.sandbox_runtime, "resolve_launch", resolve_launch)

    result = agent_server._resolve_inactive_launch(
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
    assert agent_server.sandbox_matches("docker", "docker:python:3.13-slim")
    assert agent_server.sandbox_matches(
        "docker:python:3.13-slim",
        "docker:python:3.13-slim",
    )
    assert not agent_server.sandbox_matches("host", "docker:python:3.13-slim")
    assert not agent_server.sandbox_matches("docker:other", "docker:python:3.13-slim")

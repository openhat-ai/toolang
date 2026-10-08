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
from toolang.up.types import AgentServerRef
from toolang.base.types.sandbox import SandboxRef
from toolang.base.types.model import ModelOverride


class _Progress:
    current_stage = "Creating runtime..."
    failure_reason: str | None = None
    failure_stage: str | None = None
    failure_label: str | None = None

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.finished = 0
        self.interrupted = 0

    def __call__(self, _event: object) -> None:
        pass

    def __enter__(self) -> _Progress:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def sink(self) -> _Progress | None:
        return self if self.enabled else None

    def close(self) -> None:
        self.finished += 1

    def failure_message(
        self,
        error: BaseException,
        *,
        reason: str | None = None,
        fix: str | None = None,
        log_path: Path | None = None,
    ) -> str:
        lines = [
            self.failure_label or str(error),
            f"  Stage: {self.failure_stage or 'runtime.create'}",
            f"  Reason: {reason or self.failure_reason or str(error)}",
        ]
        if fix is not None:
            lines.append(f"  Fix: {fix}")
        if log_path is not None:
            lines.append(f"  Log: {log_path}")
        return "\n".join(lines)


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

        def status(
            self, *, ui_base_url: str, check_health: bool = False
        ) -> AgentStatus | None:
            assert ui_base_url == "https://ui.test"
            return status

    monkeypatch.setattr(agent_server.agents, "AgentProcess", Process)


@pytest.mark.parametrize(
    "override",
    [
        {"compact_override": ModelOverride(identity="test/compact")},
        {"model_catalog": Path("alternate-catalog.json")},
    ],
)
def test_startup_override_cannot_be_silently_ignored_by_an_existing_runtime(
    tmp_path, monkeypatch, override
):
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(
        monkeypatch,
        layout,
        _status(value="running", endpoint="http://localhost:7001", sandbox="host"),
    )
    with pytest.raises(
        agent_server.AgentServerAcquisitionError, match="only applies when starting"
    ):
        with agent_server.acquire_agent_server(
            layout,
            sandbox=None,
            ui_base_url="https://ui.test",
            **override,
        ):
            pytest.fail("existing runtime must not ignore startup overrides")


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
        assert selected == AgentServerRef(
            sandbox="docker:python:3.13-slim",
            endpoint="http://127.0.0.1:7001",
        )


@pytest.mark.parametrize("dev", [Path("."), Path("dist")])
def test_agent_server_rejects_dev_for_an_attached_agent(
    dev: Path,
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
            dev=dev,
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
            temporary=True,
            sandbox=None,
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("an unready AgentServer must not be acquired")


@pytest.mark.parametrize("status", (None, "stopped", "failed"))
@pytest.mark.parametrize("body_fails", (False, True))
def test_agent_server_opens_embedded_host_and_releases_stopped_state(
    status: str | None,
    body_fails: bool,
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

    error = ValueError("script evaluation failed")

    def run_script() -> None:
        with agent_server.acquire_agent_server(
            layout,
            temporary=True,
            sandbox=None,
            ui_base_url="https://ui.test",
        ) as selected:
            assert selected is None
            if body_fails:
                raise error

    if body_fails:
        with pytest.raises(ValueError) as captured:
            run_script()
        assert captured.value is error
    else:
        run_script()

    assert released == [layout]


def test_agent_server_wraps_management_lock_failure(tmp_path: Path) -> None:
    layout = AgentLayout.resident(tmp_path, "alice")
    lock_path = layout.sandbox_state.with_suffix(".lock")
    lock_path.mkdir(parents=True)

    with pytest.raises(agent_server.AgentServerAcquisitionError) as captured:
        with agent_server.acquire_agent_server(layout, sandbox="host"):
            pytest.fail("must not acquire without the management lock")

    assert isinstance(captured.value.__cause__, IsADirectoryError)
    assert str(lock_path) in str(captured.value)


@pytest.mark.parametrize("dev", [Path("."), Path("dist")])
def test_agent_server_rejects_dev_for_embedded_host(
    dev: Path,
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
            dev=dev,
            ui_base_url="https://ui.test",
        ):
            raise AssertionError("embedded host must not accept --dev")


@pytest.mark.parametrize("temporary", (False, True))
def test_agent_server_launches_with_the_requested_lifecycle(
    temporary: bool,
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
    progress = _Progress(enabled=False)
    shutdown_progress = _Progress(enabled=False)
    presenters = iter((progress, shutdown_progress))
    monkeypatch.setattr(
        agent_server,
        "make_cli_progress",
        lambda **_kwargs: next(presenters),
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
        temporary=temporary,
        sandbox="docker",
        dev=development,
        ui_base_url="https://ui.test",
        show_progress=False,
    ) as selected:
        calls.append("body")
        assert selected == AgentServerRef(
            sandbox="docker:python:3.13-slim",
            endpoint="http://127.0.0.1:8123",
        )

    assert calls == [
        ("launch", launch, None),
        "body",
        *([("stop", layout, handle, False, None)] if temporary else []),
    ]
    assert progress.finished == 1
    assert shutdown_progress.finished == int(temporary)


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
    presenters = iter((progress, shutdown_progress))
    monkeypatch.setattr(
        agent_server,
        "make_cli_progress",
        lambda **_kwargs: next(presenters),
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
    progress.failure_stage = "runtime.create"
    progress.failure_label = "Checking Toolang compatibility"
    progress.failure_reason = "guest compatibility check failed"
    monkeypatch.setattr(
        agent_server,
        "make_cli_progress",
        lambda **_kwargs: progress,
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
    startup_progress = _Progress()
    cleanup_progress = _Progress()
    cleanup_progress.failure_label = "Failed to remove runtime"
    cleanup_progress.failure_stage = "runtime.destroy"
    cleanup_progress.failure_reason = "cleanup failed"
    presenters = iter((startup_progress, cleanup_progress))
    monkeypatch.setattr(
        agent_server,
        "make_cli_progress",
        lambda **_kwargs: next(presenters),
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
            temporary=True,
            sandbox="docker",
            ui_base_url="https://ui.test",
        ):
            raise LookupError("body failed")

    error = capsys.readouterr().err
    assert "Failed to remove runtime" in error
    assert "Stage: runtime.destroy" in error
    assert "Reason: cleanup failed" in error


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
        "make_cli_progress",
        lambda **_kwargs: _Progress(),
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
        return {
            "PROCESS": "value",
            "DOTENV": "agent",
            "TOOLANG_ALLOW_MODELS": "test/*",
            "TOOLANG_DEFAULT_MODEL": "test/chat effort=high",
            "TOOLANG_LIMIT_TOKENS": "12000",
            "TOOLANG_COMPACT_MODEL": "test/compact effort=low",
        }

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
            "TOOLANG_ALLOW_MODELS": "test/*",
            "TOOLANG_DEFAULT_MODEL": "test/chat effort=high",
            "TOOLANG_LIMIT_TOKENS": "12000",
            "TOOLANG_COMPACT_MODEL": "test/compact effort=low",
            "TOOLANG_ROOT": str(layout.root),
            "TOOLANG_MODEL_CATALOG": str(catalog),
        },
        "agent_log_path": layout.runtime_log,
    }
    assert captured["launch"] == {
        "workspace_additions": None,
        "layout": layout,
        "sandbox": "docker",
        "port": None,
        "dev": development,
        "output": "file",
        "log_path": layout.runtime_log,
        "log_spec": "error",
        "temporary_port": False,
        "environ": {"LOGGED": "yes"},
        "ceiling_overrides": {"models": ("test/*",)},
        "default_overrides": {
            "model": ModelOverride(identity="test/chat", effort="high")
        },
        "limit_overrides": {"tokens": 12000},
        "compact_override": ModelOverride(identity="test/compact", effort="low"),
    }


def test_sandbox_match_accepts_driver_or_exact_spec() -> None:
    assert agent_server.sandbox_matches("docker", "docker:python:3.13-slim")
    assert agent_server.sandbox_matches(
        "docker:python:3.13-slim",
        "docker:python:3.13-slim",
    )
    assert not agent_server.sandbox_matches("host", "docker:python:3.13-slim")
    assert not agent_server.sandbox_matches("docker:other", "docker:python:3.13-slim")


@pytest.mark.parametrize(
    "requested,compatible",
    [(None, True), ({}, False), ({"repo": "/one"}, True), ({"repo": "/two"}, False)],
)
def test_running_server_cannot_rebind_workspace_names(
    tmp_path, monkeypatch, requested, compatible
):
    layout = AgentLayout.resident(tmp_path, "alice")

    class Process:
        def __init__(self, selected):
            assert selected == layout

        def status(self, **_kwargs):
            return _status(
                value="running", endpoint="http://localhost:7001", sandbox="host"
            )

        def state(self):
            return {"workspace_additions": {"repo": "/one"}}

    monkeypatch.setattr(agent_server.agents, "AgentProcess", Process)
    if compatible:
        with agent_server.acquire_agent_server(
            layout, sandbox=None, workspace_additions=requested
        ) as server:
            assert server is not None
    else:
        with pytest.raises(
            agent_server.AgentServerAcquisitionError, match="bindings differ"
        ):
            with agent_server.acquire_agent_server(
                layout, sandbox=None, workspace_additions=requested
            ):
                pytest.fail("must not rebind running workspace roots")


@pytest.mark.parametrize("outcome", ("running", "failed", "timeout"))
def test_persistent_acquisition_waits_for_an_existing_startup(
    tmp_path, monkeypatch, outcome
):
    layout = AgentLayout.resident(tmp_path, "alice")
    statuses = iter(("starting", outcome))

    class Process:
        def __init__(self, _layout):
            pass

        def status(self, **_kwargs):
            return _status(
                value=next(statuses), endpoint="http://localhost:7001", sandbox="host"
            )

    monkeypatch.setattr(agent_server.agents, "AgentProcess", Process)
    monkeypatch.setattr(agent_server.time, "sleep", lambda _duration: None)
    monkeypatch.setattr(
        agent_server, "AGENT_READY_TIMEOUT_SEC", 0 if outcome == "timeout" else 1
    )
    monkeypatch.setattr(
        agent_server,
        "_resolve_inactive_launch",
        lambda *a, **k: pytest.fail("must not launch a second runtime"),
    )
    if outcome == "running":
        with agent_server.acquire_agent_server(layout, sandbox="host") as server:
            assert server == AgentServerRef(
                sandbox="host", endpoint="http://localhost:7001"
            )
    else:
        with pytest.raises(agent_server.AgentServerAcquisitionError, match="ready"):
            with agent_server.acquire_agent_server(layout, sandbox="host"):
                pytest.fail("unready runtime acquired")


@pytest.mark.parametrize("interrupted", (False, True))
def test_persistent_host_survives_command_completion_and_failure(
    tmp_path, monkeypatch, interrupted
):
    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    monkeypatch.setattr(
        agent_server.sandbox_runtime, "resolve_selection", lambda *a, **k: "host"
    )
    launch = SimpleNamespace(sandbox="host", dev_artifact=None)
    monkeypatch.setattr(
        agent_server, "_resolve_inactive_launch", lambda *a, **k: launch
    )
    handle = agent_server.sandbox_runtime.SandboxHandle(
        cast(Any, None),
        SandboxState(
            sandbox="host", ref=SandboxRef("process-1", "http://localhost:7001")
        ),
    )

    async def start(*_args, **_kwargs):
        return handle

    async def stop(*_args, **_kwargs):
        pytest.fail("persistent acquisition must not stop the runtime")

    monkeypatch.setattr(agent_server.sandbox_runtime, "launch", start)
    monkeypatch.setattr(agent_server.sandbox_runtime, "stop_handle", stop)

    def acquire():
        with agent_server.acquire_agent_server(
            layout, sandbox="host", ui_base_url="https://ui.test", show_progress=False
        ) as server:
            assert server == AgentServerRef(
                sandbox="host", endpoint="http://localhost:7001"
            )
            if interrupted:
                raise LookupError("caller failed")

    if interrupted:
        with pytest.raises(LookupError, match="caller failed"):
            acquire()
    else:
        acquire()


@pytest.mark.parametrize("ready", (True, False))
def test_persistent_acquisition_waits_for_http_after_running_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready: bool
) -> None:
    import os

    from toolang.plugin.sandboxes.host import process_ref

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    ref = process_ref(os.getpid(), "http://localhost:8123")
    SandboxState("host", ref).save(layout.sandbox_state)
    agent_server.agents.write_runtime_state(
        layout,
        endpoint=ref.endpoint,
        started_at="2026-10-08T00:00:00Z",
        pid=os.getpid(),
        process_created=ref.meta["created"],
    )
    probes: list[str] = []

    def health(url: str) -> bool:
        probes.append(url)
        return ready and len(probes) > 1

    monkeypatch.setattr(agent_server.sandbox_runtime, "_health_ready", health)
    monkeypatch.setattr(agent_server.time, "sleep", lambda _duration: None)
    monkeypatch.setattr(agent_server, "AGENT_READY_TIMEOUT_SEC", 1 if ready else 0)
    monkeypatch.setattr(
        agent_server,
        "_resolve_inactive_launch",
        lambda *_a, **_kw: pytest.fail("must not start a competing runtime"),
    )
    # A direct serve process publishes this report before HTTP starts listening.
    status = agent_server.agents.AgentProcess(layout).status(ui_base_url="")
    assert status is not None and status.status == "running"
    assert not probes

    if ready:
        with agent_server.acquire_agent_server(layout, sandbox="host") as server:
            assert server == AgentServerRef(sandbox="host", endpoint=ref.endpoint)
        assert probes == [f"{ref.endpoint}/healthz"] * 2
    else:
        with pytest.raises(agent_server.AgentServerAcquisitionError, match="ready"):
            with agent_server.acquire_agent_server(layout, sandbox="host"):
                pytest.fail("unready runtime acquired")
        assert probes == [f"{ref.endpoint}/healthz"]
    assert SandboxState.load(layout.sandbox_state) == SandboxState("host", ref)

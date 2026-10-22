"""AgentServer acquisition for CLI run execution."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import httpx

from toolang.cli.common import agent_server, startup
from toolang.common.layout import AgentLayout
from toolang.up.logging import LoggingPlan
from toolang.up.process import AgentStatus
from toolang.up.instance import SandboxState
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

    @contextmanager
    def suspended(self):
        yield

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
    if layout.placement == "visiting":
        from toolang.up.identity import visiting_home_guard

        with visiting_home_guard(layout):
            pass
    if status is not None and status.status == "running":
        from toolang.up.instance import update_instance
        from toolang.up.contract import startup_contract
        from toolang.up.records import InstanceIdentity

        update_instance(
            layout.runtime_status,
            lambda entry: entry["control"].update(
                **InstanceIdentity.from_layout(layout).to_data(),
                last_successful={
                    "sandbox": status.sandbox,
                    "endpoint": status.endpoint,
                },
            ),
        )
        startup = agent_server._resolve_launch(
            layout,
            sandbox=status.sandbox,
            dev=None,
            model_catalog=None,
            base_environ=None,
            drivers=frozenset({"chat"}),
            active_port=status.port,
        )
        update_instance(
            layout.runtime_status,
            lambda entry: entry["control"].update(
                startup_contract=startup_contract(startup), drivers=["chat"]
            ),
        )

    class Process:
        def __init__(self, selected: AgentLayout) -> None:
            assert selected == layout

        def status(
            self, *, ui_base_url: str, check_health: bool = False
        ) -> AgentStatus | None:
            assert ui_base_url == "https://ui.test"
            return status

    monkeypatch.setattr(agent_server.agents, "AgentProcess", Process)


def test_visiting_reuse_checks_explicit_settings_without_fetch(tmp_path, monkeypatch):
    layout = AgentLayout.visiting(
        "https://example.test/strict.too", "strict", root=tmp_path
    )
    _set_status(
        monkeypatch,
        layout,
        _status(value="running", endpoint="http://localhost:8123", sandbox="host"),
    )
    prepared = []
    monkeypatch.setattr(
        "toolang.up.source.resolve_visiting_layout",
        lambda *_args, **_kwargs: prepared.append(layout) or layout,
    )
    with pytest.raises(
        agent_server.AgentServerAcquisitionError, match="startup parameters differ"
    ):
        with agent_server.acquire_agent_server(
            layout,
            sandbox=None,
            ui_base_url="https://ui.test",
            compact_override=ModelOverride(identity="test/different"),
            show_progress=False,
        ):
            pytest.fail("mismatched attachment was accepted")
    assert not prepared
    with agent_server.acquire_agent_server(
        layout, sandbox=None, ui_base_url="https://ui.test", show_progress=False
    ) as ref:
        assert ref is not None
    assert prepared == []


@pytest.mark.parametrize("placement", ["resident", "visiting"])
def test_running_server_reuses_bound_settings_without_resolving_startup(
    tmp_path, monkeypatch, placement
):
    from toolang.up.instance import read_instance

    layout = (
        AgentLayout.resident(tmp_path, "alice")
        if placement == "resident"
        else AgentLayout.visiting(
            "https://example.test/alice.too", "alice", root=tmp_path
        )
    )
    _set_status(
        monkeypatch,
        layout,
        _status(value="running", endpoint="http://localhost:8123", sandbox="host"),
    )
    original = read_instance(layout.runtime_status)
    monkeypatch.setenv("PWD", "/different/directory")
    monkeypatch.setenv("TERM", "different-terminal")
    monkeypatch.setenv("TOOLANG_LIMIT_TOKENS", "invalid-startup-value")
    layout.place_config.write_text('[sandbox]\ndriver = "docker"\n')
    monkeypatch.setattr(
        agent_server,
        "_resolve_launch",
        lambda *_a, **_k: pytest.fail("attachment must not resolve a new launch"),
    )
    monkeypatch.setattr(
        agent_server,
        "_prepare_source",
        lambda *_a, **_k: pytest.fail("attachment must not prepare source"),
    )

    with agent_server.acquire_agent_server(
        layout, sandbox=None, ui_base_url="https://ui.test", required_driver="chat"
    ) as server:
        assert server is not None and server.endpoint == "http://localhost:8123"

    assert read_instance(layout.runtime_status) == original


@pytest.mark.parametrize("attach_only", [False, True])
def test_copied_runtime_status_cannot_attach_to_another_root(
    tmp_path, monkeypatch, attach_only
):
    from toolang.up.records import InstanceIdentity
    from toolang.up.instance import read_instance, update_instance

    original = AgentLayout.resident(tmp_path / "original", "alice")
    status = _status(value="running", endpoint="http://localhost:8123", sandbox="host")
    _set_status(monkeypatch, original, status)
    update_instance(
        original.runtime_status,
        lambda entry: entry["control"].update(
            InstanceIdentity.from_layout(original).to_data()
        ),
    )
    copied = AgentLayout.resident(tmp_path / "copied", "alice")
    copied.runtime_status.parent.mkdir(parents=True)
    copied.runtime_status.write_bytes(original.runtime_status.read_bytes())
    saved = read_instance(original.runtime_status)
    monkeypatch.setattr(
        agent_server.agents,
        "AgentProcess",
        lambda *_a: SimpleNamespace(status=lambda **_k: status),
    )

    with pytest.raises(
        agent_server.AgentServerAcquisitionError, match="identity|ownership"
    ):
        with agent_server.acquire_agent_server(
            copied, sandbox=None, attach_only=attach_only
        ):
            pytest.fail(
                "copied status must not redirect execution to the original root"
            )
    assert read_instance(original.runtime_status) == saved
    assert read_instance(copied.runtime_status) == saved


@pytest.mark.parametrize("placement", ["resident", "visiting", "roaming"])
@pytest.mark.parametrize("attach_only", [False, True])
def test_only_stable_placements_reuse_an_executor(
    tmp_path, monkeypatch, placement, attach_only
):
    from toolang.up.identity import allocate_roaming

    if placement == "resident":
        layout = AgentLayout.resident(tmp_path, "alice")
    elif placement == "visiting":
        layout = AgentLayout.visiting(
            "https://example.test/alice.too", "alice", root=tmp_path
        )
    else:
        source = tmp_path / "alice.too"
        source.write_text("flow main():\n  pass\n")
        layout = allocate_roaming(source, toolang_root=tmp_path)
    _set_status(
        monkeypatch,
        layout,
        _status(value="running", endpoint="http://localhost:8123", sandbox="host"),
    )
    monkeypatch.setattr(
        "toolang.up.source.resolve_visiting_layout", lambda *_args, **_kwargs: layout
    )
    if placement == "roaming" and not attach_only:
        with pytest.raises(agent_server.AgentServerAcquisitionError, match="fresh"):
            with agent_server.acquire_agent_server(
                layout, sandbox=None, ui_base_url="https://ui.test", show_progress=False
            ):
                pytest.fail("a roaming executor was reused")
    else:
        with agent_server.acquire_agent_server(
            layout,
            sandbox=None,
            ui_base_url="https://ui.test",
            show_progress=False,
            attach_only=attach_only,
        ) as server:
            assert server is not None and server.endpoint == "http://localhost:8123"


@pytest.mark.parametrize("phase", ["stopped", "failed", "preparing"])
def test_roaming_requires_its_callers_unstarted_identity(tmp_path, monkeypatch, phase):
    from toolang.up.identity import allocate_roaming
    from toolang.up.instance import update_instance

    source = tmp_path / "alice.too"
    source.write_text("flow main():\n  pass\n")
    layout = allocate_roaming(source, toolang_root=tmp_path)
    _set_status(
        monkeypatch,
        layout,
        _status(value="running", endpoint="http://localhost:8123", sandbox="host"),
    )

    def retained(entry):
        entry["workload"].update(status=phase, pid=0 if phase == "preparing" else None)
        if phase != "stopped":
            entry["control"].pop("startup_contract", None)

    update_instance(layout.runtime_status, retained)
    monkeypatch.setattr(
        agent_server.agents,
        "AgentProcess",
        lambda *_args: SimpleNamespace(
            status=lambda **_kwargs: _status(value=phase, sandbox="host")
        ),
    )

    async def launch(*_args, **_kwargs):
        pytest.fail("a retained roaming place was restarted")

    monkeypatch.setattr(agent_server.sandbox_runtime, "launch", launch)
    with pytest.raises(agent_server.AgentServerAcquisitionError, match="fresh"):
        with agent_server.acquire_agent_server(
            layout, sandbox=None, ui_base_url="https://ui.test", show_progress=False
        ):
            pytest.fail("a retained roaming place was acquired")


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
        agent_server.AgentServerAcquisitionError, match="startup parameters differ"
    ):
        with agent_server.acquire_agent_server(
            layout,
            sandbox=None,
            ui_base_url="https://ui.test",
            **override,
        ):
            pytest.fail("existing runtime must not ignore startup overrides")


@pytest.mark.parametrize("selection", ["omitted", "same", "changed"])
def test_running_server_retains_or_verifies_explicit_startup_settings(
    tmp_path, monkeypatch, selection
):
    from toolang.up.contract import startup_contract
    from toolang.up.instance import update_instance

    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(
        monkeypatch,
        layout,
        _status(
            value="running",
            endpoint="http://localhost:8123",
            sandbox="docker:python:3.13-slim",
        ),
    )
    wheel = tmp_path / "toolang-0.0.0-py3-none-any.whl"
    wheel.write_bytes(b"original wheel")
    catalog = tmp_path / "models.json"
    compact = ModelOverride(identity="test/compact")
    spec = agent_server._resolve_launch(
        layout,
        sandbox="docker:python:3.13-slim",
        dev=wheel,
        model_catalog=catalog,
        base_environ={},
        compact_override=compact,
        drivers=frozenset({"chat"}),
        active_port=8123,
    )
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(startup_contract=startup_contract(spec)),
    )
    monkeypatch.setattr(
        agent_server,
        "_resolve_launch",
        lambda *_a, **_k: pytest.fail("attachment must not resolve a launch"),
    )
    options: dict[str, Any] = (
        {}
        if selection == "omitted"
        else {"dev": wheel, "model_catalog": catalog, "compact_override": compact}
    )
    if selection == "changed":
        wheel.write_bytes(b"changed wheel")
        with pytest.raises(
            agent_server.AgentServerAcquisitionError,
            match="development wheel contents.*restart",
        ):
            with agent_server.acquire_agent_server(
                layout, sandbox="docker", ui_base_url="https://ui.test", **options
            ):
                pytest.fail("changed explicit wheel must not be ignored")
    else:
        with agent_server.acquire_agent_server(
            layout, sandbox="docker", ui_base_url="https://ui.test", **options
        ) as server:
            assert server is not None


@pytest.mark.parametrize("requested", [None, "docker", "docker:python:3.13-slim"])
def test_agent_server_attaches_to_a_compatible_running_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: str | None,
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

    with agent_server.acquire_agent_server(
        layout,
        sandbox=requested,
        ui_base_url="https://ui.test",
    ) as selected:
        assert selected == AgentServerRef(
            sandbox="docker:python:3.13-slim",
            endpoint="http://127.0.0.1:7001",
        )


@pytest.mark.parametrize("failure", ["profile", "lease", "closing"])
@pytest.mark.parametrize("attach_only", [False, True])
def test_automatic_server_exiting_during_acquisition(
    tmp_path, monkeypatch, failure, attach_only
):
    layout = AgentLayout.resident(tmp_path, "alice")
    old = AgentServerRef("host", "http://old", launch_id="old", lifetime="automatic")
    replacement = AgentServerRef(
        "host", "http://new", launch_id="new", lifetime="automatic"
    )
    prepared = []
    probes = []
    reference = SimpleNamespace(launch_id="old", ref=object())

    class Process:
        def __init__(self, _layout):
            pass

        @property
        def reference(self):
            return reference

    async def running(_ref):
        probes.append(True)
        return len(probes) < 3

    def prepare(*_args, **_kwargs):
        if prepared:
            assert len(probes) == 3
        prepared.append(True)
        return old if len(prepared) == 1 else replacement

    def respond(request):
        if (
            request.url.host == "old"
            and request.method == "PUT"
            and failure == "closing"
        ):
            return httpx.Response(409, json={"detail": "server is closing"})
        if request.url.host == "old" and (
            request.method == "GET" if failure == "profile" else request.method == "PUT"
        ):
            raise httpx.ConnectError(
                "server exited after health check", request=request
            )
        return httpx.Response(
            200,
            json={"runtime": {"launch_id": request.url.host, "lifetime": "automatic"}}
            if request.method == "GET"
            else {
                "launch_id": request.url.host,
                "lifetime": "automatic",
                "api_version": 0,
            },
        )

    client = httpx.Client
    monkeypatch.setattr(agent_server.agents, "AgentProcess", Process)
    monkeypatch.setattr(
        agent_server.sandbox_runtime,
        "load_state_sandbox",
        lambda *_args: SimpleNamespace(running=running),
    )
    monkeypatch.setattr(agent_server, "_prepare_agent_server", prepare)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )

    def acquire():
        with agent_server.acquire_agent_server(
            layout, sandbox=None, show_progress=False, attach_only=attach_only
        ) as selected:
            assert selected is not None and selected.launch_id == "new"

    if attach_only:
        with pytest.raises(agent_server.ServerClosingError):
            acquire()
        assert len(prepared) == 1
        assert len(probes) == 3
    else:
        acquire()
        assert len(prepared) == 2


def test_acquisition_does_not_replace_an_automatic_server_that_is_still_alive(
    tmp_path, monkeypatch
):
    layout = AgentLayout.resident(tmp_path, "alice")
    server = AgentServerRef(
        "host", "http://server", launch_id="launch", lifetime="automatic"
    )
    prepared = []

    def prepare(*_args, **_kwargs):
        prepared.append(True)
        return server

    def retain(_server):
        raise agent_server.ServerClosingError("connection closed during acquisition")

    async def running(_ref):
        return True

    monkeypatch.setattr(agent_server, "AGENT_READY_TIMEOUT_SEC", 0)
    monkeypatch.setattr(agent_server, "_prepare_agent_server", prepare)
    monkeypatch.setattr(agent_server, "_retain_server", retain)
    monkeypatch.setattr(
        agent_server.agents,
        "AgentProcess",
        lambda _layout: SimpleNamespace(
            reference=SimpleNamespace(launch_id="launch", ref=object())
        ),
    )
    monkeypatch.setattr(
        agent_server.sandbox_runtime,
        "load_state_sandbox",
        lambda *_args: SimpleNamespace(running=running),
    )
    with pytest.raises(agent_server.AgentServerAcquisitionError) as error:
        with agent_server.acquire_agent_server(
            layout, sandbox=None, show_progress=False
        ):
            pytest.fail("an unretained server cannot be acquired")
    assert type(error.value) is agent_server.AgentServerAcquisitionError
    assert "shutdown did not complete" in str(error.value)
    assert len(prepared) == 1


def test_managed_attachment_checks_its_profile_instead_of_waiting_for_health(
    tmp_path, monkeypatch
):
    from toolang.up.records import InstanceIdentity

    layout = AgentLayout.resident(tmp_path, "alice")

    class Process:
        def __init__(self, _layout):
            pass

        def status(self, *, ui_base_url, check_health):
            assert not check_health
            return _status(value="running", endpoint="http://server", sandbox="host")

    monkeypatch.setattr(agent_server.agents, "AgentProcess", Process)
    monkeypatch.setattr(
        agent_server,
        "read_instance",
        lambda *_args: {
            "launch_id": "launch",
            "control": InstanceIdentity.from_layout(layout).to_data(),
            "workload": {"lifetime": "automatic", "api_version": 0},
        },
    )
    retained = []

    def retain(server):
        retained.append(server.launch_id)
        return server, None

    monkeypatch.setattr(agent_server, "_retain_server", retain)
    with agent_server.acquire_agent_server(
        layout, sandbox=None, show_progress=False, attach_only=True
    ):
        assert retained == ["launch"]


@pytest.mark.parametrize("version", [1, False, 0.0, None])
def test_attachment_rejects_an_incompatible_api_without_dropping_ownership(
    tmp_path, monkeypatch, version
):
    layout = AgentLayout.resident(tmp_path, "alice")
    monkeypatch.setattr(
        agent_server,
        "read_instance",
        lambda *_args: {
            "launch_id": "launch",
            "workload": {"lifetime": "automatic", "api_version": version},
        },
    )
    with pytest.raises(agent_server.AgentServerAcquisitionError, match="API version"):
        agent_server._server_reference(layout, "host", "http://server")


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
        match="(startup parameters differ|development|wheel|dist)",
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
    lock_path = layout.runtime_status.with_name("status.json.lock")
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

    monkeypatch.setattr(agent_server, "_resolve_launch", resolve_launch)
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
        launch_id="fixture",
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
        "_resolve_launch",
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
            launch_id="fixture",
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
        "_resolve_launch",
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
        "_resolve_launch",
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
        launch_id="fixture",
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
        "_resolve_launch",
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
        agent_server._resolve_launch(
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
    monkeypatch.setattr(startup, "resolve_agent_logging", logging_plan)
    monkeypatch.setattr(agent_server.sandbox_runtime, "resolve_launch", resolve_launch)

    result = agent_server._resolve_launch(
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
            "TOOLANG_ROOT": str(layout.place),
            "TOOLANG_MODEL_CATALOG": str(catalog),
        },
        "agent_log_path": layout.runtime_log,
    }
    assert captured["launch"] == {
        "lifetime": "persistent",
        "toolang_root": layout.place,
        "drivers": None,
        "workspace_additions": None,
        "layout": layout,
        "sandbox": "docker",
        "host": "127.0.0.1",
        "endpoint_host": None,
        "workdir": None,
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
    [
        (None, True),
        ({}, True),
        ({"repo": "/one"}, True),
        ({"repo": "/two"}, False),
        ({"new": "/three"}, False),
    ],
)
def test_running_server_cannot_rebind_workspace_names(
    tmp_path, monkeypatch, requested, compatible
):
    layout = AgentLayout.resident(tmp_path, "alice")

    from toolang.up.contract import startup_contract
    from toolang.up.records import InstanceIdentity
    from toolang.up.instance import update_instance

    spec = agent_server._resolve_launch(
        layout,
        sandbox="host",
        dev=None,
        model_catalog=None,
        base_environ=None,
        workspace_additions={"repo": "/one", "other": "/three"},
        active_port=7001,
        drivers=frozenset({"chat"}),
    )
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(
            **InstanceIdentity.from_layout(layout).to_data(),
            startup_contract=startup_contract(spec),
            drivers=["chat"],
        ),
    )

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
            agent_server.AgentServerAcquisitionError,
            match="startup parameters differ: workspace bindings",
        ):
            with agent_server.acquire_agent_server(
                layout, sandbox=None, workspace_additions=requested
            ):
                pytest.fail("must not rebind running workspace roots")


@pytest.mark.parametrize("outcome", ("running", "failed", "timeout"))
def test_persistent_acquisition_waits_for_an_existing_startup(
    tmp_path, monkeypatch, capsys, outcome
):
    from toolang.up.records import InstanceIdentity
    from toolang.up.instance import update_instance

    layout = AgentLayout.resident(tmp_path, "alice")
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(
            InstanceIdentity.from_layout(layout).to_data()
        ),
    )
    statuses = iter(("starting", outcome))
    observed_wait = []

    class Process(agent_server.agents.AgentProcess):
        def status(self, **_kwargs):
            value = next(statuses)
            if value != "starting":
                observed_wait.append(
                    "Waiting for agent API..." in capsys.readouterr().err
                )
            return _status(
                value=value, endpoint="http://localhost:7001", sandbox="host"
            )

    monkeypatch.setattr(agent_server.agents, "AgentProcess", Process)
    monkeypatch.setattr(agent_server.agents.time, "sleep", lambda _duration: None)
    monkeypatch.setattr(
        agent_server, "AGENT_READY_TIMEOUT_SEC", 0 if outcome == "timeout" else 1
    )
    monkeypatch.setattr(
        agent_server,
        "_resolve_launch",
        lambda *a, **k: pytest.fail("must not launch a second runtime"),
    )
    if outcome == "running":
        with agent_server.acquire_agent_server(
            layout, sandbox="host", attach_only=True
        ) as server:
            assert server == AgentServerRef(
                sandbox="host", endpoint="http://localhost:7001"
            )
    else:
        with pytest.raises(agent_server.AgentServerAcquisitionError, match="ready"):
            with agent_server.acquire_agent_server(
                layout, sandbox="host", attach_only=True
            ):
                pytest.fail("unready runtime acquired")

    if outcome == "timeout":
        assert "Waiting for agent API..." in capsys.readouterr().err
    else:
        assert observed_wait == [True]


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
    monkeypatch.setattr(agent_server, "_resolve_launch", lambda *a, **k: launch)
    handle = agent_server.sandbox_runtime.SandboxHandle(
        cast(Any, None),
        SandboxState(
            sandbox="host",
            ref=SandboxRef("process-1", "http://localhost:7001"),
            launch_id="fixture",
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
    from toolang.up.records import InstanceIdentity
    from toolang.up.instance import update_instance

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    ref = process_ref(os.getpid(), "http://localhost:8123")
    SandboxState("host", ref, launch_id="fixture").save(layout.runtime_status)
    update_instance(
        layout.runtime_status,
        lambda entry: entry["control"].update(
            InstanceIdentity.from_layout(layout).to_data()
        ),
    )
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
    monkeypatch.setattr(agent_server.agents.time, "sleep", lambda _duration: None)
    monkeypatch.setattr(agent_server, "AGENT_READY_TIMEOUT_SEC", 1 if ready else 0)
    monkeypatch.setattr(
        agent_server,
        "_resolve_launch",
        lambda *_a, **_kw: pytest.fail("must not start a competing runtime"),
    )
    # A direct serve process publishes this report before HTTP starts listening.
    status = agent_server.agents.AgentProcess(layout).status(ui_base_url="")
    assert status is not None and status.status == "running"
    assert not probes

    if ready:
        with agent_server.acquire_agent_server(
            layout, sandbox="host", attach_only=True
        ) as server:
            assert server == AgentServerRef(sandbox="host", endpoint=ref.endpoint)
        assert probes == [f"{ref.endpoint}/healthz"] * 2
    else:
        with pytest.raises(agent_server.AgentServerAcquisitionError, match="ready"):
            with agent_server.acquire_agent_server(
                layout, sandbox="host", attach_only=True
            ):
                pytest.fail("unready runtime acquired")
        assert probes == [f"{ref.endpoint}/healthz"]
    assert SandboxState.load(layout.runtime_status) == SandboxState(
        "host", ref, launch_id="fixture"
    )


def test_startup_warning_suspends_the_lock_wait_presenter(tmp_path, monkeypatch):
    from io import StringIO

    from toolang.base.types.progress import ProgressEvent
    from toolang.cli.common.progress import CliProgress

    class TTYBuffer(StringIO):
        def isatty(self):
            return True

        def fileno(self):
            raise OSError()

    layout = AgentLayout.resident(tmp_path, "alice")
    _set_status(monkeypatch, layout, _status(value="stopped"))
    stream = TTYBuffer()
    presenter = CliProgress(stream=stream, _reveal_seconds=60)
    monkeypatch.setattr(agent_server, "make_cli_progress", lambda **_kwargs: presenter)

    @contextmanager
    def waited_lock(_layout, *, progress):
        progress(
            ProgressEvent(
                "lock", "runtime", "start", "Waiting for agent management...", "running"
            )
        )
        presenter._cancel_reveal()
        presenter._reveal_live()
        progress(
            ProgressEvent(
                "lock", "runtime", "start", "Agent management available", "ok"
            )
        )
        assert presenter._live_display is not None
        yield

    monkeypatch.setattr(agent_server.sandbox_runtime, "management_lock", waited_lock)
    monkeypatch.setattr(
        agent_server.sandbox_runtime, "resolve_selection", lambda *a, **k: "docker"
    )
    launch = SimpleNamespace(sandbox="docker", dev_artifact=None)
    monkeypatch.setattr(agent_server, "_resolve_launch", lambda *a, **k: launch)
    observed = []

    def warn(_launch):
        observed.append(presenter._live_display is None)
        print("Development package warning", file=stream)

    monkeypatch.setattr(agent_server, "warn_development_package_source", warn)
    handle = agent_server.sandbox_runtime.SandboxHandle(
        cast(Any, None),
        SandboxState(
            "docker",
            SandboxRef("container", "http://localhost:7001"),
            launch_id="fixture",
        ),
    )

    async def start(*_args, **_kwargs):
        assert presenter._live_display is not None
        return handle

    monkeypatch.setattr(agent_server.sandbox_runtime, "launch", start)
    with agent_server.acquire_agent_server(
        layout, sandbox="docker", ui_base_url="https://ui.test"
    ):
        assert presenter._live_display is None
    assert observed == [True]
    assert stream.getvalue().count("Development package warning") == 1


def test_attach_finishes_remote_source_progress_without_preparing_state(
    tmp_path, monkeypatch
):
    from io import StringIO
    from toolang.base.types.progress import ProgressEvent
    from toolang.cli.common.progress import make_cli_progress

    layout = AgentLayout.resident(tmp_path, "alice")
    layout.home.mkdir(parents=True)
    server = AgentServerRef(sandbox="host", endpoint="http://127.0.0.1:8123")
    monkeypatch.setattr(agent_server, "_prepare_agent_server", lambda *_a, **_k: server)
    stream = StringIO()
    with make_cli_progress(stream=stream, preparation=True) as outer:
        outer(
            ProgressEvent(
                id="source",
                kind="prepare",
                stage="fetch",
                label="Fetching agent...",
                status="running",
                subject="agent",
                ref="https://example.test/agent.too",
            )
        )
        outer(
            ProgressEvent(
                id="source",
                kind="prepare",
                stage="fetch",
                label="Fetched agent",
                status="ok",
                subject="agent",
                ref="https://example.test/agent.too",
                complete=True,
            )
        )
        with agent_server.acquire_agent_server(layout, sandbox=None) as acquired:
            assert acquired is server
    assert stream.getvalue().count("Prepared agent in ") == 1

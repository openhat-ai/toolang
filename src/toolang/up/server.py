"""AgentServer assembly and foreground serving."""

from __future__ import annotations

import asyncio

from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from dataclasses import replace
import errno
import logging
import json
import os
from pathlib import Path
import signal
import socket
import threading
import time
from types import FrameType, MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
import uvicorn
from uvicorn.main import STARTUP_FAILURE

from toolang.api.version import AGENT_API_VERSION
from toolang.base.model_settings import format_model_body
from toolang.base.types.model import ModelOverride
from toolang.base.types.policy import AgentCeiling
from toolang.common.config import resolve_ui_base_url
from toolang.common.env_logger import PY_LOG_ENV_VAR
from toolang.common.layout import AgentLayout
from toolang.execution.executor.resources import validate_agent_ceiling
from toolang.plugin.sandboxes.host import HOST_SANDBOX_DESCRIPTION_ENV
from toolang.setup import AgentSetup
from toolang.plugin.models.query import filter_models
from toolang.setup.config import load_setup_config
from toolang.state import watcher as state_watcher
from toolang.state.state import AgentState
from toolang.up import process as agents
from toolang.up import workload as workloads
from toolang.up.config import (
    default_drivers,
    format_drivers,
    resolve_cors_allowed_origins,
)
from toolang.up.types import ALL_DRIVERS, ExecutionDrivers, ServerLifetime
from toolang.up.lifecycle import ServerLifecycle
from toolang.up.core import AgentCore
from toolang.up.application import create_agent_application, application_statistics
from toolang.up.logging import (
    DEFAULT_LOG_LEVEL,
    build_uvicorn_log_config,
    configure_logging,
)
from toolang.driver.chore.scheduler import ChoreDriver
from toolang.driver.talk import MessagingLoop
from toolang.teaming.runtime import TeamingLoop
from toolang.teaming.agent_client import AgentClient, AgentEventClient

DEFAULT_WATCH_DEBOUNCE_MS = state_watcher.DEFAULT_DEBOUNCE_MS
RUNTIME_SHUTDOWN_TASK_TIMEOUT_SEC = 1.0
UVICORN_GRACEFUL_SHUTDOWN_SEC = 1
AUTO_RUNTIME_PORT_MIN = 7002
AUTO_RUNTIME_PORT_MAX = 7999
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServeSpec:
    """Fully resolved inputs for one AgentServer process."""

    layout: AgentLayout
    host: str
    endpoint_host: str
    port: int
    toolang_root: Path | None = None
    launch_id: str | None = None
    drivers: ExecutionDrivers = ALL_DRIVERS
    lifetime: ServerLifetime = "persistent"
    ceiling_overrides: Mapping[str, tuple[str, ...] | None] = field(
        default_factory=dict
    )
    default_overrides: Mapping[str, ModelOverride | str | None] = field(
        default_factory=dict
    )
    limit_overrides: Mapping[str, int | float | None] = field(default_factory=dict)
    compact_override: ModelOverride | None = None
    cors_allowed_origins: tuple[str, ...] | None = None
    ui_base_url: str | None = None
    log_spec: str | None = None
    workspace_additions: Mapping[str, str] = field(default_factory=dict)
    workdir: str | None = None
    automatic_port: bool = False
    registry_directory: Path | None = None
    registry_status: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "workspace_additions",
            MappingProxyType(dict(self.workspace_additions)),
        )
        object.__setattr__(
            self,
            "ceiling_overrides",
            MappingProxyType(dict(self.ceiling_overrides)),
        )
        object.__setattr__(
            self,
            "default_overrides",
            MappingProxyType(dict(self.default_overrides)),
        )
        object.__setattr__(
            self,
            "limit_overrides",
            MappingProxyType(dict(self.limit_overrides)),
        )

    @property
    def endpoint(self) -> str:
        return f"http://{self.endpoint_host}:{self.port}"


def resolve_serve(
    *,
    layout: AgentLayout,
    host: str = "127.0.0.1",
    endpoint_host: str | None = None,
    port: int | None = None,
    drivers: ExecutionDrivers | None = None,
    ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
    default_overrides: Mapping[str, ModelOverride | str | None] | None = None,
    limit_overrides: Mapping[str, int | float | None] | None = None,
    compact_override: ModelOverride | None = None,
    cors_allowed_origins: tuple[str, ...] | None = None,
    ui_base_url: str | None = None,
    log_spec: str | None = None,
    temporary_port: bool = False,
    workspace_additions: Mapping[str, str] | None = None,
    workdir: str | None = None,
    toolang_root: Path | None = None,
    launch_id: str | None = None,
    lifetime: ServerLifetime = "persistent",
) -> ServeSpec:
    """Resolve explicit CLI values without constructing runtime snapshots."""

    from .source import restore_program_acquisition

    layout = restore_program_acquisition(layout)
    return ServeSpec(
        layout=layout,
        toolang_root=toolang_root or layout.root or layout.place,
        launch_id=launch_id,
        lifetime=lifetime,
        automatic_port=port is None,
        drivers=default_drivers(layout.placement) if drivers is None else drivers,
        workspace_additions=dict(workspace_additions or {}),
        workdir=workdir,
        host=host,
        endpoint_host=endpoint_host or _default_endpoint_host(host),
        port=resolve_runtime_port(
            host=host,
            explicit_port=port,
            layout=layout,
            temporary=temporary_port,
        ),
        ceiling_overrides=dict(ceiling_overrides or {}),
        default_overrides=dict(default_overrides or {}),
        limit_overrides=dict(limit_overrides or {}),
        compact_override=compact_override,
        cors_allowed_origins=cors_allowed_origins,
        ui_base_url=ui_base_url,
        log_spec=log_spec.strip()
        if isinstance(log_spec, str) and log_spec.strip()
        else None,
    )


def build_serve_argv(
    spec: ServeSpec,
    *,
    place: Path | None = None,
    root: Path | None = None,
    host: str | None = None,
    sandbox: str = "host",
    location: str = "host",
    source_edit_lock: Path | None = None,
    registry_directory: Path | None = None,
) -> tuple[str, ...]:
    """Build explicit argv for the hidden AgentServer entrypoint."""

    command = [
        "--root",
        str(root or spec.toolang_root or spec.layout.root or spec.layout.place),
        "_serve",
        spec.layout.name,
        "--place",
        str(place or spec.layout.place),
        "--sandbox",
        sandbox,
        "--location",
        location,
        "--source-policy",
        "project" if spec.layout.project_inputs else "root",
        "--membership",
        "managed" if spec.layout.managed_membership else "unmanaged",
        "--host",
        host or spec.host,
        "--endpoint-host",
        spec.endpoint_host,
        "--port",
        str(spec.port),
        "--enable",
        format_drivers(spec.drivers),
    ]
    if registry_directory is not None:
        command.extend(
            [
                "--registry-directory",
                str(registry_directory),
                "--registry-status",
                str(spec.layout.runtime_status),
            ]
        )
    command.extend(["--lifetime", spec.lifetime])
    command.extend(["--placement", spec.layout.placement, "--store-schema", "63"])
    if spec.layout.place_code is not None:
        command.extend(["--place-code", spec.layout.place_code])
    if spec.layout.source_path is not None:
        command.extend(
            [
                "--source-path",
                str(spec.layout.source_path),
                "--source-edit-lock",
                str(source_edit_lock or spec.layout.place_edit_lock),
            ]
        )
    if spec.layout.source_ref is not None:
        command.extend(["--source-ref", spec.layout.source_ref])
    if spec.launch_id is not None:
        command.extend(["--launch-id", spec.launch_id])
    if spec.workspace_additions:
        command.extend(
            ["--workspace-bindings", json.dumps(dict(spec.workspace_additions))]
        )
    if spec.workdir is not None:
        command.extend(["--initial-workdir", spec.workdir])
    for name, selectors in spec.ceiling_overrides.items():
        command.extend(["--allow", f"{name}={_format_allow(selectors)}"])
    for name, value in spec.default_overrides.items():
        formatted = (
            format_model_body(value)
            if name == "model" and isinstance(value, ModelOverride)
            else "unset"
            if name == "model" and value is None
            else _format_value(value)
        )
        command.extend(["--default", f"{name}={formatted}"])
    if spec.compact_override is not None:
        command.extend(["--compact-model", format_model_body(spec.compact_override)])
    for name, value in spec.limit_overrides.items():
        command.extend(["--limit", f"{name}={_format_value(value)}"])
    if spec.cors_allowed_origins is not None:
        command.extend(
            ["--cors-allowed-origins", json.dumps(spec.cors_allowed_origins)]
        )
    if spec.ui_base_url is not None:
        command.extend(["--ui-base-url", spec.ui_base_url])
    if spec.log_spec is not None:
        command.extend(["--log", spec.log_spec])
    return tuple(command)


def serve(
    spec: ServeSpec,
    *,
    environ: Mapping[str, str],
    sandbox: str,
    launch_id: str | None = None,
    location: str = "host",
) -> int:
    from toolang.common.environment import (
        EnvironmentPolicy,
        ResolvedEnvironment,
        current_environment,
        environment_identity,
        environment_scope,
    )

    active = current_environment()
    environ = dict(environ)
    environ.update(
        {
            "TOOLANG_ROOT": str(
                spec.toolang_root or spec.layout.root or spec.layout.place
            ),
            "TOOLANG_SANDBOX": sandbox,
        }
    )
    if location == "host":
        environ["TOOLANG_WORKSPACE_LOCATION"] = "host"
        environ["TOOLANG_WORKSPACE_MOUNTS"] = "{}"
    environment = ResolvedEnvironment(
        spec.toolang_root or spec.layout.root or spec.layout.place,
        active.policy if active is not None else EnvironmentPolicy(),
        environ,
        identity=environment_identity(spec.layout),
        source=active.source if active is not None else None,
        picked=active.picked if active is not None else None,
        guest_values=active.guest_values if active is not None else environ,
    )
    with environment_scope(environment):
        return _serve_captured(
            spec,
            environ=environ,
            sandbox=sandbox,
            launch_id=launch_id,
            location=location,
        )


def _serve_captured(
    spec: ServeSpec,
    *,
    environ: Mapping[str, str],
    sandbox: str,
    launch_id: str | None,
    location: str,
) -> int:
    """Retry automatic host binds only after releasing each failed generation."""
    owns_control = location == "host" and not (launch_id or spec.launch_id)
    for attempt in range(3):
        try:
            return _serve_once(
                spec,
                environ=environ,
                sandbox=sandbox,
                launch_id=launch_id,
                location=location,
            )
        except OSError as exc:
            if not owns_control or exc.errno != errno.EADDRINUSE:
                raise
            from .instance import SandboxState
            from .sandbox import _clear_state

            owner = SandboxState.load(
                spec.layout.runtime_status, instance=spec.layout.instance
            )
            if (
                owner is not None
                and owner.ref.runtime_kind == "process"
                and owner.ref.runtime_id == str(os.getpid())
            ):
                _clear_state(spec.layout, expected=owner)
            if not spec.automatic_port or attempt == 2:
                raise
            spec = replace(
                spec,
                port=resolve_runtime_port(
                    host=spec.host,
                    explicit_port=None,
                    layout=spec.layout,
                    temporary=spec.layout.placement != "resident",
                ),
            )
    raise RuntimeError("automatic host bind attempts exhausted")


def _serve_once(
    spec: ServeSpec,
    *,
    environ: Mapping[str, str],
    sandbox: str,
    launch_id: str | None,
    location: str,
) -> int:
    from .closing import ClosingDeadline

    deadline = ClosingDeadline(enabled=spec.lifetime == "foreground")
    try:
        return _serve_workload(
            spec,
            environ=environ,
            sandbox=sandbox,
            launch_id=launch_id,
            location=location,
            deadline=deadline,
        )
    finally:
        deadline.close()


def _serve_workload(
    spec: ServeSpec,
    *,
    environ: Mapping[str, str],
    sandbox: str,
    launch_id: str | None,
    location: str,
    deadline,
) -> int:
    """Run one AgentServer as the current process's primary workload."""

    from toolang.up.sandbox import register_host_server

    launch_id = launch_id or spec.launch_id
    if location == "guest" and launch_id is None:
        raise ValueError("guest workload requires an explicit launch identity")
    owns_control = location == "host" and launch_id is None

    host_ref = (
        asyncio.run(
            register_host_server(
                spec.layout,
                endpoint=spec.endpoint,
                launch_id=launch_id,
                spec=spec,
                environ=environ,
            )
        )
        if location == "host"
        else None
    )
    from toolang.up.instance import patch_workload
    import psutil

    if launch_id is None and host_ref is not None:
        identity = host_ref.meta.get("launch_id")
        if isinstance(identity, str) and identity:
            launch_id = identity
    if launch_id is None:
        raise ValueError("workload requires a launch identity")
    published = patch_workload(
        spec.layout.runtime_status,
        launch_id,
        {
            "pid": os.getpid(),
            "process_created": psutil.Process().create_time(),
            "status": "starting",
        },
        instance=spec.layout.instance,
        location=location,
    )
    if not published:
        raise ValueError("workload launch identity is no longer current")
    initialized_core: AgentCore | None = None
    core_closed = False
    failure: str | None = None
    from .closing import OWNER_CHANNEL_REASON

    shutdown_reason: str | None = None

    def begin_closing() -> None:
        nonlocal shutdown_reason
        if shutdown_reason is not None:
            return
        marker = environ.get("TOOLANG_FOREGROUND_OWNER_FILE")
        from .instance import read_instance

        previous = (
            read_instance(spec.layout.runtime_status, location=location) or {}
        ).get("workload", {})
        shutdown_reason = (
            OWNER_CHANNEL_REASON
            if marker and Path(marker).is_file()
            else previous.get("shutdown_reason", "server interrupted")
        )
        started = previous.get("shutdown_started_at")
        deadline.begin(
            shutdown_reason,
            started_at=started if isinstance(started, int | float) else None,
        )
        if initialized_core is not None and initialized_core.lifecycle is not None:
            initialized_core.lifecycle.closing = True
        patch_workload(
            spec.layout.runtime_status,
            launch_id,
            {
                "shutdown_reason": shutdown_reason,
                "shutdown_started_at": deadline.started,
            },
            instance=spec.layout.instance,
            location=location,
        )

    async def close_core() -> None:
        nonlocal core_closed, failure
        if initialized_core is not None and not core_closed:
            core_closed = True
            try:
                if shutdown_reason is None:
                    await initialized_core.close()
                else:
                    await initialized_core.close(reason=shutdown_reason)
            except BaseException as exc:
                message = str(exc) or type(exc).__name__
                failure = (
                    message
                    if failure is None
                    else f"{failure}; cleanup failed: {message}"
                )
                raise

    try:
        _restore_termination_signal_defaults()
        runtime_log_spec = _runtime_log_spec_value(spec.log_spec, environ)
        configure_logging(spec=runtime_log_spec, environ=environ)

        def record_promotion() -> None:
            patch_workload(
                spec.layout.runtime_status,
                launch_id,
                {"lifetime": "persistent"},
                instance=spec.layout.instance,
                location=location,
            )

        lifecycle = ServerLifecycle(
            launch_id, spec.lifetime, on_promote=record_promotion
        )
        core = AgentCore(
            spec.layout,
            environ=environ,
            lifecycle=lifecycle,
            drivers=spec.drivers,
            sandbox=sandbox,
            ceiling_overrides=spec.ceiling_overrides,
            default_overrides=spec.default_overrides,
            limit_overrides=spec.limit_overrides,
            compact_override=spec.compact_override,
            workspace_additions=spec.workspace_additions,
            workdir=spec.workdir,
        )
        initialized_core = core
        asyncio.run(_refresh_core(core))
        state = core.state.current()
        ceiling = AgentCeiling()
        validate_agent_ceiling(core.setup.current(), state, ceiling)
        core.executor.resolve_workdir(
            core.setup.current(),
            state,
            workdir=spec.workdir,
            inherit_thread_workdir=False,
        )
        web_config = load_setup_config(spec.layout)
        cors_allowed_origins = (
            spec.cors_allowed_origins
            if spec.cors_allowed_origins is not None
            else resolve_cors_allowed_origins(web_config, environ=environ)
        )
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        def current_setup() -> AgentSetup:
            return core.setup.current()

        def current_state() -> AgentState:
            return core.state.current()

        _log_state_loaded(
            current_setup(),
            current_state(),
            ceiling=ceiling,
        )
        shutdown_signal = threading.Event()

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            nonlocal failure
            stop_signal = asyncio.Event()
            tasks: list[asyncio.Task[None]] = []
            scheduler: ChoreDriver | None = None
            teaming: TeamingLoop | None = None
            statistics = application_statistics(app)
            try:
                statistics.start()
                workloads.write_runtime_state(
                    spec.layout,
                    endpoint=spec.endpoint,
                    started_at=started_at,
                    pid=os.getpid(),
                    models=tuple(
                        model.ref for model in current_setup().models_effective()
                    ),
                    sandbox=sandbox,
                    launch_id=launch_id,
                    location=location,
                    drivers=sorted(spec.drivers),
                    lifetime=spec.lifetime,
                    api_version=AGENT_API_VERSION,
                    workspace_additions=spec.workspace_additions,
                    resource_counts=core.resource_counts(),
                    process_created=psutil.Process().create_time(),
                    sandbox_description=environ.get(HOST_SANDBOX_DESCRIPTION_ENV),
                    sandbox_instance=environ.get("TOOLANG_SANDBOX_INSTANCE"),
                )
                if "chore" in spec.drivers:
                    scheduler = ChoreDriver(
                        layout=spec.layout,
                        executor=core.executor,
                        ids=core.ids,
                        get_agent_setup=current_setup,
                        get_agent_state=current_state,
                    )
                messaging_setup = current_setup().teaming
                if messaging_setup is not None and messaging_setup.home.enabled:
                    hub_client = AgentClient(
                        spec.toolang_root or spec.layout.root or spec.layout.place,
                        actor=f"agent:{spec.layout.name}",
                        managed=bool(spec.layout.managed_membership),
                    )
                    messaging = (
                        MessagingLoop(
                            layout=spec.layout,
                            owner=messaging_setup.root.human,
                            client=hub_client,
                            endpoint=spec.endpoint,
                            executor=core.executor,
                            threads=core.threads,
                            get_agent_setup=current_setup,
                            get_agent_state=current_state,
                        )
                        if "talk" in spec.drivers
                        else None
                    )
                    teaming = TeamingLoop(
                        AgentEventClient(hub_client),
                        client=hub_client,
                        owner=messaging_setup.root.human,
                        endpoint=spec.endpoint,
                        executor=core.executor,
                        messaging=messaging,
                    )
                    teaming.start()
                if scheduler is not None:
                    await scheduler.start()
                app.state.chore_driver = scheduler
                tasks.extend(
                    [
                        asyncio.create_task(
                            core.state.run(
                                stop_signal=stop_signal,
                                interval_ms=state_watcher.DEFAULT_INTERVAL_MS,
                                debounce_ms=DEFAULT_WATCH_DEBOUNCE_MS,
                            )
                        ),
                        asyncio.create_task(core.setup.run(stop_signal=stop_signal)),
                    ]
                )

                async def reap_idle() -> None:
                    while not stop_signal.is_set():
                        if lifecycle.lifetime != "automatic":
                            return
                        if lifecycle.elect_exit(core.executor.has_owned_work):
                            shutdown_signal.set()
                            return
                        await asyncio.sleep(0.1)

                if spec.lifetime == "automatic":
                    tasks.append(asyncio.create_task(reap_idle()))
                yield
            finally:
                stop_signal.set()
                try:
                    async with AsyncExitStack() as cleanup:
                        if scheduler is not None:
                            cleanup.push_async_callback(scheduler.stop)
                        if teaming is not None:
                            cleanup.push_async_callback(teaming.close)
                        cleanup.push_async_callback(close_core)
                        cleanup.push_async_callback(asyncio.to_thread, statistics.close)
                        if teaming is not None:
                            cleanup.push_async_callback(teaming.stop_messages)
                        cleanup.push_async_callback(_finish_runtime_tasks, tasks)
                        if scheduler is not None:
                            cleanup.push_async_callback(scheduler.pause)
                except BaseException as exc:
                    failure = failure or str(exc) or type(exc).__name__
                    raise

        app = create_agent_application(
            core,
            lifespan=lifespan,
            cors_allowed_origins=cors_allowed_origins,
        )
        app.state.shutdown_signal = shutdown_signal
        webui_url = _runtime_webui_url(
            spec.endpoint,
            config=web_config,
            environ=environ,
            base_url=spec.ui_base_url,
        )
        _run_uvicorn_app(
            app,
            host=spec.host,
            port=spec.port,
            log_config=build_uvicorn_log_config(
                level=runtime_log_spec or DEFAULT_LOG_LEVEL
            ),
            shutdown_signal=shutdown_signal,
            on_starting=lambda: logger.info(
                "Agent starting place=%s",
                spec.layout.place,
                extra={"color_message": "Agent starting place=\x1b[1m%s\x1b[0m"},
            ),
            on_running=lambda: (
                lifecycle.ready(),
                _record_ready(spec, launch_id, sandbox, host_ref),
                logger.info(
                    "Agent started webui=%s drivers=%s",
                    webui_url,
                    format_drivers(spec.drivers),
                ),
            ),
            on_stopping=lambda: (
                begin_closing(),
                app.state.subscriptions.stop(),
                logger.info("Agent stopping"),
            ),
            on_stopped=lambda: logger.info("Agent stopped"),
            foreground=spec.lifetime == "foreground",
        )
        return 0
    except BaseException as exc:
        if not isinstance(exc, KeyboardInterrupt) and not (
            isinstance(exc, SystemExit) and exc.code in {None, 0}
        ):
            failure = failure or str(exc) or type(exc).__name__
        raise
    finally:
        try:
            if spec.lifetime == "foreground":
                begin_closing()
            asyncio.run(close_core())
        finally:
            from .registry import finalize_runtime

            finalize_runtime(
                spec.layout,
                launch_id,
                expected_pid=os.getpid(),
                failure=failure,
                location=location,
                directory=spec.registry_directory,
                status_path=spec.registry_status,
            )
            if owns_control and failure is None and host_ref is not None:
                from toolang.up.sandbox import _clear_state
                from toolang.up.instance import SandboxState

                _clear_state(
                    spec.layout, expected=SandboxState(sandbox, host_ref, launch_id)
                )
            deadline.close()


def _format_allow(values: tuple[str, ...] | None) -> str:
    if values is None:
        return "all"
    if not values:
        return "none"
    return ",".join(values)


def _format_value(value: object | None) -> str:
    return "none" if value is None else str(value)


def _restore_termination_signal_defaults() -> None:
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            if signal.getsignal(signum) == signal.SIG_IGN:
                signal.signal(signum, signal.SIG_DFL)
        except (OSError, RuntimeError, ValueError):
            continue


def _runtime_log_spec_value(
    log_spec: str | None,
    environ: Mapping[str, str],
) -> str | None:
    if isinstance(log_spec, str) and log_spec.strip():
        return log_spec.strip()
    value = environ.get(PY_LOG_ENV_VAR, "").strip()
    return value or None


def _log_state_loaded(
    setup: AgentSetup,
    state: AgentState,
    *,
    ceiling: AgentCeiling,
) -> None:
    logger.info(
        "Agent loaded state=%s models=%s tools=%s psyches=%s skills=%s services=%s",
        state.revision[:12],
        _model_count(setup, state, ceiling=ceiling),
        len(setup.tools()),
        _cap_count(state, "psyche"),
        _cap_count(state, "skill"),
        _cap_count(state, "service"),
    )


def _model_count(
    setup: AgentSetup,
    state: AgentState,
    *,
    ceiling: AgentCeiling,
) -> int:
    if ceiling.models is None:
        return len(setup.models_effective())
    if not ceiling.models:
        return 0
    return len(filter_models(setup.models_effective(), ceiling.models))


async def _refresh_core(core: AgentCore) -> None:
    """Initialize independent Setup and State publications concurrently."""

    await asyncio.gather(core.state.refresh(), core.setup.refresh())


def _cap_count(state: AgentState, kind: str) -> int:
    return sum(item.kind == kind for item in state.caps_for("agent"))


class _ToolangServer(uvicorn.Server):
    """Uvicorn server with one runtime-visible shutdown signal."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        shutdown_signal: threading.Event,
        on_running: Callable[[], None] | None,
        on_stopping: Callable[[], None] | None,
        foreground: bool = False,
    ) -> None:
        super().__init__(config=config)
        self._shutdown_signal = shutdown_signal
        self._on_running = on_running
        self._on_stopping = on_stopping
        self._foreground = foreground

    async def startup(self, sockets: Any | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started and self._on_running is not None:
            self._on_running()
            self._on_running = None

    async def shutdown(self, sockets: Any | None = None) -> None:
        if self._on_stopping is not None:
            self._on_stopping()
            self._on_stopping = None
        await super().shutdown(sockets=sockets)

    async def on_tick(self, counter: int) -> bool:
        return self._shutdown_signal.is_set() or await super().on_tick(counter)

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._shutdown_signal.set()
        if self._on_stopping is not None:
            callback, self._on_stopping = self._on_stopping, None
            callback()
        if self.should_exit and self._foreground:
            return
        super().handle_exit(sig, frame)


def _run_uvicorn_app(
    app: FastAPI,
    *,
    host: str,
    port: int,
    log_config: dict[str, object],
    shutdown_signal: threading.Event,
    on_starting: Callable[[], None] | None,
    on_running: Callable[[], None] | None,
    on_stopping: Callable[[], None] | None,
    on_stopped: Callable[[], None] | None,
    foreground: bool = False,
) -> None:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=log_config,
        timeout_graceful_shutdown=UVICORN_GRACEFUL_SHUTDOWN_SEC,
    )
    server = _ToolangServer(
        config,
        shutdown_signal=shutdown_signal,
        on_running=on_running,
        on_stopping=on_stopping,
        foreground=foreground,
    )
    if on_starting is not None:
        on_starting()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((host, port))
            listener.listen()
            server.run(sockets=[listener])
    except KeyboardInterrupt:
        pass
    if not server.started:
        raise SystemExit(STARTUP_FAILURE)
    if on_stopped is not None:
        on_stopped()


async def _finish_runtime_tasks(
    tasks: Sequence[asyncio.Task[Any]],
    *,
    timeout_sec: float = RUNTIME_SHUTDOWN_TASK_TIMEOUT_SEC,
) -> None:
    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout_sec)
    for task in pending:
        task.cancel()
    if pending:
        canceled, stuck = await asyncio.wait(pending, timeout=timeout_sec)
        done |= canceled
        for task in stuck:
            logger.warning(
                "runtime task did not stop after cancellation task=%r",
                task,
            )
    for task in done:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("runtime task failed during shutdown", exc_info=True)


def _runtime_webui_url(
    endpoint: str,
    *,
    config: Mapping[str, object],
    environ: Mapping[str, str],
    base_url: str | None = None,
) -> str:
    try:
        port = urlsplit(endpoint).port
    except ValueError:
        port = None
    base_url = (base_url or resolve_ui_base_url(config, environ=environ)).rstrip("/")
    return base_url if port is None else f"{base_url}/{port}"


def _default_endpoint_host(host: str) -> str:
    return "localhost" if host == "127.0.0.1" else host


def resolve_runtime_port(
    *,
    host: str,
    explicit_port: int | None,
    layout: AgentLayout,
    temporary: bool,
) -> int:
    if explicit_port is not None:
        if explicit_port < 1 or explicit_port > 65535:
            raise ValueError(f"invalid runtime port: {explicit_port}")
        return explicit_port
    if temporary or layout.placement == "roaming":
        return _pick_temporary_runtime_port(host)
    preferred = agents.preferred_runtime_port(layout)
    assigned = agents.assigned_runtime_ports(
        layout.root or layout.place,
        exclude_agent=layout.address,
    )
    # Reassign agents that previously used the default Hub port.
    if (
        preferred is not None
        and preferred != 7001
        and preferred not in assigned
        and _port_is_available(host, preferred)
    ):
        return preferred
    if layout.placement == "visiting":
        return _pick_temporary_runtime_port(host)
    for port in range(AUTO_RUNTIME_PORT_MIN, AUTO_RUNTIME_PORT_MAX + 1):
        if port not in assigned and _port_is_available(host, port):
            return port
    raise ValueError("no available agent runtime ports")


def _pick_temporary_runtime_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind((host, 0))
        return int(stream.getsockname()[1])


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            stream.bind((host, port))
        except OSError:
            return False
    return True


def _record_ready(spec: ServeSpec, launch_id: str, sandbox: str, host_ref) -> None:
    if host_ref is not None:
        from toolang.up.instance import update_instance

        update_instance(
            spec.layout.runtime_status,
            lambda entry: entry["control"].update(
                last_successful={"endpoint": spec.endpoint, "sandbox": sandbox}
            ),
            instance=spec.layout.instance,
            launch_id=launch_id,
        )

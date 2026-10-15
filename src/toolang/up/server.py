"""AgentServer assembly and foreground serving."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
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

from toolang.api.app import create_app
from toolang.base.model_settings import format_model_body
from toolang.base.types.model import ModelOverride
from toolang.base.types.policy import AgentCeiling
from toolang.catalog import CapsManager, ChoresManager
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
from toolang.up.config import (
    default_drivers,
    format_drivers,
    resolve_cors_allowed_origins,
)
from toolang.up.types import ALL_DRIVERS, ExecutionDrivers
from toolang.up.core import AgentCore
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
    ceiling_overrides: Mapping[str, tuple[str, ...] | None] = field(
        default_factory=dict
    )
    default_overrides: Mapping[str, ModelOverride | str | None] = field(
        default_factory=dict
    )
    limit_overrides: Mapping[str, int | float | None] = field(default_factory=dict)
    compact_override: ModelOverride | None = None
    log_spec: str | None = None
    workspace_additions: Mapping[str, str] = field(default_factory=dict)
    workdir: str | None = None

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
    log_spec: str | None = None,
    temporary_port: bool = False,
    workspace_additions: Mapping[str, str] | None = None,
    workdir: str | None = None,
    toolang_root: Path | None = None,
    launch_id: str | None = None,
) -> ServeSpec:
    """Resolve explicit CLI values without constructing runtime snapshots."""

    from .source import restore_program_acquisition

    layout = restore_program_acquisition(layout)
    return ServeSpec(
        layout=layout,
        toolang_root=toolang_root or layout.toolang_root or layout.root,
        launch_id=launch_id,
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
        log_spec=log_spec.strip()
        if isinstance(log_spec, str) and log_spec.strip()
        else None,
    )


def build_serve_argv(
    spec: ServeSpec,
    *,
    root: Path | None = None,
    host: str | None = None,
    sandbox: str = "host",
    location: str = "host",
) -> tuple[str, ...]:
    """Build explicit argv for the hidden AgentServer entrypoint."""

    command = [
        "--root",
        str(spec.toolang_root or spec.layout.root),
        "_serve",
        spec.layout.name,
        "--agent-root",
        str(root or spec.layout.root),
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
    if spec.layout.instance is not None:
        command.extend(["--runtime-instance", spec.layout.instance])
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
    """Run one AgentServer as the current process's primary workload."""

    from toolang.up.sandbox import register_host_server

    launch_id = launch_id or spec.launch_id
    if location == "guest" and launch_id is None:
        raise ValueError("guest workload requires an explicit launch identity")
    owns_control = location == "host" and launch_id is None

    host_ref = (
        asyncio.run(
            register_host_server(
                spec.layout, endpoint=spec.endpoint, launch_id=launch_id, spec=spec
            )
        )
        if location == "host"
        else None
    )
    from toolang.up.records import patch_workload
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

    async def close_core() -> None:
        nonlocal core_closed, failure
        if initialized_core is not None and not core_closed:
            core_closed = True
            try:
                await initialized_core.close()
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
        core = AgentCore(
            spec.layout,
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
        cors_allowed_origins = resolve_cors_allowed_origins(
            load_setup_config(spec.layout),
            environ=environ,
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
        caps_manager = CapsManager(spec.layout)
        chores_manager = ChoresManager(spec.layout)

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            nonlocal failure
            stop_signal = asyncio.Event()
            tasks: list[asyncio.Task[None]] = []
            scheduler: ChoreDriver | None = None
            teaming: TeamingLoop | None = None
            try:
                agents.write_runtime_state(
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
                    workspace_additions=spec.workspace_additions,
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
                        spec.layout.root,
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
                        activity=core.activity,
                        publish_activity=hub_client.publish_activity,
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
                        if teaming is not None:
                            cleanup.push_async_callback(teaming.stop_messages)
                        cleanup.push_async_callback(_finish_runtime_tasks, tasks)
                        if scheduler is not None:
                            cleanup.push_async_callback(scheduler.pause)
                except BaseException as exc:
                    failure = failure or str(exc) or type(exc).__name__
                    raise

        app = create_app(
            core,
            caps_manager,
            chores_manager,
            lifespan=lifespan,
            cors_allowed_origins=cors_allowed_origins,
        )
        app.state.shutdown_signal = shutdown_signal
        webui_url = _runtime_webui_url(
            spec.endpoint,
            config=load_setup_config(spec.layout),
            environ=environ,
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
                "Agent starting root=%s",
                spec.layout.root,
                extra={"color_message": "Agent starting root=\x1b[1m%s\x1b[0m"},
            ),
            on_running=lambda: (
                _record_ready(spec, launch_id, sandbox, host_ref),
                logger.info(
                    "Agent started webui=%s drivers=%s",
                    webui_url,
                    format_drivers(spec.drivers),
                ),
            ),
            on_stopping=lambda: (
                app.state.subscriptions.stop(),
                logger.info("Agent stopping"),
            ),
            on_stopped=lambda: logger.info("Agent stopped"),
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
            asyncio.run(close_core())
        finally:
            if failure is not None:
                patch_workload(
                    spec.layout.runtime_status,
                    launch_id,
                    {
                        "status": "failed",
                        "pid": None,
                        "message": failure,
                        "updated_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    },
                    instance=spec.layout.instance,
                    location=location,
                )
            else:
                agents.stop_runtime_state(
                    spec.layout,
                    expected_pid=os.getpid(),
                    launch_id=launch_id,
                    location=location,
                )
            if owns_control and failure is None and host_ref is not None:
                from toolang.up.sandbox import _clear_state
                from toolang.up.records import SandboxState

                _clear_state(
                    spec.layout, expected=SandboxState(sandbox, host_ref, launch_id)
                )


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
    ) -> None:
        super().__init__(config=config)
        self._shutdown_signal = shutdown_signal
        self._on_running = on_running
        self._on_stopping = on_stopping

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

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._shutdown_signal.set()
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
    )
    if on_starting is not None:
        on_starting()
    try:
        server.run()
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
) -> str:
    try:
        port = urlsplit(endpoint).port
    except ValueError:
        port = None
    base_url = resolve_ui_base_url(config, environ=environ).rstrip("/")
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
    if temporary:
        return _pick_temporary_runtime_port(host)
    preferred = agents.preferred_runtime_port(layout)
    assigned = agents.assigned_runtime_ports(
        layout.root,
        exclude_agent=layout.name,
    )
    # Reassign pre-upgrade agent records on the default Hub port.
    if (
        preferred is not None
        and preferred != 7001
        and preferred not in assigned
        and _port_is_available(host, preferred)
    ):
        return preferred
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
        from toolang.up.records import update_record

        update_record(
            spec.layout.runtime_status,
            lambda entry: entry["control"].update(
                last_successful={"endpoint": spec.endpoint, "sandbox": sandbox}
            ),
            instance=spec.layout.instance,
            launch_id=launch_id,
        )

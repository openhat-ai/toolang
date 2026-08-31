"""AgentServer assembly and foreground serving."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
import logging
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
from toolang.base.types.policy import AgentCeiling
from toolang.catalog import CapsManager, JobsManager
from toolang.common.config import resolve_ui_base_url
from toolang.common.env_logger import PY_LOG_ENV_VAR
from toolang.common.layout import AgentLayout
from toolang.execution.executor.resources import (
    agent_model_targets,
    validate_agent_ceiling,
)
from toolang.plugin.sandboxes.host import HOST_SANDBOX_DESCRIPTION_ENV
from toolang.setup import AgentSetup
from toolang.setup.config import load_setup_config
from toolang.state import watcher as state_watcher
from toolang.state.state import AgentState, StatePublication
from toolang.up import process as agents
from toolang.up.config import resolve_cors_allowed_origins
from toolang.up.core import AgentCore
from toolang.up.logging import (
    DEFAULT_LOG_LEVEL,
    build_uvicorn_log_config,
    configure_logging,
)
from toolang.work import inbox as files
from toolang.work.scheduler import JobScheduler

DEFAULT_TRIGGER_INTERVAL_MS: dict[str, float] = {
    "file": files.DEFAULT_INTERVAL_MS,
}
DEFAULT_WATCH_DEBOUNCE_MS = state_watcher.DEFAULT_DEBOUNCE_MS
DEFAULT_FILE_STABLE_MS = files.DEFAULT_STABLE_MS
RUNTIME_SHUTDOWN_TASK_TIMEOUT_SEC = 1.0
UVICORN_GRACEFUL_SHUTDOWN_SEC = 1
AUTO_RUNTIME_PORT_MIN = 7001
AUTO_RUNTIME_PORT_MAX = 7999
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServeSpec:
    """Fully resolved inputs for one AgentServer process."""

    layout: AgentLayout
    host: str
    endpoint_host: str
    port: int
    ceiling_overrides: Mapping[str, tuple[str, ...] | None] = field(
        default_factory=dict
    )
    default_overrides: Mapping[str, str | None] = field(default_factory=dict)
    limit_overrides: Mapping[str, int | Decimal | None] = field(default_factory=dict)
    file_inboxes: tuple[Path, ...] = ()
    log_spec: str | None = None

    def __post_init__(self) -> None:
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
    ceiling_overrides: Mapping[str, tuple[str, ...] | None] | None = None,
    default_overrides: Mapping[str, str | None] | None = None,
    limit_overrides: Mapping[str, int | Decimal | None] | None = None,
    file_inboxes: Sequence[Path] | None = None,
    log_spec: str | None = None,
    temporary_port: bool = False,
) -> ServeSpec:
    """Resolve explicit CLI values without constructing runtime snapshots."""

    resolved_inboxes = _normalize_file_inboxes(file_inboxes)
    return ServeSpec(
        layout=layout,
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
        file_inboxes=resolved_inboxes,
        log_spec=log_spec.strip()
        if isinstance(log_spec, str) and log_spec.strip()
        else None,
    )


def build_serve_argv(
    spec: ServeSpec,
    *,
    root: Path | None = None,
    host: str | None = None,
) -> tuple[str, ...]:
    """Build explicit argv for the hidden AgentServer entrypoint."""

    command = [
        "--root",
        str(root or spec.layout.root),
        "serve",
        spec.layout.name,
        "--host",
        host or spec.host,
        "--endpoint-host",
        spec.endpoint_host,
        "--port",
        str(spec.port),
    ]
    for name, selectors in spec.ceiling_overrides.items():
        command.extend(["--allow", f"{name}={_format_allow(selectors)}"])
    for name, value in spec.default_overrides.items():
        command.extend(["--default", f"{name}={_format_value(value)}"])
    for name, value in spec.limit_overrides.items():
        command.extend(["--limit", f"{name}={_format_value(value)}"])
    for inbox in spec.file_inboxes:
        command.extend(["--inbox", str(inbox)])
    if spec.log_spec is not None:
        command.extend(["--log", spec.log_spec])
    return tuple(command)


def serve(
    spec: ServeSpec,
    *,
    environ: Mapping[str, str],
    sandbox: str,
) -> int:
    """Run one AgentServer as the current process's primary workload."""

    _restore_termination_signal_defaults()
    runtime_log_spec = _runtime_log_spec_value(spec.log_spec, environ)
    configure_logging(spec=runtime_log_spec, environ=environ)
    for name, interval_ms in DEFAULT_TRIGGER_INTERVAL_MS.items():
        if interval_ms <= 0:
            raise ValueError(f"trigger interval must be positive: {name}")
    core = AgentCore(
        spec.layout,
        sandbox=sandbox,
        ceiling_overrides=spec.ceiling_overrides,
        default_overrides=spec.default_overrides,
        limit_overrides=spec.limit_overrides,
    )
    asyncio.run(core.state.refresh())
    asyncio.run(core.setup.refresh())
    state = core.state.current()
    ceiling = AgentCeiling()
    _validate_file_agic(state.state, enabled=bool(spec.file_inboxes))
    validate_agent_ceiling(core.setup.current(), state, ceiling)
    cors_allowed_origins = resolve_cors_allowed_origins(
        load_setup_config(spec.layout),
        environ=environ,
    )
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def current_setup() -> AgentSetup:
        return core.setup.current()

    def current_state() -> StatePublication:
        return core.state.current()

    _log_state_loaded(
        current_setup(),
        current_state(),
        ceiling=ceiling,
    )
    shutdown_signal = threading.Event()
    caps_manager = CapsManager(spec.layout)
    jobs_manager = JobsManager(spec.layout)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stop_signal = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []
        scheduler: JobScheduler | None = None
        try:
            agents.write_runtime_state(
                spec.layout,
                endpoint=spec.endpoint,
                started_at=started_at,
                pid=os.getpid(),
                models=current_setup().models.refs(),
                sandbox=sandbox,
                sandbox_description=environ.get(HOST_SANDBOX_DESCRIPTION_ENV),
                sandbox_instance=environ.get("TOOLANG_SANDBOX_INSTANCE"),
            )
            scheduler = JobScheduler(
                layout=spec.layout,
                executor=core.executor,
                ids=core.ids,
                get_agent_setup=current_setup,
                get_agent_state=current_state,
            )
            await scheduler.start()
            app.state.job_scheduler = scheduler
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
            if spec.file_inboxes:
                tasks.append(
                    files.spawn(
                        layout=spec.layout,
                        executor=core.executor,
                        get_agent_setup=current_setup,
                        get_agent_state=current_state,
                        inboxes=spec.file_inboxes,
                        interval_ms=DEFAULT_TRIGGER_INTERVAL_MS["file"],
                        stable_ms=DEFAULT_FILE_STABLE_MS,
                        stop_signal=stop_signal,
                    )
                )
            yield
        finally:
            agents.stop_runtime_state(
                spec.layout,
                expected_pid=os.getpid(),
                expected_started_at=started_at,
            )
            stop_signal.set()
            if scheduler is not None:
                await scheduler.pause()
            await _finish_runtime_tasks(tasks)
            await core.close()
            if scheduler is not None:
                await scheduler.stop()

    app = create_app(
        core,
        caps_manager,
        jobs_manager,
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
        on_running=lambda: logger.info(
            "Agent started webui=%s",
            webui_url,
            extra={"color_message": "Agent started webui=\x1b[1m%s\x1b[0m"},
        ),
        on_stopping=lambda: logger.info("Agent stopping"),
        on_stopped=lambda: logger.info("Agent stopped"),
    )
    return 0


def _format_allow(values: tuple[str, ...] | None) -> str:
    if values is None:
        return "all"
    if not values:
        return "none"
    return ",".join(values)


def _format_value(value: object | None) -> str:
    return "none" if value is None else str(value)


def _validate_file_agic(state: AgentState, *, enabled: bool) -> None:
    if not enabled:
        return
    agic = state.modules["agent"].find_agic("file")
    if agic is None:
        raise ValueError("file agic not found")
    if agic.input is None:
        raise ValueError("file agic must accept message input")
    missing = [param.name for param in agic.params if not param.optional]
    if missing:
        joined = ", ".join(f"{name}=..." for name in missing)
        raise ValueError(f"file agic cannot have required parameters: {joined}")


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
    state: AgentState | StatePublication,
    *,
    ceiling: AgentCeiling,
) -> None:
    logger.info(
        "Agent loaded state=%s models=%s tools=%s psyches=%s skills=%s services=%s",
        state.revision[:12],
        _model_count(setup, state, ceiling=ceiling),
        len(setup.tools),
        _cap_count(state, "psyche"),
        _cap_count(state, "skill"),
        _cap_count(state, "service"),
    )


def _model_count(
    setup: AgentSetup,
    state: AgentState | StatePublication,
    *,
    ceiling: AgentCeiling,
) -> int:
    _default, targets = agent_model_targets(setup, ceiling)
    return len(targets)


def _cap_count(state: AgentState | StatePublication, kind: str) -> int:
    if isinstance(state, StatePublication):
        return sum(item.kind == kind for item in state.resources.caps_for("agent"))
    return len(
        {
            "psyche": state.psyches,
            "skill": state.skills,
            "service": state.services,
            "prompt": state.prompts,
        }.get(kind, {})
    )


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


def _normalize_file_inboxes(
    file_inboxes: Sequence[Path] | None,
) -> tuple[Path, ...]:
    if file_inboxes is None:
        return ()
    result: list[Path] = []
    for value in file_inboxes:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"inbox not found: {path}")
        if path not in result:
            result.append(path)
    return tuple(result)


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
    if preferred is not None and preferred not in assigned:
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

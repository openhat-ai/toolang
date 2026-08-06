"""AgentServer assembly and foreground serving."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
import socket
import threading
import time
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
import uvicorn
from uvicorn.main import STARTUP_FAILURE

from toolang.api.app import create_app
from toolang.catalog import CapsManager, JobsManager
from toolang.common.config import resolve_ui_base_url
from toolang.common.env_logger import PY_LOG_ENV_VAR
from toolang.common.layout import AgentLayout
from toolang.execution.executor import CeilingSpec
from toolang.execution.executor.ceiling import (
    agent_model_targets,
    validate_ceiling_spec,
)
from toolang.plugin.models.resolution import (
    split_model_selectors,
)
from toolang.plugin.tools.registry import split_tool_selectors
from toolang.setup import AgentSetup
from toolang.state import watcher as state_watcher
from toolang.state.state import AgentState, split_cap_selectors
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
    ceiling: CeilingSpec = CeilingSpec()
    file_inboxes: tuple[Path, ...] = ()
    log_spec: str | None = None

    @property
    def endpoint(self) -> str:
        return f"http://{self.endpoint_host}:{self.port}"


def resolve_serve(
    *,
    layout: AgentLayout,
    host: str = "127.0.0.1",
    endpoint_host: str | None = None,
    port: int | None = None,
    models: Sequence[str] | None = None,
    tools: Sequence[str] | None = None,
    caps: Sequence[str] | None = None,
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
        ceiling=CeilingSpec(
            models=_normalize_model_selectors(models) or None,
            tools=_normalize_tool_selectors(tools),
            caps=_normalize_cap_selectors(caps) or None,
        ),
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
    for selector in spec.ceiling.models or ():
        command.extend(["--models", selector])
    for selector in spec.ceiling.tools or ():
        command.extend(["--tools", selector])
    for selector in spec.ceiling.caps or ():
        command.extend(["--caps", selector])
    for inbox in spec.file_inboxes:
        command.extend(["--inbox", str(inbox)])
    if spec.log_spec is not None:
        command.extend(["--log", spec.log_spec])
    return tuple(command)


def serve(spec: ServeSpec, *, environ: Mapping[str, str]) -> int:
    """Run one AgentServer as the current process's primary workload."""

    _restore_termination_signal_defaults()
    runtime_log_spec = _runtime_log_spec_value(spec.log_spec, environ)
    configure_logging(spec=runtime_log_spec, environ=environ)
    for name, interval_ms in DEFAULT_TRIGGER_INTERVAL_MS.items():
        if interval_ms <= 0:
            raise ValueError(f"trigger interval must be positive: {name}")

    core = AgentCore(spec.layout)
    asyncio.run(core.state.refresh())
    asyncio.run(core.setup.refresh())
    state = core.state.current()
    ceiling = spec.ceiling
    _validate_file_agic(state, enabled=bool(spec.file_inboxes))
    validate_ceiling_spec(core.setup.current(), state, ceiling)
    cors_allowed_origins = resolve_cors_allowed_origins(
        state.root_config,
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
                models=ceiling.models or (),
                sandbox=environ.get("TOOLANG_SANDBOX", "none"),
            )
            scheduler = JobScheduler(
                layout=spec.layout,
                executor=core.executor,
                ids=core.ids,
                get_agent_setup=current_setup,
                get_agent_state=current_state,
                ceiling=ceiling,
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
                        ceiling=ceiling,
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
            await _finish_runtime_tasks(tasks)
            if scheduler is not None:
                await scheduler.pause()
            await core.close()
            if scheduler is not None:
                await scheduler.stop()

    app = create_app(
        core,
        caps_manager,
        jobs_manager,
        ceiling=ceiling,
        lifespan=lifespan,
        cors_allowed_origins=cors_allowed_origins,
    )
    app.state.shutdown_signal = shutdown_signal
    webui_url = _runtime_webui_url(
        spec.endpoint,
        state=state,
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


def _validate_file_agic(state: AgentState, *, enabled: bool) -> None:
    if not enabled:
        return
    agic = state.program.find_agic("file")
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
    state: AgentState,
    *,
    ceiling: CeilingSpec,
) -> None:
    logger.info(
        "Agent loaded state=%s models=%s tools=%s psyches=%s skills=%s services=%s",
        state.fingerprint[:12],
        _model_count(setup, state, ceiling=ceiling),
        len(setup.tools),
        _cap_count(state, "psyche"),
        _cap_count(state, "skill"),
        _cap_count(state, "service"),
    )


def _model_count(
    setup: AgentSetup,
    state: AgentState,
    *,
    ceiling: CeilingSpec,
) -> int:
    _default, targets = agent_model_targets(setup, state, ceiling)
    return len(targets)


def _cap_count(state: AgentState, kind: str) -> int:
    return sum(1 for entry in state.caps if entry.kind == kind)


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
    state: AgentState,
    environ: Mapping[str, str],
) -> str:
    try:
        port = urlsplit(endpoint).port
    except ValueError:
        port = None
    base_url = resolve_ui_base_url(state.root_config, environ=environ).rstrip("/")
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


def _normalize_model_selectors(
    models: Sequence[str] | None,
) -> tuple[str, ...]:
    values = tuple(models) if models is not None else ()
    return tuple(dict.fromkeys(split_model_selectors(values)))


def _normalize_tool_selectors(
    tools: Sequence[str] | None,
) -> tuple[str, ...] | None:
    if tools is None:
        return None
    return tuple(dict.fromkeys(split_tool_selectors(tuple(tools))))


def _normalize_cap_selectors(
    caps: Sequence[str] | None,
) -> tuple[str, ...]:
    values = tuple(caps) if caps is not None else ()
    return tuple(dict.fromkeys(split_cap_selectors(values)))


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

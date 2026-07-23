"""Agent startup implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from importlib.metadata import version as package_version
import logging
import os
from pathlib import Path
import signal
import socket
import time
import threading
from types import FrameType
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI
import uvicorn
from uvicorn.main import STARTUP_FAILURE

from toolang.up import process as agents
from toolang.up.mounts import prepare_root_mounts
from toolang.state import state as cap_store
from toolang.base.protocols.model import ModelProvider
from toolang.base.protocols.sandbox import AgentSandbox
from toolang.base.types.message import Message
from toolang.base.types.model import ModelAlias
from toolang.base.types.sandbox import (
    SandboxSelector,
    SandboxStartRequest,
    SandboxState,
)
from toolang.plugin.tools.registry import split_tool_selectors
from toolang.up.logging import (
    DEFAULT_LOG_LEVEL,
    build_uvicorn_log_config,
    configure_logging,
    configure_logging_plan,
    resolve_agent_logging,
)
from toolang.plugin.config import (
    merge_named_configs,
    merge_sandbox_config,
    parse_channel_bindings,
    parse_sandbox_binding,
)
from toolang.common.env_logger import PY_LOG_ENV_VAR
from toolang.common.config import resolve_ui_base_url
from toolang.common.ids import allocate_run_id
from toolang.up.config import resolve_cors_allowed_origins
from toolang.execution.executor import Executor
from toolang.execution.reply import ReplySink
from toolang.execution.records import RunRecord
from toolang.execution.executor.request import ExecutableKind, RunRequest
from toolang.plugin.models.resolution import select_model_selectors
from toolang.execution.store import RunStore, run_store_path
from toolang.up.setup import AgentSetup
from toolang.work.scheduler import DEFAULT_INTERVAL_MS as DEFAULT_SCHEDULER_INTERVAL_MS
from toolang.work.scheduler import Scheduler
from toolang.work.store import open_job_store
from toolang.work.watcher import JobWatcher
from toolang.catalog.cap import AuthoredCaps
from toolang.catalog.config import WiredCaps
from toolang.catalog.job import AuthoredJobs
from toolang.api.app import ApiContext, create_app
from toolang.work import inbox as files
from toolang.up import channels as poll
from toolang.common.progress import ProgressSink
from toolang.state.source import read_authored_source
from toolang.state.state import AgentState
from toolang.state.prepare import prepare_agent_state
from toolang.state import watcher as state_watcher
from toolang.plugin.models.config import (
    parse_default_models,
    parse_model_aliases,
    parse_model_provider_configs,
)
from toolang.plugin.models.loading import load_model_adapters, load_model_providers
from toolang.plugin.models.resolution import split_model_selectors
from toolang.plugin.channels.loading import create_channel_plugin
from toolang.plugin.sandboxes.loading import create_sandbox_plugin
from toolang.plugin.tools.loading import (
    load_runtime_tools,
    select_tools,
    validate_tool_selectors,
)

DEFAULT_TRIGGER_INTERVAL_MS: dict[str, float] = {
    "file": files.DEFAULT_INTERVAL_MS,
    "pulse": DEFAULT_SCHEDULER_INTERVAL_MS,
    "poll": poll.DEFAULT_INTERVAL_MS,
}
DEFAULT_WATCH_DEBOUNCE_MS = state_watcher.DEFAULT_DEBOUNCE_MS
DEFAULT_FILE_STABLE_MS = files.DEFAULT_STABLE_MS
RUNTIME_SHUTDOWN_TASK_TIMEOUT_SEC = 1.0
UVICORN_GRACEFUL_SHUTDOWN_SEC = 1
AUTO_RUNTIME_PORT_MIN = 7001
AUTO_RUNTIME_PORT_MAX = 7999
logger = logging.getLogger("toolang.runtime")
state_logger = logging.getLogger("toolang.state")


@dataclass(frozen=True, slots=True)
class AgentHosting:
    """Resolved host driver and plugin inputs for one agent process."""

    plugin: AgentSandbox
    selector: SandboxSelector
    config: dict[str, object]


@dataclass(frozen=True, slots=True)
class StartupSpec:
    """One fully resolved agent startup request."""

    toolang_root: Path
    agent_name: str
    host: str
    endpoint_host: str
    port: int
    hosting: AgentHosting
    dev_artifact: Path | None
    model_selectors: tuple[str, ...]
    tool_selectors: tuple[str, ...] | None
    cap_selectors: tuple[str, ...]
    file_inboxes: tuple[Path, ...] = ()
    log_spec: str | None = None


@dataclass(frozen=True, slots=True)
class _StartupModelSelection:
    model_providers: Mapping[str, ModelProvider]
    model_aliases: Mapping[str, ModelAlias]
    default_models: tuple[str, ...]
    model_environ: Mapping[str, str]
    model_cache_dir: Path | None = None
    model_cache_refresh: bool = False


def up(
    *,
    toolang_root: Path,
    agent_name: str,
    host: str = "127.0.0.1",
    endpoint_host: str | None = None,
    port: int | None = None,
    sandbox: str | None = None,
    models: Sequence[str] | None = None,
    tools: Sequence[str] | None = None,
    caps: Sequence[str] | None = None,
    file_inboxes: Sequence[Path] | None = None,
    dev: Path | None = None,
    sandbox_child: bool = False,
    log_spec: str | None = None,
    environ: Mapping[str, str],
    progress: ProgressSink | None = None,
    wait: bool = False,
) -> int:
    """Start one agent runtime."""

    spec = resolve_startup(
        host=host,
        toolang_root=toolang_root,
        agent_name=agent_name,
        endpoint_host=endpoint_host,
        port=port,
        sandbox=sandbox,
        models=models,
        tools=tools,
        caps=caps,
        file_inboxes=file_inboxes,
        dev=dev,
        log_spec=log_spec,
        environ=environ,
    )
    return start_runtime(
        spec,
        environ=environ,
        sandbox_child=sandbox_child,
        progress=progress,
        wait=wait,
    )


def start_runtime(
    spec: StartupSpec,
    *,
    environ: Mapping[str, str],
    sandbox_child: bool = False,
    progress: ProgressSink | None = None,
    agent_state: AgentState | None = None,
    wait: bool = False,
) -> int:
    """Start one already resolved agent runtime."""

    _restore_termination_signal_defaults()
    if spec.hosting.selector.driver != "none":
        return _up_managed_sandbox(
            plugin=spec.hosting.plugin,
            selector=spec.hosting.selector,
            sandbox_config=spec.hosting.config,
            toolang_root=spec.toolang_root,
            agent_name=spec.agent_name,
            host=spec.host,
            endpoint_host=spec.endpoint_host,
            port=spec.port,
            environ=environ,
            dev_artifact=spec.dev_artifact,
            model_selectors=spec.model_selectors,
            tool_selectors=spec.tool_selectors,
            cap_selectors=spec.cap_selectors,
            file_inboxes=spec.file_inboxes,
            wait=wait,
        )
    return _up_local(
        toolang_root=spec.toolang_root,
        agent_name=spec.agent_name,
        host=spec.host,
        endpoint_host=spec.endpoint_host,
        port=spec.port,
        environ=environ,
        sandbox_child=sandbox_child,
        model_selectors=spec.model_selectors,
        tool_selectors=spec.tool_selectors,
        cap_selectors=spec.cap_selectors,
        file_inboxes=spec.file_inboxes,
        log_spec=spec.log_spec,
        progress=progress,
        agent_state=agent_state,
    )


def _restore_termination_signal_defaults() -> None:
    """Reset ignored termination signals before the runtime installs handlers."""

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            if signal.getsignal(signum) == signal.SIG_IGN:
                signal.signal(signum, signal.SIG_DFL)
        except (OSError, RuntimeError, ValueError):
            continue


def invoke(
    *,
    toolang_root: Path,
    agent_name: str,
    executable_kind: ExecutableKind = "agic",
    executable_name: str | None = None,
    input_text: str | None = None,
    models: Sequence[str] | None = None,
    tools: Sequence[str] | None = (),
    caps: Sequence[str] | None = None,
    metadata: Mapping[str, object] | None = None,
    environ: Mapping[str, str],
    reply: ReplySink | None = None,
    log_spec: str | None = None,
    agent_state: AgentState | None = None,
) -> RunRecord:
    """Execute one agic or flow without starting the long-lived runtime."""

    invoke_environ = dict(environ)
    if log_spec is not None:
        invoke_environ[PY_LOG_ENV_VAR] = log_spec
    state = agent_state or prepare_agent(
        toolang_root=toolang_root, agent_name=agent_name
    )
    executor, watcher = assemble_execution(
        toolang_root=toolang_root,
        agent_name=agent_name,
        environ=invoke_environ,
        model_selectors=_normalize_model_selectors(models),
        tool_selectors=_normalize_tool_selectors(tools),
        cap_selectors=_normalize_cap_selectors(caps),
        agent_state=state,
    )
    run_id = allocate_run_id(executor.id_state_path)
    log_plan = resolve_agent_logging(
        mode="invoke",
        environ=invoke_environ,
        run_log_path=agents.agent_script_run_log_path(
            toolang_root,
            agent_name,
            executable_name=executable_name,
            run_id=run_id,
        ),
    )
    if log_plan.destination != "none":
        configure_logging_plan(log_plan)
        if log_plan.path is not None:
            log_plan.path.touch(exist_ok=True)
    try:
        return asyncio.run(
            executor.run(
                RunRequest(
                    origin="script",
                    input=Message.user(input_text or ""),
                    run_id=run_id,
                    executable_kind=executable_kind,
                    executable_name=executable_name,
                    context=dict(metadata or {}),
                ),
                watcher.current(),
                reply=reply,
            )
        )
    finally:
        asyncio.run(executor.shutdown())
        executor.store.close()


def prepare_agent(
    *,
    toolang_root: Path,
    agent_name: str,
    force: bool = False,
    progress: ProgressSink | None = None,
) -> AgentState:
    """Prepare one agent for either long-lived startup or one-shot execution."""

    return prepare_agent_state(
        toolang_root,
        agent_name,
        toolang_version=package_version("toolang"),
        force=force,
        progress=progress,
    )


def resolve_startup(
    *,
    toolang_root: Path,
    agent_name: str,
    host: str = "127.0.0.1",
    endpoint_host: str | None = None,
    port: int | None = None,
    sandbox: str | None = None,
    models: Sequence[str] | None = None,
    tools: Sequence[str] | None = None,
    caps: Sequence[str] | None = None,
    file_inboxes: Sequence[Path] | None = None,
    dev: Path | None = None,
    log_spec: str | None = None,
    temporary_port: bool = False,
    environ: Mapping[str, str],
    agent_state: AgentState | None = None,
) -> StartupSpec:
    """Resolve one explicit startup request into stable runtime inputs."""

    endpoint_host = endpoint_host or _default_endpoint_host(host)
    resolved_port = resolve_runtime_port(
        host=host,
        explicit_port=port,
        toolang_root=toolang_root,
        agent_name=agent_name,
        temporary=temporary_port,
    )
    state = agent_state or prepare_agent(
        toolang_root=toolang_root,
        agent_name=agent_name,
    )
    hosting = resolve_agent_hosting(
        state,
        sandbox=sandbox,
        environ=environ,
    )
    dev_artifact = _resolve_dev_artifact(dev) if dev is not None else None
    model_selectors = _normalize_model_selectors(models)
    tool_selectors = _normalize_tool_selectors(tools)
    cap_selectors = _normalize_cap_selectors(caps)
    resolved_file_inboxes = _normalize_file_inboxes(file_inboxes)
    _validate_startup_models(
        toolang_root=toolang_root,
        agent_name=agent_name,
        selectors=model_selectors,
        environ=environ,
        agent_state=state,
    )
    if resolved_file_inboxes:
        _validate_file_agic(toolang_root=toolang_root, agent_name=agent_name)
    return StartupSpec(
        toolang_root=toolang_root,
        agent_name=agent_name,
        host=host,
        endpoint_host=endpoint_host,
        port=resolved_port,
        hosting=hosting,
        dev_artifact=dev_artifact,
        model_selectors=model_selectors,
        tool_selectors=tool_selectors,
        cap_selectors=cap_selectors,
        file_inboxes=resolved_file_inboxes,
        log_spec=log_spec.strip()
        if isinstance(log_spec, str) and log_spec.strip()
        else None,
    )


def resolve_agent_hosting(
    state: AgentState,
    *,
    sandbox: str | None,
    environ: Mapping[str, str],
) -> AgentHosting:
    """Resolve one explicit or configured agent hosting driver."""

    sandbox_binding = parse_sandbox_binding(
        merge_sandbox_config(_config_layers(state), environ=environ)
    )
    if sandbox is not None:
        value = sandbox.strip()
        if not value:
            raise ValueError("sandbox selector cannot be empty")
        sandbox_driver, _, _ = value.partition(":")
        sandbox_driver = sandbox_driver.strip()
        if not sandbox_driver:
            raise ValueError("sandbox selector is missing driver")
    elif sandbox_binding is not None:
        sandbox_driver = sandbox_binding.selector.driver
    else:
        sandbox_driver = "none"
    sandbox_config = (
        dict(sandbox_binding.config)
        if sandbox_binding is not None
        and sandbox_binding.selector.driver == sandbox_driver
        else {}
    )
    sandbox_plugin = create_sandbox_plugin(sandbox_driver, config=sandbox_config)
    selector = sandbox_plugin.resolve_selector(
        sandbox,
        configured_selector=(
            sandbox_binding.selector
            if sandbox_binding is not None
            and sandbox_binding.selector.driver == sandbox_driver
            else None
        ),
    )
    return AgentHosting(
        plugin=sandbox_plugin,
        selector=selector,
        config=sandbox_config,
    )


def _normalize_file_inboxes(file_inboxes: Sequence[Path] | None) -> tuple[Path, ...]:
    if file_inboxes is None:
        return ()
    inboxes: list[Path] = []
    for raw_path in file_inboxes:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_dir():
            raise FileNotFoundError(f"inbox not found: {path}")
        if path not in inboxes:
            inboxes.append(path)
    return tuple(inboxes)


def _validate_file_agic(*, toolang_root: Path, agent_name: str) -> None:
    program = read_authored_source(toolang_root, agent_name).load_program().parse()
    agic = program.find_agic("file")
    if agic is None:
        raise ValueError("file agic not found")
    if agic.input is None:
        raise ValueError("file agic must accept message input")
    missing_params = [param.name for param in agic.params if not param.optional]
    if missing_params:
        joined = ", ".join(f"{name}=..." for name in missing_params)
        raise ValueError(f"file agic cannot have required parameters: {joined}")


def _config_layers(
    state: AgentState,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    return state.root_config, state.home_config


def load_model_aliases(
    toolang_root: Path,
    agent_name: str,
    *,
    agent_state: AgentState | None = None,
) -> dict[str, ModelAlias]:
    """Load model aliases at the agent composition boundary."""

    state = agent_state or prepare_agent(
        toolang_root=toolang_root, agent_name=agent_name
    )
    return parse_model_aliases(_config_layers(state))


def load_default_models(
    toolang_root: Path,
    agent_name: str,
    *,
    agent_state: AgentState | None = None,
) -> tuple[str, ...]:
    """Load default model selectors at the agent composition boundary."""

    state = agent_state or prepare_agent(
        toolang_root=toolang_root, agent_name=agent_name
    )
    return parse_default_models(_config_layers(state))


def _load_model_providers(
    toolang_root: Path,
    agent_name: str,
    *,
    agent_state: AgentState | None = None,
) -> dict[str, ModelProvider]:
    state = agent_state or prepare_agent(
        toolang_root=toolang_root, agent_name=agent_name
    )
    config_layers = _config_layers(state)
    return load_model_providers(parse_model_provider_configs(config_layers))


def _validate_startup_models(
    *,
    toolang_root: Path,
    agent_name: str,
    selectors: Sequence[str],
    environ: Mapping[str, str],
    agent_state: AgentState,
) -> None:
    context = _StartupModelSelection(
        model_providers=_load_model_providers(
            toolang_root, agent_name, agent_state=agent_state
        ),
        model_aliases=load_model_aliases(
            toolang_root, agent_name, agent_state=agent_state
        ),
        default_models=load_default_models(
            toolang_root, agent_name, agent_state=agent_state
        ),
        model_environ=environ,
        model_cache_dir=toolang_root / ".runtime" / "model-cache",
    )
    if not selectors:
        select_model_selectors(context)
        return
    _validate_model_selectors(context, selectors)


def build_run_argv(
    spec: StartupSpec,
    *,
    root: Path | None = None,
    host: str | None = None,
    endpoint_host: str | None = None,
    sandbox: str | None = None,
    models: Sequence[str] | None = None,
    tools: Sequence[str] | None = None,
    caps: Sequence[str] | None = None,
    sandbox_child: bool = False,
    background_hosting: bool = False,
) -> tuple[str, ...]:
    """Build one explicit argv for the hidden managed-runtime run path."""

    command: list[str] = []
    command.extend(
        [
            "--root",
            str(root or spec.toolang_root),
            "run",
            spec.agent_name,
            "--host",
            host or spec.host,
            "--endpoint-host",
            endpoint_host or spec.endpoint_host,
            "--port",
            str(spec.port),
            "--sandbox",
            sandbox or spec.hosting.selector.render(),
        ]
    )
    effective_models = _normalize_model_selectors(models) or spec.model_selectors
    for selector in effective_models:
        command.extend(["--models", selector])
    effective_tools = _normalize_tool_selectors(tools)
    if effective_tools is None:
        effective_tools = spec.tool_selectors
    for selector in effective_tools or ():
        command.extend(["--tools", selector])
    effective_caps = _normalize_cap_selectors(caps) or spec.cap_selectors
    for selector in effective_caps:
        command.extend(["--caps", selector])
    if spec.dev_artifact is not None and not sandbox_child:
        command.extend(["--dev", str(spec.dev_artifact)])
    if sandbox_child:
        command.append("--sandbox-child")
    if background_hosting:
        command.append("--background-hosting")
    for inbox in spec.file_inboxes:
        command.extend(["--inbox", str(inbox)])
    return tuple(command)


def _default_endpoint_host(host: str) -> str:
    if host == "127.0.0.1":
        return "localhost"
    return host


def _up_local(
    *,
    toolang_root: Path,
    agent_name: str,
    host: str,
    endpoint_host: str,
    port: int,
    environ: Mapping[str, str],
    sandbox_child: bool,
    model_selectors: tuple[str, ...],
    tool_selectors: tuple[str, ...] | None,
    cap_selectors: tuple[str, ...],
    log_spec: str | None = None,
    file_inboxes: tuple[Path, ...] = (),
    progress: ProgressSink | None = None,
    agent_state: AgentState | None = None,
) -> int:
    runtime_log_spec = _runtime_log_spec_value(log_spec, environ)
    configure_logging(spec=runtime_log_spec, environ=environ)
    for name, interval_ms in DEFAULT_TRIGGER_INTERVAL_MS.items():
        if interval_ms <= 0:
            raise ValueError(f"trigger interval must be positive: {name}")
    prepared_state = agent_state or prepare_agent(
        toolang_root=toolang_root,
        agent_name=agent_name,
        progress=progress,
    )
    cors_allowed_origins = resolve_cors_allowed_origins(
        prepared_state.root_config,
        environ=environ,
    )
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    executor, watcher = assemble_execution(
        toolang_root=toolang_root,
        agent_name=agent_name,
        environ=environ,
        model_selectors=model_selectors,
        tool_selectors=tool_selectors,
        cap_selectors=cap_selectors,
        progress=progress,
        agent_state=prepared_state,
    )
    state = watcher.current()
    _log_state_loaded(executor, state)
    executor.store.append_update(
        kind="started",
        payload={
            "state_fingerprint": state.fingerprint,
        },
        created_at=started_at,
    )
    endpoint = f"http://{endpoint_host}:{port}"
    shutdown_signal = threading.Event()
    channel_bindings = parse_channel_bindings(
        merge_named_configs(
            _config_layers(state),
            section="channels",
            environ=environ,
        )
    )
    channel_plugins = {
        name: create_channel_plugin(binding.plugin, config=binding.config)
        for name, binding in channel_bindings.items()
    }
    context = ApiContext(
        root=toolang_root,
        name=agent_name,
        home=executor.home,
        executor=executor,
        state_watcher=watcher,
        authored_jobs=AuthoredJobs(executor.home),
        private_authored_caps=AuthoredCaps(executor.home),
        shared_authored_caps=AuthoredCaps(toolang_root),
        private_wired_caps=WiredCaps(executor.home / "config.toml"),
        shared_wired_caps=WiredCaps(toolang_root / "config.toml"),
        host=host,
        port=port,
        cors_allowed_origins=cors_allowed_origins,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_signal = asyncio.Event()
        bg_tasks: list[asyncio.Task[None]] = []
        job_store = None
        try:
            if not sandbox_child:
                agents.write_runtime_state(
                    toolang_root,
                    agent_name,
                    endpoint=endpoint,
                    started_at=started_at,
                    pid=os.getpid(),
                    models=model_selectors,
                )
            job_store = open_job_store(toolang_root, agent_name)
            job_watcher = JobWatcher(toolang_root, agent_name)
            scheduler = Scheduler(
                job_store=job_store,
                executor=executor,
                get_home_jobs=job_watcher.current,
                get_agent_state=watcher.current,
                kinds=("task", "chore"),
                interval_ms=DEFAULT_TRIGGER_INTERVAL_MS["pulse"],
            )
            bg_tasks.extend(
                [
                    job_watcher.start(stop_signal=stop_signal),
                    scheduler.start(stop_signal=stop_signal),
                    poll.spawn(
                        name=agent_name,
                        home=executor.home,
                        bindings=channel_bindings,
                        plugins=channel_plugins,
                        executor=executor,
                        get_agent_state=watcher.current,
                        interval_ms=DEFAULT_TRIGGER_INTERVAL_MS["poll"],
                        stop_signal=stop_signal,
                    ),
                ]
            )
            bg_tasks.append(
                asyncio.create_task(
                    watcher.run(
                        stop_signal=stop_signal,
                        interval_ms=state_watcher.DEFAULT_INTERVAL_MS,
                        debounce_ms=DEFAULT_WATCH_DEBOUNCE_MS,
                    )
                )
            )
            if file_inboxes:
                bg_tasks.append(
                    files.spawn(
                        root=toolang_root,
                        name=agent_name,
                        executor=executor,
                        get_agent_state=watcher.current,
                        inboxes=file_inboxes,
                        interval_ms=DEFAULT_TRIGGER_INTERVAL_MS["file"],
                        stable_ms=DEFAULT_FILE_STABLE_MS,
                        stop_signal=stop_signal,
                    )
                )

            yield
        finally:
            if not sandbox_child:
                agents.stop_runtime_state(
                    toolang_root,
                    agent_name,
                    expected_pid=os.getpid(),
                    expected_started_at=started_at,
                )
            stop_signal.set()
            for task in tuple(context.run_tasks):
                task.cancel()
            shutdown_tasks: list[asyncio.Task[Any]] = [
                *bg_tasks,
                *context.run_tasks,
            ]
            await _finish_runtime_tasks(shutdown_tasks)
            if job_store is not None:
                job_store.close()
            executor.store.append_update(
                kind="stopped",
                payload={
                    "outcome": "stopped",
                },
            )
            await executor.shutdown()
            executor.store.close()

    app = create_app(context, lifespan=lifespan, shutdown_signal=shutdown_signal)
    webui_url = _runtime_webui_url(endpoint, state=state, environ=environ)
    _run_uvicorn_app(
        app,
        host=host,
        port=port,
        log_config=build_uvicorn_log_config(
            level=runtime_log_spec or DEFAULT_LOG_LEVEL
        ),
        shutdown_signal=shutdown_signal,
        on_starting=lambda: logger.info(
            "Agent starting root=%s",
            toolang_root,
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


def _runtime_log_spec_value(
    log_spec: str | None, environ: Mapping[str, str]
) -> str | None:
    if isinstance(log_spec, str) and log_spec.strip():
        return log_spec.strip()
    env_spec = environ.get(PY_LOG_ENV_VAR, "").strip()
    return env_spec or None


def _log_state_loaded(executor: Executor, state: AgentState) -> None:
    state_logger.info(
        "Agent loaded state=%s models=%s tools=%s psyches=%s skills=%s services=%s",
        _short_fingerprint(state.fingerprint),
        _model_count(executor),
        len(executor.setup.tools),
        _cap_count(state, "psyche"),
        _cap_count(state, "skill"),
        _cap_count(state, "service"),
    )


def _model_count(executor: Executor) -> int:
    try:
        selectors = executor.allowed_model_selectors
        if selectors:
            return len(select_model_selectors(executor, activation_selectors=selectors))
        return len(select_model_selectors(executor))
    except Exception:
        return len(executor.allowed_model_selectors)


def _cap_count(state: AgentState, kind: str) -> int:
    return sum(1 for entry in state.caps if entry.kind == kind)


def _short_fingerprint(value: str) -> str:
    return value[:12]


class _ToolangServer(uvicorn.Server):
    """Uvicorn server with one runtime-visible shutdown signal."""

    def __init__(
        self,
        config: uvicorn.Config,
        *,
        shutdown_signal: threading.Event,
        on_running: Callable[[], None] | None = None,
        on_stopping: Callable[[], None] | None = None,
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
    on_starting: Callable[[], None] | None = None,
    on_running: Callable[[], None] | None = None,
    on_stopping: Callable[[], None] | None = None,
    on_stopped: Callable[[], None] | None = None,
) -> None:
    """Run one FastAPI app with signal-aware shutdown for long-lived streams."""

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_config=log_config,
        timeout_graceful_shutdown=UVICORN_GRACEFUL_SHUTDOWN_SEC,
    )
    server = _ToolangServer(
        config=config,
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
    """Let runtime tasks stop cooperatively, then cancel anything stuck."""

    if not tasks:
        return
    done, pending = await asyncio.wait(tasks, timeout=timeout_sec)
    for task in pending:
        task.cancel()
    if pending:
        done_after_cancel, pending_after_cancel = await asyncio.wait(
            pending, timeout=timeout_sec
        )
        done |= done_after_cancel
        for task in pending_after_cancel:
            logger.warning("runtime task did not stop after cancellation task=%r", task)
    for task in done:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("runtime task failed during shutdown", exc_info=True)


def assemble_execution(
    *,
    toolang_root: Path,
    agent_name: str,
    environ: Mapping[str, str],
    model_selectors: Sequence[str] = (),
    tool_selectors: Sequence[str] | None = None,
    cap_selectors: Sequence[str] = (),
    progress: ProgressSink | None = None,
    agent_state: AgentState | None = None,
) -> tuple[Executor, state_watcher.StateWatcher]:
    """Assemble one executor and its versioned agent state."""
    if agent_state is None:
        agent_state = prepare_agent(
            toolang_root=toolang_root, agent_name=agent_name, progress=progress
        )
    state = agent_state
    normalized_model_selectors = _normalize_model_selectors(model_selectors)
    normalized_tool_selectors = _normalize_tool_selectors(tool_selectors)
    normalized_cap_selectors = _normalize_cap_selectors(cap_selectors)
    model_providers = _load_model_providers(toolang_root, agent_name, agent_state=state)
    model_aliases = load_model_aliases(toolang_root, agent_name, agent_state=state)
    default_models = load_default_models(toolang_root, agent_name, agent_state=state)
    _validate_model_selectors(
        _StartupModelSelection(
            model_providers=model_providers,
            model_aliases=model_aliases,
            default_models=default_models,
            model_environ=environ,
            model_cache_dir=toolang_root / ".runtime" / "model-cache",
        ),
        normalized_model_selectors,
    )
    _validate_cap_selectors(state, normalized_cap_selectors, agent_name=agent_name)
    state = _select_agent_caps(state, normalized_cap_selectors, agent_name=agent_name)
    default_model_selector = (
        normalized_model_selectors[0] if normalized_model_selectors else None
    )
    store = RunStore(run_store_path(toolang_root, agent_name))
    tools = load_runtime_tools(
        plugin_config=merge_named_configs(
            _config_layers(state),
            section="tools",
            environ=environ,
        ),
    )
    selected_tools = select_tools(tools, normalized_tool_selectors)
    validate_tool_selectors(tools, normalized_tool_selectors)
    setup = AgentSetup(
        name=agent_name,
        home=agents.agent_home(toolang_root, agent_name),
        tools=selected_tools,
        model_providers=model_providers,
        model_adapters=load_model_adapters(),
        model_environ=environ,
        model_selectors=normalized_model_selectors,
        model_cache_dir=toolang_root / ".runtime" / "model-cache",
    )
    executor = Executor(
        root=toolang_root,
        name=agent_name,
        home=agents.agent_home(toolang_root, agent_name),
        id_state_path=agents.agent_id_state_path(toolang_root, agent_name),
        setup=setup,
        store=store,
        model_aliases=model_aliases,
        default_models=default_models,
        model_environ=environ,
        default_model_selector=default_model_selector,
        allowed_model_selectors=normalized_model_selectors,
    )
    watcher = state_watcher.StateWatcher(
        toolang_root,
        agent_name,
        state,
        transform=lambda value: _select_agent_caps(
            value, normalized_cap_selectors, agent_name=agent_name
        ),
    )
    return executor, watcher


def _runtime_webui_url(
    endpoint: str,
    *,
    state: AgentState,
    environ: Mapping[str, str],
) -> str:
    try:
        endpoint_port = urlsplit(endpoint).port
    except ValueError:
        endpoint_port = None
    base_url = resolve_ui_base_url(state.root_config, environ=environ).rstrip("/")
    if endpoint_port is None:
        return base_url
    return f"{base_url}/{endpoint_port}"


def _up_managed_sandbox(
    *,
    plugin: AgentSandbox,
    selector: SandboxSelector,
    sandbox_config: Mapping[str, object],
    toolang_root: Path,
    agent_name: str,
    host: str,
    endpoint_host: str,
    port: int,
    environ: Mapping[str, str],
    dev_artifact: Path | None,
    model_selectors: tuple[str, ...],
    tool_selectors: tuple[str, ...] | None,
    cap_selectors: tuple[str, ...],
    file_inboxes: tuple[Path, ...],
    wait: bool,
) -> int:
    endpoint = f"http://{endpoint_host}:{port}"
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    initial_sandbox_state = SandboxState(selector=selector).to_data()
    startup = StartupSpec(
        toolang_root=toolang_root,
        agent_name=agent_name,
        host=host,
        endpoint_host=endpoint_host,
        port=port,
        hosting=AgentHosting(
            plugin=plugin,
            selector=selector,
            config=dict(sandbox_config),
        ),
        dev_artifact=dev_artifact,
        model_selectors=model_selectors,
        tool_selectors=tool_selectors,
        cap_selectors=cap_selectors,
        file_inboxes=file_inboxes,
    )
    agents.write_runtime_state(
        toolang_root,
        agent_name,
        endpoint=endpoint,
        started_at=started_at,
        pid=os.getpid(),
        sandbox=initial_sandbox_state,
        models=model_selectors,
        status="preparing",
    )
    raw_sandbox_root = sandbox_config.get("sandbox_root")
    if not isinstance(raw_sandbox_root, str) or not raw_sandbox_root.strip():
        raw_sandbox_root = sandbox_config.get("root")
    if isinstance(raw_sandbox_root, str) and raw_sandbox_root.strip():
        sandbox_root = Path(raw_sandbox_root.strip())
    elif selector.driver == "none":
        raise ValueError(
            "sandbox root must be resolved by the local runtime for none sandbox"
        )
    else:
        sandbox_root = Path("/root/.toolang")
    sandbox_home = sandbox_root / "agents" / agent_name
    request = SandboxStartRequest(
        selector=selector,
        local_root=toolang_root,
        local_home=agents.agent_home(toolang_root, agent_name),
        sandbox_root=sandbox_root,
        sandbox_home=sandbox_home,
        agent_name=agent_name,
        bind_host=host,
        endpoint_host=endpoint_host,
        port=port,
        endpoint=endpoint,
        run_command=(
            "toolang",
            *build_run_argv(
                startup,
                root=sandbox_root,
                host="0.0.0.0",
                sandbox="none",
                sandbox_child=True,
            ),
        ),
        env_vars=dict(environ),
        mounts=prepare_root_mounts(toolang_root, sandbox_root),
        local_dev_artifact=dev_artifact,
    )
    plan = None
    start = None
    try:
        plan = plugin.prepare(request)
        agents.write_runtime_state(
            toolang_root,
            agent_name,
            endpoint=endpoint,
            started_at=started_at,
            pid=os.getpid(),
            sandbox=plan.state.to_data()
            if plan.state is not None
            else initial_sandbox_state,
            models=model_selectors,
            status="starting",
        )
        start = plugin.start(plan)
        _wait_for_sandbox_ready(
            plugin=plugin,
            state=start.state,
            host=host,
            port=port,
            timeout_sec=30.0,
            stable_sec=1.0,
        )
    except Exception as exc:
        cleanup_state = (
            start.state
            if start is not None
            else plan.state
            if plan is not None
            else None
        )
        if cleanup_state is not None:
            try:
                plugin.stop(cleanup_state, force=True)
            except Exception:
                logger.exception(
                    "Failed to clean up sandbox after startup error agent=%s sandbox=%s",
                    agent_name,
                    selector.render(),
                )
        failed_endpoint = (
            start.endpoint if start is not None and start.endpoint else endpoint
        )
        failed_sandbox_state = (
            start.state.to_data()
            if start is not None
            else (
                plan.state.to_data()
                if plan is not None and plan.state is not None
                else initial_sandbox_state
            )
        )
        agents.write_runtime_state(
            toolang_root,
            agent_name,
            endpoint=failed_endpoint,
            started_at=started_at,
            pid=os.getpid(),
            sandbox=failed_sandbox_state,
            models=model_selectors,
            status="failed",
            message=str(exc),
        )
        raise
    agents.write_runtime_state(
        toolang_root,
        agent_name,
        endpoint=start.endpoint or endpoint,
        started_at=started_at,
        pid=None,
        sandbox=start.state.to_data(),
        models=model_selectors,
        status="running",
    )
    logger.info(
        "Sandbox ready agent=%s sandbox=%s endpoint=%s",
        agent_name,
        selector.render(),
        start.endpoint or endpoint,
    )
    if wait:
        try:
            result = _wait_for_managed_sandbox(plugin, start.state)
        except KeyboardInterrupt:
            result = 130
        finally:
            plugin.stop(start.state)
            agents.stop_runtime_state(
                toolang_root,
                agent_name,
                expected_started_at=started_at,
            )
        return result
    return 0


def _wait_for_managed_sandbox(
    plugin: AgentSandbox,
    state: SandboxState,
) -> int:
    """Wait in the foreground while preserving managed-sandbox cleanup."""

    termination_signal: int | None = None
    previous_handlers: dict[int, object] = {}

    def request_termination(signum: int, _frame: object) -> None:
        nonlocal termination_signal
        termination_signal = signum

    try:
        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_termination)
            except (OSError, RuntimeError, ValueError):
                previous_handlers.pop(signum, None)
        while termination_signal is None and plugin.alive(state):
            time.sleep(0.25)
        return 128 + termination_signal if termination_signal is not None else 0
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, RuntimeError, ValueError):
                continue


def _normalize_model_selectors(models: Sequence[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in split_model_selectors(tuple(models or ())):
        selector = raw.strip()
        if not selector:
            raise ValueError("model selector cannot be empty")
        if selector in seen:
            continue
        seen.add(selector)
        result.append(selector)
    return tuple(result)


def _normalize_tool_selectors(tools: Sequence[str] | None) -> tuple[str, ...] | None:
    if tools is None:
        return None
    result: list[str] = []
    seen: set[str] = set()
    for raw in split_tool_selectors(tuple(tools)):
        selector = raw.strip()
        if not selector:
            raise ValueError("tool selector cannot be empty")
        if selector in seen:
            continue
        seen.add(selector)
        result.append(selector)
    return tuple(result)


def _normalize_cap_selectors(caps: Sequence[str] | None) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in cap_store.split_cap_selectors(tuple(caps or ())):
        selector = raw.strip()
        if not selector:
            raise ValueError("cap selector cannot be empty")
        if selector in seen:
            continue
        seen.add(selector)
        result.append(selector)
    return tuple(result)


def _select_agent_caps(
    state: AgentState, selectors: Sequence[str], *, agent_name: str
) -> AgentState:
    if not selectors:
        return state
    return replace(
        state,
        caps=cap_store.select_cap_entries(
            state.caps,
            tuple(selectors),
            agent_name=agent_name,
        ),
    )


def _validate_model_selectors(
    context: _StartupModelSelection, selectors: Sequence[str]
) -> None:
    if not selectors:
        select_model_selectors(context)
        return
    select_model_selectors(context, activation_selectors=selectors)
    for selector in selectors:
        select_model_selectors(context, activation_selectors=(selector,))


def _validate_cap_selectors(
    state: AgentState, selectors: Sequence[str], *, agent_name: str
) -> None:
    if not selectors:
        return
    missing = [
        selector
        for selector in selectors
        if not cap_store.select_cap_entries(
            state.caps,
            (selector,),
            agent_name=agent_name,
        )
    ]
    if missing:
        raise ValueError(f"cap selector matched no caps: {', '.join(missing)}")


def _pick_runtime_port(
    host: str,
    *,
    toolang_root: Path,
    agent_name: str,
    preferred_port: int | None = None,
) -> int:
    tried = agents.assigned_runtime_ports(
        toolang_root,
        exclude_agent=agent_name,
    )
    if preferred_port is not None:
        tried.add(preferred_port)
    for candidate in range(AUTO_RUNTIME_PORT_MIN, AUTO_RUNTIME_PORT_MAX + 1):
        if candidate in tried:
            continue
        if _port_is_available(host, candidate):
            return candidate
    raise ValueError(
        "no available runtime port in Toolang auto range "
        f"{AUTO_RUNTIME_PORT_MIN}-{AUTO_RUNTIME_PORT_MAX}; pass --port"
    )


def resolve_runtime_port(
    *,
    host: str,
    explicit_port: int | None,
    toolang_root: Path,
    agent_name: str,
    temporary: bool = False,
) -> int:
    if explicit_port is not None:
        return explicit_port
    preferred_port = agents.preferred_runtime_port(toolang_root, agent_name)
    if preferred_port is not None:
        if _port_is_available(host, preferred_port):
            return preferred_port
    if temporary:
        return _pick_temporary_runtime_port(host)
    return _pick_runtime_port(
        host,
        toolang_root=toolang_root,
        agent_name=agent_name,
        preferred_port=preferred_port,
    )


def _pick_temporary_runtime_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _port_is_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _wait_for_port_available(host: str, port: int, *, timeout_sec: float) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if _port_is_available(host, port):
            return True
        time.sleep(0.05)
    return _port_is_available(host, port)


def _resolve_dev_artifact(raw: Path) -> Path:
    candidate = raw.expanduser().resolve()
    if candidate.is_file():
        if candidate.suffix == ".whl":
            return candidate
        raise ValueError(
            f"dev path must be a wheel file or directory containing wheels: {candidate}"
        )
    if not candidate.is_dir():
        raise FileNotFoundError(f"dev path not found: {candidate}")
    wheels = sorted(
        (item for item in candidate.rglob("*.whl") if item.is_file()),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if not wheels:
        raise FileNotFoundError(f"no wheel files found in: {candidate}")
    return wheels[0]


def _wait_for_sandbox_ready(
    *,
    plugin,
    state,
    host: str,
    port: int,
    timeout_sec: float,
    stable_sec: float,
) -> None:
    deadline = time.monotonic() + timeout_sec
    ready_since: float | None = None
    while time.monotonic() < deadline:
        if not plugin.alive(state):
            raise ValueError(
                f"sandbox exited before becoming ready: http://{host}:{port}"
            )
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            reachable = sock.connect_ex((host, port)) == 0
        if reachable:
            now = time.monotonic()
            if ready_since is None:
                ready_since = now
            elif now - ready_since >= stable_sec:
                return
        else:
            ready_since = None
        time.sleep(0.1)
    raise ValueError(f"sandbox endpoint did not become ready: http://{host}:{port}")

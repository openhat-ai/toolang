"""Agent startup implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
import logging
import os
from pathlib import Path
import signal
import socket
import time
import threading
from types import FrameType
from typing import Any, cast
from urllib.parse import urlsplit

from fastapi import FastAPI
import uvicorn
from uvicorn.main import STARTUP_FAILURE

from toolang.agent import local as agents
from toolang.agent.sandbox import prepare_root_mounts
from toolang.state import caps as cap_store
from toolang.base.protocols.model import ModelProvider
from toolang.base.protocols.sandbox import AgentSandbox
from toolang.base.types.model import ModelAlias
from toolang.base.types.sandbox import (
    SandboxSelector,
    SandboxStartRequest,
    SandboxState,
)
from toolang.plugin.tools.registry import split_tool_selectors
from toolang.config.log import (
    DEFAULT_LOG_LEVEL,
    build_uvicorn_log_config,
    configure_logging,
    configure_logging_plan,
    resolve_agent_logging,
)
from toolang.config.files import (
    load_config_layers,
    load_named_config,
    load_sandbox_config,
)
from toolang.plugin.config import parse_channel_bindings, parse_sandbox_binding
from toolang.config.log_spec import PY_LOG_ENV_VAR
from toolang.config.web import resolve_cors_allowed_origins, resolve_ui_base_url
from toolang.execution.executor import Executor
from toolang.execution.reply import ReplySink
from toolang.execution.records import RunRecord
from toolang.execution.request import ExecutableKind, RunRequest
from toolang.plugin.models.resolution import select_model_selectors
from toolang.execution.store import RunStore, run_store_path
from toolang.execution.setup import AgentSetup
from toolang.work.scheduler import DEFAULT_INTERVAL_MS as DEFAULT_SCHEDULER_INTERVAL_MS
from toolang.work.scheduler import Scheduler
from toolang.work.store import open_job_store
from toolang.work.watcher import JobWatcher
from toolang.api.app import create_app
from toolang.agent.features import (
    DEFAULT_ENABLED_COMPONENTS,
    RUNNER_COMPONENTS,
    TRIGGER_COMPONENTS,
    ComponentName,
    component_group,
    format_component_group,
    normalize_component_names,
)
from toolang.api.context import ApiContext
from toolang.config.runtime import RuntimeConfig
from toolang.work import inbox as files
from toolang.agent import channel_runtime as poll
from toolang.agent import state_updates as watch
from toolang.common.progress import ProgressSink
from toolang.state.durable import scan_durable_state
from toolang.state.agent import AgentState, load_agent_state
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
    "watch": state_watcher.DEFAULT_INTERVAL_MS,
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
class StartupSpec:
    """One fully resolved agent startup request."""

    toolang_root: Path
    agent_name: str
    host: str
    endpoint_host: str
    port: int
    enabled_components: tuple[ComponentName, ...]
    sandbox_plugin: AgentSandbox
    selector: SandboxSelector
    sandbox_config: dict[str, object]
    dev_artifact: Path | None
    model_selectors: tuple[str, ...]
    tool_selectors: tuple[str, ...] | None
    cap_selectors: tuple[str, ...]
    file_inboxes: tuple[Path, ...] = ()
    log_spec: str | None = None

    @property
    def enabled_features(self) -> tuple[ComponentName, ...]:
        return self.enabled_components


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
    component_names: Sequence[str] | None = None,
    feature_names: Sequence[str] | None = None,
    log_spec: str | None = None,
    environ: Mapping[str, str],
    progress: ProgressSink | None = None,
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
        component_names=component_names,
        feature_names=feature_names,
        log_spec=log_spec,
        environ=environ,
    )
    return start_runtime(
        spec,
        environ=environ,
        sandbox_child=sandbox_child,
        progress=progress,
    )


def start_runtime(
    spec: StartupSpec,
    *,
    environ: Mapping[str, str],
    sandbox_child: bool = False,
    progress: ProgressSink | None = None,
    agent_state: AgentState | None = None,
) -> int:
    """Start one already resolved agent runtime."""

    _restore_termination_signal_defaults()
    if spec.selector.driver != "none":
        return _up_managed_sandbox(
            plugin=spec.sandbox_plugin,
            selector=spec.selector,
            sandbox_config=spec.sandbox_config,
            toolang_root=spec.toolang_root,
            agent_name=spec.agent_name,
            host=spec.host,
            endpoint_host=spec.endpoint_host,
            port=spec.port,
            enabled_components=spec.enabled_components,
            environ=environ,
            dev_artifact=spec.dev_artifact,
            model_selectors=spec.model_selectors,
            tool_selectors=spec.tool_selectors,
            cap_selectors=spec.cap_selectors,
            file_inboxes=spec.file_inboxes,
        )
    return _up_local(
        toolang_root=spec.toolang_root,
        agent_name=spec.agent_name,
        host=spec.host,
        endpoint_host=spec.endpoint_host,
        port=spec.port,
        enabled_components=spec.enabled_components,
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
    executor, watcher, _ = assemble_execution(
        toolang_root=toolang_root,
        agent_name=agent_name,
        enabled_components=(),
        environ=invoke_environ,
        model_selectors=_normalize_model_selectors(models),
        tool_selectors=_normalize_tool_selectors(tools),
        cap_selectors=_normalize_cap_selectors(caps),
        agent_state=state,
    )
    run_id = executor.allocate_run_id()
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
                    group="script",
                    origin="script",
                    run_id=run_id,
                    executable_kind=executable_kind,
                    executable_name=executable_name,
                    input=input_text or "",
                    metadata=dict(metadata or {}),
                ),
                watcher.current(),
                reply=reply,
            )
        )
    finally:
        executor.store.close()


def prepare_agent(
    *,
    toolang_root: Path,
    agent_name: str,
    progress: ProgressSink | None = None,
) -> AgentState:
    """Prepare one agent for either long-lived startup or one-shot execution."""

    durable = scan_durable_state(toolang_root, agent_name)
    return load_agent_state(state_watcher.prepare_locks(durable, progress=progress))


def prepare_runtime(
    *,
    toolang_root: Path,
    agent_name: str,
    progress: ProgressSink | None = None,
) -> None:
    """Prepare one agent runtime without starting it."""

    prepare_agent(toolang_root=toolang_root, agent_name=agent_name, progress=progress)


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
    component_names: Sequence[str] | None = None,
    feature_names: Sequence[str] | None = None,
    log_spec: str | None = None,
    temporary_port: bool = False,
    environ: Mapping[str, str],
) -> StartupSpec:
    """Resolve one explicit startup request into stable runtime inputs."""

    requested_components = (
        component_names if component_names is not None else feature_names
    )
    enabled_components = normalize_component_names(
        requested_components or DEFAULT_ENABLED_COMPONENTS
    )
    endpoint_host = endpoint_host or _default_endpoint_host(host)
    resolved_port = resolve_runtime_port(
        host=host,
        explicit_port=port,
        toolang_root=toolang_root,
        agent_name=agent_name,
        temporary=temporary_port,
    )
    sandbox_binding = parse_sandbox_binding(
        load_sandbox_config(
            toolang_root,
            agent_name,
            environ=environ,
        )
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
    dev_artifact = _resolve_dev_artifact(dev) if dev is not None else None
    model_selectors = _normalize_model_selectors(models)
    tool_selectors = _normalize_tool_selectors(tools)
    cap_selectors = _normalize_cap_selectors(caps)
    resolved_file_inboxes = _normalize_file_inboxes(file_inboxes)
    if resolved_file_inboxes:
        enabled_components = _components_with_file_request(enabled_components)
    if _startup_requires_model(enabled_components):
        _validate_startup_models(
            toolang_root=toolang_root,
            agent_name=agent_name,
            selectors=model_selectors,
            environ=environ,
        )
    if "runner.file" in enabled_components:
        _validate_file_agic(toolang_root=toolang_root, agent_name=agent_name)
    return StartupSpec(
        toolang_root=toolang_root,
        agent_name=agent_name,
        host=host,
        endpoint_host=endpoint_host,
        port=resolved_port,
        enabled_components=enabled_components,
        sandbox_plugin=sandbox_plugin,
        selector=selector,
        sandbox_config=sandbox_config,
        dev_artifact=dev_artifact,
        model_selectors=model_selectors,
        tool_selectors=tool_selectors,
        cap_selectors=cap_selectors,
        file_inboxes=resolved_file_inboxes,
        log_spec=log_spec.strip()
        if isinstance(log_spec, str) and log_spec.strip()
        else None,
    )


def _startup_requires_model(enabled_components: Sequence[str]) -> bool:
    return any(
        component_name in RUNNER_COMPONENTS for component_name in enabled_components
    )


def _components_with_file_request(
    components: tuple[ComponentName, ...],
) -> tuple[ComponentName, ...]:
    enabled = list(components)
    for component_name in ("runner.file", "trigger.file"):
        if component_name not in enabled:
            enabled.append(cast(ComponentName, component_name))
    return tuple(enabled)


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
    program = scan_durable_state(toolang_root, agent_name).load_program().parse()
    agic = program.find_agic("file")
    if agic is None:
        raise ValueError("file agic not found")
    if agic.input is None:
        raise ValueError("file agic must accept message input")
    missing_params = [param.name for param in agic.params if not param.optional]
    if missing_params:
        joined = ", ".join(f"{name}=..." for name in missing_params)
        raise ValueError(f"file agic cannot have required parameters: {joined}")


def load_model_aliases(
    toolang_root: Path,
    agent_name: str,
) -> dict[str, ModelAlias]:
    """Load model aliases at the agent composition boundary."""

    return parse_model_aliases(load_config_layers(toolang_root, agent_name))


def load_default_models(toolang_root: Path, agent_name: str) -> tuple[str, ...]:
    """Load default model selectors at the agent composition boundary."""

    return parse_default_models(load_config_layers(toolang_root, agent_name))


def _load_model_providers(
    toolang_root: Path,
    agent_name: str,
) -> dict[str, ModelProvider]:
    config_layers = load_config_layers(toolang_root, agent_name)
    return load_model_providers(parse_model_provider_configs(config_layers))


def _validate_startup_models(
    *,
    toolang_root: Path,
    agent_name: str,
    selectors: Sequence[str],
    environ: Mapping[str, str],
) -> None:
    context = _StartupModelSelection(
        model_providers=_load_model_providers(toolang_root, agent_name),
        model_aliases=load_model_aliases(toolang_root, agent_name),
        default_models=load_default_models(toolang_root, agent_name),
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
            sandbox or spec.selector.render(),
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
    for inbox in spec.file_inboxes:
        command.extend(["--inbox", str(inbox)])
    for component_name in spec.enabled_components:
        command.extend(["--enable", component_name])
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
    enabled_components: tuple[ComponentName, ...],
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
    loop_intervals_ms = dict(DEFAULT_TRIGGER_INTERVAL_MS)
    for component_name in component_group(TRIGGER_COMPONENTS, "trigger"):
        if (
            component_name in loop_intervals_ms
            and loop_intervals_ms[component_name] <= 0
        ):
            raise ValueError(f"trigger interval must be positive: {component_name}")
    cors_allowed_origins = resolve_cors_allowed_origins(
        toolang_root,
        environ=environ,
    )
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    executor, watcher, config = assemble_execution(
        toolang_root=toolang_root,
        agent_name=agent_name,
        enabled_components=enabled_components,
        environ=environ,
        model_selectors=model_selectors,
        host=host,
        port=port,
        cors_allowed_origins=cors_allowed_origins or [],
        tool_selectors=tool_selectors,
        cap_selectors=cap_selectors,
        file_inboxes=file_inboxes,
        progress=progress,
        agent_state=agent_state,
    )
    state = watcher.current()
    _log_state_loaded(executor, config, state)
    executor.store.append_update(
        kind="started",
        payload={
            "components": list(enabled_components),
            "state_fingerprint": state.fingerprint,
        },
        created_at=started_at,
    )
    executor.store.append_event(
        domain="agent",
        domain_id=agent_name,
        type="agent_start",
        payload={
            "agent": agent_name,
            "components": list(enabled_components),
            "state_fingerprint": state.fingerprint,
            "started_at": started_at,
        },
    )
    endpoint = f"http://{endpoint_host}:{port}"
    shutdown_signal = threading.Event()
    channel_bindings = parse_channel_bindings(
        load_named_config(
            toolang_root,
            agent_name,
            section="channels",
            environ=environ,
        )
    )
    context = ApiContext(
        root=toolang_root,
        name=agent_name,
        home=executor.home,
        room=agents.agent_room(toolang_root, agent_name),
        get_agent_state=watcher.current,
        channel_bindings=channel_bindings,
        channel_plugins={
            name: create_channel_plugin(binding.plugin, config=binding.config)
            for name, binding in channel_bindings.items()
        },
        executor=executor,
        store=executor.store,
        config=config,
        enabled_components=enabled_components,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_signal = asyncio.Event()
        try:
            if not sandbox_child:
                agents.write_runtime_state(
                    toolang_root,
                    agent_name,
                    endpoint=endpoint,
                    started_at=started_at,
                    pid=os.getpid(),
                    components=enabled_components,
                    models=model_selectors,
                )
            bg_tasks: list[asyncio.Task[None]] = []
            job_store = None
            if "trigger.pulse" in enabled_components:
                interval_value = config.require("components.trigger.pulse.interval_ms")
                if not isinstance(interval_value, int | float):
                    raise TypeError(
                        "invalid config: components.trigger.pulse.interval_ms"
                    )
                job_store = open_job_store(toolang_root, agent_name)
                job_watcher = JobWatcher(toolang_root, agent_name)
                scheduler = Scheduler(
                    job_store=job_store,
                    executor=executor,
                    get_home_jobs=job_watcher.current,
                    get_agent_state=watcher.current,
                    kinds=tuple(
                        kind
                        for kind in ("task", "chore")
                        if f"runner.{kind}" in enabled_components
                    ),
                    interval_ms=float(interval_value),
                )
                bg_tasks.extend(
                    [
                        job_watcher.start(stop_signal=stop_signal),
                        scheduler.start(stop_signal=stop_signal),
                    ]
                )
            if "trigger.poll" in enabled_components:
                bg_tasks.append(
                    poll.spawn(
                        name=agent_name,
                        home=executor.home,
                        bindings=channel_bindings,
                        plugins=context.channel_plugins,
                        executor=executor,
                        get_agent_state=watcher.current,
                        enabled_components=enabled_components,
                        interval_ms=DEFAULT_TRIGGER_INTERVAL_MS["poll"],
                        stop_signal=stop_signal,
                    )
                )
            if "trigger.watch" in enabled_components:
                bg_tasks.append(
                    watch.spawn(
                        root=toolang_root,
                        name=agent_name,
                        watcher=watcher,
                        executor=executor,
                        store=executor.store,
                        config=config,
                        stop_signal=stop_signal,
                    )
                )
            if "trigger.file" in enabled_components:
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
            await executor.close()
            shutdown_tasks: list[asyncio.Task[Any]] = [*bg_tasks]
            await _finish_runtime_tasks(shutdown_tasks)
            if job_store is not None:
                job_store.close()
            executor.store.append_update(
                kind="stopped",
                payload={
                    "outcome": "stopped",
                },
            )
            executor.store.append_event(
                domain="agent",
                domain_id=agent_name,
                type="agent_stop",
                payload={"agent": agent_name, "outcome": "stopped"},
            )
            executor.store.close()

    app = create_app(context, lifespan=lifespan, shutdown_signal=shutdown_signal)
    webui_url = _runtime_webui_url(endpoint, toolang_root=toolang_root, environ=environ)
    _run_uvicorn_app(
        app,
        host=host,
        port=port,
        log_config=build_uvicorn_log_config(
            level=runtime_log_spec or DEFAULT_LOG_LEVEL
        ),
        shutdown_signal=shutdown_signal,
        on_starting=lambda: logger.info(
            "Agent starting root=%s trigger=%s runner=%s router=%s",
            toolang_root,
            format_component_group(enabled_components, "trigger"),
            format_component_group(enabled_components, "runner"),
            format_component_group(enabled_components, "router"),
            extra={
                "color_message": "Agent starting root="
                + "\x1b[1m%s\x1b[0m"
                + " trigger="
                + "\x1b[1m%s\x1b[0m"
                + " runner="
                + "\x1b[1m%s\x1b[0m"
                + " router="
                + "\x1b[1m%s\x1b[0m"
            },
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


def _log_state_loaded(
    executor: Executor, config: RuntimeConfig, state: AgentState
) -> None:
    state_logger.info(
        "Agent loaded state=%s models=%s tools=%s psyches=%s skills=%s services=%s",
        _short_fingerprint(state.fingerprint),
        _model_count(executor, config),
        len(executor.setup.tools),
        _cap_count(state, "psyche"),
        _cap_count(state, "skill"),
        _cap_count(state, "service"),
    )


def _model_count(executor: Executor, config: RuntimeConfig) -> int:
    try:
        selectors = _model_allowed_selectors(config)
        if selectors:
            return len(select_model_selectors(executor, activation_selectors=selectors))
        return len(select_model_selectors(executor))
    except Exception:
        selectors = config.get("models.allowed_selectors")
        if isinstance(selectors, tuple):
            return len(selectors)
        if isinstance(selectors, list):
            return len(selectors)
        return 0


def _model_allowed_selectors(config: RuntimeConfig) -> tuple[str, ...]:
    selectors = config.get("models.allowed_selectors")
    if isinstance(selectors, tuple):
        return tuple(
            item for item in selectors if isinstance(item, str) and item.strip()
        )
    if isinstance(selectors, list):
        return tuple(
            item for item in selectors if isinstance(item, str) and item.strip()
        )
    return ()


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
    enabled_components: tuple[ComponentName, ...],
    environ: Mapping[str, str],
    model_selectors: Sequence[str] = (),
    tool_selectors: Sequence[str] | None = None,
    cap_selectors: Sequence[str] = (),
    host: str = "127.0.0.1",
    port: int = 0,
    cors_allowed_origins: Sequence[str] = (),
    file_inboxes: Sequence[Path] = (),
    progress: ProgressSink | None = None,
    agent_state: AgentState | None = None,
) -> tuple[Executor, state_watcher.StateWatcher, RuntimeConfig]:
    """Assemble one executor and its versioned agent state."""
    runtime_state = agents.AgentProcess(toolang_root, agent_name).state() or {}
    if agent_state is None:
        agent_state = prepare_agent(
            toolang_root=toolang_root, agent_name=agent_name, progress=progress
        )
    state = agent_state
    normalized_model_selectors = _normalize_model_selectors(model_selectors)
    normalized_tool_selectors = _normalize_tool_selectors(tool_selectors)
    normalized_cap_selectors = _normalize_cap_selectors(cap_selectors)
    model_providers = _load_model_providers(toolang_root, agent_name)
    model_aliases = load_model_aliases(toolang_root, agent_name)
    default_models = load_default_models(toolang_root, agent_name)
    if (
        normalized_model_selectors
        or _startup_requires_model(enabled_components)
        or not enabled_components
    ):
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
    config = RuntimeConfig(
        {
            "server.host": host,
            "server.port": port,
            "server.endpoint": _runtime_endpoint_value(
                host=host, port=port, runtime_state=runtime_state
            ),
            "components.enabled": tuple(enabled_components),
            "components.trigger.pulse.interval_ms": DEFAULT_TRIGGER_INTERVAL_MS[
                "pulse"
            ],
            "components.trigger.poll.interval_ms": DEFAULT_TRIGGER_INTERVAL_MS["poll"],
            "components.trigger.watch.interval_ms": DEFAULT_TRIGGER_INTERVAL_MS[
                "watch"
            ],
            "components.trigger.watch.debounce_ms": DEFAULT_WATCH_DEBOUNCE_MS,
            "components.trigger.file.interval_ms": DEFAULT_TRIGGER_INTERVAL_MS["file"],
            "components.trigger.file.stable_ms": DEFAULT_FILE_STABLE_MS,
            "components.trigger.file.inboxes": tuple(file_inboxes),
            "web.cors_allowed_origins": list(cors_allowed_origins),
            "models.default_selector": default_model_selector,
            "models.allowed_selectors": normalized_model_selectors,
            "tools.allowed_selectors": normalized_tool_selectors,
            "caps.allowed_selectors": normalized_cap_selectors,
            "runtime.sandbox": _runtime_sandbox_value(runtime_state),
        }
    )
    store = RunStore(run_store_path(toolang_root, agent_name))
    tools = load_runtime_tools(
        plugin_config=load_named_config(
            toolang_root,
            agent_name,
            section="tools",
            environ=environ,
        ),
        entries=state.caps,
    )
    selected_tools = select_tools(tools, normalized_tool_selectors)
    validate_tool_selectors(tools, normalized_tool_selectors)
    setup = AgentSetup(
        tools=selected_tools,
        model_providers=model_providers,
        model_adapters=load_model_adapters(),
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
        config=config,
    )
    watcher = state_watcher.StateWatcher(
        toolang_root,
        agent_name,
        state,
        transform=lambda value: _select_agent_caps(
            value, normalized_cap_selectors, agent_name=agent_name
        ),
    )
    return executor, watcher, config


def _runtime_endpoint_value(
    *,
    host: str,
    port: int,
    runtime_state: Mapping[str, object],
) -> str | None:
    endpoint = runtime_state.get("endpoint")
    if isinstance(endpoint, str) and endpoint.strip():
        return endpoint.strip()
    if port > 0:
        return f"http://{host}:{port}"
    return None


def _runtime_sandbox_value(runtime_state: Mapping[str, object]) -> str:
    sandbox = runtime_state.get("sandbox")
    if isinstance(sandbox, dict):
        sandbox_data = {str(key): value for key, value in sandbox.items()}
        selector = sandbox_data.get("selector")
        if isinstance(selector, dict):
            selector_data = {str(key): value for key, value in selector.items()}
            driver = selector_data.get("driver")
            target = selector_data.get("target")
            if isinstance(driver, str) and driver.strip():
                if isinstance(target, str) and target.strip():
                    return f"{driver.strip()}:{target.strip()}"
                return driver.strip()
    return "none"


def _runtime_webui_url(
    endpoint: str,
    *,
    toolang_root: Path,
    environ: Mapping[str, str],
) -> str:
    try:
        endpoint_port = urlsplit(endpoint).port
    except ValueError:
        endpoint_port = None
    base_url = resolve_ui_base_url(toolang_root, environ=environ).rstrip("/")
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
    enabled_components: tuple[ComponentName, ...],
    environ: Mapping[str, str],
    dev_artifact: Path | None,
    model_selectors: tuple[str, ...],
    tool_selectors: tuple[str, ...] | None,
    cap_selectors: tuple[str, ...],
    file_inboxes: tuple[Path, ...],
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
        enabled_components=enabled_components,
        sandbox_plugin=plugin,
        selector=selector,
        sandbox_config=dict(sandbox_config),
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
        components=enabled_components,
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
        component_names=enabled_components,
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
            components=enabled_components,
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
            components=enabled_components,
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
        components=enabled_components,
        models=model_selectors,
        status="running",
    )
    logger.info(
        "Sandbox ready agent=%s sandbox=%s endpoint=%s",
        agent_name,
        selector.render(),
        start.endpoint or endpoint,
    )
    return 0


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

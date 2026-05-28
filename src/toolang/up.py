"""Agent startup implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
import logging
import os
from pathlib import Path
import signal
import shlex
import socket
import sys
import time
import threading
from types import FrameType
from typing import Any, cast
from urllib.parse import urlsplit

import click
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from uvicorn.main import STARTUP_FAILURE

from . import agents, caps as cap_store
from toolang.base.protocols.channel import AgentChannel
from toolang.base.protocols.model import ModelProvider
from toolang.base.protocols.model import ModelAdapter
from toolang.base.protocols.sandbox import AgentSandbox
from toolang.base.protocols.tool import AgentTool, AgentToolSet
from toolang.base.types.channel import ChannelContext, InboundDelivery
from toolang.base.types.model import ModelAlias
from toolang.base.types.sandbox import SandboxSelector, SandboxStartRequest, SandboxState
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.base.utils.channels import bind_delivery
from toolang.tools.registry import (
    ToolRef,
    parse_tool_registration_key,
    selected_tool_names,
    split_tool_selectors,
    tool_ref_for_model_tool,
)
from .config.log import (
    DEFAULT_LOG_LEVEL,
    build_uvicorn_log_config,
    configure_logging_plan,
    resolve_agent_logging,
)
from .config.plugins import ChannelBinding, load_channel_bindings, load_sandbox_binding, load_tool_plugin_config
from .config.log_spec import PY_LOG_ENV_VAR
from .config.web import resolve_cors_allowed_origins, resolve_ui_base_url
from .execution.input import allocate_run_id
from .execution.response import ResponseSink, build_channel_response_sink
from .execution.execute import execute_run
from .models.resolution import resolve_model, select_model_selectors
from .execution.runner import QueueRunner, RunRequest, RunSubmission, RunOutcome
from .execution.db import ExecutionStore, execution_db_path
from .execution.stream import RuntimeEventBus
from .components.router import chat
from .components.router.inspect import create_router as create_inspect_router
from .components.router.manage import create_router as create_manage_router
from .components.registry import (
    DEFAULT_ENABLED_COMPONENTS,
    RUNNER_COMPONENTS,
    TRIGGER_COMPONENTS,
    ComponentName,
    component_group,
    format_component_group,
    normalize_component_names,
)
from .components.trigger import poll, pulse, watch
from .common.progress import ProgressSink
from .state.durable import scan_durable_state
from .state.live import LiveState, load_live_state
from .state.prepared import PreparedEntry, PreparedState
from .models.config import (
    load_default_models,
    load_model_aliases,
    load_model_provider_configs,
)
from .models.resolution import split_model_selectors
from .plugin import (
    PluginInfo,
    create_plugin,
    list_plugin_infos,
    list_plugin_names,
    load_plugin_factory,
    load_plugins,
)

DEFAULT_TRIGGER_INTERVAL_MS: dict[str, float] = {
    "pulse": pulse.DEFAULT_INTERVAL_MS,
    "poll": poll.DEFAULT_INTERVAL_MS,
    "watch": watch.DEFAULT_INTERVAL_MS,
}
RUN_NAMES = frozenset(component_group(RUNNER_COMPONENTS, "runner"))
RUN_FEATURES = RUN_NAMES | {"pulse", "poll"}
DEFAULT_WATCH_DEBOUNCE_MS = watch.DEFAULT_DEBOUNCE_MS
RUNTIME_SHUTDOWN_TASK_TIMEOUT_SEC = 1.0
UVICORN_GRACEFUL_SHUTDOWN_SEC = 1
DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "https://too.run",
]
AUTO_RUNTIME_PORT_MIN = 7001
AUTO_RUNTIME_PORT_MAX = 7999
OPENAPI_TAGS = [
    {"name": "agent", "description": "Agent profile and health endpoints."},
    {"name": "chat", "description": "Chat submission and streaming endpoints."},
    {"name": "caps", "description": "Capability inspection and mutation endpoints."},
    {"name": "jobs", "description": "Task, chore, and will inspection endpoints."},
    {"name": "activity", "description": "Thread, run, and event history endpoints."},
]
logger = logging.getLogger("toolang.runtime")
_PLUGIN_API_REEXPORTS = (
    PluginInfo,
    list_plugin_infos,
    list_plugin_names,
    load_plugin_factory,
)


class UptimeConfig:
    """Minimal string-keyed uptime config."""

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        self._values = dict(values or {})
        self._migrate_component_keys()

    def get(self, key: str, default: object | None = None) -> object | None:
        return self._values.get(key, default)

    def require(self, key: str) -> object:
        if key not in self._values:
            raise KeyError(f"missing config: {key}")
        return self._values[key]

    def set(self, key: str, value: object) -> None:
        self._values[key] = value
        if key == "features.enabled":
            self._values["components.enabled"] = value
        elif key == "features.pulse.interval_ms":
            self._values["components.trigger.pulse.interval_ms"] = value
        elif key == "features.poll.interval_ms":
            self._values["components.trigger.poll.interval_ms"] = value
        elif key == "features.watch.interval_ms":
            self._values["components.trigger.watch.interval_ms"] = value
        elif key == "features.watch.debounce_ms":
            self._values["components.trigger.watch.debounce_ms"] = value
        self._migrate_component_keys()

    def snapshot(self) -> dict[str, object]:
        return dict(self._values)

    def _migrate_component_keys(self) -> None:
        if "components.enabled" not in self._values and "features.enabled" in self._values:
            self._values["components.enabled"] = self._values["features.enabled"]
        raw_components = self._values.get("components.enabled")
        if isinstance(raw_components, tuple):
            self._values["components.enabled"] = normalize_component_names(raw_components)
        if "components.trigger.pulse.interval_ms" not in self._values and "features.pulse.interval_ms" in self._values:
            self._values["components.trigger.pulse.interval_ms"] = self._values["features.pulse.interval_ms"]
        if "components.trigger.poll.interval_ms" not in self._values and "features.poll.interval_ms" in self._values:
            self._values["components.trigger.poll.interval_ms"] = self._values["features.poll.interval_ms"]
        if "components.trigger.watch.interval_ms" not in self._values and "features.watch.interval_ms" in self._values:
            self._values["components.trigger.watch.interval_ms"] = self._values["features.watch.interval_ms"]
        if "components.trigger.watch.debounce_ms" not in self._values and "features.watch.debounce_ms" in self._values:
            self._values["components.trigger.watch.debounce_ms"] = self._values["features.watch.debounce_ms"]


class UptimeContext:
    """Shared uptime state used across loop implementations."""

    def __init__(
        self,
        *,
        root: Path,
        name: str,
        live: LiveState,
        tools: dict[str, AgentTool],
        model_providers: dict[str, ModelProvider],
        model_adapters: dict[str, ModelAdapter],
        model_aliases: dict[str, ModelAlias],
        default_models: tuple[str, ...],
        model_environ: Mapping[str, str],
        channel_bindings: dict[str, ChannelBinding],
        channel_plugins: dict[str, AgentChannel],
        runner: QueueRunner,
        store: ExecutionStore,
        events: RuntimeEventBus,
        config: UptimeConfig,
    ) -> None:
        self.root = root
        self.name = name
        self.home = agents.agent_home(root, name)
        self.room = agents.agent_room(root, name)
        self.live = live
        self.tools = dict(tools)
        self.model_providers = dict(model_providers)
        self.model_adapters = dict(model_adapters)
        self.model_aliases = dict(model_aliases)
        self.default_models = tuple(default_models)
        self.model_environ = dict(model_environ)
        self.channel_bindings = dict(channel_bindings)
        self.channel_plugins = dict(channel_plugins)
        self.runner = runner
        self.store = store
        self.events = events
        self.config = config

    def tool_context(self, tool_name: str, *, run_id: str, wd: Path | None = None) -> ToolContext:
        """Return one resolved tool context for a model-facing tool name."""

        plugin_name = getattr(self.tools.get(tool_name), "plugin_name", None)
        if not isinstance(plugin_name, str) or not plugin_name:
            raise KeyError(f"unknown tool: {tool_name}")
        return ToolContext(
            run_id=run_id,
            home=self.home,
            room=agents.tool_room(self.root, self.name, plugin_name),
            wd=(wd or self.home),
        )

    def channel_context(self, binding_name: str) -> ChannelContext:
        """Return one resolved channel context for a configured binding."""

        return ChannelContext(
            home=self.home,
            room=agents.channel_room(self.root, self.name, binding_name),
        )

    def enqueue_run(
        self,
        component_name: str,
        *,
        thunk: str,
        thread_id: str | None = None,
    ) -> int:
        """Queue one run request for a run-producing component."""

        if component_name not in RUN_FEATURES:
            raise ValueError(f"component does not produce runs: run.{component_name}")
        from .execution.runner import RunRequest

        return self.runner.enqueue(
            RunRequest(
                group=component_name,
                origin=component_name,
                thread_id=thread_id,
                thunk=thunk,
            )
        )

    def enqueue_delivery(
        self,
        component_name: str,
        binding_name: str,
        delivery: InboundDelivery,
    ) -> int:
        """Queue one run request produced by one channel delivery."""

        if component_name not in RUN_FEATURES:
            raise ValueError(f"component does not produce runs: run.{component_name}")
        from .execution.runner import RunRequest

        bound = bind_delivery(binding_name, delivery)
        metadata = dict(bound.meta)
        metadata["channel"] = binding_name
        metadata["sender"] = bound.sender
        return self.runner.enqueue(
            RunRequest(
                group=component_name,
                origin=bound.origin,
                thread_id=bound.thread_id,
                thunk=bound.text,
                metadata=metadata,
            ),
            response=build_channel_response_sink(
                self,
                binding_name=binding_name,
                target=bound.reply_target,
            ),
        )


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


@dataclass(frozen=True, slots=True)
class _LoadedTool(AgentTool):
    """One model-facing tool loaded from one named plugin."""

    plugin_name: str
    ref: ToolRef
    leaf_tool: AgentTool

    @property
    def name(self) -> str:
        return self.ref.model_name

    @property
    def namespace(self) -> str:
        return self.ref.namespace

    @property
    def public_name(self) -> str:
        return self.ref.selector

    def definition(self) -> ToolDefinition:
        definition = self.leaf_tool.definition()
        return ToolDefinition(
            name=self.name,
            description=definition.description,
            parameters=dict(definition.parameters),
        )

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        return self.leaf_tool.invoke(arguments, context)


def create_app(
    context: UptimeContext,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
    shutdown_signal: threading.Event | None = None,
) -> FastAPI:
    """Create one FastAPI app for an existing runtime component context."""

    enabled_components = cast(tuple[str, ...], context.config.require("components.enabled"))
    raw_cors_origins = context.config.get("web.cors_allowed_origins")
    cors_origins = (
        [item for item in raw_cors_origins if isinstance(item, str) and item.strip()]
        if isinstance(raw_cors_origins, list)
        else None
    )
    app = FastAPI(
        title="Toolang Agent API",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
    )
    _add_cors(
        app,
        allow_origins=cors_origins or None,
    )
    app.state.runtime = context
    app.state.enabled_components = enabled_components
    app.state.shutdown_signal = shutdown_signal

    @app.get("/healthz", tags=["agent"], summary="Health Check")
    def healthz() -> dict[str, object]:
        return {"ok": True, "enabled_components": list(enabled_components)}

    if "router.chat" in enabled_components:
        app.include_router(chat.create_router())
    if "router.manage" in enabled_components:
        app.include_router(create_manage_router())
    if "router.inspect" in enabled_components:
        app.include_router(create_inspect_router())
    return app


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
    prepared_state: PreparedState | None = None,
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
        log_spec=spec.log_spec,
        progress=progress,
        prepared_state=prepared_state,
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
    thunk_name: str | None = None,
    input_text: str | None = None,
    models: Sequence[str] | None = None,
    tools: Sequence[str] | None = (),
    caps: Sequence[str] | None = None,
    metadata: Mapping[str, object] | None = None,
    environ: Mapping[str, str],
    response: ResponseSink | None = None,
    log_spec: str | None = None,
    prepared_state: PreparedState | None = None,
) -> RunOutcome:
    """Execute one thunk once without starting the long-lived runtime."""

    invoke_environ = dict(environ)
    if log_spec is not None:
        invoke_environ[PY_LOG_ENV_VAR] = log_spec
    prepared = prepared_state or prepare_agent(toolang_root=toolang_root, agent_name=agent_name)
    context = _load_runtime_context(
        toolang_root=toolang_root,
        agent_name=agent_name,
        enabled_components=(),
        environ=invoke_environ,
        model_selectors=_normalize_model_selectors(models),
        tool_selectors=_normalize_tool_selectors(tools),
        cap_selectors=_normalize_cap_selectors(caps),
        prepared_state=prepared,
    )
    run_id = allocate_run_id(context)
    log_plan = resolve_agent_logging(
        mode="invoke",
        environ=invoke_environ,
        run_log_path=agents.agent_script_run_log_path(
            toolang_root,
            agent_name,
            thunk_name=thunk_name,
            run_id=run_id,
        ),
    )
    if log_plan.destination != "none":
        configure_logging_plan(log_plan)
        if log_plan.path is not None:
            log_plan.path.touch(exist_ok=True)
    try:
        outcome = asyncio.run(
            execute_run(
                context,
                RunSubmission(
                    request=RunRequest(
                        group="script",
                        origin="script",
                        run_id=run_id,
                        thunk=input_text or "",
                        thunk_name=thunk_name,
                        metadata=dict(metadata or {}),
                    ),
                    response=response,
                    live=context.live,
                ),
                delay_sec=0.0,
                sleep=asyncio.sleep,
            )
        )
        return replace(outcome, log_path=str(log_plan.path) if log_plan.path is not None else None)
    finally:
        context.store.close()


def prepare_agent(
    *,
    toolang_root: Path,
    agent_name: str,
    progress: ProgressSink | None = None,
) -> PreparedState:
    """Prepare one agent for either long-lived startup or one-shot execution."""

    durable = scan_durable_state(toolang_root, agent_name)
    return watch.build_prepared_state(durable, progress=progress)


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
    dev: Path | None = None,
    component_names: Sequence[str] | None = None,
    feature_names: Sequence[str] | None = None,
    log_spec: str | None = None,
    temporary_port: bool = False,
    environ: Mapping[str, str],
) -> StartupSpec:
    """Resolve one explicit startup request into stable runtime inputs."""

    requested_components = component_names if component_names is not None else feature_names
    enabled_components = normalize_component_names(requested_components or DEFAULT_ENABLED_COMPONENTS)
    endpoint_host = endpoint_host or _default_endpoint_host(host)
    resolved_port = resolve_runtime_port(
        host=host,
        explicit_port=port,
        toolang_root=toolang_root,
        agent_name=agent_name,
        temporary=temporary_port,
    )
    sandbox_binding = load_sandbox_binding(
        toolang_root,
        agent_name,
        environ=environ,
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
        if sandbox_binding is not None and sandbox_binding.selector.driver == sandbox_driver
        else {}
    )
    sandbox_plugin = create_sandbox_plugin(sandbox_driver, config=sandbox_config)
    selector = sandbox_plugin.resolve_selector(
        sandbox,
        configured_selector=(
            sandbox_binding.selector
            if sandbox_binding is not None and sandbox_binding.selector.driver == sandbox_driver
            else None
        ),
    )
    dev_artifact = _resolve_dev_artifact(dev) if dev is not None else None
    model_selectors = _normalize_model_selectors(models)
    tool_selectors = _normalize_tool_selectors(tools)
    cap_selectors = _normalize_cap_selectors(caps)
    if _startup_requires_model(enabled_components):
        _validate_startup_models(
            toolang_root=toolang_root,
            agent_name=agent_name,
            selectors=model_selectors,
            environ=environ,
        )
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
        log_spec=log_spec.strip() if isinstance(log_spec, str) and log_spec.strip() else None,
    )


def _startup_requires_model(enabled_components: Sequence[str]) -> bool:
    return any(component_name in RUNNER_COMPONENTS for component_name in enabled_components)


def _validate_startup_models(
    *,
    toolang_root: Path,
    agent_name: str,
    selectors: Sequence[str],
    environ: Mapping[str, str],
) -> None:
    context = _StartupModelSelection(
        model_providers=load_model_providers(toolang_root, agent_name),
        model_aliases=load_model_aliases(toolang_root, agent_name),
        default_models=load_default_models(toolang_root, agent_name),
        model_environ=environ,
    )
    if not selectors:
        select_model_selectors(context)
        return
    for selector in selectors:
        resolve_model(context, selector=selector)


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
    command.extend([
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
    ])
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
    log_spec: str | None,
    progress: ProgressSink | None = None,
    prepared_state: PreparedState | None = None,
) -> int:
    loop_intervals_ms = dict(DEFAULT_TRIGGER_INTERVAL_MS)
    for component_name in component_group(TRIGGER_COMPONENTS, "trigger"):
        if component_name in loop_intervals_ms and loop_intervals_ms[component_name] <= 0:
            raise ValueError(f"trigger interval must be positive: {component_name}")
    cors_allowed_origins = resolve_cors_allowed_origins(
        toolang_root,
        environ=environ,
    )
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    context = _load_runtime_context(
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
        progress=progress,
        prepared_state=prepared_state,
    )
    live = context.live
    context.store.append_update(
        kind="started",
        payload={
            "components": list(enabled_components),
            "live_fingerprint": live.fingerprint,
        },
        created_at=started_at,
    )
    context.events.publish(
        domain="agent",
        domain_id=context.name,
        type="agent_start",
        payload={
            "agent": context.name,
            "components": list(enabled_components),
            "live_fingerprint": live.fingerprint,
            "started_at": started_at,
        },
    )
    endpoint = f"http://{endpoint_host}:{port}"
    shutdown_signal = threading.Event()

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
            if "trigger.pulse" in enabled_components:
                bg_tasks.append(pulse.spawn(context, stop_signal=stop_signal))
            if "trigger.poll" in enabled_components:
                bg_tasks.append(poll.spawn(context, stop_signal=stop_signal))
            if "trigger.watch" in enabled_components:
                bg_tasks.append(watch.spawn(context, stop_signal=stop_signal))

            runner_task = None
            if any(component in RUNNER_COMPONENTS for component in enabled_components):
                runner_task = context.runner.spawn(context)
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
            context.runner.close()
            shutdown_tasks: list[asyncio.Task[Any]] = [*bg_tasks]
            if runner_task is not None:
                shutdown_tasks.append(runner_task)
            await _finish_runtime_tasks(shutdown_tasks)
            context.store.append_update(
                kind="stopped",
                payload={
                    "outcome": "stopped",
                },
            )
            context.events.publish(
                domain="agent",
                domain_id=context.name,
                type="agent_stop",
                payload={"agent": context.name, "outcome": "stopped"},
            )
            context.store.close()

    app = create_app(context, lifespan=lifespan, shutdown_signal=shutdown_signal)
    webui_url = _runtime_webui_url(endpoint, toolang_root=toolang_root, environ=environ)
    _run_uvicorn_app(
        app,
        host=host,
        port=port,
        log_config=build_uvicorn_log_config(level=log_spec or DEFAULT_LOG_LEVEL),
        shutdown_signal=shutdown_signal,
        on_starting=lambda: logger.info(
            "Agent %s starting root=%s trigger=%s runner=%s router=%s",
            context.name,
            toolang_root,
            format_component_group(enabled_components, "trigger"),
            format_component_group(enabled_components, "runner"),
            format_component_group(enabled_components, "router"),
            extra={
                "color_message": "Agent %s starting root="
                + click.style("%s", bold=True)
                + " trigger="
                + click.style("%s", bold=True)
                + " runner="
                + click.style("%s", bold=True)
                + " router="
                + click.style("%s", bold=True)
            },
        ),
        on_running=lambda: logger.info(
            "Agent %s started webui=%s",
            context.name,
            webui_url,
            extra={
                "color_message": "Agent %s started webui=" + click.style("%s", bold=True)
            },
        ),
        on_stopping=lambda: logger.info("Agent %s stopping", context.name),
        on_stopped=lambda: logger.info("Agent %s stopped", context.name),
    )
    return 0


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
        done_after_cancel, pending_after_cancel = await asyncio.wait(pending, timeout=timeout_sec)
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


def _load_runtime_context(
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
    progress: ProgressSink | None = None,
    prepared_state: PreparedState | None = None,
) -> UptimeContext:
    channel_bindings = load_channel_bindings(
        toolang_root,
        agent_name,
        environ=environ,
    )
    runtime_state = agents.load_runtime_state(toolang_root, agent_name) or {}
    if prepared_state is None:
        prepared_state = prepare_agent(toolang_root=toolang_root, agent_name=agent_name, progress=progress)
    live = load_live_state(prepared_state, enabled_components=enabled_components)
    normalized_model_selectors = _normalize_model_selectors(model_selectors)
    normalized_tool_selectors = _normalize_tool_selectors(tool_selectors)
    normalized_cap_selectors = _normalize_cap_selectors(cap_selectors)
    live = _select_live_caps(live, normalized_cap_selectors, agent_name=agent_name)
    default_model_selector = normalized_model_selectors[0] if normalized_model_selectors else None
    config = UptimeConfig(
        {
            "server.host": host,
            "server.port": port,
            "server.endpoint": _runtime_endpoint_value(host=host, port=port, runtime_state=runtime_state),
            "components.enabled": tuple(enabled_components),
            "components.trigger.pulse.interval_ms": DEFAULT_TRIGGER_INTERVAL_MS["pulse"],
            "components.trigger.poll.interval_ms": DEFAULT_TRIGGER_INTERVAL_MS["poll"],
            "components.trigger.watch.interval_ms": DEFAULT_TRIGGER_INTERVAL_MS["watch"],
            "components.trigger.watch.debounce_ms": DEFAULT_WATCH_DEBOUNCE_MS,
            "web.cors_allowed_origins": list(cors_allowed_origins),
            "models.default_selector": default_model_selector,
            "models.allowed_selectors": normalized_model_selectors,
            "tools.allowed_selectors": normalized_tool_selectors,
            "caps.allowed_selectors": normalized_cap_selectors,
            "runtime.sandbox": _runtime_sandbox_value(runtime_state),
        }
    )
    store = ExecutionStore(execution_db_path(toolang_root, agent_name))
    return UptimeContext(
        root=toolang_root,
        name=agent_name,
        live=live,
        tools=load_runtime_tool_plugins(
            toolang_root=toolang_root,
            agent_name=agent_name,
            live=live,
            environ=environ,
            selectors=normalized_tool_selectors,
        ),
        model_providers=load_model_providers(toolang_root, agent_name),
        model_adapters=load_model_adapters(),
        model_aliases=load_model_aliases(toolang_root, agent_name),
        default_models=load_default_models(toolang_root, agent_name),
        model_environ=environ,
        channel_bindings=channel_bindings,
        channel_plugins={
            name: create_channel_plugin(binding.plugin, config=binding.config)
            for name, binding in channel_bindings.items()
        },
        runner=QueueRunner(),
        store=store,
        events=RuntimeEventBus(store, agent_id=agent_name),
        config=config,
    )


def load_runtime_tool_plugins(
    *,
    toolang_root: Path,
    agent_name: str,
    live: LiveState,
    environ: Mapping[str, str],
    selectors: Sequence[str] | None = None,
) -> dict[str, AgentTool]:
    """Load tool plugins with runtime service caps exposed to service_use."""

    tools = load_tool_plugins(
        config=runtime_tool_plugin_config(
            toolang_root=toolang_root,
            agent_name=agent_name,
            live=live,
            environ=environ,
        )
    )
    return _select_runtime_tools(tools, selectors)


def runtime_tool_plugin_config(
    *,
    toolang_root: Path,
    agent_name: str,
    live: LiveState,
    environ: Mapping[str, str],
) -> dict[str, dict[str, object]]:
    """Return tool plugin config merged with effective service cap visibility."""

    config = load_tool_plugin_config(
        toolang_root,
        agent_name,
        environ=environ,
    )
    visible_services = [
        service
        for entry in live.cap_entries
        if entry.kind == "service"
        if (service := _visible_service_config_from_cap(entry)) is not None
    ]
    if visible_services:
        service_use = dict(config.get("service_use", {}))
        service_use["visible_services"] = visible_services
        config["service_use"] = service_use
    return config


def _visible_service_config_from_cap(entry: PreparedEntry) -> dict[str, object] | None:
    transport = _optional_config_text(entry.meta.get("transport"))
    target = _optional_config_text(entry.meta.get("target"))
    if transport not in {"http", "stdio"} or target is None:
        return None
    service: dict[str, object] = {
        "name": entry.name,
        "description": _optional_config_text(entry.meta.get("description")),
        "transport": transport,
        "target": target,
    }
    if transport == "stdio":
        try:
            command = shlex.split(target)
        except ValueError:
            command = []
        if command:
            service["command"] = command
    env_vars = _service_env_names(entry.meta.get("env"))
    if env_vars:
        service["env_vars"] = env_vars
    return {key: value for key, value in service.items() if value is not None}


def _service_env_names(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _optional_config_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


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
        raise ValueError("sandbox root must be resolved by the local runtime for none sandbox")
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
            sandbox=plan.state.to_data() if plan.state is not None else initial_sandbox_state,
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
        failed_endpoint = start.endpoint if start is not None and start.endpoint else endpoint
        failed_sandbox_state = (
            start.state.to_data()
            if start is not None
            else (plan.state.to_data() if plan is not None and plan.state is not None else initial_sandbox_state)
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
        "sandbox ready agent=%s sandbox=%s endpoint=%s",
        agent_name,
        selector.render(),
        start.endpoint or endpoint,
    )
    print(
        f"{agent_name}\trunning\t{start.endpoint or endpoint}\t{selector.render()}",
        file=sys.stderr,
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


def _select_live_caps(live: LiveState, selectors: Sequence[str], *, agent_name: str) -> LiveState:
    if not selectors:
        return live
    return replace(
        live,
        cap_entries=cap_store.select_cap_entries(
            live.cap_entries,
            tuple(selectors),
            agent_name=agent_name,
        ),
    )


def _select_runtime_tools(
    tools: dict[str, AgentTool],
    selectors: Sequence[str] | None,
) -> dict[str, AgentTool]:
    if selectors is None:
        return tools
    if not selectors:
        return {}
    refs_by_model_name = {
        name: tool_ref_for_model_tool(name, tool)
        for name, tool in tools.items()
    }
    selected_names = selected_tool_names(refs_by_model_name, selectors)
    return {
        name: tools[name]
        for name in selected_names
        if name in tools
    }

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
        raise ValueError(f"dev path must be a wheel file or directory containing wheels: {candidate}")
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
            raise ValueError(f"sandbox exited before becoming ready: http://{host}:{port}")
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


def _add_cors(
    app: FastAPI,
    *,
    allow_origins: list[str] | None = None,
) -> None:
    origins = list(allow_origins or DEFAULT_CORS_ORIGINS)
    if not origins:
        return
    app.add_middleware(
        cast(Any, CORSMiddleware),
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_private_network=True,
    )


def load_model_providers(
    toolang_root: Path | None = None,
    agent_name: str | None = None,
) -> dict[str, ModelProvider]:
    """Load model provider plugins for one uptime."""

    provider_configs = (
        load_model_provider_configs(toolang_root, agent_name)
        if toolang_root is not None and agent_name is not None
        else {}
    )
    config = {
        name: _provider_config_payload(payload)
        for name, payload in provider_configs.items()
    }
    return cast(
        dict[str, ModelProvider],
        load_plugins(group="toolang.model_provider", config=config),
    )


def load_model_adapters() -> dict[str, ModelAdapter]:
    """Load model adapter plugins for one uptime."""

    return cast(dict[str, ModelAdapter], load_plugins(group="toolang.model_adapter"))


def _provider_config_payload(config: object | None) -> Mapping[str, Any]:
    if config is None:
        return {}
    return {
        "endpoint": getattr(config, "endpoint", None),
        "key_env": getattr(config, "key_env", None),
        "adapter": getattr(config, "adapter", None),
        "scope": getattr(config, "scope", None),
        "options": getattr(config, "options", {}),
        "details": getattr(config, "details", None),
    }


def load_tool_plugins(
    *,
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, AgentTool]:
    tools: dict[str, AgentTool] = {}
    plugins = cast(
        dict[str, AgentToolSet],
        load_plugins(group="toolang.tool", config=config),
    )
    for plugin in plugins.values():
        for leaf_name, leaf_tool in plugin.tools().items():
            ref = parse_tool_registration_key(plugin.name, leaf_name, leaf_tool.name)
            loaded = _LoadedTool(
                plugin_name=plugin.name,
                ref=ref,
                leaf_tool=leaf_tool,
            )
            if loaded.name in tools:
                raise ValueError(f"duplicate tool name: {loaded.public_name}")
            tools[loaded.name] = loaded
    return tools


def create_channel_plugin(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> AgentChannel:
    return cast(
        AgentChannel,
        create_plugin(name, group="toolang.channel", config=config),
    )


def create_sandbox_plugin(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> AgentSandbox:
    return cast(
        AgentSandbox,
        create_plugin(name, group="toolang.sandbox", config=config),
    )


def create_model_provider(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> ModelProvider:
    return cast(
        ModelProvider,
        create_plugin(name, group="toolang.model_provider", config=config),
    )


def create_model_adapter(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> ModelAdapter:
    return cast(
        ModelAdapter,
        create_plugin(name, group="toolang.model_adapter", config=config),
    )

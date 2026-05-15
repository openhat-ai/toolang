"""Agent startup implementation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import entry_points
import logging
import os
from pathlib import Path
import signal
import shlex
import socket
import sys
import time
import threading
import tomllib
from types import FrameType
from typing import Any, Literal, TypeVar, cast
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from uvicorn.main import STARTUP_FAILURE

from . import agents
from toolang.base.protocols.channel import ChannelPlugin
from toolang.base.protocols.model import ModelProvider
from toolang.base.protocols.sandbox import SandboxPlugin
from toolang.base.protocols.tool import Tool, ToolPlugin
from toolang.base.types.channel import ChannelContext, InboundDelivery
from toolang.base.error import ToolangError
from toolang.base.types.model import ModelRoute
from toolang.base.types.sandbox import SandboxSelector, SandboxStartRequest, SandboxState
from toolang.base.types.tool import ToolContext, ToolDefinition
from toolang.base.utils.channels import bind_delivery
from toolang.base.utils.tools import join_tool_name
from .config.log import DEFAULT_LOG_LEVEL, build_uvicorn_log_config
from .config.plugins import ChannelBinding, load_channel_bindings, load_sandbox_binding, load_tool_plugin_config
from .config.web import resolve_cors_allowed_origins, resolve_ui_base_url
from .execution.response import build_channel_response_sink
from .execution.execute import execute_run
from .execution.runner import QueueRunner, RunRequest, RunSubmission, RunOutcome
from .execution.db import ExecutionStore, execution_db_path
from .execution.stream import RuntimeEventBus
from .features import chat, hook, poll, pulse, watch
from .features.control import create_router as create_control_router
from .features.inspect import create_router as create_inspect_router
from .progress import ProgressSink
from .state.durable import scan_durable_state
from .state.live import LiveState, load_live_state
from .state.prepared import PreparedEntry

FeatureName = Literal[
    "chat", "pulse", "poll", "hook", "control", "inspect", "watch"
]

ALL_FEATURES: tuple[FeatureName, ...] = (
    "chat",
    "pulse",
    "poll",
    "hook",
    "control",
    "inspect",
    "watch",
)
DEFAULT_ENABLED_FEATURES: tuple[FeatureName, ...] = (
    "chat",
    "pulse",
    "control",
    "inspect",
    "watch",
)
RUN_FEATURES = frozenset({"chat", "pulse", "poll", "hook"})
HTTP_FEATURES = frozenset({"chat", "hook", "control", "inspect"})
BACKGROUND_FEATURES: tuple[FeatureName, ...] = ("pulse", "poll", "watch")

DEFAULT_FEATURE_INTERVAL_MS: dict[str, float] = {
    "pulse": pulse.DEFAULT_INTERVAL_MS,
    "poll": poll.DEFAULT_INTERVAL_MS,
    "watch": watch.DEFAULT_INTERVAL_MS,
}
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
    {"name": "hook", "description": "Inbound hook submission endpoints."},
]
logger = logging.getLogger("toolang.runtime")
FactoryT = TypeVar("FactoryT")
PluginSource = Literal["built-in", "external"]


@dataclass(frozen=True, slots=True)
class PluginInfo:
    """One discoverable plugin entry point."""

    name: str
    source: PluginSource


class UptimeConfig:
    """Minimal string-keyed uptime config."""

    def __init__(self, values: Mapping[str, object] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, key: str, default: object | None = None) -> object | None:
        return self._values.get(key, default)

    def require(self, key: str) -> object:
        if key not in self._values:
            raise KeyError(f"missing config: {key}")
        return self._values[key]

    def set(self, key: str, value: object) -> None:
        self._values[key] = value

    def snapshot(self) -> dict[str, object]:
        return dict(self._values)


class UptimeContext:
    """Shared uptime state used across loop implementations."""

    def __init__(
        self,
        *,
        root: Path,
        name: str,
        live: LiveState,
        tools: dict[str, Tool],
        model_providers: dict[str, ModelProvider],
        model_routes: dict[str, ModelRoute],
        default_models: tuple[str, ...],
        model_environ: Mapping[str, str],
        channel_bindings: dict[str, ChannelBinding],
        channel_plugins: dict[str, ChannelPlugin],
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
        self.model_routes = dict(model_routes)
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
        feature_name: str,
        *,
        thunk: str,
        thread_id: str | None = None,
    ) -> int:
        """Queue one run request for a run-producing feature."""

        if feature_name not in RUN_FEATURES:
            raise ValueError(f"feature does not produce runs: {feature_name}")
        from .execution.runner import RunRequest

        return self.runner.enqueue(
            RunRequest(
                group=feature_name,
                origin=feature_name,
                thread_id=thread_id,
                thunk=thunk,
            )
        )

    def enqueue_delivery(
        self,
        feature_name: str,
        binding_name: str,
        delivery: InboundDelivery,
    ) -> int:
        """Queue one run request produced by one channel delivery."""

        if feature_name not in RUN_FEATURES:
            raise ValueError(f"feature does not produce runs: {feature_name}")
        from .execution.runner import RunRequest

        bound = bind_delivery(binding_name, delivery)
        metadata = dict(bound.meta)
        metadata["channel"] = binding_name
        metadata["sender"] = bound.sender
        return self.runner.enqueue(
            RunRequest(
                group=feature_name,
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


def load_model_providers() -> dict[str, ModelProvider]:
    """Load all installed model providers for one uptime."""

    from .models.ollama import create_model as create_ollama_model
    from .models.openai import create_model as create_openai_model

    providers: dict[str, ModelProvider] = {
        "openai": create_openai_model({}),
        "ollama": create_ollama_model({}),
    }
    for entry_point in entry_points(group="toolang.model"):
        try:
            factory = cast(Callable[[Mapping[str, Any]], ModelProvider], entry_point.load())
        except ModuleNotFoundError:
            continue
        provider = factory({})
        if provider.name in providers:
            continue
        providers[provider.name] = provider
    return providers


def load_model_routes(toolang_root: Path, agent_name: str) -> dict[str, ModelRoute]:
    """Load named model routes for one uptime."""

    routes: dict[str, ModelRoute] = {}
    for payload in _model_config_payloads(toolang_root, agent_name):
        raw_routes = payload.get("model_routes")
        if not isinstance(raw_routes, dict):
            continue
        for name, value in raw_routes.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                continue
            routes[name] = _parse_model_route(name, cast(dict[str, object], value))
    return routes


def load_default_models(toolang_root: Path, agent_name: str) -> tuple[str, ...]:
    """Load default model route or selector names for one uptime."""

    defaults: tuple[str, ...] = ()
    for payload in _model_config_payloads(toolang_root, agent_name):
        raw_models = payload.get("models")
        if not isinstance(raw_models, dict):
            continue
        models_table = cast(dict[str, object], raw_models)
        raw_default = models_table.get("default")
        if isinstance(raw_default, list):
            defaults = tuple(str(item).strip() for item in raw_default if str(item).strip())
    return defaults


def _parse_model_route(name: str, payload: dict[str, object]) -> ModelRoute:
    ref = _required_model_route_str(payload, "ref", route_name=name)
    provider = _required_model_route_str(payload, "provider", route_name=name)
    model = _optional_model_route_str(payload.get("model"))
    display_name = _optional_model_route_str(payload.get("name"))
    adapter = _optional_model_route_str(payload.get("adapter"))
    base_url = _optional_model_route_str(payload.get("base_url"))
    api_key_env = _optional_model_route_str(payload.get("api_key_env"))
    tools = _optional_model_route_bool(payload.get("tools"))
    streaming = _optional_model_route_bool(payload.get("streaming"))
    headers = _model_route_string_table(payload.get("headers"))
    options = (
        dict(cast(dict[str, object], payload.get("options", {})))
        if isinstance(payload.get("options"), dict)
        else {}
    )
    details = _optional_model_route_str(payload.get("details"))
    return ModelRoute(
        name=name,
        ref=ref,
        provider=provider,
        model=model,
        display_name=display_name,
        adapter=adapter,
        base_url=base_url,
        api_key_env=api_key_env,
        tools=tools,
        streaming=streaming,
        headers=headers,
        options=options,
        details=details,
    )


def _model_config_payloads(toolang_root: Path, agent_name: str) -> tuple[dict[str, object], dict[str, object]]:
    return (
        _load_toml(toolang_root / "config.toml"),
        _load_toml(toolang_root / "agents" / agent_name / "config.toml"),
    )


def _load_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    return cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))


def _required_model_route_str(payload: dict[str, object], key: str, *, route_name: str) -> str:
    value = _optional_model_route_str(payload.get(key))
    if value is None:
        raise ToolangError(f"model route {route_name!r} is missing {key}")
    return value


def _optional_model_route_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _optional_model_route_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _model_route_string_table(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        text_key = _optional_model_route_str(key)
        text_value = _optional_model_route_str(item)
        if text_key is None or text_value is None:
            continue
        result[text_key] = text_value
    return result


@dataclass(frozen=True, slots=True)
class StartupSpec:
    """One fully resolved agent startup request."""

    toolang_root: Path
    agent_name: str
    host: str
    endpoint_host: str
    port: int
    enabled_features: tuple[FeatureName, ...]
    sandbox_plugin: SandboxPlugin
    selector: SandboxSelector
    sandbox_config: dict[str, object]
    dev_artifact: Path | None
    model_selectors: tuple[str, ...]
    log_spec: str | None = None


@dataclass(frozen=True, slots=True)
class _LoadedTool(Tool):
    """One model-facing tool loaded from one named plugin."""

    plugin_name: str
    plugin_description: str | None
    leaf_tool: Tool

    @property
    def name(self) -> str:
        return join_tool_name(self.plugin_name, self.leaf_tool.name)

    def definition(self) -> ToolDefinition:
        definition = self.leaf_tool.definition()
        description = definition.description
        if self.plugin_description and self.plugin_description not in description:
            description = f"{self.plugin_description} {description}".strip()
        return ToolDefinition(
            name=self.name,
            description=description,
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
    """Create one FastAPI app for an existing feature context."""

    enabled_features = cast(tuple[str, ...], context.config.require("features.enabled"))
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
    app.state.enabled_features = enabled_features
    app.state.shutdown_signal = shutdown_signal

    @app.get("/healthz", tags=["agent"], summary="Health Check")
    def healthz() -> dict[str, object]:
        return {"ok": True, "enabled_features": list(enabled_features)}

    if "chat" in enabled_features:
        app.include_router(chat.create_router())
    if "hook" in enabled_features:
        app.include_router(hook.create_router())
    if "control" in enabled_features:
        app.include_router(create_control_router())
    if "inspect" in enabled_features:
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
    dev: Path | None = None,
    sandbox_child: bool = False,
    feature_names: Sequence[str] | None = None,
    log_spec: str | None = None,
    environ: Mapping[str, str],
    progress: ProgressSink | None = None,
) -> int:
    """Start one agent runtime."""

    _restore_termination_signal_defaults()
    spec = resolve_startup(
        host=host,
        toolang_root=toolang_root,
        agent_name=agent_name,
        endpoint_host=endpoint_host,
        port=port,
        sandbox=sandbox,
        models=models,
        dev=dev,
        feature_names=feature_names,
        log_spec=log_spec,
        environ=environ,
    )
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
            enabled_features=spec.enabled_features,
            environ=environ,
            dev_artifact=spec.dev_artifact,
            model_selectors=spec.model_selectors,
        )
    return _up_local(
        toolang_root=spec.toolang_root,
        agent_name=spec.agent_name,
        host=spec.host,
        endpoint_host=spec.endpoint_host,
        port=spec.port,
        enabled_features=spec.enabled_features,
        environ=environ,
        sandbox_child=sandbox_child,
        model_selectors=spec.model_selectors,
        log_spec=spec.log_spec,
        progress=progress,
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
    metadata: Mapping[str, object] | None = None,
    environ: Mapping[str, str],
) -> RunOutcome:
    """Execute one thunk once without starting the long-lived runtime."""

    context = _load_runtime_context(
        toolang_root=toolang_root,
        agent_name=agent_name,
        enabled_features=(),
        environ=environ,
        model_selectors=_normalize_model_selectors(models),
    )
    try:
        return asyncio.run(
            execute_run(
                context,
                RunSubmission(
                    request=RunRequest(
                        group="script",
                        origin="script",
                        thunk=input_text or "",
                        thunk_name=thunk_name,
                        metadata=dict(metadata or {}),
                    ),
                    live=context.live,
                ),
                delay_sec=0.0,
                sleep=asyncio.sleep,
            )
        )
    finally:
        context.store.close()


def prepare_runtime(
    *,
    toolang_root: Path,
    agent_name: str,
    progress: ProgressSink | None = None,
) -> None:
    """Prepare one agent runtime without starting it."""

    durable = scan_durable_state(toolang_root, agent_name)
    watch.build_prepared_state(durable, progress=progress)


def resolve_startup(
    *,
    toolang_root: Path,
    agent_name: str,
    host: str = "127.0.0.1",
    endpoint_host: str | None = None,
    port: int | None = None,
    sandbox: str | None = None,
    models: Sequence[str] | None = None,
    dev: Path | None = None,
    feature_names: Sequence[str] | None = None,
    log_spec: str | None = None,
    environ: Mapping[str, str],
) -> StartupSpec:
    """Resolve one explicit startup request into stable runtime inputs."""

    enabled_features = normalize_feature_names(feature_names or DEFAULT_ENABLED_FEATURES)
    endpoint_host = endpoint_host or _default_endpoint_host(host)
    resolved_port = resolve_runtime_port(
        host=host,
        explicit_port=port,
        toolang_root=toolang_root,
        agent_name=agent_name,
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
    return StartupSpec(
        toolang_root=toolang_root,
        agent_name=agent_name,
        host=host,
        endpoint_host=endpoint_host,
        port=resolved_port,
        enabled_features=enabled_features,
        sandbox_plugin=sandbox_plugin,
        selector=selector,
        sandbox_config=sandbox_config,
        dev_artifact=dev_artifact,
        model_selectors=_normalize_model_selectors(models),
        log_spec=log_spec.strip() if isinstance(log_spec, str) and log_spec.strip() else None,
    )


def build_run_argv(
    spec: StartupSpec,
    *,
    root: Path | None = None,
    host: str | None = None,
    endpoint_host: str | None = None,
    sandbox: str | None = None,
    models: Sequence[str] | None = None,
    sandbox_child: bool = False,
) -> tuple[str, ...]:
    """Build one explicit argv for the hidden managed-runtime run path."""

    command: list[str] = []
    if spec.log_spec is not None:
        command.extend(["--log", spec.log_spec])
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
        command.extend(["--model", selector])
    if spec.dev_artifact is not None and not sandbox_child:
        command.extend(["--dev", str(spec.dev_artifact)])
    if sandbox_child:
        command.append("--sandbox-child")
    for feature_name in spec.enabled_features:
        command.extend(["--feature", feature_name])
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
    enabled_features: tuple[FeatureName, ...],
    environ: Mapping[str, str],
    sandbox_child: bool,
    model_selectors: tuple[str, ...],
    log_spec: str | None,
    progress: ProgressSink | None = None,
) -> int:
    loop_intervals_ms = dict(DEFAULT_FEATURE_INTERVAL_MS)
    for feature_name in BACKGROUND_FEATURES:
        if feature_name in loop_intervals_ms and loop_intervals_ms[feature_name] <= 0:
            raise ValueError(f"feature interval must be positive: {feature_name}")
    cors_allowed_origins = resolve_cors_allowed_origins(
        toolang_root,
        environ=environ,
    )
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    context = _load_runtime_context(
        toolang_root=toolang_root,
        agent_name=agent_name,
        enabled_features=enabled_features,
        environ=environ,
        model_selectors=model_selectors,
        host=host,
        port=port,
        cors_allowed_origins=cors_allowed_origins or [],
        progress=progress,
    )
    live = context.live
    context.store.append_update(
        kind="started",
        payload={
            "features": list(enabled_features),
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
            "features": list(enabled_features),
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
                    features=enabled_features,
                    models=model_selectors,
                )
            bg_tasks: list[asyncio.Task[None]] = []
            if "pulse" in enabled_features:
                bg_tasks.append(pulse.spawn(context, stop_signal=stop_signal))
            if "poll" in enabled_features:
                bg_tasks.append(poll.spawn(context, stop_signal=stop_signal))
            if "watch" in enabled_features:
                bg_tasks.append(watch.spawn(context, stop_signal=stop_signal))

            runner_task = None
            if any(feature in RUN_FEATURES for feature in enabled_features):
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
            "Agent %s started root=%s features=%s",
            context.name,
            toolang_root,
            ",".join(enabled_features),
        ),
        on_running=lambda: logger.info(
            "Agent %s running on %s",
            context.name,
            webui_url,
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
    enabled_features: tuple[FeatureName, ...],
    environ: Mapping[str, str],
    model_selectors: Sequence[str] = (),
    host: str = "127.0.0.1",
    port: int = 0,
    cors_allowed_origins: Sequence[str] = (),
    progress: ProgressSink | None = None,
) -> UptimeContext:
    channel_bindings = load_channel_bindings(
        toolang_root,
        agent_name,
        environ=environ,
    )
    runtime_state = agents.load_runtime_state(toolang_root, agent_name) or {}
    durable = scan_durable_state(toolang_root, agent_name)
    prepared_state = watch.build_prepared_state(durable, progress=progress)
    live = load_live_state(prepared_state, enabled_features=enabled_features)
    normalized_model_selectors = _normalize_model_selectors(model_selectors)
    default_model_selector = normalized_model_selectors[0] if normalized_model_selectors else None
    config = UptimeConfig(
        {
            "server.host": host,
            "server.port": port,
            "server.endpoint": _runtime_endpoint_value(host=host, port=port, runtime_state=runtime_state),
            "features.enabled": tuple(enabled_features),
            "features.pulse.interval_ms": DEFAULT_FEATURE_INTERVAL_MS["pulse"],
            "features.poll.interval_ms": DEFAULT_FEATURE_INTERVAL_MS["poll"],
            "features.watch.interval_ms": DEFAULT_FEATURE_INTERVAL_MS["watch"],
            "features.watch.debounce_ms": DEFAULT_WATCH_DEBOUNCE_MS,
            "web.cors_allowed_origins": list(cors_allowed_origins),
            "models.default_selector": default_model_selector,
            "models.allowed_selectors": normalized_model_selectors,
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
        ),
        model_providers=load_model_providers(),
        model_routes=load_model_routes(toolang_root, agent_name),
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
) -> dict[str, Tool]:
    """Load tool plugins with runtime service caps exposed to service_use."""

    return load_tool_plugins(
        config=runtime_tool_plugin_config(
            toolang_root=toolang_root,
            agent_name=agent_name,
            live=live,
            environ=environ,
        )
    )


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
    plugin: SandboxPlugin,
    selector: SandboxSelector,
    sandbox_config: Mapping[str, object],
    toolang_root: Path,
    agent_name: str,
    host: str,
    endpoint_host: str,
    port: int,
    enabled_features: tuple[FeatureName, ...],
    environ: Mapping[str, str],
    dev_artifact: Path | None,
    model_selectors: tuple[str, ...],
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
        enabled_features=enabled_features,
        sandbox_plugin=plugin,
        selector=selector,
        sandbox_config=dict(sandbox_config),
        dev_artifact=dev_artifact,
        model_selectors=model_selectors,
    )
    agents.write_runtime_state(
        toolang_root,
        agent_name,
        endpoint=endpoint,
        started_at=started_at,
        pid=os.getpid(),
        sandbox=initial_sandbox_state,
        features=enabled_features,
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
        feature_names=enabled_features,
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
            features=enabled_features,
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
            features=enabled_features,
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
        features=enabled_features,
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
    for raw in models or ():
        selector = raw.strip()
        if not selector:
            raise ValueError("model selector cannot be empty")
        if selector in seen:
            continue
        seen.add(selector)
        result.append(selector)
    return tuple(result)


def normalize_feature_names(feature_names: Sequence[str]) -> tuple[FeatureName, ...]:
    """Validate and de-duplicate feature names while preserving order."""

    enabled: list[FeatureName] = []
    for feature_name in feature_names:
        if feature_name not in ALL_FEATURES:
            raise ValueError(f"unknown feature: {feature_name}")
        typed_name = cast(FeatureName, feature_name)
        if typed_name not in enabled:
            enabled.append(typed_name)
    return tuple(enabled)


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


def list_plugin_names(*, group: str) -> list[str]:
    return sorted(entry_point.name for entry_point in entry_points(group=group))


def list_plugin_infos(*, group: str) -> list[PluginInfo]:
    return sorted(
        (
            PluginInfo(
                name=entry_point.name,
                source=_entry_point_plugin_source(entry_point),
            )
            for entry_point in entry_points(group=group)
        ),
        key=lambda item: item.name,
    )


def _entry_point_plugin_source(entry_point: object) -> PluginSource:
    dist = getattr(entry_point, "dist", None)
    metadata = getattr(dist, "metadata", None)
    if metadata is not None:
        name = metadata.get("Name")
        if isinstance(name, str) and _normalize_distribution_name(name) == "toolang":
            return "built-in"
    value = getattr(entry_point, "value", None)
    if isinstance(value, str) and value.startswith("toolang."):
        return "built-in"
    return "external"


def _normalize_distribution_name(name: str) -> str:
    return name.replace("_", "-").replace(".", "-").lower()


def load_plugin_factory(name: str, *, group: str) -> FactoryT:
    for entry_point in entry_points(group=group):
        if entry_point.name == name:
            return cast(FactoryT, entry_point.load())
    raise ValueError(f"unknown {group} plugin: {name}")


def load_tool_plugins(
    *,
    config: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Tool]:
    tools: dict[str, Tool] = {}
    plugin_config = dict(config or {})
    for entry_point in entry_points(group="toolang.tool"):
        factory = cast(Callable[[Mapping[str, Any]], ToolPlugin], entry_point.load())
        plugin = factory(dict(plugin_config.get(entry_point.name, {})))
        for leaf_name, leaf_tool in plugin.tools().items():
            if leaf_name != leaf_tool.name:
                raise ValueError(
                    f"tool plugin {plugin.name!r} returned mismatched leaf tool name: "
                    f"{leaf_name!r} != {leaf_tool.name!r}"
                )
            loaded = _LoadedTool(
                plugin_name=plugin.name,
                plugin_description=plugin.description,
                leaf_tool=leaf_tool,
            )
            if loaded.name in tools:
                raise ValueError(f"duplicate tool name: {loaded.name}")
            tools[loaded.name] = loaded
    return tools


def create_channel_plugin(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> ChannelPlugin:
    factory = cast(Callable[[Mapping[str, Any]], ChannelPlugin], load_plugin_factory(name, group="toolang.channel"))
    return factory(dict(config or {}))


def create_sandbox_plugin(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> SandboxPlugin:
    factory = cast(Callable[[Mapping[str, Any]], SandboxPlugin], load_plugin_factory(name, group="toolang.sandbox"))
    return factory(dict(config or {}))


def create_model_provider(
    name: str,
    *,
    config: Mapping[str, Any] | None = None,
) -> ModelProvider:
    factory = cast(Callable[[Mapping[str, Any]], ModelProvider], load_plugin_factory(name, group="toolang.model"))
    return factory(dict(config or {}))

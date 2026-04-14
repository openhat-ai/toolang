"""Service-use tool plugin backed by mcat_cli."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import dotenv_values
from mcat_cli import auth as mcat_auth
from mcat_cli import bridge as mcat_bridge
from mcat_cli import mcp as mcat_mcp
from mcat_cli.util.connection_file import CURRENT_CONNECTION_VERSION, write_connection_file

from ..base.error import ToolangError
from ..base.protocols.tool import Tool, ToolPlugin
from ..base.types.tool import ToolContext, ToolDefinition

ServiceTransport = Literal["http", "stdio"]
ConnectionFileWriter = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class VisibleService:
    name: str
    transport: ServiceTransport
    target: str | None = None
    description: str | None = None
    command: tuple[str, ...] = ()
    port: int | None = None
    env_vars: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ServiceRuntime:
    service: VisibleService
    connection_path: Path
    session_path: Path
    token_path: Path
    env_vars: dict[str, str]


@dataclass(frozen=True, slots=True)
class _LeafTool(Tool):
    name: str
    _definition: ToolDefinition
    _invoke: Callable[[Mapping[str, Any], ToolContext], dict[str, Any]]

    def definition(self) -> ToolDefinition:
        return self._definition

    def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
        return self._invoke(arguments, context)


@dataclass(frozen=True, slots=True)
class _ServiceUseAdapter:
    plugin_name: str
    services: Mapping[str, VisibleService]
    connection_version: int
    write_connection_file: ConnectionFileWriter

    def build_tools(self) -> dict[str, Tool]:
        service_schema = self._service_schema()
        tools = {
            "bridge_start": self._tool(
                "bridge_start",
                "Start one visible service bridge or prepare one HTTP service for auth and session use.",
                _schema(
                    properties={"service": service_schema},
                    required=("service",),
                ),
                self._invoke_bridge_start,
            ),
            "bridge_stop": self._tool(
                "bridge_stop",
                "Stop one visible service bridge or clear prepared HTTP service state.",
                _schema(
                    properties={"service": service_schema},
                    required=("service",),
                ),
                self._invoke_bridge_stop,
            ),
            "init": self._tool(
                "init",
                "Initialize one visible service session after bridge setup and any required auth.",
                _schema(
                    properties={"service": service_schema},
                    required=("service",),
                ),
                self._invoke_init,
            ),
            "auth_start": self._tool(
                "auth_start",
                "Start OAuth for one visible HTTP service.",
                _schema(
                    properties={"service": service_schema},
                    required=("service",),
                ),
                self._invoke_auth_start,
            ),
            "auth_complete": self._tool(
                "auth_complete",
                "Complete OAuth for one visible HTTP service after the user authorizes access.",
                _schema(
                    properties={"service": service_schema},
                    required=("service",),
                ),
                self._invoke_auth_complete,
            ),
            "tool_list": self._tool(
                "tool_list",
                "List tools exposed by one visible service.",
                _schema(
                    properties={"service": service_schema},
                    required=("service",),
                ),
                self._invoke_tool_list,
            ),
            "tool_call": self._tool(
                "tool_call",
                "Call one tool exposed by one visible service.",
                _schema(
                    properties={
                        "service": service_schema,
                        "tool_name": {"type": "string"},
                        "input": {"type": "object"},
                    },
                    required=("service", "tool_name"),
                ),
                self._invoke_tool_call,
            ),
            "resource_list": self._tool(
                "resource_list",
                "List resources exposed by one visible service.",
                _schema(
                    properties={
                        "service": service_schema,
                        "cursor": {"type": "string"},
                    },
                    required=("service",),
                ),
                self._invoke_resource_list,
            ),
            "resource_template_list": self._tool(
                "resource_template_list",
                "List resource templates exposed by one visible service.",
                _schema(
                    properties={
                        "service": service_schema,
                        "cursor": {"type": "string"},
                    },
                    required=("service",),
                ),
                self._invoke_resource_template_list,
            ),
            "resource_read": self._tool(
                "resource_read",
                "Read one resource exposed by one visible service.",
                _schema(
                    properties={
                        "service": service_schema,
                        "resource_uri": {"type": "string"},
                    },
                    required=("service", "resource_uri"),
                ),
                self._invoke_resource_read,
            ),
            "prompt_list": self._tool(
                "prompt_list",
                "List prompts exposed by one visible service.",
                _schema(
                    properties={
                        "service": service_schema,
                        "cursor": {"type": "string"},
                    },
                    required=("service",),
                ),
                self._invoke_prompt_list,
            ),
            "prompt_get": self._tool(
                "prompt_get",
                "Get one prompt exposed by one visible service.",
                _schema(
                    properties={
                        "service": service_schema,
                        "prompt_name": {"type": "string"},
                        "input": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    required=("service", "prompt_name"),
                ),
                self._invoke_prompt_get,
            ),
        }
        return tools

    def _tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        invoke_fn: Callable[[ServiceRuntime, Mapping[str, Any]], dict[str, Any]],
    ) -> Tool:
        definition = ToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
        )

        def invoke(arguments: Mapping[str, Any], context: ToolContext) -> dict[str, Any]:
            runtime = self.runtime(arguments, context)
            result = invoke_fn(runtime, arguments)
            return {
                "ok": True,
                "result": {
                    "service": runtime.service.name,
                    "transport": runtime.service.transport,
                    "result": result,
                },
            }

        return _LeafTool(name=name, _definition=definition, _invoke=invoke)

    def _service_schema(self) -> dict[str, Any]:
        service_names = sorted(self.services)
        if service_names:
            return {"type": "string", "enum": service_names, "description": "Visible service name."}
        return {"type": "string", "description": "Visible service name."}

    def runtime(self, arguments: Mapping[str, Any], context: ToolContext) -> ServiceRuntime:
        service_name = str(arguments.get("service", "")).strip()
        service = self.services.get(service_name)
        if service is None:
            raise ToolangError(f"service is not visible to this agent: {service_name}")
        base = context.room / service.name
        return ServiceRuntime(
            service=service,
            connection_path=base / "connection.json",
            session_path=base / "runs" / context.run_id / "session.json",
            token_path=base / "token.json",
            env_vars=_service_env(context.home, service.env_vars),
        )

    def _invoke_bridge_start(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del arguments
        _safe_unlink(runtime.session_path)
        if runtime.service.transport == "http":
            _ensure_http_connection(
                runtime,
                connection_version=self.connection_version,
                write_connection_file=self.write_connection_file,
            )
            return {
                "status": "ready",
                "connection_file": str(runtime.connection_path),
            }
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_bridge.bridge_start(
                    connection_file=str(runtime.connection_path),
                    port=runtime.service.port,
                    command=list(runtime.service.command),
                )
            ),
        )

    def _invoke_bridge_stop(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del arguments
        _safe_unlink(runtime.session_path)
        if runtime.service.transport == "http":
            stopped = runtime.connection_path.exists()
            _safe_unlink(runtime.connection_path)
            return {
                "status": "stopped",
                "connection_file": str(runtime.connection_path),
                "stopped": stopped,
            }
        if not runtime.connection_path.exists():
            return {
                "connection_file": str(runtime.connection_path),
                "stopped": False,
            }
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_bridge.bridge_stop(connection_file=str(runtime.connection_path))
            ),
        )

    def _invoke_auth_start(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del arguments
        _safe_unlink(runtime.session_path)
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_auth.run_auth(
                    endpoint=None,
                    connection_file=str(runtime.connection_path),
                    key_ref=None,
                    complete=False,
                    overwrite=False,
                    callback_url=None,
                    listen=None,
                )
            ),
        )

    def _invoke_auth_complete(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del arguments
        _safe_unlink(runtime.session_path)
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_auth.run_auth(
                    endpoint=None,
                    connection_file=str(runtime.connection_path),
                    key_ref=None,
                    complete=True,
                    overwrite=False,
                    callback_url=None,
                    listen=None,
                )
            ),
        )

    def _invoke_init(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del arguments
        _safe_unlink(runtime.session_path)
        runtime.session_path.parent.mkdir(parents=True, exist_ok=True)
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_mcp.init_session(
                    connection_file=str(runtime.connection_path),
                    sess_info_file=str(runtime.session_path),
                )
            ),
        )

    def _invoke_tool_list(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        del arguments
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_mcp.list_tools(sess_info_file=str(runtime.session_path))
            ),
        )

    def _invoke_tool_call(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        tool_name = _required_text(arguments.get("tool_name"), name="tool_name")
        payload = _json_object_input(arguments.get("input"), name="input")
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_mcp.call_tool(
                    tool_name=tool_name,
                    arguments=payload,
                    sess_info_file=str(runtime.session_path),
                )
            ),
        )

    def _invoke_resource_list(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_mcp.list_resources(
                    sess_info_file=str(runtime.session_path),
                    cursor=_optional_text(arguments.get("cursor")),
                )
            ),
        )

    def _invoke_resource_template_list(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_mcp.list_resource_templates(
                    sess_info_file=str(runtime.session_path),
                    cursor=_optional_text(arguments.get("cursor")),
                )
            ),
        )

    def _invoke_resource_read(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        resource_uri = _required_text(arguments.get("resource_uri"), name="resource_uri")
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_mcp.read_resource(
                    uri=resource_uri,
                    sess_info_file=str(runtime.session_path),
                )
            ),
        )

    def _invoke_prompt_list(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_mcp.list_prompts(
                    sess_info_file=str(runtime.session_path),
                    cursor=_optional_text(arguments.get("cursor")),
                )
            ),
        )

    def _invoke_prompt_get(
        self,
        runtime: ServiceRuntime,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        prompt_name = _required_text(arguments.get("prompt_name"), name="prompt_name")
        payload = _string_map_input(arguments.get("input"), name="input")
        return _with_service_env(
            runtime,
            lambda: _wrap_mcat_errors(
                lambda: mcat_mcp.get_prompt(
                    prompt_name=prompt_name,
                    arguments=payload,
                    sess_info_file=str(runtime.session_path),
                )
            ),
        )


@dataclass(slots=True)
class ServiceUsePlugin:
    """One service plugin backed by mcat_cli modules."""

    config: dict[str, Any]
    connection_version: int
    write_connection_file: ConnectionFileWriter
    name: str
    description: str | None = None
    _tools: dict[str, Tool] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        adapter = _ServiceUseAdapter(
            plugin_name=self.name,
            services=load_visible_services(self.config.get("visible_services")),
            connection_version=self.connection_version,
            write_connection_file=self.write_connection_file,
        )
        self._tools = adapter.build_tools()

    def tools(self) -> Mapping[str, Tool]:
        return dict(self._tools)


def create_tool(config: Mapping[str, Any]) -> ToolPlugin:
    """Create the service_use tool plugin."""

    return ServiceUsePlugin(
        config=dict(config),
        connection_version=CURRENT_CONNECTION_VERSION,
        write_connection_file=write_connection_file,
        name="service_use",
        description="Access visible MCP services through mcat.",
    )


def load_visible_services(raw: object) -> dict[str, VisibleService]:
    if raw is None:
        return {}
    if not isinstance(raw, list):
        raise ValueError("visible_services must be a list")
    services: dict[str, VisibleService] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("visible_services entries must be objects")
        service = visible_service_from_data(cast(Mapping[str, Any], item))
        services[service.name] = service
    return services


def visible_service_from_data(data: Mapping[str, Any]) -> VisibleService:
    name = _required_text(data.get("name"), name="visible service name")
    raw_transport = _optional_text(data.get("transport"))
    transport: ServiceTransport
    if raw_transport in {"http", "stdio"}:
        transport = cast(ServiceTransport, raw_transport)
    elif _optional_text(data.get("target")) is not None:
        transport = "http"
    elif data.get("command") is not None:
        transport = "stdio"
    else:
        raise ValueError(f"service {name!r} is missing a usable transport")
    target = _optional_text(data.get("target"))
    command = _command_list(data.get("command"), data.get("args"))
    if transport == "http" and target is None:
        raise ValueError(f"service {name!r} is missing an HTTP target")
    if transport == "stdio" and not command:
        raise ValueError(f"service {name!r} is missing a stdio command")
    return VisibleService(
        name=name,
        transport=transport,
        target=target,
        description=_optional_text(data.get("description")),
        command=tuple(command),
        port=_optional_int(data.get("port")),
        env_vars=tuple(_string_list(data.get("env_vars"))),
    )


def token_key_ref(path: Path) -> str:
    return f"json://{path}"


def _schema(
    *,
    properties: Mapping[str, dict[str, Any]],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


def _wrap_mcat_errors(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except ToolangError:
        raise
    except Exception as exc:
        message = str(exc).strip() or "mcat command failed"
        raise ToolangError(message) from exc


def _with_service_env(runtime: ServiceRuntime, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    with patched_environ(runtime.env_vars):
        return fn()


@contextmanager
def patched_environ(values: Mapping[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _ensure_http_connection(
    runtime: ServiceRuntime,
    *,
    connection_version: int,
    write_connection_file: ConnectionFileWriter,
) -> None:
    if runtime.connection_path.exists():
        return
    runtime.connection_path.parent.mkdir(parents=True, exist_ok=True)
    write_connection_file(
        str(runtime.connection_path),
        {
            "version": connection_version,
            "kind": "http",
            "endpoint": runtime.service.target,
            "key_ref": token_key_ref(runtime.token_path),
            "flow": None,
            "state": {},
        },
    )


def _service_env(home: Path, env_names: tuple[str, ...]) -> dict[str, str]:
    if not env_names:
        return {}
    payload = {
        key: str(value)
        for key, value in dotenv_values(home / ".env").items()
        if value is not None
    }
    resolved: dict[str, str] = {}
    for name in env_names:
        value = os.environ.get(name, payload.get(name))
        if value is None:
            raise ToolangError(f"service env var is missing: {name}")
        resolved[name] = value
    return resolved


def _command_list(command: object, args: object) -> list[str]:
    items: list[str] = []
    if isinstance(command, str) and command.strip():
        items.append(command.strip())
    elif isinstance(command, list):
        items.extend(str(item).strip() for item in command if str(item).strip())
    if isinstance(args, list):
        items.extend(str(item).strip() for item in args if str(item).strip())
    return items


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("list value must be a list")
    return [str(item).strip() for item in value if str(item).strip()]


def _required_text(value: object, *, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"missing {name}")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("integer value is invalid")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("integer value is invalid") from exc


def _json_object_input(value: object, *, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolangError(f"{name} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _string_map_input(value: object, *, name: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ToolangError(f"{name} must be a JSON object")
    resolved: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise ToolangError(f"{name} values must be strings")
        resolved[str(key)] = item
    return resolved


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return

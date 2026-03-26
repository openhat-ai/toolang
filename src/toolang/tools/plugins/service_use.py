"""Default service-use tool provider backed by mcat-cli."""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

from dotenv import dotenv_values
from pydantic import BaseModel, Field

from toolang.concepts.layout import AgentHome
from toolang.concepts.tools import ToolDefinition
from toolang.errors import ToolangError

from ..contracts import ToolContext, ToolProvider

ServiceTransport = Literal["http", "stdio"]


class _VisibleService(BaseModel):
    name: str
    transport: str | None = None
    target: str | None = None
    description: str | None = None
    command: str | list[str] | None = None
    args: list[str] = Field(default_factory=list)
    port: int | None = None
    env_vars: list[str] = Field(default_factory=list)
    auth_env_var: str | None = None


class _ProviderConfig(BaseModel):
    runner: str | list[str] | None = None
    visible_services: list[_VisibleService] = Field(default_factory=list)


class _ProxyState(BaseModel):
    endpoint: str
    port: int
    command: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "_ProxyState":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")


class _ResolvedService(BaseModel):
    name: str
    transport: ServiceTransport
    endpoint: str | None = None
    command: list[str] = Field(default_factory=list)
    port: int | None = None
    env_vars: list[str] = Field(default_factory=list)
    auth_env_var: str | None = None


class ServiceUseTool(ToolProvider):
    """Default service-use tool provider using the external `mcat` CLI."""

    family = "service_use"

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = _ProviderConfig.model_validate(config)
        self._catalog = {item.name: item for item in self._config.visible_services}
        self._runner = _command_list(self._config.runner, default=["mcat"])

    def definition(self) -> ToolDefinition:
        service_names = sorted(self._catalog)
        service_description = (
            f"Available services: {', '.join(service_names)}."
            if service_names
            else "No visible services are currently available."
        )
        return ToolDefinition(
            family="service_use",
            name="service_use",
            description=(
                "Use one visible MCP service via mcat-cli. " + service_description
            ),
            parameters={
                "type": "object",
                "properties": {
                    "service": (
                        {"type": "string", "enum": service_names}
                        if service_names
                        else {"type": "string"}
                    ),
                    "action": {
                        "type": "string",
                        "enum": [
                            "tool_list",
                            "tool_call",
                            "resource_list",
                            "resource_list_template",
                            "resource_read",
                            "prompt_list",
                            "prompt_get",
                        ],
                    },
                    "tool_name": {"type": "string"},
                    "prompt_name": {"type": "string"},
                    "resource_uri": {"type": "string"},
                    "cursor": {"type": "string"},
                    "input": {"type": "object"},
                },
                "required": ["service", "action"],
                "additionalProperties": False,
            },
        )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        service_name = _required_text(arguments, "service")
        action = _required_text(arguments, "action")
        resolved = self._resolve_service(service_name)
        service_env = _load_service_env(context, resolved)
        session_path = self._ensure_session(
            resolved,
            context=context,
            service_env=service_env,
        )
        result = self._invoke_service_action(
            resolved,
            action=action,
            arguments=arguments,
            session_path=session_path,
            service_env=service_env,
            cwd=context.agent.home,
        )
        return {
            "service": resolved.name,
            "transport": resolved.transport,
            "action": action,
            "result": result,
        }

    def _resolve_service(self, service_name: str) -> _ResolvedService:
        visible = self._catalog.get(service_name)
        if visible is None:
            raise ToolangError(f"service is not visible to this agent: {service_name}")
        transport = _transport_from_visible(visible)
        if transport == "http":
            endpoint = _normalized_text(visible.target)
            if endpoint is None:
                raise ToolangError(
                    f"service {service_name!r} is missing an MCP HTTP endpoint in front matter."
                )
            return _ResolvedService(
                name=service_name,
                transport="http",
                endpoint=endpoint,
                env_vars=list(visible.env_vars),
                auth_env_var=_normalized_text(visible.auth_env_var),
            )
        if transport == "stdio":
            return _ResolvedService(
                name=service_name,
                transport="stdio",
                command=_service_command(visible),
                port=_normalized_port(visible.port),
                env_vars=list(visible.env_vars),
                auth_env_var=_normalized_text(visible.auth_env_var),
            )
        raise ToolangError(
            f"service {service_name!r} is missing a usable transport in service front matter."
        )

    def _ensure_session(
        self,
        service: _ResolvedService,
        *,
        context: ToolContext,
        service_env: dict[str, str],
    ) -> Path:
        state_dir = _service_state_dir(context, service.name)
        state_dir.mkdir(parents=True, exist_ok=True)
        session_path = state_dir / "session.json"
        if service.transport == "http":
            if session_path.exists():
                return session_path
            _init_session(
                runner=self._runner,
                endpoint=service.endpoint or "",
                session_path=session_path,
                key_ref=(
                    f"env://{service.auth_env_var}" if service.auth_env_var is not None else None
                ),
                cwd=context.agent.home,
                service_env=service_env,
            )
            return session_path

        proxy_state_path = state_dir / "proxy.json"
        proxy_state = _ensure_stdio_proxy(
            runner=self._runner,
            service=service,
            proxy_state_path=proxy_state_path,
            cwd=context.agent.home,
            service_env=service_env,
        )
        if (
            not session_path.exists()
            or not _session_matches_endpoint(session_path, proxy_state.endpoint)
        ):
            _init_session(
                runner=self._runner,
                endpoint=proxy_state.endpoint,
                session_path=session_path,
                key_ref=(
                    f"env://{service.auth_env_var}" if service.auth_env_var is not None else None
                ),
                cwd=context.agent.home,
                service_env=service_env,
            )
        return session_path

    def _invoke_service_action(
        self,
        service: _ResolvedService,
        *,
        action: str,
        arguments: dict[str, Any],
        session_path: Path,
        service_env: dict[str, str],
        cwd: Path,
    ) -> dict[str, Any]:
        base = ["-s", str(session_path)]
        if action == "tool_list":
            return _run_mcat_json(
                self._runner,
                ["tool", "list", *base],
                cwd=cwd,
                service_env=service_env,
            )
        if action == "tool_call":
            tool_name = _required_text(arguments, "tool_name")
            payload = _json_input(arguments.get("input"), name="input")
            return _run_mcat_json(
                self._runner,
                ["tool", "call", tool_name, "-i", json.dumps(payload, ensure_ascii=False), *base],
                cwd=cwd,
                service_env=service_env,
            )
        if action == "resource_list":
            args = ["resource", "list", *base]
            cursor = _normalized_text(arguments.get("cursor"))
            if cursor is not None:
                args.extend(["--cursor", cursor])
            return _run_mcat_json(self._runner, args, cwd=cwd, service_env=service_env)
        if action == "resource_list_template":
            args = ["resource", "list-template", *base]
            cursor = _normalized_text(arguments.get("cursor"))
            if cursor is not None:
                args.extend(["--cursor", cursor])
            return _run_mcat_json(self._runner, args, cwd=cwd, service_env=service_env)
        if action == "resource_read":
            resource_uri = _required_text(arguments, "resource_uri")
            return _run_mcat_json(
                self._runner,
                ["resource", "read", resource_uri, *base],
                cwd=cwd,
                service_env=service_env,
            )
        if action == "prompt_list":
            args = ["prompt", "list", *base]
            cursor = _normalized_text(arguments.get("cursor"))
            if cursor is not None:
                args.extend(["--cursor", cursor])
            return _run_mcat_json(self._runner, args, cwd=cwd, service_env=service_env)
        if action == "prompt_get":
            prompt_name = _required_text(arguments, "prompt_name")
            payload = _string_map_input(arguments.get("input"), name="input")
            return _run_mcat_json(
                self._runner,
                ["prompt", "get", prompt_name, "-i", json.dumps(payload, ensure_ascii=False), *base],
                cwd=cwd,
                service_env=service_env,
            )
        raise ToolangError(f"unsupported service_use action: {action}")


def create_service_use_tool(config: dict[str, Any]) -> ToolProvider:
    """Create the default `service_use` tool provider."""

    return ServiceUseTool(config)


def _ensure_stdio_proxy(
    *,
    runner: list[str],
    service: _ResolvedService,
    proxy_state_path: Path,
    cwd: Path,
    service_env: dict[str, str],
) -> _ProxyState:
    if proxy_state_path.exists():
        state = _ProxyState.load(proxy_state_path)
        status = _run_mcat_json(
            runner,
            ["proxy", "status", str(state.port)],
            cwd=cwd,
            service_env=service_env,
        )
        if status.get("running") is True:
            return state
    port = service.port or _pick_port()
    result = _run_mcat_json(
        runner,
        ["proxy", "up", str(port), "--", *service.command],
        cwd=cwd,
        service_env=service_env,
    )
    endpoint = _required_result_text(result, "endpoint")
    actual_port = _port_from_endpoint(endpoint)
    state = _ProxyState(endpoint=endpoint, port=actual_port, command=list(service.command))
    state.save(proxy_state_path)
    return state


def _init_session(
    *,
    runner: list[str],
    endpoint: str,
    session_path: Path,
    key_ref: str | None,
    cwd: Path,
    service_env: dict[str, str],
) -> None:
    session_path.parent.mkdir(parents=True, exist_ok=True)
    args = ["init", endpoint]
    if key_ref is not None:
        args.extend(["-k", key_ref])
    args.extend(["-o", str(session_path)])
    _run_mcat_json(runner, args, cwd=cwd, service_env=service_env)
    if not session_path.exists():
        raise ToolangError(f"mcat did not create session file: {session_path}")


def _run_mcat_json(
    runner: list[str],
    args: list[str],
    *,
    cwd: Path,
    service_env: dict[str, str],
) -> dict[str, Any]:
    command = [*runner, *args]
    env = os.environ.copy()
    env.update(service_env)
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ToolangError(
            f"service_use requires the mcat CLI. Command not found: {command[0]!r}"
        ) from exc
    stdout = completed.stdout.strip()
    stderr = completed.stderr.strip()
    payload = _parse_mcat_payload(stdout, stderr=stderr)
    if completed.returncode != 0:
        message = _normalized_text(payload.get("error")) or stderr or "mcat command failed"
        raise ToolangError(message)
    if payload.get("ok") is not True:
        message = _normalized_text(payload.get("error")) or stderr or "mcat command failed"
        raise ToolangError(message)
    result = payload.get("result")
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ToolangError("mcat command returned a non-object result")
    return result


def _parse_mcat_payload(stdout: str, *, stderr: str) -> dict[str, Any]:
    if not stdout:
        raise ToolangError(stderr or "mcat command produced no JSON output")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ToolangError(f"mcat command returned invalid JSON: {stdout}") from exc
    if not isinstance(payload, dict):
        raise ToolangError("mcat command returned a non-object JSON payload")
    return payload


def _service_state_dir(context: ToolContext, service_name: str) -> Path:
    room = AgentHome.resolve(context.agent.home).room(context.agent.name)
    return room.service_use_binding_dir("mcat", service_name)


def _transport_from_visible(service: _VisibleService) -> ServiceTransport | None:
    transport = _normalized_text(service.transport)
    if transport in {"http", "stdio"}:
        return cast(ServiceTransport, transport)
    if _normalized_text(service.target) is not None:
        return "http"
    if service.command is not None:
        return "stdio"
    return None


def _service_command(service: _VisibleService) -> list[str]:
    raw = service.command
    if isinstance(raw, list):
        command = [str(item).strip() for item in raw if str(item).strip()]
        if command:
            return command
    elif isinstance(raw, str):
        if service.args:
            return [raw, *service.args]
        command = shlex.split(raw)
        if command:
            return command
    raise ToolangError("stdio service front matter requires a non-empty command.")


def _load_service_env(context: ToolContext, service: _ResolvedService) -> dict[str, str]:
    if not service.env_vars:
        return {}
    env_path = AgentHome.resolve(context.agent.home).env_path
    raw_values = dotenv_values(env_path) if env_path.exists() else {}
    loaded: dict[str, str] = {}
    missing: list[str] = []
    for env_name in service.env_vars:
        value = raw_values.get(env_name)
        if value is None or not str(value).strip():
            missing.append(env_name)
            continue
        loaded[env_name] = str(value)
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise ToolangError(
            f"service_use requires {missing_text} in {env_path}"
        )
    return loaded


def _command_list(raw: object, *, default: list[str]) -> list[str]:
    if raw is None:
        return list(default)
    if isinstance(raw, list):
        values = [str(item).strip() for item in raw if str(item).strip()]
        if values:
            return values
    if isinstance(raw, str):
        values = shlex.split(raw)
        if values:
            return values
    raise ToolangError("service_use runner must be a non-empty string or string array")


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = _normalized_text(arguments.get(name))
    if value is None:
        raise ToolangError(f"service_use requires {name!r}")
    return value


def _required_result_text(payload: dict[str, Any], name: str) -> str:
    value = _normalized_text(payload.get(name))
    if value is None:
        raise ToolangError(f"mcat result is missing {name!r}")
    return value


def _normalized_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalized_port(value: object) -> int | None:
    if value is None:
        return None
    try:
        port = int(str(value))
    except ValueError as exc:
        raise ToolangError("service_use stdio port must be an integer") from exc
    if port <= 0 or port > 65535:
        raise ToolangError("service_use stdio port must be in range 1..65535")
    return port


def _json_input(value: object, *, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolangError(f"service_use {name!r} must be a JSON object")
    return {str(key): item for key, item in value.items()}


def _string_map_input(value: object, *, name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ToolangError(f"service_use {name!r} must be a JSON object")
    return {str(key): str(item) for key, item in value.items()}


def _session_matches_endpoint(session_path: Path, endpoint: str) -> bool:
    try:
        payload = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    current = _normalized_text(payload.get("endpoint"))
    return current == endpoint


def _pick_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_from_endpoint(endpoint: str) -> int:
    parsed = urlsplit(endpoint)
    if parsed.port is None:
        raise ToolangError(f"proxy endpoint is missing a port: {endpoint}")
    return int(parsed.port)

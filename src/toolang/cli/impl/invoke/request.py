"""Invoke request parsing and input coercion."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import click

from toolang.work import files as file_requests
from toolang.state.state import split_cap_selectors
from toolang.execution.executor.request import ExecutableKind
from toolang.lang.ast import AgicDecl, FlowDecl, Parameter
from toolang.plugin.models.resolution import split_model_selectors
from toolang.plugin.tools.registry import split_tool_selectors


@dataclass(frozen=True, slots=True)
class RoamingInvokeRequest:
    executable_name: str | None
    executable_kind: ExecutableKind
    verbosity: int
    input_text: str | None
    models: tuple[str, ...]
    tools: tuple[str, ...]
    caps: tuple[str, ...]
    sandbox: str | None
    invoke_params: dict[str, object]
    invoke_parts: list[dict[str, str]]
    quiet: bool = False


class MissingInvokeInput(click.ClickException):
    pass


def consume_control_options(
    argv: list[str],
) -> tuple[
    bool,
    int,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str | None,
    list[str],
]:
    quiet = False
    verbosity = 0
    models: list[str] = []
    tools: list[str] = []
    caps: list[str] = []
    sandbox: str | None = None
    remaining: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            remaining.extend(argv[index:])
            break
        if token in {"--quiet", "-q"}:
            quiet = True
            index += 1
            continue
        if token == "--verbose":
            verbosity += 1
            index += 1
            continue
        if short_verbosity := _short_verbosity(token):
            verbosity += short_verbosity
            index += 1
            continue
        if token.startswith("--models="):
            model = token.partition("=")[2].strip()
            if model:
                models.append(model)
                index += 1
                continue
        if token == "--models" and index + 1 < len(argv):
            model = argv[index + 1].strip()
            if model:
                models.append(model)
                index += 2
                continue
        if token.startswith("--tools="):
            tool = token.partition("=")[2].strip()
            if tool:
                tools.append(tool)
                index += 1
                continue
        if token == "--tools" and index + 1 < len(argv):
            tool = argv[index + 1].strip()
            if tool:
                tools.append(tool)
                index += 2
                continue
        if token.startswith("--caps="):
            cap = token.partition("=")[2].strip()
            if cap:
                caps.append(cap)
                index += 1
                continue
        if token == "--caps" and index + 1 < len(argv):
            cap = argv[index + 1].strip()
            if cap:
                caps.append(cap)
                index += 2
                continue
        if token.startswith("--sandbox="):
            sandbox = _sandbox_value(token.partition("=")[2])
            index += 1
            continue
        if token == "--sandbox":
            if index + 1 >= len(argv):
                raise click.ClickException("--sandbox requires a value")
            sandbox = _sandbox_value(argv[index + 1])
            index += 2
            continue
        remaining.append(token)
        index += 1
    return (
        quiet,
        verbosity,
        split_model_selectors(tuple(models)),
        split_tool_selectors(tuple(tools)),
        split_cap_selectors(tuple(caps)),
        sandbox,
        remaining,
    )


def parse_request(
    executable: AgicDecl | FlowDecl,
    argv: list[str],
    *,
    executable_kind: ExecutableKind,
    leading_verbosity: int = 0,
    leading_models: tuple[str, ...] = (),
    leading_tools: tuple[str, ...] = (),
    leading_caps: tuple[str, ...] = (),
    leading_sandbox: str | None = None,
) -> RoamingInvokeRequest:
    executable_params = tuple(executable.params)
    param_index = {param.name: param for param in executable_params}
    invoke_params: dict[str, object] = {}
    parts: list[str] = []
    models = list(leading_models)
    tools = list(leading_tools)
    caps = list(leading_caps)
    sandbox = leading_sandbox
    quiet = False
    verbosity = leading_verbosity
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            parts.extend(argv[index + 1 :])
            break
        if token.startswith("--models="):
            model = token.partition("=")[2].strip()
            if not model:
                raise click.ClickException("--models requires a value")
            models.extend(split_model_selectors((model,)))
            index += 1
            continue
        if token == "--models":
            if index + 1 >= len(argv):
                raise click.ClickException("--models requires a value")
            model = argv[index + 1].strip()
            if not model:
                raise click.ClickException("--models requires a value")
            models.extend(split_model_selectors((model,)))
            index += 2
            continue
        if token.startswith("--tools="):
            tool = token.partition("=")[2].strip()
            if not tool:
                raise click.ClickException("--tools requires a value")
            tools.extend(split_tool_selectors((tool,)))
            index += 1
            continue
        if token == "--tools":
            if index + 1 >= len(argv):
                raise click.ClickException("--tools requires a value")
            tool = argv[index + 1].strip()
            if not tool:
                raise click.ClickException("--tools requires a value")
            tools.extend(split_tool_selectors((tool,)))
            index += 2
            continue
        if token.startswith("--caps="):
            cap = token.partition("=")[2].strip()
            if not cap:
                raise click.ClickException("--caps requires a value")
            caps.extend(split_cap_selectors((cap,)))
            index += 1
            continue
        if token == "--caps":
            if index + 1 >= len(argv):
                raise click.ClickException("--caps requires a value")
            cap = argv[index + 1].strip()
            if not cap:
                raise click.ClickException("--caps requires a value")
            caps.extend(split_cap_selectors((cap,)))
            index += 2
            continue
        if token.startswith("--sandbox="):
            sandbox = _sandbox_value(token.partition("=")[2])
            index += 1
            continue
        if token == "--sandbox":
            if index + 1 >= len(argv):
                raise click.ClickException("--sandbox requires a value")
            sandbox = _sandbox_value(argv[index + 1])
            index += 2
            continue
        if token in {"--quiet", "-q"}:
            quiet = True
            index += 1
            continue
        if token == "--verbose":
            verbosity += 1
            index += 1
            continue
        if short_verbosity := _short_verbosity(token):
            verbosity += short_verbosity
            index += 1
            continue
        if token.startswith("--"):
            raise click.ClickException(f"unknown Toolang invoke option: {token}")
        param_name, has_assignment, raw_value = token.partition("=")
        param = param_index.get(param_name) if has_assignment else None
        if param is not None:
            if param_name in invoke_params:
                raise click.ClickException(f"duplicate invoke parameter: {param_name}")
            invoke_params[param_name] = _coerce_invoke_value(raw_value, param=param)
            index += 1
            continue
        parts.append(token)
        index += 1
    missing = [
        param.name
        for param in executable_params
        if not param.optional and param.name not in invoke_params
    ]
    if missing:
        joined = ", ".join(f"{name}=..." for name in missing)
        raise click.ClickException(f"missing required invoke parameters: {joined}")
    target_name = executable_name(executable)
    accepts_message = executable.input is not None
    if accepts_message and not parts:
        raise MissingInvokeInput(f"target {target_name!r} requires at least one INPUT")
    if parts and not accepts_message:
        raise click.ClickException(f"target {target_name!r} does not accept INPUT")
    input_text, invoke_parts = _render_roaming_input(parts) if parts else (None, [])
    return RoamingInvokeRequest(
        executable_name=target_name,
        executable_kind=executable_kind,
        verbosity=verbosity,
        input_text=input_text,
        models=tuple(dict.fromkeys(models)),
        tools=tuple(dict.fromkeys(tools)),
        caps=tuple(dict.fromkeys(caps)),
        sandbox=sandbox,
        invoke_params=invoke_params,
        invoke_parts=invoke_parts,
        quiet=quiet,
    )


def _sandbox_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise click.ClickException("--sandbox requires a value")
    return value


def _parse_boolean_value(raw: str, *, option_name: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise click.ClickException(f"{option_name} expects a boolean value")


def _short_verbosity(token: str) -> int:
    return len(token) - 1 if token.startswith("-") and set(token[1:]) == {"v"} else 0


def _coerce_invoke_value(raw: str, *, param: Parameter) -> object:
    type_name = param.type_name
    if type_name == "Number":
        try:
            if any(marker in raw for marker in (".", "e", "E")):
                return float(raw)
            return int(raw)
        except ValueError as exc:
            raise click.ClickException(f"{param.name} expects a number") from exc
    if type_name == "Boolean":
        return _parse_boolean_value(raw, option_name=param.name)
    if type_name == "Path":
        return str(Path(raw).expanduser().resolve())
    return raw


def _render_roaming_input(parts: list[str]) -> tuple[str, list[dict[str, str]]]:
    rendered: list[str] = []
    invoke_parts: list[dict[str, str]] = []
    for part in parts:
        if part.startswith("@@"):
            text = part[1:]
            rendered.append(text)
            invoke_parts.append({"type": "text", "text": text})
            continue
        if part.startswith("@"):
            candidate = Path(part[1:]).expanduser().resolve()
            if not candidate.exists():
                raise click.ClickException(f"invoke input not found: {candidate}")
            text, path_parts = file_requests.render_file_input(candidate)
            rendered.append(text)
            invoke_parts.extend(path_parts)
            continue
        rendered.append(part)
        invoke_parts.append({"type": "text", "text": part})
    return "\n\n".join(rendered), invoke_parts


def default_agic_name(agic: AgicDecl) -> str:
    return agic.name or "default"


def executable_name(executable: AgicDecl | FlowDecl) -> str:
    if isinstance(executable, AgicDecl):
        return default_agic_name(executable)
    return executable.name

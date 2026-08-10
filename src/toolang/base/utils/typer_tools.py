"""Helpers for exposing Typer commands as Toolang tools."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar
from contextlib import (
    AbstractContextManager,
    chdir,
    nullcontext,
    redirect_stderr,
    redirect_stdout,
)
from dataclasses import dataclass, field
import io
import json
from typing import TYPE_CHECKING, Any, cast

from ..errors import ToolangError
from ..protocols.tool import AgentTool
from ..types.tool import ToolContext, ToolDefinition

if TYPE_CHECKING:
    import typer

_SKIPPED_PARAM_NAMES = frozenset({"help", "install_completion", "show_completion"})
_CURRENT_TOOL_CONTEXT: ContextVar[ToolContext | None] = ContextVar(
    "toolang_experiments_current_tool_context",
    default=None,
)
_CLICK_MODULE: Any | None = None
_GET_COMMAND: Callable[[Any], Any] | None = None

TyperToolPrepare = Callable[[tuple[str, ...], Mapping[str, Any], ToolContext], Any]
TyperToolArgumentInjector = Callable[
    [tuple[str, ...], Mapping[str, Any], ToolContext, Any],
    Mapping[str, Any],
]
TyperToolScopeFactory = Callable[
    [tuple[str, ...], Mapping[str, Any], ToolContext, Any],
    AbstractContextManager[None] | None,
]
TyperToolExtraArgvFactory = Callable[
    [tuple[str, ...], Mapping[str, Any], ToolContext, Any],
    Sequence[str],
]
TyperToolResultTransformer = Callable[
    [tuple[str, ...], dict[str, Any], Mapping[str, Any], ToolContext, Any],
    dict[str, Any],
]


@dataclass(frozen=True, slots=True)
class TyperToolConfig:
    """One optional adapter config for one Typer leaf command."""

    name: str | None = None
    description: str | None = None
    hidden_params: frozenset[str] = field(default_factory=frozenset)
    param_aliases: Mapping[str, str] = field(default_factory=dict)
    param_schemas: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    extra_params: Mapping[str, dict[str, Any]] = field(default_factory=dict)
    required_params: frozenset[str] = field(default_factory=frozenset)
    prepare: TyperToolPrepare | None = None
    inject_arguments: TyperToolArgumentInjector | None = None
    extra_argv: TyperToolExtraArgvFactory | None = None
    invoke_scope: TyperToolScopeFactory | None = None
    transform_result: TyperToolResultTransformer | None = None


@dataclass(frozen=True, slots=True)
class _CommandScope:
    """One ancestor scope and the child token it activates."""

    child_token: str
    params: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class _LeafCommandSpec:
    """One leaf Typer command exposed as a tool."""

    tool_name: str
    prog_name: str
    root_command: Any
    path_tokens: tuple[str, ...]
    scopes: tuple[_CommandScope, ...]
    command: Any
    config: TyperToolConfig

    def definition(self) -> ToolDefinition:
        params = [*self._scope_params(), *_visible_params(self.command.params)]
        return ToolDefinition(
            name=self.tool_name,
            description=self.config.description
            or _command_description(
                self.command, self.path_tokens, prog_name=self.prog_name
            ),
            parameters=_schema_from_click_params(
                params,
                hidden_params=self.config.hidden_params,
                param_aliases=self.config.param_aliases,
                param_schemas=self.config.param_schemas,
                extra_params=self.config.extra_params,
                required_params=self.config.required_params,
            ),
        )

    def invoke(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        click = _require_click()
        prepared = (
            self.config.prepare(self.path_tokens, arguments, context)
            if self.config.prepare is not None
            else None
        )
        cli_arguments = _cli_arguments(
            arguments, param_aliases=self.config.param_aliases
        )
        if self.config.inject_arguments is not None:
            cli_arguments.update(
                self.config.inject_arguments(
                    self.path_tokens, arguments, context, prepared
                )
            )
        argv = _build_argv(
            scopes=self.scopes,
            command=self.command,
            arguments=cli_arguments,
        )
        if self.config.extra_argv is not None:
            argv.extend(
                str(item)
                for item in self.config.extra_argv(
                    self.path_tokens, arguments, context, prepared
                )
            )
        stdout = io.StringIO()
        stderr = io.StringIO()
        exit_code = 0
        result: Any = None
        reset_token = _CURRENT_TOOL_CONTEXT.set(context)
        invoke_scope = (
            self.config.invoke_scope(self.path_tokens, arguments, context, prepared)
            if self.config.invoke_scope is not None
            else None
        )
        try:
            with (
                chdir(context.wd),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                invoke_scope or nullcontext(),
            ):
                result = self.root_command.main(
                    args=argv,
                    prog_name=self.prog_name,
                    standalone_mode=False,
                )
        except click.ClickException as exc:
            exit_code = exc.exit_code
            with redirect_stderr(stderr):
                exc.show()
        except click.exceptions.Exit as exc:
            exit_code = int(exc.exit_code or 0)
        finally:
            _CURRENT_TOOL_CONTEXT.reset(reset_token)
        payload = {
            "command": " ".join([self.prog_name, *argv]),
            "argv": argv,
            "exit_code": exit_code,
            "ok": exit_code == 0,
            "stdout": stdout.getvalue(),
            "stderr": stderr.getvalue(),
        }
        if result is not None:
            payload["result"] = _normalize_result(result)
        if self.config.transform_result is not None:
            return self.config.transform_result(
                self.path_tokens,
                payload,
                arguments,
                context,
                prepared,
            )
        return payload

    def _scope_params(self) -> list[Any]:
        params: list[Any] = []
        for scope in self.scopes:
            params.extend(scope.params)
        return params


@dataclass(frozen=True, slots=True)
class _TyperTool(AgentTool):
    """AgentTool backed by one Typer leaf command."""

    spec: _LeafCommandSpec

    @property
    def name(self) -> str:
        return self.spec.tool_name

    def definition(self) -> ToolDefinition:
        return self.spec.definition()

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.spec.invoke, arguments, context)


def create_typer_tools(
    app: "typer.Typer",
    *,
    prog_name: str,
    name_prefix: str | None = None,
    include_paths: Iterable[tuple[str, ...]] | None = None,
    configs: Mapping[tuple[str, ...], TyperToolConfig] | None = None,
) -> dict[str, AgentTool]:
    """Build tools for each leaf command in one Typer app."""

    root = _require_get_command()(app)
    resolved_configs = dict(configs or {})
    selected_paths = {
        tuple(path) for path in (include_paths or resolved_configs.keys())
    }
    specs = _collect_leaf_specs(
        root_command=root,
        command=root,
        path_tokens=(),
        scopes=(),
        prog_name=prog_name,
        name_prefix=name_prefix,
        include_paths=selected_paths,
        configs=resolved_configs,
    )
    return {spec.tool_name: _TyperTool(spec=spec) for spec in specs}


def current_tool_context() -> ToolContext:
    """Return the current tool context while one Typer-backed tool is running."""

    context = _CURRENT_TOOL_CONTEXT.get()
    if context is None:
        raise ToolangError(
            "tool context is only available while a Typer-backed tool is running"
        )
    return context


def _collect_leaf_specs(
    root_command: Any,
    command: Any,
    *,
    path_tokens: tuple[str, ...],
    scopes: tuple[_CommandScope, ...],
    prog_name: str,
    name_prefix: str | None,
    include_paths: set[tuple[str, ...]],
    configs: Mapping[tuple[str, ...], TyperToolConfig],
) -> list[_LeafCommandSpec]:
    click = _require_click()
    if isinstance(command, click.Group) and command.commands:
        result: list[_LeafCommandSpec] = []
        parent_params = tuple(_visible_params(command.params))
        for token, child in command.commands.items():
            child_path = (*path_tokens, token)
            if include_paths and not _is_selected_path(include_paths, child_path):
                continue
            if child.hidden and not _is_selected_path(
                include_paths or set(configs), child_path
            ):
                continue
            result.extend(
                _collect_leaf_specs(
                    root_command,
                    child,
                    path_tokens=child_path,
                    scopes=(
                        *scopes,
                        _CommandScope(child_token=token, params=parent_params),
                    ),
                    prog_name=prog_name,
                    name_prefix=name_prefix,
                    include_paths=include_paths,
                    configs=configs,
                )
            )
        return result
    normalized_tokens = path_tokens or (
        _normalize_tool_name(command.name or prog_name),
    )
    config = configs.get(normalized_tokens, TyperToolConfig())
    if include_paths and normalized_tokens not in include_paths:
        return []
    tool_name = config.name or "_".join(
        _normalize_tool_name(token) for token in normalized_tokens
    )
    if name_prefix:
        tool_name = f"{_normalize_tool_name(name_prefix)}_{tool_name}"
    return [
        _LeafCommandSpec(
            tool_name=tool_name,
            prog_name=prog_name,
            root_command=root_command,
            path_tokens=normalized_tokens,
            scopes=scopes,
            command=command,
            config=config,
        )
    ]


def _visible_params(params: Iterable[Any]) -> list[Any]:
    visible: list[Any] = []
    for param in params:
        if param.name in _SKIPPED_PARAM_NAMES:
            continue
        if getattr(param, "hidden", False):
            continue
        visible.append(param)
    return visible


def _is_selected_path(paths: Iterable[tuple[str, ...]], path: tuple[str, ...]) -> bool:
    selected = tuple(paths)
    if not selected:
        return False
    return any(config_path[: len(path)] == path for config_path in selected)


def _command_description(
    command: Any, path_tokens: tuple[str, ...], *, prog_name: str
) -> str:
    summary = (command.help or command.short_help or "").strip()
    if summary:
        return summary
    return f"Run `{prog_name} {' '.join(path_tokens)}`."


def _schema_from_click_params(
    params: Iterable[Any],
    *,
    hidden_params: frozenset[str],
    param_aliases: Mapping[str, str],
    param_schemas: Mapping[str, dict[str, Any]],
    extra_params: Mapping[str, dict[str, Any]],
    required_params: frozenset[str],
) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in params:
        if param.name is None:
            continue
        if param.name in hidden_params:
            continue
        param_name = param_aliases.get(param.name, param.name)
        if param_name in properties:
            raise ToolangError(
                f"duplicate parameter while building Typer tool: {param_name}"
            )
        schema = dict(param_schemas.get(param_name) or _schema_for_click_param(param))
        help_text = getattr(param, "help", None)
        if isinstance(help_text, str) and help_text.strip():
            schema["description"] = help_text.strip()
        properties[param_name] = schema
        if param.required:
            required.append(param_name)
    for param_name, schema in extra_params.items():
        if param_name in properties:
            raise ToolangError(
                f"duplicate parameter while building Typer tool: {param_name}"
            )
        properties[param_name] = dict(schema)
        if param_name in required_params:
            required.append(param_name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _schema_for_click_param(param: Any) -> dict[str, Any]:
    if getattr(param, "multiple", False) or getattr(param, "nargs", 1) != 1:
        return {
            "type": "array",
            "items": _schema_for_click_type(getattr(param, "type", None)),
        }
    schema = _schema_for_click_type(getattr(param, "type", None))
    default = getattr(param, "default", None)
    if default not in (None, (), []):
        schema["default"] = default
    return schema


def _schema_for_click_type(param_type: Any) -> dict[str, Any]:
    click = _require_click()
    custom_schema = getattr(param_type, "tool_schema", None)
    if callable(custom_schema):
        payload = custom_schema()
        if isinstance(payload, dict):
            return dict(payload)
    elif isinstance(custom_schema, dict):
        return dict(custom_schema)
    if isinstance(param_type, click.Choice):
        return {"type": "string", "enum": list(param_type.choices)}
    type_name = getattr(param_type, "name", "")
    if type_name == "integer":
        return {"type": "integer"}
    if type_name == "float":
        return {"type": "number"}
    if type_name == "boolean":
        return {"type": "boolean"}
    return {"type": "string"}


def _build_argv(
    *,
    scopes: tuple[_CommandScope, ...],
    command: Any,
    arguments: Mapping[str, Any],
) -> list[str]:
    argv: list[str] = []
    for scope in scopes:
        argv.extend(_serialize_params(scope.params, arguments))
        argv.append(scope.child_token)
    argv.extend(_serialize_params(_visible_params(command.params), arguments))
    return argv


def _serialize_params(params: Iterable[Any], values: Mapping[str, Any]) -> list[str]:
    click = _require_click()
    options: list[str] = []
    args: list[str] = []
    for param in params:
        if param.name is None or param.name not in values:
            continue
        raw_value = values[param.name]
        if isinstance(param, click.Option):
            options.extend(_serialize_option(param, raw_value))
            continue
        if isinstance(param, click.Argument):
            args.extend(_serialize_argument(param, raw_value))
    return [*options, *args]


def _serialize_option(option: Any, raw_value: Any) -> list[str]:
    if option.is_flag:
        bool_value = bool(raw_value)
        if bool_value:
            positive = _preferred_option_name(option.opts)
            return [positive] if positive else []
        negative = _preferred_option_name(option.secondary_opts)
        return [negative] if negative else []
    flag = _preferred_option_name(option.opts)
    if flag is None:
        raise ToolangError(f"option has no public flag: {option.name}")
    if option.multiple:
        items = _as_sequence(raw_value, name=option.name or "option")
        serialized: list[str] = []
        for item in items:
            serialized.extend([flag, _stringify(item)])
        return serialized
    if option.nargs != 1:
        items = _as_sequence(raw_value, name=option.name or "option")
        return [flag, *[_stringify(item) for item in items]]
    return [flag, _stringify(raw_value)]


def _serialize_argument(argument: Any, raw_value: Any) -> list[str]:
    if argument.nargs == 1:
        return [_stringify(raw_value)]
    return [
        _stringify(item)
        for item in _as_sequence(raw_value, name=argument.name or "argument")
    ]


def _as_sequence(value: Any, *, name: str) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    raise ToolangError(f"{name} requires a list value")


def _preferred_option_name(options: Iterable[str]) -> str | None:
    candidates = [item for item in options if item]
    if not candidates:
        return None
    long_options = [item for item in candidates if item.startswith("--")]
    if long_options:
        return cast(str, sorted(long_options, key=len)[0])
    return candidates[0]


def _normalize_tool_name(value: str) -> str:
    return value.replace("-", "_").strip("_")


def _cli_arguments(
    values: Mapping[str, Any],
    *,
    param_aliases: Mapping[str, str],
) -> dict[str, Any]:
    reverse_aliases = {
        tool_name: cli_name for cli_name, tool_name in param_aliases.items()
    }
    return {reverse_aliases.get(name, name): value for name, value in values.items()}


def _normalize_result(value: Any) -> Any:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, tuple, str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _stringify(value: Any) -> str:
    if value is None:
        raise ToolangError("CLI tool arguments may not be null")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _require_click() -> Any:
    global _CLICK_MODULE
    if _CLICK_MODULE is None:
        try:
            import click
        except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
            raise ToolangError(
                "Typer tool helpers require the optional 'click' and 'typer' dependencies."
            ) from exc
        _CLICK_MODULE = click
    return _CLICK_MODULE


def _require_get_command() -> Callable[[Any], Any]:
    global _GET_COMMAND
    if _GET_COMMAND is None:
        try:
            from typer.main import get_command
        except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent
            raise ToolangError(
                "Typer tool helpers require the optional 'click' and 'typer' dependencies."
            ) from exc
        _GET_COMMAND = get_command
    return _GET_COMMAND

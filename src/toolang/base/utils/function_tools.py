"""Helpers for building tools from Python callables."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import inspect
import math
from types import GenericAlias, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

from pydantic import BeforeValidator, ConfigDict, Strict, TypeAdapter

from ..errors import ToolangError
from ..protocols.tool import Tool
from ..types.tool import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolSummary,
    ToolPaths,
)


@dataclass(frozen=True, slots=True)
class _FunctionToolSpec:
    name: str
    description: str
    parameters: dict[str, Any] | None
    func: Callable[..., Any]
    wants_context: bool
    signature: inspect.Signature
    paths: ToolPaths | None
    summary: ToolSummary | None


@dataclass(frozen=True, slots=True)
class _FunctionTool(Tool):
    """Tool backed by one Python callable."""

    spec: _FunctionToolSpec
    adapters: Mapping[str, TypeAdapter[Any]]
    parameters: dict[str, Any]
    extra_parameter: str | None

    @property
    def name(self) -> str:
        return self.spec.name

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.spec.name,
            description=self.spec.description,
            parameters=self.parameters,
        )

    def bind_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if "context" in arguments:
            raise ToolangError("tool input cannot supply reserved parameter: context")
        bound: dict[str, Any] = {}
        for name, value in arguments.items():
            if not isinstance(name, str):
                raise ToolangError("tool parameter names must be strings")
            parameter = self.spec.signature.parameters.get(name)
            key = (
                name
                if parameter is not None
                and parameter.kind != inspect.Parameter.VAR_KEYWORD
                else self.extra_parameter
            )
            if key is None:
                raise ToolangError(f"unknown tool parameter: {name}")
            try:
                bound[name] = self.adapters[key].validate_python(value)
            except ValueError as exc:
                raise ToolangError(f"invalid tool parameter {name}: {exc}") from exc
        for parameter in self.spec.signature.parameters.values():
            if (
                parameter.name != "context"
                and parameter.kind != inspect.Parameter.VAR_KEYWORD
                and parameter.default is inspect.Parameter.empty
                and parameter.name not in bound
            ):
                raise ToolangError(f"missing required tool parameter: {parameter.name}")
        return bound

    def summary(
        self,
        arguments: Mapping[str, Any],
        result: ToolResult | None = None,
    ) -> str | None:
        return self.spec.summary(arguments, result) if self.spec.summary else None

    def paths(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> Mapping[str, tuple[str, ...]] | None:
        bound = self.bind_arguments(arguments)
        return self.spec.paths(bound, context) if self.spec.paths else None

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        kwargs = self.bind_arguments(arguments)
        if self.spec.wants_context:
            kwargs["context"] = context
        if inspect.iscoroutinefunction(self.spec.func):
            value = await self.spec.func(**kwargs)
        else:
            value = await asyncio.to_thread(self.spec.func, **kwargs)
        if inspect.isawaitable(value):
            value = await value
        return (
            value
            if isinstance(value, ToolResult)
            else ToolResult(_normalize_output(value))
        )


def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, Any] | None = None,
    paths: ToolPaths | None = None,
    summary: ToolSummary | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Annotate a function with the same summary/paths hooks as a Tool."""

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(func)
        spec = _FunctionToolSpec(
            name=name or getattr(func, "__name__", func.__class__.__name__.lower()),
            description=(description or inspect.getdoc(func) or "").strip(),
            parameters=parameters,
            func=func,
            wants_context="context" in signature.parameters,
            signature=signature,
            paths=paths,
            summary=summary,
        )
        setattr(func, "__tool_spec__", spec)
        return func

    return decorate


def create_function_tool(func: Callable[..., Any]) -> Tool:
    """Build one tool from a callable annotated with `@tool`."""

    spec = getattr(func, "__tool_spec__", None)
    if spec is None:
        raise ToolangError(f"function is not marked as a tool: {func!r}")
    adapters: dict[str, TypeAdapter[Any]] = {}
    properties: dict[str, Any] = {}
    required: list[str] = []
    extra_parameter = None
    additional: bool | dict[str, Any] = False
    for parameter in spec.signature.parameters.values():
        if parameter.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.VAR_POSITIONAL,
        ):
            raise ToolangError(
                f"unsupported tool parameter {parameter.name}: {parameter.kind.description}"
            )
        if parameter.name == "context":
            if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                raise ToolangError("context must be a named tool parameter")
            continue
        try:
            annotation = _resolve_annotation(spec.func, parameter)
            adapter = TypeAdapter(
                _input_annotation(annotation), config=ConfigDict(allow_inf_nan=False)
            )
            schema = adapter.json_schema()
            if parameter.default is not inspect.Parameter.empty:
                adapter.validate_python(parameter.default, strict=True)
                schema["default"] = parameter.default
        except (NameError, SyntaxError, TypeError, ValueError) as exc:
            raise ToolangError(
                f"invalid tool parameter {parameter.name}: {exc}"
            ) from exc
        adapters[parameter.name] = adapter
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            extra_parameter = parameter.name
            additional = schema
            continue
        properties[parameter.name] = schema
        if parameter.default is inspect.Parameter.empty:
            required.append(parameter.name)
    inferred = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": additional,
    }
    return _FunctionTool(
        spec,
        adapters,
        spec.parameters if spec.parameters is not None else inferred,
        extra_parameter,
    )


def _resolve_annotation(func: Callable[..., Any], parameter: inspect.Parameter) -> Any:
    if parameter.annotation is inspect.Parameter.empty:
        return Any

    def input_hint():
        pass

    # Resolve only this input: context and return annotations are not public input.
    input_hint.__annotations__ = {parameter.name: parameter.annotation}
    return get_type_hints(
        input_hint, globalns=getattr(func, "__globals__", {}), include_extras=True
    )[parameter.name]


def _reject_boolean(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("Boolean values are not numbers")
    return value


def _input_annotation(annotation: Any) -> Any:
    """Limit adapters to pure JSON-facing types and guard numeric branches."""

    if annotation in (Any, str, bool, type(None)):
        return annotation
    if annotation in (int, float):
        return Annotated[annotation, BeforeValidator(_reject_boolean)]
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is list and len(args) == 1:
        return GenericAlias(list, _input_annotation(args[0]))
    if origin is dict and len(args) == 2 and args[0] is str:
        return GenericAlias(dict, (str, _input_annotation(args[1])))
    if origin in (Union, UnionType):
        return Union[tuple(_input_annotation(arg) for arg in args)]
    if origin is Annotated and all(type(metadata) is Strict for metadata in args[1:]):
        return Annotated[_input_annotation(args[0]), *args[1:]]
    if origin is Literal and all(
        type(value) in (str, int, float, bool, type(None)) for value in args
    ):
        if any(type(value) is float and not math.isfinite(value) for value in args):
            raise TypeError("numeric Literal values must be finite")
        if any(type(value) in (int, float) for value in args):

            def literal_input(value: Any) -> Any:
                if isinstance(value, bool) and not any(value is item for item in args):
                    return _reject_boolean(value)
                return value

            return Annotated[annotation, BeforeValidator(literal_input)]
        return annotation
    raise TypeError(f"unsupported input annotation: {annotation!r}")


def _normalize_output(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    return {"value": value}

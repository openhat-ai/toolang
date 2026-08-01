"""Helpers for building tools from Python callables."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
import inspect
from typing import Any, get_args, get_origin

from ..errors import ToolangError
from ..protocols.tool import AgentTool
from ..types.tool import ToolContext, ToolDefinition


@dataclass(frozen=True, slots=True)
class _FunctionToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]
    wants_context: bool
    signature: inspect.Signature


@dataclass(frozen=True, slots=True)
class _FunctionTool(AgentTool):
    """AgentTool backed by one Python callable."""

    spec: _FunctionToolSpec

    @property
    def name(self) -> str:
        return self.spec.name

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.spec.name,
            description=self.spec.description,
            parameters=self.spec.parameters,
        )

    async def invoke(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> dict[str, Any]:
        kwargs = {
            name: value
            for name, value in arguments.items()
            if name in self.spec.signature.parameters and name != "context"
        }
        if self.spec.wants_context:
            kwargs["context"] = context
        if inspect.iscoroutinefunction(self.spec.func):
            value = await self.spec.func(**kwargs)
        else:
            value = await asyncio.to_thread(self.spec.func, **kwargs)
        if inspect.isawaitable(value):
            value = await value
        return _normalize_output(value)


def tool(
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Annotate one callable as a tool and derive a simple JSON schema."""

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(func)
        spec = _FunctionToolSpec(
            name=name or getattr(func, "__name__", func.__class__.__name__.lower()),
            description=(description or inspect.getdoc(func) or "").strip(),
            parameters=parameters or _schema_from_signature(signature),
            func=func,
            wants_context="context" in signature.parameters,
            signature=signature,
        )
        setattr(func, "__tool_spec__", spec)
        return func

    return decorate


def create_function_tool(func: Callable[..., Any]) -> AgentTool:
    """Build one tool from a callable annotated with `@tool`."""

    spec = getattr(func, "__tool_spec__", None)
    if spec is None:
        raise ToolangError(f"function is not marked as a tool: {func!r}")
    return _FunctionTool(spec=spec)


def _schema_from_signature(signature: inspect.Signature) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter in signature.parameters.values():
        if parameter.name == "context":
            continue
        properties[parameter.name] = _schema_for_parameter(parameter)
        if parameter.default is inspect.Signature.empty:
            required.append(parameter.name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _schema_for_parameter(parameter: inspect.Parameter) -> dict[str, Any]:
    schema = _schema_for_annotation(parameter.annotation)
    if parameter.default is not inspect.Signature.empty:
        schema["default"] = parameter.default
    return schema


def _schema_for_annotation(annotation: object) -> dict[str, Any]:
    if annotation in (inspect.Signature.empty, Any):
        return {}
    if annotation is str:
        return {"type": "string"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is bool:
        return {"type": "boolean"}
    origin = get_origin(annotation)
    if origin is list:
        args = get_args(annotation)
        items = _schema_for_annotation(args[0]) if args else {}
        return {"type": "array", "items": items}
    return {}


def _normalize_output(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    return {"value": value}

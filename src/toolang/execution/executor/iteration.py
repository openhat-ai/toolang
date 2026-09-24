"""Task-local iteration scopes shared by calls and shadowed by nested loops."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Iterator

from toolang.common.immutable import freeze_mapping
from toolang.common.template import template_root_names
from toolang.common.errors import ToolangError

from .common import Local, json_value, value_parts, value_text


@dataclass(frozen=True, slots=True)
class IterationFrame:
    entry: Mapping[str, Local]
    exit: Mapping[str, Local]


@dataclass(frozen=True, slots=True)
class IterationScope:
    window: int
    frames: tuple[IterationFrame, ...] = ()


_SCOPE: ContextVar[IterationScope | None] = ContextVar(
    "toolang_iteration", default=None
)


@contextmanager
def iteration_scope(scope: IterationScope) -> Iterator[None]:
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def snapshot(locals: Mapping[str, Local]) -> Mapping[str, Local]:
    """Keep ordinary values and provenance, excluding injected runtime bindings."""
    values = {
        name: local
        for name, local in locals.items()
        if (name == "_" or not name.startswith("_")) and local.shape != "none"
    }
    frozen = freeze_mapping({name: local.value for name, local in values.items()})
    return freeze_mapping(
        {name: replace(local, value=frozen[name]) for name, local in values.items()}
    )


def template_value(local: Local) -> object:
    if value_parts(local.value, type_name=local.type_name) is not None:
        return value_text(local.value)
    return json_value(local.value)


def iteration_values() -> dict[str, object]:
    scope = _SCOPE.get()
    if scope is None:
        return {}
    return {
        f"_{index + 1}": (
            {
                **{
                    f"_{name}": template_value(local)
                    for name, local in scope.frames[index].entry.items()
                },
                **{
                    name: template_value(local)
                    for name, local in scope.frames[index].exit.items()
                },
            }
            if index < len(scope.frames)
            else None
        )
        for index in range(scope.window)
    }


def history_available(templates: tuple[str, ...]) -> bool:
    """Until waits for valid in-window dependencies, including guarded reads."""
    scope = _SCOPE.get()
    required = 0
    for template in templates:
        for root in template_root_names(template):
            if root.startswith("_") and root[1:].isdigit():
                depth = int(root[1:])
                if (
                    scope is None
                    or not 1 <= depth <= scope.window
                    or root != f"_{depth}"
                ):
                    raise ToolangError(
                        f"iteration history reference is outside the active window: {root}"
                    )
                required = max(required, depth)
    return required <= (len(scope.frames) if scope else 0)

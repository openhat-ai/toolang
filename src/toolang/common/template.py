"""Restricted Mustache-style text template rendering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

import mstache

from toolang.common.errors import ToolangError

_TAG_NAME_RE = re.compile(r"^(\.|[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*)$")


def render_text_template(template: str, context: Mapping[str, object]) -> str:
    """Render one restricted execution template."""

    _validate_template(template)
    _validate_context(context)
    try:
        return str(
            mstache.render(
                template,
                dict(context),
                escape=_identity_escape,
                resolver=_reject_partial,
            )
        )
    except Exception as exc:
        raise ToolangError(f"invalid Toolang template: {exc}") from exc


def _validate_template(template: str) -> None:
    stack: list[str] = []
    index = 0
    while index < len(template):
        start = template.find("{{", index)
        if start < 0:
            break
        if template.startswith("{{{", start):
            end = template.find("}}}", start + 3)
            if end < 0:
                raise ToolangError("unclosed Toolang template tag.")
            raise ToolangError("Toolang templates do not support unescaped tags.")
        end = template.find("}}", start + 2)
        if end < 0:
            raise ToolangError("unclosed Toolang template tag.")
        raw = template[start + 2 : end].strip()
        index = end + 2
        if not raw:
            raise ToolangError("empty Toolang template tag is not allowed.")
        prefix = raw[0]
        if prefix in {">", "!", "&", "="}:
            raise ToolangError(f"Toolang templates do not support tags starting with {prefix!r}.")
        if prefix in {"#", "^", "/"}:
            name = raw[1:].strip()
            _require_tag_name(name)
            if prefix == "/":
                if not stack or stack[-1] != name:
                    raise ToolangError(f"unmatched Toolang template section close: {name}")
                stack.pop()
                continue
            stack.append(name)
            continue
        if raw.startswith("{") and raw.endswith("}"):
            raise ToolangError("Toolang templates do not support unescaped tags.")
        _require_tag_name(raw)
    if stack:
        raise ToolangError(f"unclosed Toolang template section: {stack[-1]}")


def _require_tag_name(name: str) -> None:
    if not _TAG_NAME_RE.fullmatch(name):
        raise ToolangError(f"unsupported Toolang template tag: {name!r}")


def _validate_context(value: object, *, path: str = "context") -> None:
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if callable(value):
        raise ToolangError(f"Toolang template context does not support callables at {path}.")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ToolangError(f"Toolang template context keys must be strings at {path}.")
            _validate_context(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _validate_context(item, path=f"{path}[{index}]")
        return
    raise ToolangError(f"unsupported Toolang template context value at {path}: {type(value).__name__}")


def _identity_escape(value: Any) -> Any:
    return value


def _reject_partial(name: str | bytes) -> str | bytes | None:
    raise ToolangError(f"Toolang templates do not support partials: {name!r}")

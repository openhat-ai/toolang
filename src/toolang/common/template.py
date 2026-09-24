"""Restricted Mustache-style text template rendering."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import re
import json
from typing import Any, cast

import mstache

from toolang.common.errors import ToolangError

_TAG_NAME_PATTERN = r"\.|[A-Za-z_][\w-]*(?:\.(?:[A-Za-z_][\w-]*|0|[1-9]\d*))*"
_TAG_NAME_RE = re.compile(rf"^(?:{_TAG_NAME_PATTERN})$")
_REFERENCE_TAG_RE = re.compile(
    rf"{{{{\s*(?P<sigil>[#^/]?)\s*(?P<name>{_TAG_NAME_PATTERN})\s*}}}}"
)


def render_text_template(template: str, context: Mapping[str, object]) -> str:
    """Render one restricted execution template."""

    validate_template(template)
    _validate_context(context)
    template, getter = _runtime_template(template, context)
    try:
        return str(
            mstache.render(
                template,
                dict(context),
                escape=_identity_escape,
                resolver=_reject_partial,
                getter=getter,
            )
        )
    except Exception as exc:
        raise ToolangError(f"invalid Toolang template: {exc}") from exc


def template_root_names(template: str) -> tuple[str, ...]:
    """Return referenced root names in source order without deduplication."""

    return tuple(
        name.partition(".")[0]
        for match in _REFERENCE_TAG_RE.finditer(template)
        if match.group("sigil") != "/" and (name := match.group("name")) != "."
    )


def template_dependencies(template: str) -> tuple[str, ...]:
    """Infer free template roots, excluding fields inside positive sections."""
    validate_template(template)
    result: list[str] = []
    scopes: list[bool] = []
    for match in _REFERENCE_TAG_RE.finditer(template):
        sigil, name = match.group("sigil", "name")
        if sigil == "/":
            scopes.pop()
            continue
        root = name.partition(".")[0]
        if name != "." and (
            not any(scopes)
            or (root in {"_far", "_near", "_past"} or re.fullmatch(r"_[0-9]+", root))
        ):
            if root not in result:
                result.append(root)
        if sigil in {"#", "^"}:
            scopes.append(sigil == "#")
    return tuple(result)


def require_template_inputs(template: str, values: Mapping[str, object]) -> None:
    """Check free bindings without requiring fields scoped inside sections."""
    for name in template_dependencies(template):
        if name not in values and (name == "_" or not name.startswith("_")):
            raise ToolangError(f"template input is missing: {name}")


def template_runtime_names(template: str) -> tuple[str, ...]:
    """Validate reserved roots without requiring a particular execution frame."""
    validate_template(template)
    names: list[str] = []
    sections: list[bool] = []
    for match in _REFERENCE_TAG_RE.finditer(template):
        sigil, name = match.group("sigil", "name")
        root = name.partition(".")[0]
        historic = root.startswith("_") and root[1:].isdigit()
        runtime = historic or root in {"_far", "_near", "_past"}
        if root.startswith("_") and root != "_" and not runtime and not any(sections):
            raise ToolangError(f"unknown runtime reference: {root}")
        if historic and re.fullmatch(r"_[1-9][0-9]*", root) is None:
            raise ToolangError(
                f"iteration history reference is outside the active window: {root}"
            )
        if sigil == "/":
            sections.pop()
        else:
            if runtime:
                names.append(root)
            if sigil in {"#", "^"}:
                sections.append(sigil == "#" and not historic)
    return tuple(names)


def template_history_depth(template: str, window: int) -> int:
    """Check a known retention window; availability of frames is a runtime fact."""
    required = 0
    limit = str(window)
    for name in template_runtime_names(template):
        if name[1:].isdigit():
            digits = name[1:]
            # Compare canonical decimal strings before conversion: an authored
            # index may exceed Python's integer-string conversion limit.
            if (len(digits), digits) > (len(limit), limit):
                raise ToolangError(
                    f"iteration history reference is outside the active window: {name}"
                )
            required = max(required, int(digits))
    return required


def _runtime_template(
    template: str, context: Mapping[str, object]
) -> tuple[str, Callable[..., Any]]:
    """Check reserved names statically; resolve history only in rendered branches."""
    aliases: dict[str, tuple[str, bool]] = {}
    template_runtime_names(template)
    authored_names = set(template_root_names(template))
    replacements: list[tuple[int, int, str]] = []
    sections: list[tuple[str | None, bool]] = []
    for match in _REFERENCE_TAG_RE.finditer(template):
        sigil, name = match.group("sigil", "name")
        root = name.partition(".")[0]
        historic = root.startswith("_") and root[1:].isdigit()
        runtime = historic or root in {"_far", "_near", "_past"}
        if historic and root not in context:
            raise ToolangError(
                f"iteration history reference is outside the active window: {root}"
            )
        if sigil == "/":
            alias, _ = sections.pop()
            if alias is not None:
                replacements.append((match.start(), match.end(), "{{/" + alias + "}}"))
            continue
        alias = None
        if (historic and sigil in {"#", "^"}) or (runtime and not sigil):
            alias = f"__toolang_runtime_{len(aliases)}"
            while alias in authored_names:
                alias += "_"
            aliases[alias] = (name, bool(sigil))
            replacements.append(
                (match.start(), match.end(), "{{" + sigil + alias + "}}")
            )
        if sigil in {"#", "^"}:
            sections.append((alias, sigil == "#" and not historic))
    for start, end, replacement in reversed(replacements):
        template = template[:start] + replacement + template[end:]

    def getter(
        scope: Any, scopes: Sequence[Any], key: str | bytes, default: Any = None
    ) -> Any:
        alias = key.decode() if isinstance(key, bytes) else key
        if alias not in aliases:
            return mstache.default_getter(scope, scopes, key, default)
        name, guard = aliases[alias]
        root, *path = name.split(".")
        value = context.get(root)
        if root[1:].isdigit() and value is None:
            if guard:
                return False
            raise ToolangError(f"iteration history frame is not available: {root}")
        for field in path:
            if isinstance(value, Mapping) and field in value:
                value = cast(Mapping[str, object], value)[field]
            elif (
                isinstance(value, Sequence)
                and not isinstance(value, str)
                and field.isdigit()
                and int(field) < len(value)
            ):
                value = value[int(field)]
            else:
                raise ToolangError(f"runtime field is missing: {name}")
        if guard:
            return value if value else True
        if isinstance(value, Mapping | list | tuple | bool):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return value

    return template, getter


def validate_template(template: str) -> None:
    """Validate template syntax without resolving values or runtime history."""
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
            raise ToolangError(
                f"Toolang templates do not support tags starting with {prefix!r}."
            )
        if prefix in {"#", "^", "/"}:
            name = raw[1:].strip()
            _require_tag_name(name)
            if prefix == "/":
                if not stack or stack[-1] != name:
                    raise ToolangError(
                        f"unmatched Toolang template section close: {name}"
                    )
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
        raise ToolangError(
            f"Toolang template context does not support callables at {path}."
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ToolangError(
                    f"Toolang template context keys must be strings at {path}."
                )
            _validate_context(item, path=f"{path}.{key}")
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _validate_context(item, path=f"{path}[{index}]")
        return
    raise ToolangError(
        f"unsupported Toolang template context value at {path}: {type(value).__name__}"
    )


def _identity_escape(value: Any) -> Any:
    return value


def _reject_partial(name: str | bytes) -> str | bytes | None:
    raise ToolangError(f"Toolang templates do not support partials: {name!r}")

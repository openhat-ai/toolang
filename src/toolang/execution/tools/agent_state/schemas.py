"""Compact protocol schemas for current-agent authored resources."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Literal, NoReturn, cast

from toolang.base.errors import ToolFailure

ResourceKind = Literal[
    "task",
    "chore",
    "psyche",
    "skill",
    "service",
    "prompt",
    "flow",
]
Operation = Literal["list", "get", "create", "update", "delete"]

RESOURCE_KINDS: tuple[ResourceKind, ...] = (
    "task",
    "chore",
    "psyche",
    "skill",
    "service",
    "prompt",
    "flow",
)
NAMED_KINDS: tuple[ResourceKind, ...] = (
    "psyche",
    "skill",
    "service",
    "prompt",
    "flow",
)
JOB_KINDS = frozenset({"task", "chore"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_ISSUES = 32

_CONTENT_PROPERTIES: dict[str, dict[str, Any]] = {
    "title": {"type": "string"},
    "body": {"type": "string"},
    "schedule": {"type": "string"},
    "description": {"type": "string"},
    "transport": {"type": "string", "enum": ["http", "stdio"]},
    "target": {"type": "string"},
    "headers": {
        "type": "object",
        "additionalProperties": {"type": "string"},
    },
    "env": {"type": "array", "items": {"type": "string"}},
    "source": {"type": "string"},
}
_CONTENT_TYPES: dict[str, object] = {
    "title": str,
    "body": str,
    "schedule": str,
    "description": str,
    "transport": str,
    "target": str,
    "headers": dict,
    "env": list,
    "source": str,
}
_CREATE_FIELDS: dict[ResourceKind, tuple[frozenset[str], frozenset[str]]] = {
    "task": (frozenset({"body", "title"}), frozenset({"body"})),
    "chore": (
        frozenset({"body", "title", "schedule"}),
        frozenset({"body"}),
    ),
    "psyche": (frozenset({"body"}), frozenset({"body"})),
    "skill": (
        frozenset({"description", "body"}),
        frozenset({"description", "body"}),
    ),
    "service": (
        frozenset({"description", "transport", "target", "body", "headers", "env"}),
        frozenset({"description", "transport", "target"}),
    ),
    "prompt": (frozenset({"body"}), frozenset({"body"})),
    "flow": (frozenset({"source"}), frozenset({"source"})),
}
_UPDATE_FIELDS: dict[ResourceKind, tuple[frozenset[str], frozenset[str]]] = {
    "task": (frozenset({"body", "title"}), frozenset()),
    "chore": (frozenset({"body", "title", "schedule"}), frozenset()),
    "psyche": (frozenset({"body"}), frozenset({"body"})),
    "skill": (frozenset({"description", "body"}), frozenset()),
    "service": (
        frozenset({"description", "transport", "target", "body", "headers", "env"}),
        frozenset(),
    ),
    "prompt": (frozenset({"body"}), frozenset({"body"})),
    "flow": (frozenset({"source"}), frozenset({"source"})),
}


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    """One validated compact `_me` request."""

    operation: Operation
    kind: ResourceKind
    key: str | None = None
    content: dict[str, Any] | None = None
    if_digest: str | None = None


def tool_parameters(operation: Operation) -> dict[str, Any]:
    """Return the stable JSON Schema for one compact `_me` action."""

    kinds = NAMED_KINDS if operation == "delete" else RESOURCE_KINDS
    properties: dict[str, Any] = {
        "kind": {"type": "string", "enum": list(kinds)},
    }
    required = ["kind"]
    if operation in {"get", "create", "update", "delete"}:
        properties["key"] = {
            "type": "string",
            "description": "Task/chore id or authored cap/flow name.",
        }
    if operation in {"get", "update", "delete"}:
        required.append("key")
    if operation in {"create", "update"}:
        properties["content"] = {
            "type": "object",
            "properties": dict(_CONTENT_PROPERTIES),
            "additionalProperties": False,
        }
        required.append("content")
    if operation in {"update", "delete"}:
        properties["if_digest"] = {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
            "description": "Optional current SHA-256 digest precondition.",
        }
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def decode_request(
    operation: Operation,
    arguments: Mapping[str, Any],
) -> ResourceRequest:
    """Decode and strictly validate one model-provided action envelope."""

    allowed = {
        "list": frozenset({"kind"}),
        "get": frozenset({"kind", "key"}),
        "create": frozenset({"kind", "key", "content"}),
        "update": frozenset({"kind", "key", "content", "if_digest"}),
        "delete": frozenset({"kind", "key", "if_digest"}),
    }[operation]
    unknown = sorted(set(arguments).difference(allowed))
    if unknown:
        fail(
            "invalid_request",
            f"unsupported {operation} argument: {unknown[0]}",
            operation=operation,
            issues=(
                issue(
                    "unsupported-field",
                    unknown[0],
                    f"field is not accepted by _me {operation}",
                ),
            ),
        )

    raw_kind = arguments.get("kind")
    if not isinstance(raw_kind, str) or raw_kind not in RESOURCE_KINDS:
        fail(
            "invalid_request",
            "kind must name a supported current-agent resource",
            operation=operation,
            kind=_safe_text(raw_kind),
            issues=(issue("invalid-kind", "kind", "unsupported resource kind"),),
        )
    kind = cast(ResourceKind, raw_kind)
    if operation == "delete" and kind not in NAMED_KINDS:
        fail(
            "unsupported_operation",
            f"delete is not supported for {kind}",
            operation=operation,
            kind=kind,
            issues=(
                issue(
                    "unsupported-operation",
                    "kind",
                    f"{kind} lifecycle is not delete",
                ),
            ),
        )

    raw_key = arguments.get("key")
    key: str | None = None
    key_required = operation in {"get", "update", "delete"} or (
        operation == "create" and kind in NAMED_KINDS
    )
    key_forbidden = operation == "create" and kind in JOB_KINDS
    if key_forbidden and "key" in arguments:
        fail(
            "invalid_request",
            f"{kind} create allocates its key",
            operation=operation,
            kind=kind,
            key=_safe_text(raw_key),
            issues=(issue("forbidden-field", "key", "key must be omitted"),),
        )
    if key_required:
        if not isinstance(raw_key, str) or not raw_key.strip():
            fail(
                "invalid_request",
                f"key is required for {operation} {kind}",
                operation=operation,
                kind=kind,
                key=_safe_text(raw_key),
                issues=(issue("required-field", "key", "non-empty key required"),),
            )
        key = raw_key.strip()
        if key in {".", ".."} or "/" in key or "\\" in key:
            fail(
                "invalid_request",
                f"key is invalid for {operation} {kind}",
                operation=operation,
                kind=kind,
                key=_safe_text(raw_key),
                issues=(
                    issue(
                        "invalid-key",
                        "key",
                        "key must be one non-path resource identifier",
                    ),
                ),
            )

    content: dict[str, Any] | None = None
    if operation in {"create", "update"}:
        raw_content = arguments.get("content")
        if not isinstance(raw_content, Mapping):
            fail(
                "invalid_content",
                f"content must be an object for {operation} {kind}",
                operation=operation,
                kind=kind,
                key=key,
                issues=(issue("invalid-type", "content", "object required"),),
            )
        content = {str(name): value for name, value in raw_content.items()}
        _validate_content(
            cast(Literal["create", "update"], operation),
            kind,
            key,
            content,
        )

    if_digest = arguments.get("if_digest")
    if if_digest is not None and (
        not isinstance(if_digest, str) or _SHA256_RE.fullmatch(if_digest) is None
    ):
        fail(
            "invalid_request",
            "if_digest must be a lowercase SHA-256 digest",
            operation=operation,
            kind=kind,
            key=key,
            issues=(
                issue(
                    "invalid-digest",
                    "if_digest",
                    "64 lowercase hexadecimal characters required",
                ),
            ),
        )
    return ResourceRequest(
        operation=operation,
        kind=kind,
        key=key,
        content=content,
        if_digest=cast(str | None, if_digest),
    )


def issue(
    code: str,
    path: str,
    message: str,
    *,
    line: int | None = None,
    column: int | None = None,
) -> dict[str, Any]:
    """Build one stable structured tool issue."""

    result: dict[str, Any] = {"code": code, "path": path, "message": message}
    if line is not None:
        result["line"] = line
    if column is not None:
        result["column"] = column
    return result


def fail(
    code: str,
    message: str,
    *,
    operation: Operation,
    kind: str | None = None,
    key: str | None = None,
    issues: tuple[Mapping[str, Any], ...] = (),
) -> NoReturn:
    """Raise one expected failed tool call with bounded structured output."""

    selected = issues[:_MAX_ISSUES]
    error: dict[str, Any] = {
        "code": code,
        "message": message,
        "operation": operation,
        "issues": [dict(item) for item in selected],
        "truncated": len(issues) > len(selected),
    }
    if kind is not None:
        error["kind"] = kind
    if key is not None:
        error["key"] = key
    raise ToolFailure(message, output={"error": error})


def _validate_content(
    operation: Literal["create", "update"],
    kind: ResourceKind,
    key: str | None,
    content: Mapping[str, Any],
) -> None:
    allowed, required = (
        _CREATE_FIELDS[kind] if operation == "create" else _UPDATE_FIELDS[kind]
    )
    issues: list[dict[str, Any]] = []
    for name in sorted(set(content).difference(allowed)):
        issues.append(
            issue(
                "unsupported-field",
                f"content.{name}",
                f"field is not supported for {kind}",
            )
        )
    for name in sorted(required.difference(content)):
        issues.append(
            issue(
                "required-field",
                f"content.{name}",
                f"field is required for {operation} {kind}",
            )
        )
    if operation == "update" and not content:
        issues.append(
            issue(
                "empty-update",
                "content",
                f"at least one {kind} field is required",
            )
        )
    for name, value in content.items():
        expected = _CONTENT_TYPES.get(name)
        if expected is str and not isinstance(value, str):
            issues.append(issue("invalid-type", f"content.{name}", "string required"))
        elif expected is dict and (
            not isinstance(value, Mapping)
            or not all(
                isinstance(header, str) and isinstance(item, str)
                for header, item in value.items()
            )
        ):
            issues.append(
                issue(
                    "invalid-type",
                    f"content.{name}",
                    "string map required",
                )
            )
        elif expected is list and (
            not isinstance(value, list)
            or not all(isinstance(item, str) for item in value)
        ):
            issues.append(
                issue(
                    "invalid-type",
                    f"content.{name}",
                    "string array required",
                )
            )
    if issues:
        fail(
            "invalid_content",
            f"invalid content for {operation} {kind}",
            operation=operation,
            kind=kind,
            key=key,
            issues=tuple(issues),
        )


def _safe_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:128] if text else None

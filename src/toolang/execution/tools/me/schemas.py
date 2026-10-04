"""Closed file-operation schemas for the current agent's home."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any, Literal, NoReturn, cast

from .errors import ResourceError

Operation = Literal["list", "get", "create", "update", "delete"]
Encoding = Literal["utf-8", "base64"]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    operation: Operation
    key: str | None = None
    content: str | None = None
    encoding: Encoding = "utf-8"
    if_digest: str | None = None


def tool_parameters(operation: Operation) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required = []
    if operation != "list":
        properties["key"] = {
            "type": "string",
            "description": "Exact home-relative file path returned by list, e.g. agent.too or flows/research.too.",
        }
        required.append("key")
    if operation in {"create", "update"}:
        properties["content"] = {
            "type": "string",
            "description": "Complete file content, not a patch.",
        }
        properties["encoding"] = {
            "type": "string",
            "enum": ["utf-8", "base64"],
            "description": "Defaults to utf-8. Use base64 to supply exact binary bytes.",
        }
        required.append("content")
    if operation in {"update", "delete"}:
        properties["if_digest"] = {
            "type": "string",
            "pattern": _SHA256_RE.pattern,
            "description": "Required SHA-256 of the whole file from get/list or a previous successful write.",
        }
        required.append("if_digest")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def decode_request(
    operation: Operation, arguments: Mapping[str, Any]
) -> ResourceRequest:
    schema = tool_parameters(operation)
    unknown = sorted(set(arguments).difference(schema["properties"]))
    if unknown:
        fail(
            "invalid_request",
            f"unsupported {operation} argument: {unknown[0]}",
            operation=operation,
        )
    for name in schema["required"]:
        if name not in arguments:
            fail("invalid_request", f"{name} is required", operation=operation)
    key = arguments.get("key")
    if operation != "list":
        if (
            not isinstance(key, str)
            or not key
            or key != PurePosixPath(key).as_posix()
            or PurePosixPath(key).is_absolute()
            or any(part in {".", ".."} for part in key.split("/"))
            or "\0" in key
        ):
            fail(
                "invalid_request",
                "key must be a canonical home-relative file path",
                operation=operation,
            )
    content = arguments.get("content")
    if operation in {"create", "update"} and not isinstance(content, str):
        fail(
            "invalid_content",
            "content must be the complete file text",
            operation=operation,
            key=key,
        )
    encoding = arguments.get("encoding", "utf-8")
    if not isinstance(encoding, str) or encoding not in {"utf-8", "base64"}:
        fail(
            "invalid_request",
            "encoding must be utf-8 or base64",
            operation=operation,
            key=key,
        )
    digest = arguments.get("if_digest")
    if operation in {"update", "delete"} and (
        not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
    ):
        fail(
            "invalid_request",
            "if_digest must be a lowercase SHA-256 digest",
            operation=operation,
            key=key,
        )
    return ResourceRequest(operation, key, content, cast(Encoding, encoding), digest)


def issue(
    code: str, path: str, message: str, *, line: int | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "code": code[:128],
        "path": path[:256],
        "message": message[:512],
    }
    if line is not None:
        result["line"] = line
    return result


def fail(
    code: str,
    message: str,
    *,
    operation: Operation,
    key: str | None = None,
    issues: tuple[Mapping[str, Any], ...] = (),
) -> NoReturn:
    error: dict[str, Any] = {
        "code": code,
        "message": message[:512],
        "operation": operation,
        "issues": [dict(item) for item in issues[:32]],
        "truncated": len(issues) > 32,
    }
    if key is not None:
        error["key"] = key[:512]
    raise ResourceError(error["message"], {"error": error})

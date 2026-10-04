"""Closed schemas for home file operations and loaded receipt comparisons."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any, Literal, NoReturn, cast

from .errors import ResourceError

Operation = Literal["list", "get", "create", "update", "delete", "loaded"]
Encoding = Literal["utf-8", "base64"]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class Receipt:
    key: str
    digest: str | None


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    operation: Operation
    key: str | None = None
    content: str | None = None
    encoding: Encoding = "utf-8"
    if_digest: str | None = None
    receipts: tuple[Receipt, ...] = ()


def tool_parameters(operation: Operation) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required = []
    key_schema = {
        "type": "string",
        "description": "Canonical home-relative file path, e.g. agent.too or flows/research.too.",
    }
    if operation == "loaded":
        properties["receipts"] = {
            "type": "array",
            "description": "File versions to compare with this call's loaded State. Null means absent or untracked. Keys must be unique.",
            "items": {
                "type": "object",
                "properties": {
                    "key": key_schema,
                    "digest": {
                        "anyOf": [
                            {"type": "string", "pattern": _SHA256_RE.pattern},
                            {"type": "null"},
                        ],
                    },
                },
                "required": ["key", "digest"],
                "additionalProperties": False,
            },
        }
        required.append("receipts")
    elif operation != "list":
        properties["key"] = key_schema
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
        fail("invalid_request", f"unsupported {operation} argument: {unknown[0]}")
    for name in schema["required"]:
        if name not in arguments:
            fail("invalid_request", f"{name} is required")
    if operation == "loaded":
        return ResourceRequest(operation, receipts=_receipts(arguments["receipts"]))
    key = _key(arguments.get("key")) if operation != "list" else None
    content = arguments.get("content")
    if operation in {"create", "update"} and not isinstance(content, str):
        fail("invalid_request", "content must be the complete file text", key=key)
    encoding = arguments.get("encoding", "utf-8")
    if not isinstance(encoding, str) or encoding not in {"utf-8", "base64"}:
        fail("invalid_request", "encoding must be utf-8 or base64", key=key)
    digest = arguments.get("if_digest")
    if operation in {"update", "delete"} and not _is_digest(digest):
        fail("invalid_request", "if_digest must be a lowercase SHA-256 digest", key=key)
    return ResourceRequest(operation, key, content, cast(Encoding, encoding), digest)


def _key(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != PurePosixPath(value).as_posix()
        or PurePosixPath(value).is_absolute()
        or any(part in {".", ".."} for part in value.split("/"))
        or "\0" in value
    ):
        fail("invalid_request", "key must be a canonical home-relative file path")
    return value


def _is_digest(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _receipts(value: Any) -> tuple[Receipt, ...]:
    if not isinstance(value, list):
        fail("invalid_request", "receipts must be an array")
    result: list[Receipt] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"key", "digest"}:
            fail("invalid_request", "each receipt must contain only key and digest")
        key = _key(item["key"])
        digest = item["digest"]
        if digest is not None and not _is_digest(digest):
            fail(
                "invalid_request",
                "receipt digest must be a lowercase SHA-256 digest or null",
                key=key,
            )
        if key in seen:
            fail("invalid_request", "receipt keys must be unique", key=key)
        seen.add(key)
        result.append(Receipt(key, digest))
    return tuple(result)


def fail(
    code: str, message: str, *, key: str | None = None, **details: Any
) -> NoReturn:
    error = {"error": code, "message": message[:512], **details}
    if key is not None:
        error["key"] = key
    raise ResourceError(error["message"], error)

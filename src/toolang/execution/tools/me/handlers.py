"""File operations over the current agent's latest authored home."""

from __future__ import annotations

import base64
import binascii
from hashlib import sha256
from typing import Any

from toolang.base.types.tool import ToolContext

from .errors import DigestMismatchError, ResourceError, UnsafeAuthoringPathError
from .schemas import ResourceRequest, fail, issue
from .storage import HomeFiles, classify
from .types import MeToolContext


def execute(request: ResourceRequest, context: ToolContext) -> dict[str, Any]:
    try:
        if not isinstance(context, MeToolContext):
            fail(
                "invalid_request",
                "me requires a current agent context",
                operation=request.operation,
            )
        storage = _storage(context)
        if request.operation == "list":
            with storage.lock():
                return {
                    "items": [
                        _item(key, storage.read(classify(key)), include_content=False)
                        for key in storage.keys()
                    ]
                }
        assert request.key is not None
        try:
            file = classify(request.key)
        except ValueError as exc:
            fail(
                "invalid_request",
                str(exc),
                operation=request.operation,
                key=request.key,
            )
        with storage.lock(file):
            if request.operation == "get":
                return {"item": _item(file.key, storage.read(file))}
            encoded: bytes | None = None
            if request.operation != "delete":
                assert request.content is not None
                if request.encoding == "base64":
                    try:
                        encoded = base64.b64decode(request.content, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise ValueError("content is not valid base64") from exc
                else:
                    encoded = request.content.encode("utf-8")
            previous = storage.before_write(
                file,
                create=request.operation == "create",
                expected=request.if_digest,
            )
            result: dict[str, Any]
            if request.operation == "delete":
                result = {"key": file.key, "deleted": True}
            else:
                assert encoded is not None
                result = {"item": _item(file.key, encoded)}
                result["created" if request.operation == "create" else "changed"] = (
                    previous != encoded
                )
            # Prepare the complete result before the only filesystem commit point.
            if previous != encoded:
                storage.save(file, encoded)
            return result
    except ResourceError:
        raise
    except DigestMismatchError as exc:
        fail(
            "digest_mismatch",
            str(exc),
            operation=request.operation,
            key=request.key,
            issues=(
                issue(
                    "digest-mismatch",
                    "if_digest",
                    f"expected {exc.expected}, found {exc.actual}",
                ),
            ),
        )
    except FileNotFoundError as exc:
        fail("not_found", str(exc), operation=request.operation, key=request.key)
    except FileExistsError as exc:
        fail("conflict", str(exc), operation=request.operation, key=request.key)
    except UnsafeAuthoringPathError as exc:
        fail("storage_error", str(exc), operation=request.operation, key=request.key)
    except (ValueError, TypeError) as exc:
        fail(
            "invalid_content",
            str(exc) or type(exc).__name__,
            operation=request.operation,
            key=request.key,
        )
    except OSError as exc:
        fail(
            "storage_error",
            f"could not {request.operation} home file",
            operation=request.operation,
            key=request.key,
            issues=(issue("storage-error", "key", type(exc).__name__),),
        )


def _storage(context: MeToolContext) -> HomeFiles:
    home = context.home.expanduser()
    if (
        home.is_symlink()
        or home.parent.is_symlink()
        or not home.is_dir()
        or home.resolve() != context.layout.home.resolve()
        or home.parent.name != "agents"
    ):
        raise UnsafeAuthoringPathError("me requires the current agent's own home")
    return HomeFiles(context.layout)


def _item(key: str, content: bytes, *, include_content: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "key": key,
        "digest": sha256(content).hexdigest(),
        "bytes": len(content),
    }
    if include_content:
        try:
            result["content"] = content.decode("utf-8")
            result["encoding"] = "utf-8"
        except UnicodeDecodeError:
            result["content"] = base64.b64encode(content).decode("ascii")
            result["encoding"] = "base64"
    return result

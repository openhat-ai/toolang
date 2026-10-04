"""Current home file operations and comparisons with the calling State."""

from __future__ import annotations

import base64
import binascii
from hashlib import sha256
from typing import Any

from toolang.base.types.tool import ToolContext

from .errors import DigestMismatchError, ResourceError, UnsafeAuthoringPathError
from .schemas import ResourceRequest, fail
from .storage import HomeFiles, classify
from .types import MeToolContext


def execute(request: ResourceRequest, context: ToolContext) -> dict[str, Any]:
    try:
        if not isinstance(context, MeToolContext):
            fail("invalid_request", "me requires a current agent context")
        if request.operation == "loaded":
            return _loaded(request, context)
        storage = _storage(context)
        if request.operation == "list":
            with storage.lock():
                return {
                    "files": [storage.metadata(classify(key)) for key in storage.keys()]
                }
        assert request.key is not None
        try:
            file = classify(request.key)
        except ValueError as exc:
            fail("invalid_request", str(exc), key=request.key)
        with storage.lock(file):
            if request.operation == "get":
                return _item(file.key, storage.read(file))
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
            result = {
                "key": file.key,
                "digest": sha256(encoded).hexdigest() if encoded is not None else None,
            }
            # Prepare the receipt before the only filesystem commit point.
            if previous != encoded:
                storage.save(file, encoded)
            return result
    except ResourceError:
        raise
    except DigestMismatchError as exc:
        fail(
            "digest_mismatch",
            str(exc),
            key=request.key,
            expected_digest=exc.expected,
            actual_digest=exc.actual,
        )
    except FileNotFoundError as exc:
        fail("not_found", str(exc), key=request.key)
    except FileExistsError as exc:
        fail("already_exists", str(exc), key=request.key)
    except UnsafeAuthoringPathError as exc:
        fail("io_error", str(exc), key=request.key)
    except (ValueError, TypeError) as exc:
        fail("invalid_request", str(exc) or type(exc).__name__, key=request.key)
    except OSError:
        fail("io_error", f"could not {request.operation} home file", key=request.key)


def _loaded(request: ResourceRequest, context: MeToolContext) -> dict[str, Any]:
    state = context.state
    if state is None:
        fail("invalid_request", "loaded requires the calling AgentState")
    files = {item.key: item.digest for item in state.files if item.scope == "home"}
    mismatches = [
        {"key": receipt.key, "digest": files.get(receipt.key)}
        for receipt in request.receipts
        if receipt.digest != files.get(receipt.key)
    ]
    return {
        "loaded": not mismatches,
        "revision": state.revision,
        "mismatches": mismatches,
    }


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


def _item(key: str, content: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {
        "key": key,
        "digest": sha256(content).hexdigest(),
        "bytes": len(content),
    }
    try:
        result["content"] = content.decode("utf-8")
        result["encoding"] = "utf-8"
    except UnicodeDecodeError:
        result["content"] = base64.b64encode(content).decode("ascii")
        result["encoding"] = "base64"
    return result

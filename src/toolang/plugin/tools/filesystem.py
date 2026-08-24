"""Filesystem tool plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import shutil
import threading
from typing import Any

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool, AgentToolSet
from toolang.base.types.tool import ToolContext
from toolang.base.utils.function_tools import create_function_tool, tool

DEFAULT_MAX_CHARS = 20_000


@dataclass(slots=True)
class FilesystemPlugin:
    """Filesystem tools scoped to the current run's allowed roots."""

    config: dict[str, Any]
    name: str = "filesystem"
    description: str | None = "Inspect and edit files inside the current run roots."
    _max_chars: int = field(init=False, repr=False)
    _tools: dict[str, AgentTool] = field(init=False, repr=False)
    _path_locks: dict[Path, threading.Lock] = field(init=False, repr=False)
    _path_locks_guard: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._max_chars = _int_value(
            self.config.get("max_chars"), default=DEFAULT_MAX_CHARS
        )
        self._path_locks = {}
        self._path_locks_guard = threading.Lock()
        self._tools = self._build_tools()

    def tools(self) -> Mapping[str, AgentTool]:
        return dict(self._tools)

    def _build_tools(self) -> dict[str, AgentTool]:
        @tool(
            name="list", description="List one directory inside the current agent home."
        )
        def list_dir(
            path: str = ".", context: ToolContext | None = None
        ) -> dict[str, Any]:
            resolved = _resolve_path(path, context=context)
            if not resolved.exists():
                raise ToolangError(f"directory does not exist: {resolved}")
            if not resolved.is_dir():
                raise ToolangError(f"path is not a directory: {resolved}")
            return {
                "path": str(resolved),
                "entries": [
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "is_dir": entry.is_dir(),
                    }
                    for entry in sorted(resolved.iterdir(), key=lambda item: item.name)
                ],
            }

        @tool(
            name="read_text",
            description="Read one text file inside the current agent home.",
        )
        def read_text(
            path: str,
            max_chars: int = self._max_chars,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved = _resolve_path(path, context=context)
            text = resolved.read_text(encoding="utf-8")
            limit = _int_value(max_chars, default=self._max_chars)
            return {
                "path": str(resolved),
                "text": text[:limit],
                "truncated": len(text) > limit,
            }

        @tool(
            name="write_text",
            description="Write one text file inside the current agent home.",
        )
        def write_text(
            path: str, text: str, context: ToolContext | None = None
        ) -> dict[str, Any]:
            resolved = _resolve_path(path, context=context)
            with self._path_lock(resolved):
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(text, encoding="utf-8")
            return {"path": str(resolved), "bytes_written": len(text.encode("utf-8"))}

        @tool(
            name="append_text",
            description="Append text to one file inside the current agent home.",
        )
        def append_text(
            path: str, text: str, context: ToolContext | None = None
        ) -> dict[str, Any]:
            resolved = _resolve_path(path, context=context)
            with self._path_lock(resolved):
                resolved.parent.mkdir(parents=True, exist_ok=True)
                with resolved.open("a", encoding="utf-8") as handle:
                    handle.write(text)
            return {"path": str(resolved), "bytes_appended": len(text.encode("utf-8"))}

        @tool(name="glob", description="Match file paths under one directory.")
        def glob(
            path: str = ".",
            pattern: str = "*",
            recursive: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved = _resolve_path(path, context=context)
            matches = resolved.rglob(pattern) if recursive else resolved.glob(pattern)
            return {
                "path": str(resolved),
                "pattern": pattern,
                "matches": [str(item) for item in sorted(matches)],
            }

        @tool(name="stat", description="Inspect one file or directory.")
        def stat(path: str, context: ToolContext | None = None) -> dict[str, Any]:
            resolved = _resolve_path(path, context=context)
            exists = resolved.exists()
            return {
                "path": str(resolved),
                "exists": exists,
                "is_file": resolved.is_file() if exists else False,
                "is_dir": resolved.is_dir() if exists else False,
                "size": resolved.stat().st_size if exists else None,
            }

        @tool(name="mkdir", description="Create one directory.")
        def mkdir(
            path: str, parents: bool = True, context: ToolContext | None = None
        ) -> dict[str, Any]:
            resolved = _resolve_path(path, context=context)
            resolved.mkdir(parents=parents, exist_ok=True)
            return {"path": str(resolved), "created": True}

        @tool(name="remove", description="Remove one file or directory.")
        def remove(
            path: str,
            recursive: bool = False,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved = _resolve_path(path, context=context)
            if _is_allowed_root(resolved, context=context):
                raise ToolangError(f"cannot remove an allowed root: {resolved}")
            if not resolved.exists():
                raise ToolangError(f"path does not exist: {resolved}")
            if resolved.is_dir():
                if recursive:
                    shutil.rmtree(resolved)
                else:
                    resolved.rmdir()
            else:
                resolved.unlink()
            return {"path": str(resolved), "removed": True}

        return {
            "list": create_function_tool(list_dir),
            "read_text": create_function_tool(read_text),
            "write_text": create_function_tool(write_text),
            "append_text": create_function_tool(append_text),
            "glob": create_function_tool(glob),
            "stat": create_function_tool(stat),
            "mkdir": create_function_tool(mkdir),
            "remove": create_function_tool(remove),
        }

    def _path_lock(self, path: Path) -> threading.Lock:
        with self._path_locks_guard:
            lock = self._path_locks.get(path)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[path] = lock
            return lock


def create_tool_set(config: Mapping[str, Any]) -> AgentToolSet:
    """Create the filesystem tool plugin."""

    return FilesystemPlugin(config=dict(config))


def _resolve_path(path_value: str, *, context: ToolContext | None) -> Path:
    if context is None:
        raise ToolangError("filesystem tool context is required")
    text = str(path_value).strip()
    if not text:
        raise ToolangError("filesystem tool requires a non-empty path")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = context.wd / candidate
    resolved = candidate.resolve()
    roots = tuple(root.resolve() for root in context.roots) or (context.home.resolve(),)
    if any(resolved == root or resolved.is_relative_to(root) for root in roots):
        return resolved
    raise ToolangError(f"filesystem path escapes allowed roots: {resolved}")


def _is_allowed_root(path: Path, *, context: ToolContext | None) -> bool:
    if context is None:
        return False
    roots = tuple(root.resolve() for root in context.roots) or (context.home.resolve(),)
    return any(path == root for root in roots)


def _int_value(value: object, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = (
            value
            if isinstance(value, int) and not isinstance(value, bool)
            else int(str(value))
        )
    except (TypeError, ValueError) as exc:
        raise ToolangError("filesystem integer argument is invalid") from exc
    if parsed <= 0:
        raise ToolangError("filesystem integer argument must be positive")
    return parsed

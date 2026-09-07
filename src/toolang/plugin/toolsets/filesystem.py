"""Filesystem toolset plugin."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
import shutil
import threading
from typing import Any

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import AgentTool, Toolset
from toolang.base.types.tool import ToolContext, ToolDefinition, ToolPreparation
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.base.utils.workspace_paths import (
    authorize_workspace_path,
    parse_workspace_uri,
    resolve_workspace_path,
    workspace_root,
    workspace_uri,
)

DEFAULT_MAX_CHARS = 20_000
_PATH_GUIDANCE = (
    " Use workspace://<name>/<path> for a configured workspace."
    " Alternatively, provide workspace with a root-relative path."
    " Agent home and the process working directory are not implicit roots."
)


@dataclass(slots=True)
class FilesystemToolset:
    """Filesystem tools for explicitly addressed, configured workspaces."""

    config: dict[str, Any]
    name: str = "fs"
    description: str | None = "Inspect and edit workspace files."
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
            name="list",
            description="List a directory, or list available workspaces at workspace://."
            + _PATH_GUIDANCE,
        )
        def list_dir(
            path: str = ".",
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None and workspace is not None
            return _list_directory(Path(path), context.workspaces[workspace])

        @tool(
            name="read",
            description="Read one text file." + _PATH_GUIDANCE,
        )
        def read_text(
            path: str,
            max_chars: int = self._max_chars,
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved = Path(path)
            text = resolved.read_text(encoding="utf-8")
            limit = _int_value(max_chars, default=self._max_chars)
            return {
                "path": str(resolved),
                "text": text[:limit],
                "truncated": len(text) > limit,
            }

        @tool(
            name="write",
            description="Write one text file." + _PATH_GUIDANCE,
        )
        def write_text(
            path: str,
            text: str,
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved = Path(path)
            with self._path_lock(resolved):
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(text, encoding="utf-8")
            return {"path": str(resolved), "bytes_written": len(text.encode("utf-8"))}

        @tool(
            name="append",
            description="Append text to one file." + _PATH_GUIDANCE,
        )
        def append_text(
            path: str,
            text: str,
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved = Path(path)
            with self._path_lock(resolved):
                resolved.parent.mkdir(parents=True, exist_ok=True)
                with resolved.open("a", encoding="utf-8") as handle:
                    handle.write(text)
            return {"path": str(resolved), "bytes_appended": len(text.encode("utf-8"))}

        @tool(
            name="glob",
            description="Match file paths under one directory." + _PATH_GUIDANCE,
        )
        def glob(
            path: str = ".",
            pattern: str = "*",
            recursive: bool = False,
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None and workspace is not None
            return _glob_paths(
                Path(path), pattern, recursive, context.workspaces[workspace]
            )

        @tool(
            name="stat",
            description="Inspect one file or directory." + _PATH_GUIDANCE,
        )
        def stat(
            path: str, workspace: str | None = None, context: ToolContext | None = None
        ) -> dict[str, Any]:
            resolved = Path(path)
            exists = resolved.exists()
            return {
                "path": str(resolved),
                "exists": exists,
                "is_file": resolved.is_file() if exists else False,
                "is_dir": resolved.is_dir() if exists else False,
                "size": resolved.stat().st_size if exists else None,
            }

        @tool(
            name="mkdir",
            description="Create one directory." + _PATH_GUIDANCE,
        )
        def mkdir(
            path: str,
            parents: bool = True,
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved = Path(path)
            resolved.mkdir(parents=parents, exist_ok=True)
            return {"path": str(resolved), "created": True}

        @tool(
            name="remove",
            description="Remove one file or directory." + _PATH_GUIDANCE,
        )
        def remove(
            path: str,
            recursive: bool = False,
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved = Path(path)
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

        functions = (
            list_dir,
            read_text,
            write_text,
            append_text,
            glob,
            stat,
            mkdir,
            remove,
        )
        return {
            wrapped.name: _FilesystemTool(wrapped)
            for func in functions
            for wrapped in (create_function_tool(func),)
        }

    def _path_lock(self, path: Path) -> threading.Lock:
        with self._path_locks_guard:
            lock = self._path_locks.get(path)
            if lock is None:
                lock = threading.Lock()
                self._path_locks[path] = lock
            return lock


def create_toolset(config: Mapping[str, Any]) -> Toolset:
    """Create the filesystem toolset plugin."""

    return FilesystemToolset(config=dict(config))


@dataclass(frozen=True, slots=True)
class _FilesystemTool:
    """Bind each call's paths and presentation without retaining workspace grants."""

    tool: AgentTool

    @property
    def name(self) -> str:
        return self.tool.name

    def definition(self) -> ToolDefinition:
        return self.tool.definition()

    async def invoke(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        return await self.prepare(arguments, context).invoke()

    def prepare(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> ToolPreparation:
        value = arguments.get("path", "." if self.name in {"list", "glob"} else None)
        if not isinstance(value, str) or not value:
            raise ToolangError("tool requires a non-empty path")
        workspace = arguments.get("workspace")
        if value.startswith("workspace:"):
            if workspace is not None:
                raise ToolangError("workspace URI cannot be combined with workspace")
            if value == "workspace://":
                if self.name != "list":
                    raise ToolangError("only fs.list can address workspace://")
                return ToolPreparation(
                    (), lambda: asyncio.to_thread(_list_workspaces, context)
                )
            name, relative = parse_workspace_uri(value)
        else:
            if "://" in value:
                raise ToolangError(f"unsupported filesystem URI: {value}")
            if not isinstance(workspace, str) or not workspace:
                raise ToolangError(
                    "use a workspace URI or specify workspace; agent home is not accessible"
                )
            name, relative = workspace, value
        root = workspace_root(name, context)
        path = resolve_workspace_path(name, relative, root)
        if self.name == "remove" and path.resolved == root:
            raise ToolangError("cannot remove a workspace root")
        uri = workspace_uri(name, path.relative)
        kwargs: dict[str, Any] = dict(
            arguments, path=str(path.resolved), workspace=name
        )
        # Enumeration uses the same canonical root captured during preparation.
        bound_context = replace(context, workspaces={name: root})

        async def invoke() -> dict[str, Any]:
            try:
                result = await self.tool.invoke(kwargs, bound_context)
            except (OSError, ToolangError) as exc:
                detail = exc.strerror if isinstance(exc, OSError) else str(exc)
                detail = (detail or "filesystem operation failed").replace(
                    str(path.resolved), uri
                )
                raise ToolangError(f"{uri}: {detail}") from exc

            def display(physical: str) -> str:
                suffix = Path(physical).relative_to(path.resolved)
                relative = PurePosixPath(path.relative) / suffix.as_posix()
                return workspace_uri(name, str(relative))

            result["path"] = uri
            for entry in result.get("entries", ()):
                entry["path"] = display(entry["path"])
            if "matches" in result:
                result["matches"] = [display(item) for item in result["matches"]]
            return result

        return ToolPreparation((path,), invoke)


def _list_workspaces(context: ToolContext) -> dict[str, Any]:
    return {
        "path": "workspace://",
        "entries": [
            {"name": name, "path": workspace_uri(name), "available": root.is_dir()}
            for name, root in sorted(context.workspaces.items())
        ],
    }


def _list_directory(path: Path, root: Path) -> dict[str, Any]:
    entries = []
    for entry in sorted(path.iterdir()):
        authorize_workspace_path(entry, root)
        entries.append(
            {"name": entry.name, "path": str(entry), "is_dir": entry.is_dir()}
        )
    return {"path": str(path), "entries": entries}


def _glob_paths(
    path: Path, pattern: str, recursive: bool, root: Path
) -> dict[str, Any]:
    if (
        not isinstance(pattern, str)
        or not pattern
        or pattern.startswith("/")
        or ".." in pattern.split("/")
    ):
        raise ToolangError("glob pattern must stay within the selected directory")
    # Walk explicitly: Path.glob can follow symlink directories in literal pattern
    # components. Never enumerate a symlink target before authorizing it.
    components = list(PurePosixPath(pattern).parts)
    if not components or any("**" in part and part != "**" for part in components):
        raise ToolangError("invalid glob pattern")
    patterns = (["**"] if recursive else []) + components
    directories_only = pattern.endswith("/")

    def walk(directory: Path, remaining: list[str]):
        part, *rest = remaining
        if part == "**":
            if rest:
                yield from walk(directory, rest)
            else:
                yield directory
            for entry in sorted(directory.iterdir()):
                authorize_workspace_path(entry, root)
                if entry.is_dir() and not entry.is_symlink():
                    yield from walk(entry, remaining)
        else:
            for entry in sorted(directory.iterdir()):
                if not fnmatchcase(entry.name, part):
                    continue
                authorize_workspace_path(entry, root)
                if not rest:
                    if not directories_only or entry.is_dir():
                        yield entry
                elif entry.is_dir() and not entry.is_symlink():
                    yield from walk(entry, rest)

    return {
        "path": str(path),
        "pattern": pattern,
        "matches": [str(p) for p in sorted(set(walk(path, patterns)))],
    }


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

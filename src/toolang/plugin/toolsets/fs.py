"""Filesystem toolset plugin."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
import shutil
import threading
from typing import Any

from toolang.base.errors import ToolangError
from toolang.base.protocols.tool import Tool, Toolset
from toolang.base.types.tool import (
    ToolContext,
    ToolDefinition,
    ToolResult,
)
from toolang.base.utils.function_tools import create_function_tool, tool
from toolang.base.utils.tool_descriptions import action_summary, workspace_label
from toolang.base.utils.workspace_paths import (
    authorize_workspace_path,
    parse_cwd,
    resolve_input_path,
    workspace_root,
    workspace_uri,
)

DEFAULT_MAX_CHARS = 20_000


@dataclass(slots=True)
class FilesystemToolset:
    """Filesystem tools for explicitly addressed, configured workspaces."""

    config: dict[str, Any]
    name: str = "fs"
    description: str | None = "Inspect and edit files."
    _max_chars: int = field(init=False, repr=False)
    _tools: dict[str, Tool] = field(init=False, repr=False)
    _path_locks: dict[Path, threading.Lock] = field(init=False, repr=False)
    _path_locks_guard: threading.Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._max_chars = _int_value(
            self.config.get("max_chars"), default=DEFAULT_MAX_CHARS
        )
        self._path_locks = {}
        self._path_locks_guard = threading.Lock()
        self._tools = self._build_tools()

    def tools(self) -> Mapping[str, Tool]:
        return dict(self._tools)

    def _build_tools(self) -> dict[str, Tool]:
        @tool(
            name="list",
            description="List one directory.",
        )
        def list_dir(
            path: str = ".",
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            assert context is not None and workspace is not None
            return _list_directory(Path(path), workspace_root(workspace, context))

        @tool(
            name="read",
            description="Read one text file.",
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
            description="Write one text file.",
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
            description="Append text to one file.",
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
            description="Match file paths under one directory.",
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
                Path(path), pattern, recursive, workspace_root(workspace, context)
            )

        @tool(
            name="stat",
            description="Inspect one file or directory.",
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
            description="Create one directory.",
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
            description="Remove one file or directory.",
        )
        def remove(
            path: str,
            recursive: bool = False,
            workspace: str | None = None,
            context: ToolContext | None = None,
        ) -> dict[str, Any]:
            resolved = Path(path)
            if resolved.is_symlink():
                resolved.unlink()
            elif not resolved.exists():
                raise ToolangError(f"path does not exist: {resolved}")
            elif resolved.is_dir():
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
    """Create the fs toolset plugin."""

    return FilesystemToolset(config=dict(config))


@dataclass(frozen=True, slots=True)
class _FilesystemTool(Tool):
    """Bind each call's paths and presentation without retaining workspace grants."""

    tool: Tool

    @property
    def name(self) -> str:
        return self.tool.name

    def definition(self) -> ToolDefinition:
        definition = self.tool.definition()
        parameters = dict(definition.parameters)
        parameters["properties"] = {
            key: value
            for key, value in parameters["properties"].items()
            if key != "workspace"
        }
        parameters["required"] = [
            name for name in parameters["required"] if name != "workspace"
        ]
        return ToolDefinition(definition.name, definition.description, parameters)

    def summary(
        self, arguments: Mapping[str, Any], result: ToolResult | None = None
    ) -> str | None:
        verbs = {
            "list": ("list", "Listing", "Listed"),
            "read": ("read", "Reading", "Read"),
            "write": ("write", "Writing", "Wrote"),
            "append": ("append to", "Appending to", "Appended to"),
            "glob": ("match", "Matching", "Matched"),
            "stat": ("inspect", "Inspecting", "Inspected"),
            "mkdir": ("create directory", "Creating directory", "Created directory"),
            "remove": ("remove", "Removing", "Removed"),
        }[self.name]
        path = arguments.get("path", "." if self.name in {"list", "glob"} else None)
        if not isinstance(path, str) or "workspace" in arguments or "cwd" in arguments:
            return None
        try:
            name, relative = parse_cwd(path)
            target = (
                workspace_label(name, relative) if name is not None else f"“{path}”"
            )
        except ToolangError:
            if "://" in path or path.startswith(":"):
                return None
            target = f"“{path}”"
        if self.name == "glob":
            target = f"{arguments.get('pattern', '*')} in {target}"
        return action_summary(result, verbs, target)

    async def invoke(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> ToolResult:
        arguments = self.bind_arguments(arguments)
        value = self._target(arguments)
        resolved, name, relative = resolve_input_path(
            value, context, follow=self.name != "remove"
        )
        uri = workspace_uri(name, relative)
        kwargs = dict(arguments, path=str(resolved), workspace=name)
        try:
            result = await self.tool.invoke(kwargs, context)
        except (OSError, ToolangError) as exc:
            detail = exc.strerror if isinstance(exc, OSError) else str(exc)
            detail = (detail or "filesystem operation failed").replace(
                str(resolved), uri
            )
            raise ToolangError(f"{uri}: {detail}") from exc
        if result.error is not None:
            return result

        def display(physical: str) -> str:
            suffix = Path(physical).relative_to(resolved)
            return workspace_uri(name, str(PurePosixPath(relative) / suffix.as_posix()))

        output = result.output
        output["path"] = uri
        for entry in output.get("entries", ()):
            entry["path"] = display(entry["path"])
        if "matches" in output:
            output["matches"] = [display(item) for item in output["matches"]]
        return result

    def paths(
        self, arguments: Mapping[str, Any], context: ToolContext
    ) -> Mapping[str, tuple[str, ...]]:
        arguments = self.bind_arguments(arguments)
        value = self._target(arguments)
        _resolved, name, relative = resolve_input_path(
            value, context, follow=self.name != "remove"
        )
        return {name: (relative,)}

    def bind_arguments(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if "workspace" in arguments or "cwd" in arguments:
            raise ToolangError("fs accepts path, not workspace or cwd arguments")
        return self.tool.bind_arguments(arguments)

    def _target(self, arguments: Mapping[str, Any]) -> str:
        value = arguments.get("path", "." if self.name in {"list", "glob"} else None)
        if not isinstance(value, str) or not value:
            raise ToolangError("tool requires a non-empty path")
        return value


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
    patterns = ["**"] if recursive else []
    for component in components:
        if component != "**" or not patterns or patterns[-1] != "**":
            patterns.append(component)
    directories_only = pattern.endswith("/")

    def walk(directory: Path, remaining: list[str]):
        part, *rest = remaining
        if part == "**":
            if rest:
                yield from walk(directory, rest)
            else:
                yield directory
            for entry in sorted(directory.iterdir()):
                if entry.is_symlink():
                    continue
                authorize_workspace_path(entry, root)
                if entry.is_dir():
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

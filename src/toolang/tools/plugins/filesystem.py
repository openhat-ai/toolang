"""Default filesystem tool provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from toolang.concepts.tools import ToolDefinition
from toolang.errors import ToolangError

from ..contracts import ToolContext, ToolProvider

DEFAULT_MAX_CHARS = 20_000


class FilesystemTool(ToolProvider):
    """Default local filesystem tool scoped to one agent home."""

    family = "filesystem"

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            family="filesystem",
            name="filesystem",
            description=(
                "Read and write text files, inspect directories, and create folders "
                "within the current agent home."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "read_text",
                            "write_text",
                            "append_text",
                            "list_dir",
                            "glob",
                            "stat",
                            "mkdir",
                        ],
                    },
                    "path": {"type": "string"},
                    "text": {"type": "string"},
                    "pattern": {"type": "string"},
                    "recursive": {"type": "boolean"},
                    "parents": {"type": "boolean"},
                    "max_chars": {"type": "integer", "minimum": 1},
                },
                "required": ["action", "path"],
                "additionalProperties": False,
            },
        )

    def invoke(self, arguments: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        action = _required_text(arguments, "action")
        path = _resolve_scoped_path(arguments.get("path"), context=context)
        if action == "read_text":
            max_chars = _int_value(arguments.get("max_chars"), default=DEFAULT_MAX_CHARS)
            text = path.read_text(encoding="utf-8")
            return {
                "path": str(path),
                "text": text[:max_chars],
                "truncated": len(text) > max_chars,
            }
        if action == "write_text":
            text = _required_text(arguments, "text")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return {"path": str(path), "bytes_written": len(text.encode("utf-8"))}
        if action == "append_text":
            text = _required_text(arguments, "text")
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)
            return {"path": str(path), "bytes_appended": len(text.encode("utf-8"))}
        if action == "list_dir":
            if not path.exists():
                raise ToolangError(f"directory does not exist: {path}")
            if not path.is_dir():
                raise ToolangError(f"path is not a directory: {path}")
            return {
                "path": str(path),
                "entries": [
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "is_dir": entry.is_dir(),
                    }
                    for entry in sorted(path.iterdir(), key=lambda item: item.name)
                ],
            }
        if action == "glob":
            pattern = _required_text(arguments, "pattern")
            recursive = bool(arguments.get("recursive"))
            matches = path.rglob(pattern) if recursive else path.glob(pattern)
            return {
                "path": str(path),
                "pattern": pattern,
                "matches": [str(item) for item in sorted(matches)],
            }
        if action == "stat":
            return {
                "path": str(path),
                "exists": path.exists(),
                "is_file": path.is_file(),
                "is_dir": path.is_dir(),
                "size": path.stat().st_size if path.exists() else None,
            }
        if action == "mkdir":
            parents = bool(arguments.get("parents", True))
            path.mkdir(parents=parents, exist_ok=True)
            return {"path": str(path), "created": True}
        raise ToolangError(f"unsupported filesystem action: {action}")


def create_filesystem_tool(config: dict[str, Any]) -> ToolProvider:
    """Create the default filesystem tool provider."""

    return FilesystemTool()


def _resolve_scoped_path(raw_path: object, *, context: ToolContext) -> Path:
    text = str(raw_path or "").strip()
    if not text:
        raise ToolangError("filesystem tool requires a non-empty path")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = context.working_directory / candidate
    resolved = candidate.resolve()
    home = Path(context.agent.home).resolve()
    try:
        resolved.relative_to(home)
    except ValueError as exc:
        raise ToolangError(f"filesystem path escapes agent home: {resolved}") from exc
    return resolved


def _required_text(arguments: dict[str, Any], name: str) -> str:
    value = str(arguments.get(name, "")).strip()
    if not value:
        raise ToolangError(f"filesystem tool requires {name!r}")
    return value


def _int_value(value: object, *, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = value if isinstance(value, int) and not isinstance(value, bool) else int(str(value))
    except (TypeError, ValueError) as exc:
        raise ToolangError("filesystem tool max_chars must be an integer") from exc
    if parsed <= 0:
        raise ToolangError("filesystem tool max_chars must be positive")
    return parsed

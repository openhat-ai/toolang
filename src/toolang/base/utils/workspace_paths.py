"""Workspace URI encoding and explicit directory grants."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
import re
from urllib.parse import quote, unquote

from ..errors import ToolangError
from ..types.tool import ToolContext, ToolPath

_URI = re.compile(r"workspace://(\.|[a-z0-9]+(?:-[a-z0-9]+)*)(/[^?#]*)?")
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")


def parse_workspace_uri(value: str) -> tuple[str, str]:
    """Validate an external URI once and decode its workspace-relative path."""

    match = _URI.fullmatch(value)
    if match is None or _BAD_ESCAPE.search(value):
        raise ToolangError(f"invalid workspace URI: {value}")
    try:
        path = unquote(match[2] or "/", errors="strict")
    except UnicodeDecodeError as exc:
        raise ToolangError(f"invalid workspace URI: {value}") from exc
    if "\x00" in path:
        raise ToolangError("workspace URI contains a null byte")
    return match[1], path


def workspace_uri(name: str, path: str = "/") -> str:
    """Encode a known workspace-relative path, not an operating-system path."""

    return f"workspace://{name}/" + quote(path.lstrip("/"), safe="/")


def capture_cwd(directory: Path, workspaces: Mapping[str, str]) -> ToolPath:
    """Capture a local caller's directory without widening a registered grant."""

    directory = directory.resolve()
    if not directory.is_dir():
        raise ToolangError("cwd must be an existing directory")
    for name, path in workspaces.items():
        root = Path(path).resolve()
        if directory.is_relative_to(root):
            relative = directory.relative_to(root).as_posix()
            return ToolPath(directory, name, "/" if relative == "." else f"/{relative}")
    return ToolPath(directory, ".")


def resolve_workspace_location(name: str, value: str, context: ToolContext) -> ToolPath:
    """Resolve the cwd alias to its real workspace before authorization and recall."""

    if name == ".":
        cwd = context.cwd
        if cwd is None or cwd.workspace is None:
            raise ToolangError("no cwd is available for this run")
        name = cwd.workspace
        root = workspace_root(name, context)
        if (root / cwd.relative.lstrip("/")).resolve() != cwd.resolved:
            raise ToolangError(
                "cwd workspace no longer resolves to its recorded location"
            )
        value = cwd.relative.rstrip("/") + "/" + value.lstrip("/")
    else:
        root = workspace_root(name, context)
    return resolve_workspace_path(name, value, root)


def authorize_workspace_path(path: Path, root: Path) -> Path:
    """Resolve a path without granting access outside its workspace root."""

    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ToolangError("path escapes workspace")
    return resolved


def workspace_root(name: str, context: ToolContext) -> Path:
    """Read one directory grant from the captured Tool Step context."""

    if name not in context.workspaces:
        raise ToolangError(f"workspace is not available: {name}")
    root = context.workspaces[name].resolve()
    if not root.is_dir():
        raise ToolangError(f"workspace directory is not available: {name}")
    return root


def resolve_workspace_path(name: str, value: str, root: Path) -> ToolPath:
    """Bind a workspace-relative path to a concrete, authorized target."""

    candidate = root / value.lstrip("/")
    normalized = Path(os.path.abspath(candidate))
    if not normalized.is_relative_to(root):
        raise ToolangError(f"path escapes workspace: {name}")
    resolved = authorize_workspace_path(candidate, root)
    # Preserve a logical alias unless parent traversal changes its meaning.
    if ".." in candidate.parts and normalized.resolve() != resolved:
        normalized = resolved
    relative = "/" + normalized.relative_to(root).as_posix()
    return ToolPath(resolved, name, "/" if relative == "/." else relative)

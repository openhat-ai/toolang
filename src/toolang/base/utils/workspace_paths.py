"""Workspace-scoped paths, portable references, and path grants."""

from __future__ import annotations

import os
from pathlib import Path
import re
from urllib.parse import quote, unquote

from ..errors import ToolangError
from ..types.tool import ToolContext

_NAME = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_FORCED = re.compile(rf":({_NAME})://([^?#]*)\Z")
_SCHEME = re.compile(rf"({_NAME})://([^?#]*)\Z")
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_KNOWN = frozenset(
    {
        "file",
        "workspace",
        "runspace",
        "http",
        "https",
        "ftp",
        "ftps",
        "ws",
        "wss",
        "ssh",
        "git",
        "data",
        "mailto",
        "urn",
        "about",
    }
)


def _decode(value: str, *, relative: bool = True) -> str:
    if _BAD_ESCAPE.search(value):
        raise ToolangError(f"invalid path escape: {value}")
    try:
        path = unquote(value, errors="strict")
    except UnicodeDecodeError as exc:
        raise ToolangError(f"invalid UTF-8 path: {value}") from exc
    if "\x00" in path or "?" in value or "#" in value:
        raise ToolangError(f"invalid path reference: {value}")
    if relative and path.startswith("/"):
        raise ToolangError(f"workspace path must be relative: {value}")
    return path


def workspace_uri(name: str, path: str = "") -> str:
    """The canonical unambiguous location and filesystem result spelling."""
    if re.fullmatch(_NAME, name) is None:
        raise ToolangError(f"invalid workspace name: {name}")
    return f":{name}://" + quote(path.lstrip("/"), safe="/")


def parse_cwd(cwd: str) -> tuple[str | None, str]:
    """Decode a durable canonical location without consulting the filesystem."""
    if cwd == "":
        return None, ""
    match = _FORCED.fullmatch(cwd)
    if match is None:
        raise ToolangError(f"invalid working location: {cwd}")
    relative = _decode(match[2])
    if (
        relative
        and (
            relative.endswith("/")
            or "//" in relative
            or "." in relative.split("/")
            or ".." in relative.split("/")
        )
    ) or workspace_uri(match[1], relative) != cwd:
        raise ToolangError(f"noncanonical working location: {cwd}")
    return match[1], relative


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
    root = context.workspaces[name]
    key = (root, "", True)
    if key not in context._paths:
        context._paths[key] = (root.resolve(), "/")
    root = context._paths[key][0]
    if not root.is_dir():
        raise ToolangError(f"workspace directory is not available: {name}")
    return root


def resolve_workspace_path(
    name: str, value: str, context: ToolContext, *, follow: bool = True
) -> tuple[Path, str]:
    """Bind a root-relative path and preserve unlink semantics for final links."""
    root = workspace_root(name, context)
    key = (root, value, follow)
    if key in context._paths:
        return context._paths[key]
    candidate = root / value.lstrip("/")
    normalized = Path(os.path.abspath(candidate))
    if not normalized.is_relative_to(root):
        raise ToolangError(f"path escapes workspace: {name}")
    resolved = authorize_workspace_path(candidate, root)
    if ".." in candidate.parts and normalized.resolve() != resolved:
        normalized = resolved
    relative = "/" + normalized.relative_to(root).as_posix()
    relative = "/" if relative == "/." else relative
    if not follow:
        entry = root / relative.lstrip("/")
        if entry == root:
            raise ToolangError("cannot remove a workspace root")
        resolved = authorize_workspace_path(entry.parent, root) / entry.name
    result = (resolved, relative)
    context._paths[key] = result
    return result


def resolve_input_path(
    value: str, context: ToolContext, *, follow: bool = True
) -> tuple[Path, str, str]:
    """Resolve one user path to its physical target and logical workspace identity."""
    if not isinstance(value, str) or not value:
        raise ToolangError("tool requires a non-empty path")
    key = (value, follow)
    if key in context._input_paths:
        return context._input_paths[key]
    match = _FORCED.fullmatch(value)
    if match:
        name, path = match[1], _decode(match[2])
    else:
        match = _SCHEME.fullmatch(value)
        if match:
            name, path = match[1], match[2]
            if name == "file":
                if not path:
                    raise ToolangError("file:// requires a path")
                path = _decode(path, relative=not path.startswith("/"))
                if path.startswith("/"):
                    result = _absolute(path, context, follow=follow)
                    context._input_paths[key] = result
                    return result
                name, current = parse_cwd(context.cwd)
                if name is None:
                    raise ToolangError(
                        "relative path requires a current workspace; use _toolang.cd"
                    )
                path = str(Path(current) / path)
            elif name in _KNOWN:
                raise ToolangError(
                    f"unsupported path scheme: {name}; use :{name}:// for a workspace"
                )
            else:
                path = _decode(path)
        elif value.startswith("/"):
            result = _absolute(value, context, follow=follow)
            context._input_paths[key] = result
            return result
        elif value.startswith(":") or "://" in value:
            raise ToolangError(f"invalid path reference: {value}")
        else:
            name, current = parse_cwd(context.cwd)
            if name is None:
                raise ToolangError(
                    "relative path requires a current workspace; use _toolang.cd"
                )
            path = str(Path(current) / value)
    assert name is not None
    resolved, relative = resolve_workspace_path(name, path, context, follow=follow)
    result = (resolved, name, relative)
    context._input_paths[key] = result
    return result


def _absolute(
    value: str, context: ToolContext, *, follow: bool
) -> tuple[Path, str, str]:
    path = Path(value).resolve()
    roots = []
    for name, source in context.workspaces.items():
        if not source.is_dir():
            continue
        root = workspace_root(name, context)
        if path.is_relative_to(root):
            roots.append((root, name))
    if not roots:
        raise ToolangError(f"path is outside configured workspaces: {value}")
    root, name = max(roots, key=lambda item: len(item[0].parts))
    relative = "/" + path.relative_to(root).as_posix()
    return (
        resolve_workspace_path(name, relative, context, follow=follow)[0],
        name,
        relative if relative != "/." else "/",
    )

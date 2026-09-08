"""Shared path resolution for home-scoped tools and workspace rules."""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import ToolangError
from ..types.tool import ToolContext


def authorize_home_path(path: Path, home: Path) -> Path:
    """Resolve symlinks without granting access outside the existing home root."""

    resolved = path.resolve()
    if not resolved.is_relative_to(home.resolve()):
        raise ToolangError(f"path escapes agent home: {resolved}")
    return resolved


def resolve_tool_path(
    value: str, context: ToolContext, *, workspace: str | None = None
) -> tuple[Path, str | None, str]:
    """Resolve a path while preserving significant whitespace and workspace identity."""

    if not isinstance(value, str) or not value:
        raise ToolangError("tool requires a non-empty path")
    roots = {
        name: Path(os.path.abspath(root)) for name, root in context.workspaces.items()
    }
    key = (context.home, f"{workspace or ''}:{value}", True)
    cached = context._paths.get(key)
    # The logical anchor is still selected from this call's immutable grants.
    if workspace is not None:
        if not isinstance(workspace, str) or workspace not in roots:
            raise ToolangError(f"workspace is not available: {workspace}")
        root = roots[workspace]
        candidate = root / value.lstrip("/")
        normalized = Path(os.path.abspath(candidate))
        if not normalized.is_relative_to(root):
            raise ToolangError(f"path escapes workspace {workspace}: {value}")
        resolved = cached[0] if cached else authorize_home_path(candidate, context.home)
        relative = normalized.relative_to(root).as_posix()
        if ".." in candidate.parts and normalized.resolve() != resolved:
            # Resolve parent traversal before spelling the normalized honor path.
            # Lexically dropping '..' can select a different file after a symlink.
            try:
                relative = resolved.relative_to(root.resolve()).as_posix()
            except ValueError as exc:
                raise ToolangError(
                    f"parent traversal through a symlink leaves workspace {workspace}; use a direct path"
                ) from exc
    else:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = context.home / candidate
        resolved = cached[0] if cached else authorize_home_path(candidate, context.home)
        matches = [
            name
            for name, root in roots.items()
            if resolved.is_relative_to(root.resolve())
        ]
        if len(matches) > 1:
            raise ToolangError("path matches multiple workspaces; specify workspace")
        workspace = matches[0] if matches else None
        relative = (
            resolved.relative_to(roots[workspace].resolve()).as_posix()
            if workspace
            else "."
        )
    relative = "/" if relative == "." else "/" + relative
    if cached:
        relative = cached[1]
    else:
        context._paths[key] = (resolved, relative)
    return resolved, workspace, relative

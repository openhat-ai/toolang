"""Human-owned external workspace grants."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
import tomllib
from typing import Any, cast

import tomlkit

from toolang.common.files import atomic_write_text, file_write_lock
from toolang.common.layout import AgentLayout
from toolang.common.workspace import (
    Workspace,
    parse_workspaces,
    validate_workspace_name,
    workspace_paths_overlap,
)

from .errors import CatalogConflictError, CatalogNotFoundError


class AgentWorkspaces:
    """CRUD for workspace grants in one resident agent config."""

    def __init__(self, layout: AgentLayout) -> None:
        if layout.placement != "resident":
            raise ValueError("workspace grants require a resident agent")
        self.layout = layout
        self.config_path = layout.config

    @property
    def lock_path(self) -> Path:
        return self.config_path.with_name(f".{self.config_path.name}.lock")

    def write_lock(self) -> AbstractContextManager[None]:
        return file_write_lock(self.lock_path)

    def list(self) -> tuple[Workspace, ...]:
        if not self.config_path.is_file():
            return ()
        workspaces = parse_workspaces(
            cast(
                dict[str, object],
                tomllib.loads(self.config_path.read_text(encoding="utf-8")),
            )
        )
        for workspace in workspaces:
            _validate_workspace_location(self.layout, workspace)
        return workspaces

    def add(self, path: Path, *, name: str | None = None) -> Workspace:
        try:
            canonical = path.expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"workspace directory not found: {path}") from exc
        if not canonical.is_dir():
            raise ValueError(f"workspace path is not a directory: {canonical}")
        workspace = Workspace(name=name or canonical.name, path=canonical)
        _validate_workspace_location(self.layout, workspace)
        with self.write_lock():
            current = self.list()
            if any(item.name == workspace.name for item in current):
                raise CatalogConflictError(
                    f"workspace already exists: {workspace.name}"
                )
            for item in current:
                if workspace_paths_overlap(item.path, workspace.path):
                    raise CatalogConflictError(
                        f"workspace path overlaps {item.name}: {workspace.path}"
                    )
            document = _load_document(self.config_path)
            table = _document_workspace_table(document, create=True)
            assert table is not None
            table[workspace.name] = str(workspace.path)
            atomic_write_text(self.config_path, tomlkit.dumps(document))
        return workspace

    def remove(self, name: str) -> Workspace:
        validate_workspace_name(name)
        with self.write_lock():
            current = {item.name: item for item in self.list()}
            workspace = current.get(name)
            if workspace is None:
                raise CatalogNotFoundError(f"workspace not found: {name}")
            document = _load_document(self.config_path)
            table = _document_workspace_table(document, create=False)
            if table is None or name not in table:
                raise CatalogNotFoundError(f"workspace not found: {name}")
            del table[name]
            atomic_write_text(self.config_path, tomlkit.dumps(document))
        return workspace


def _load_document(path: Path) -> Any:
    content = path.read_text(encoding="utf-8") if path.is_file() else ""
    return tomlkit.parse(content)


def _document_workspace_table(document: Any, *, create: bool) -> Any | None:
    value = document.get("workspaces")
    if value is not None:
        if not isinstance(value, Mapping):
            raise ValueError("workspaces config must be a table")
        return value
    if not create:
        return None
    value = tomlkit.table()
    document["workspaces"] = value
    return value


def _validate_workspace_location(layout: AgentLayout, workspace: Workspace) -> None:
    if workspace_paths_overlap(layout.home.resolve(), workspace.path):
        raise ValueError(f"workspace path overlaps agent home: {workspace.path}")

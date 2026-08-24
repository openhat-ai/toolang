"""Tests for human-owned external workspace grants."""

from pathlib import Path
import tomllib

import pytest

from toolang.catalog.errors import CatalogConflictError, CatalogNotFoundError
from toolang.catalog.workspace import AgentWorkspaces
from toolang.common.workspace import (
    hosted_workspaces_env,
    resolve_active_workspaces,
)
from toolang.common.layout import AgentLayout


def _catalog(tmp_path: Path) -> AgentWorkspaces:
    layout = AgentLayout.resident(tmp_path / "root", "alice")
    layout.home.mkdir(parents=True)
    layout.program.write_text("agent alice\n", encoding="utf-8")
    layout.config.write_text('title = "Alice"\n', encoding="utf-8")
    return AgentWorkspaces(layout)


def test_workspace_add_list_remove_preserves_unrelated_config(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    project = tmp_path / "project"
    project.mkdir()

    added = catalog.add(project)

    assert added.name == "project"
    assert catalog.list() == (added,)
    config = tomllib.loads(catalog.config_path.read_text(encoding="utf-8"))
    assert config == {
        "title": "Alice",
        "workspaces": {"project": str(project.resolve())},
    }

    assert catalog.remove("project") == added
    assert catalog.list() == ()
    assert tomllib.loads(catalog.config_path.read_text(encoding="utf-8")) == {
        "title": "Alice",
        "workspaces": {},
    }


def test_workspace_grants_validate_names_paths_and_overlap(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    catalog.add(project, name="main")

    with pytest.raises(CatalogConflictError, match="already exists"):
        catalog.add(nested, name="main")
    with pytest.raises(CatalogConflictError, match="overlaps main"):
        catalog.add(nested, name="nested")
    with pytest.raises(ValueError, match="ASCII identifier"):
        catalog.add(tmp_path, name="bad.name")
    with pytest.raises(ValueError, match="overlaps agent home"):
        catalog.add(catalog.layout.home, name="home")
    with pytest.raises(FileNotFoundError, match="not found"):
        catalog.add(tmp_path / "missing")
    with pytest.raises(CatalogNotFoundError, match="workspace not found"):
        catalog.remove("missing")

    catalog.config_path.write_text(
        '[workspaces]\nrelative = "project"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="must be absolute"):
        catalog.list()


def test_docker_active_workspaces_intersect_current_config(tmp_path: Path) -> None:
    configured = tmp_path / "configured"
    active = tmp_path / "active"
    configured.mkdir()
    active.mkdir()
    env_name, env_value = hosted_workspaces_env({"project": (str(configured), active)})
    environ = {
        "TOOLANG_SANDBOX": "docker:python:3.13-slim",
        env_name: env_value,
    }

    resolved = resolve_active_workspaces(
        {"workspaces": {"project": str(configured)}},
        environ=environ,
    )
    removed = resolve_active_workspaces({}, environ=environ)
    changed = resolve_active_workspaces(
        {"workspaces": {"project": str(tmp_path)}},
        environ=environ,
    )
    active.rmdir()
    missing_mount = resolve_active_workspaces(
        {"workspaces": {"project": str(configured)}},
        environ=environ,
    )

    assert [(item.name, item.path) for item in resolved] == [
        ("project", active.resolve())
    ]
    assert removed == ()
    assert changed == ()
    assert missing_mount == ()

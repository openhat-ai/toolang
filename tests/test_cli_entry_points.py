from __future__ import annotations

from importlib import import_module, metadata
from pathlib import Path
import tomllib
from typing import Any


PROJECT_ROOT = Path(__file__).parents[1]


def _project_scripts() -> dict[str, str]:
    document = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    return document["project"]["scripts"]


def _load_target(value: str) -> Any:
    module_name, separator, object_path = value.partition(":")
    assert separator, f"console script target has no object path: {value}"
    target: Any = import_module(module_name)
    for name in object_path.split("."):
        target = getattr(target, name)
    return target


def test_project_console_script_targets_are_importable() -> None:
    scripts = _project_scripts()

    assert scripts.keys() == {"toolang", "too", "caps"}
    for name, value in scripts.items():
        assert callable(_load_target(value)), f"console script is not callable: {name}"


def test_installed_console_scripts_match_project() -> None:
    installed = {
        entry_point.name: entry_point.value
        for entry_point in metadata.distribution("toolang").entry_points
        if entry_point.group == "console_scripts"
    }

    assert installed == _project_scripts()

from __future__ import annotations

from importlib import import_module, metadata
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any

import click
import pytest
import typer
from typer.testing import CliRunner

from toolang.cli.caps.main import app as caps_app
from toolang.cli.toolang.main import app as toolang_app
from tests import PROJECT_ROOT


def _command_paths(app: typer.Typer) -> tuple[tuple[str, ...], ...]:
    paths: list[tuple[str, ...]] = []

    def collect(command: click.Command, prefix: tuple[str, ...]) -> None:
        if not isinstance(command, click.Group):
            return
        for name, child in command.commands.items():
            path = (*prefix, name)
            paths.append(path)
            collect(child, path)

    collect(typer.main.get_command(app), ())
    return tuple(paths)


CLI_COMMANDS = tuple(
    (name, app, path)
    for name, app in (
        ("toolang", toolang_app),
        ("caps", caps_app),
    )
    for path in _command_paths(app)
)


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


@pytest.mark.parametrize(
    ("script", "prefix"),
    [("toolang", "toolang "), ("too", "toolang "), ("caps", "caps ")],
)
def test_installed_console_scripts_execute(script: str, prefix: str) -> None:
    executable = Path(sys.executable).with_name(script)
    completed = subprocess.run(
        [executable, "--version"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith(prefix)


@pytest.mark.parametrize(
    ("module", "prefix"),
    [
        ("toolang.cli.toolang", "toolang "),
        ("toolang.cli.caps", "caps "),
    ],
)
def test_cli_package_is_executable_as_a_module(module: str, prefix: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-m", module, "--version"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith(prefix)


@pytest.mark.parametrize(
    ("name", "app"), (("toolang", toolang_app), ("caps", caps_app))
)
def test_version_option_has_no_short_alias(name: str, app: typer.Typer) -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"], prog_name=name)
    short_result = runner.invoke(app, ["-V"], prog_name=name)

    assert help_result.exit_code == 0, help_result.output
    assert "--version" in help_result.output
    assert "-V" not in help_result.output
    assert short_result.exit_code == 2
    assert "No such option: -V" in short_result.output


def test_inspect_help_is_concise_and_consistent() -> None:
    runner = CliRunner()
    root_result = runner.invoke(toolang_app, ["--help"], prog_name="toolang")
    inspect_result = runner.invoke(
        toolang_app, ["inspect", "--help"], prog_name="toolang"
    )

    assert root_result.exit_code == 0, root_result.output
    assert inspect_result.exit_code == 0, inspect_result.output
    for result in (root_result, inspect_result):
        output = click.unstyle(result.output)
        assert "Inspect runs." in output
        assert "Inspect run records." not in output
        assert "Inspect a historical record or one of its fields." not in output


@pytest.mark.parametrize(
    ("name", "app", "path"),
    CLI_COMMANDS,
    ids=[f"{name} {' '.join(path)}" for name, _app, path in CLI_COMMANDS],
)
def test_every_cli_command_renders_help(
    name: str, app: typer.Typer, path: tuple[str, ...]
) -> None:
    result = CliRunner().invoke(app, [*path, "--help"], prog_name=name)

    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output

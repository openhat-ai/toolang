from __future__ import annotations

from importlib import import_module, metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any

import pytest
import typer
from typer._click import Command
from typer._click.utils import strip_ansi
from typer.core import TyperGroup
from typer.testing import CliRunner

from toolang.cli.common.lazy import LazyCommand
from toolang.cli.toolang.main import app as toolang_app
from toolang.common.version import displayed_toolang_version
from tests import PROJECT_ROOT


def _command_paths(app: typer.Typer) -> tuple[tuple[str, ...], ...]:
    paths: list[tuple[str, ...]] = []

    def collect(command: Command, prefix: tuple[str, ...]) -> None:
        if isinstance(command, LazyCommand):
            command = command.load()
        if not isinstance(command, TyperGroup):
            return
        for name, child in command.commands.items():
            path = (*prefix, name)
            paths.append(path)
            collect(child, path)

    collect(typer.main.get_command(app), ())
    return tuple(paths)


CLI_COMMANDS = tuple(
    (name, app, path)
    for name, app in (("toolang", toolang_app),)
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

    assert scripts.keys() == {"toolang", "too"}
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
    # `toolang` and `too` print only the version.
    [("toolang", ""), ("too", "")],
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
    assert completed.stdout == f"{prefix}{displayed_toolang_version()}\n"


@pytest.mark.parametrize("script", ["too", "toolang"])
@pytest.mark.parametrize(
    "tail,expected",
    [
        (("al",), {"alice"}),
        (("models", "al"), set()),
        (("skill", "list", "al"), set()),
    ],
)
def test_installed_console_scripts_complete_agent_prefixes_only(
    script, tail, expected, tmp_path
):
    from shlex import join
    from toolang.catalog.agent import LocalAgents

    LocalAgents(tmp_path / "agents").create("alice", content="flow main():\n  pass\n")
    before = set(tmp_path.rglob("*"))
    words = [script, "--root", str(tmp_path), *tail]
    # A fresh process prevents other Typer apps from pre-registering the backends.
    completed = subprocess.run(
        [Path(sys.executable).with_name(script)],
        env={
            **os.environ,
            f"_{script.upper()}_COMPLETE": "complete_bash",
            "COMP_WORDS": join(words),
            "COMP_CWORD": str(len(words) - 1),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert set(completed.stdout.split()) == expected
    assert not completed.stderr
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("script", ["too", "toolang"])
def test_installed_console_scripts_complete_only_agent_scoped_commands(
    script, tmp_path
):
    from shlex import join

    words = [script, "--root", str(tmp_path), "alice", ""]
    completed = subprocess.run(
        [Path(sys.executable).with_name(script)],
        env={
            **os.environ,
            f"_{script.upper()}_COMPLETE": "complete_bash",
            "COMP_WORDS": join(words),
            "COMP_CWORD": str(len(words) - 1),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    candidates = set(completed.stdout.split())
    assert {"info", "chat", "skill", "workspace"} <= candidates
    assert candidates.isdisjoint(
        {"new", "clone", "remove", "list", "hub", "talk", "convo", "init", "run"}
    )
    assert not completed.stderr
    assert not tuple(tmp_path.iterdir())


def test_toolang_version_does_not_load_runtime_command_modules() -> None:
    probe = """
import json
import sys
from toolang.cli.toolang.main import main

status = main(["--version"])
loaded = sorted(
    name
    for name in sys.modules
    if name == "fastapi"
    or name == "toolang.up.core"
    or name == "toolang.execution.executor"
    or name == "toolang.cli.toolang.commands.script"
    or name == "toolang.cli.toolang.commands.model_catalog"
    or name == "toolang.cli.toolang.commands.chat.main"
)
print(json.dumps(loaded), file=sys.stderr)
raise SystemExit(status)
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"{displayed_toolang_version()}\n"
    assert json.loads(completed.stderr) == []


@pytest.mark.parametrize(
    "path",
    [(), *(path for _name, _app, path in CLI_COMMANDS)],
    ids=["root", *(" ".join(path) for _name, _app, path in CLI_COMMANDS)],
)
def test_cli_help_does_not_load_execution_dependencies(path: tuple[str, ...]) -> None:
    probe = """
import json
import sys
from toolang.cli.toolang.main import main

status = main(sys.argv[1:])
prefixes = (
    "fastapi", "prompt_toolkit", "toolang.execution.executor", "toolang.api.schemas",
    "toolang.up.server", "toolang.up.core",
    "toolang.state.prepare", "toolang.state.watcher",
    "toolang.cli.common.script_progress",
    "toolang.cli.common.execution_progress.projector",
)
loaded = sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
)
print(json.dumps(loaded), file=sys.stderr)
raise SystemExit(status)
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, *path, "--help"],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    reference = len(path) == 2 and path[0] == "help"
    assert "Usage:" in completed.stdout or path == ("more",) or reference
    assert json.loads(completed.stderr) == []


def test_importing_up_submodule_does_not_load_agent_core() -> None:
    probe = """
import json
import sys
import toolang.up.logging

print(json.dumps("toolang.up.core" in sys.modules))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) is False


@pytest.mark.parametrize(
    ("arguments", "status", "expected"),
    [
        (["--help"], 0, "Runnables:"),
        # The unnamed flow is the script's default runnable, so a bare
        # invocation runs it instead of printing group help.
        ([], 2, "Missing required primary input."),
        (["flow:", "--help"], 0, "The flow proceeds as follows:"),
        (["flow:"], 2, "Missing required primary input."),
    ],
)
def test_script_help_does_not_load_execution_dependencies(
    arguments: list[str], status: int, expected: str
) -> None:
    probe = """
import json
import sys
from toolang.cli.toolang.main import main

status = main(["examples/flows/deep_search.too", *sys.argv[1:]])
prefixes = (
    "fastapi", "httpx", "toolang.execution.executor", "toolang.execution.schemas",
    "toolang.execution.remote", "toolang.execution.store", "toolang.setup",
    "toolang.state.prepare", "toolang.state.watcher", "toolang.up.sandbox",
    "toolang.cli.common.script_progress",
)
loaded = sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
)
print(json.dumps(loaded), file=sys.stderr)
raise SystemExit(status)
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, *arguments],
        cwd=PROJECT_ROOT,
        stdin=subprocess.DEVNULL,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == status, completed.stderr
    diagnostic, _, loaded = completed.stderr.rstrip("\n").rpartition("\n")
    assert json.loads(loaded) == []
    if status:
        assert not completed.stdout
        assert "Usage:" in diagnostic
        assert expected in diagnostic
        assert "The flow proceeds as follows:" not in diagnostic
    else:
        assert not diagnostic
        assert "Usage:" in completed.stdout
        assert expected in completed.stdout


@pytest.mark.parametrize(
    ("module", "prefix"),
    [
        ("toolang.cli.toolang", ""),
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
    assert completed.stdout == f"{prefix}{displayed_toolang_version()}\n"


@pytest.mark.parametrize(
    ("name", "app", "prefix"),
    (("toolang", toolang_app, ""),),
)
def test_version_option_has_uppercase_short_alias(
    name: str, app: typer.Typer, prefix: str
) -> None:
    runner = CliRunner()
    help_result = runner.invoke(app, ["--help"], prog_name=name)
    short_result = runner.invoke(app, ["-V"], prog_name=name)
    help_output = strip_ansi(help_result.output)
    short_output = strip_ansi(short_result.output)

    assert help_result.exit_code == 0, help_result.output
    assert "-V, --version" in help_output
    assert "Show version and exit" in help_output
    assert short_result.exit_code == 0, short_result.output
    assert short_output == f"{prefix}{displayed_toolang_version()}\n"


def test_inspect_help_is_concise_and_consistent() -> None:
    runner = CliRunner()
    root_result = runner.invoke(toolang_app, ["--help"], prog_name="toolang")
    inspect_result = runner.invoke(
        toolang_app, ["inspect", "--help"], prog_name="toolang"
    )

    assert root_result.exit_code == 0, root_result.output
    assert inspect_result.exit_code == 0, inspect_result.output
    for result in (root_result, inspect_result):
        output = strip_ansi(result.output)
        assert "Inspect agent runs" in output
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
    reference = len(path) == 2 and path[0] == "help"
    if name == "toolang" and (path == ("more",) or reference):
        assert "Usage:" not in result.output
        return
    assert "Usage:" in result.output
    usage = result.output.partition("Usage:")[2].splitlines()[0]
    assert "{" not in usage
    assert not any(label in result.output for label in ("<str>", "<int>", "<path>"))

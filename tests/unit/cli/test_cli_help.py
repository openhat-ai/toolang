"""Help output follows the same operand conventions across CLI entry points."""

from enum import Enum
from pathlib import Path
from typing import Annotated

import pytest
import typer
from typer._click import Context
from typer._click.utils import strip_ansi
from typer.core import TyperArgument, TyperCommand, TyperGroup
from typer.models import TyperPath
from typer.testing import CliRunner

from toolang.cli.caps.main import main as caps_main
from toolang.cli.common.help import CliCommand
from toolang.cli.common.lazy import LazyCommand
from toolang.cli.common.parameters import PathType
from toolang.cli.toolang.main import app, main as too_main


@pytest.mark.parametrize("main", [too_main, caps_main])
def test_prompt_help_uses_conventional_metavars(main, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["too"])
    assert main(["--root", str(tmp_path), "a", "prompt", "new", "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    assert "Usage: too [AGENT] prompt new [OPTIONS] NAME" in " ".join(output.split())
    assert "<str>" not in output
    for name in ("AGENT", "NAME"):
        row = next(
            line for line in output.splitlines() if "│" in line and name in line.split()
        )
        assert "TEXT" in row
    assert "NAME" in next(line for line in output.splitlines() if "--template" in line)


@pytest.mark.parametrize(
    ("arguments", "options"),
    [
        (
            ["a", "run"],
            ("--sandbox", "--allow", "--limit", "--default", "--compact-model"),
        ),
        (
            ["a", "start"],
            ("--sandbox", "--allow", "--limit", "--default", "--compact-model"),
        ),
        (
            ["a", "chat"],
            ("--sandbox", "--allow", "--limit", "--default", "--compact-model"),
        ),
        (["a", "retry"], ("--allow", "--limit")),
        (["a", "compact"], ("--model", "--limit")),
        (["a", "rerun"], ("--sandbox", "--allow", "--limit", "--model")),
        (
            ["serve", "a"],
            ("--allow", "--limit", "--default", "--compact-model", "--log"),
        ),
    ],
)
def test_help_uses_semantic_configuration_metavars(
    arguments, options, tmp_path, capsys
):
    metavars = {
        "--sandbox": "SANDBOX_SPEC",
        "--allow": "RESOURCE=QUERY",
        "--limit": "LIMIT=VALUE",
        "--default": "SETTING=VALUE",
        "--compact-model": "MODEL_SPEC",
        "--model": "MODEL_SPEC",
        "--log": "LOG_SPEC",
    }
    assert too_main(["--root", str(tmp_path), *arguments, "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    for option in options:
        row = next(line for line in output.splitlines() if option in line.split())
        assert metavars[option] in row.split()


@pytest.mark.parametrize("main", [too_main, caps_main])
def test_error_help_path_excludes_virtual_arguments(
    main, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    assert main(["--root", str(tmp_path), "a", "prompt", "new", "--unknown"]) == 2
    output = strip_ansi(capsys.readouterr().err)
    assert "Try 'too prompt new --help'" in output
    assert "TEXT" not in output


@pytest.mark.parametrize("group", ["workspace", "chore", "task"])
def test_required_group_agent_is_documented(group, tmp_path: Path, capsys):
    assert too_main(["--root", str(tmp_path), "a", group, "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    row = next(
        (
            line
            for line in output.splitlines()
            if "│" in line and "AGENT" in line.split()
        ),
        "",
    )
    assert "TEXT" in row and "[required]" in row


class _Mode(str, Enum):
    FAST = "fast"
    SLOW = "slow"


def test_help_preserves_native_validation_and_completion(tmp_path):
    def make_app(cls):
        path_type = PathType if cls is CliCommand else TyperPath
        app = typer.Typer(add_completion=False)

        @app.command(name="sample", cls=cls)
        def callback(
            path: Annotated[
                Path,
                typer.Argument(
                    metavar="FILE",
                    click_type=path_type(
                        exists=True, dir_okay=False, resolve_path=True
                    ),
                ),
            ],
            count: Annotated[
                int,
                typer.Option(
                    "--count", metavar="INTEGER", min=1, max=3, envvar="HELP_TEST_COUNT"
                ),
            ] = 2,
            mode: _Mode = _Mode.FAST,
            item: list[int] | None = None,
            enabled: bool = False,
        ):
            assert isinstance(path, Path)
            assert isinstance(count, int)
            assert isinstance(mode, _Mode)
            typer.echo(f"{path.name}:{count}:{mode.value}:{item}:{enabled}")

        return app

    apps = [make_app(cls) for cls in (TyperCommand, CliCommand)]
    commands = [typer.main.get_command(app) for app in apps]
    contexts = [Context(command, info_name="sample") for command in commands]
    for before, after in zip(commands[0].params, commands[1].params, strict=True):
        assert isinstance(after.type, type(before.type))
        assert before.type.name == after.type.name
        assert before.opts == after.opts
        before_items = before.type.shell_complete(contexts[0], before, "f")
        after_items = after.type.shell_complete(contexts[1], after, "f")
        assert [(item.value, item.type, item.help) for item in before_items] == [
            (item.value, item.type, item.help) for item in after_items
        ]
    assert (
        commands[0].params[0].type.get_metavar(commands[0].params[0], contexts[0])
        is None
    )
    assert (
        commands[1].params[0].type.get_metavar(commands[1].params[0], contexts[1])
        == "FILE"
    )

    source = tmp_path / "sample.txt"
    source.write_text("test")
    runner = CliRunner()
    success = runner.invoke(
        apps[1],
        [str(source), "--item", "1", "--item", "2", "--enabled"],
        env={"HELP_TEST_COUNT": "3"},
    )
    assert success.exit_code == 0, success.output
    assert "sample.txt:3:fast:[1, 2]:True" in success.output
    for args in (
        [str(source), "--count", "4"],
        [str(source), "--mode", "other"],
        [str(tmp_path)],
        [str(tmp_path / "missing")],
    ):
        failure = runner.invoke(apps[1], args)
        assert failure.exit_code == 2, failure.output

    help_result = runner.invoke(apps[1], ["--help"])
    assert help_result.exit_code == 0
    assert "INTEGER" in help_result.output
    assert "1<=x<=3" in help_result.output
    assert "fast" in help_result.output and "slow" in help_result.output


@pytest.mark.parametrize("required", [False, True])
def test_repeated_explicit_metavar_is_not_duplicated(required):
    command = CliCommand(
        name="demo",
        params=[
            TyperArgument(
                param_decls=["files"], metavar="FILES...", nargs=-1, required=required
            )
        ],
    )
    usage = command.get_usage(Context(command, info_name="demo"))
    assert usage == f"Usage: demo [OPTIONS] {'FILES...' if required else '[FILES]...'}"


@pytest.mark.parametrize("command", ["run", "start", "serve"])
def test_explicit_metavars_keep_lowercase_runtime_flags(command, capsys):
    root = typer.main.get_command(app)
    assert isinstance(root, TyperGroup)
    lazy = root.commands[command]
    assert isinstance(lazy, LazyCommand)
    loaded = lazy.load()
    # Parse without invoking the runtime or opening a listener.
    with loaded.make_context(
        command, ["--host", "127.0.0.2", "--port", "7002"], resilient_parsing=True
    ) as ctx:
        assert ctx.params["host"] == "127.0.0.2"
        assert ctx.params["port"] == 7002
    options = {
        option for param in loaded.get_params(Context(loaded)) for option in param.opts
    }
    assert {"--host", "--port"} <= options
    assert not any(
        option.startswith("--") and option != option.lower() for option in options
    )
    if command in ("run", "start"):
        assert "--sandbox" in options
    loaded.get_help(Context(loaded, info_name=command))
    help_text = strip_ansi(capsys.readouterr().out)
    port_row = next(line for line in help_text.splitlines() if "--port" in line.split())
    assert "PORT" in port_row.split()

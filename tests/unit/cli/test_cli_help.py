"""Help output follows the same operand conventions across CLI entry points."""

from enum import Enum
from io import StringIO
from pathlib import Path
from typing import Annotated

import pytest
import typer
from rich.console import Console
from rich.text import Text
from typer._click import Context
from typer._click.utils import strip_ansi
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption
from typer.models import TyperPath
from typer.testing import CliRunner

from toolang.cli.caps.main import app as caps_app, main as caps_main
from toolang.cli.common.help import CliCommand, CliGroup
from toolang.cli.common.lazy import LazyCommand, lazy_typer_command
from toolang.cli.common.parameters import PathType
from toolang.cli.toolang.main import app, main as too_main
from toolang.common.typer.ui import PLAIN, UV, run


@pytest.mark.parametrize("theme", [PLAIN, UV])
@pytest.mark.parametrize(
    ("arguments", "status"),
    [
        (["--help"], 0),
        (["prompt", "--help"], 0),
        (["prompt", "new", "--help"], 0),
        (["prompt", "new", "--template", "default"], 0),
        (["prompt", "new", "--unknown"], 2),
    ],
)
def test_lazy_commands_inherit_theme_and_output(
    theme, arguments, status, tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    stdout, stderr = StringIO(), StringIO()
    console = Console(file=stdout, force_terminal=True, color_system="standard")
    error_console = Console(file=stderr, force_terminal=True, color_system="standard")
    assert (
        run(
            app,
            args=["--root", str(tmp_path), *arguments],
            prog_name="too",
            theme=theme,
            console=console,
            error_console=error_console,
        )
        == status
    )
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    output = Text.from_ansi(stderr.getvalue() if status else stdout.getvalue())
    assert "Usage: too" in output.plain
    style = output.get_style_at_offset(console, output.plain.index("Usage:"))
    assert style.bold
    assert (style.color is not None) is (theme is UV)


def test_lazy_command_exposes_short_help_without_loading():
    command = lazy_typer_command(
        "future",
        "not_imported:future",
        help="Detailed documentation.\n\nMore information.",
        short_help="Explicit summary.",
    )
    group = CliGroup(name="demo", commands={"future": command})
    output = group.get_help(group.context_class(group, info_name="demo"))
    assert "Explicit summary." in output
    assert "Detailed documentation." not in output


@pytest.mark.parametrize("application", [app, caps_app])
def test_declared_help_omits_final_period(application):
    pending = [(typer.main.get_command(application), None)]
    while pending:
        command, parent = pending.pop()
        if isinstance(command, LazyCommand):
            command = command.load()
        ctx = command.context_class(command, parent=parent, info_name=command.name)
        summary = command.short_help or (command.help or "").split("\n\n", 1)[0]
        assert not summary.endswith("."), (ctx.command_path, summary)
        native_help = command.get_help_option(ctx)
        for param in command.get_params(ctx):
            if param is native_help:
                continue
            assert isinstance(param, (TyperArgument, TyperOption))
            assert not (param.help or "").endswith("."), (
                ctx.command_path,
                param.name,
                param.help,
            )
        if isinstance(command, TyperGroup):
            pending.extend((child, ctx) for child in command.commands.values())


@pytest.mark.parametrize(
    ("arguments", "command", "message"),
    [
        (["help"], "too", "No such command 'help'."),
        (["prompt", "missing"], "too prompt", "No such command 'missing'."),
        (
            ["a", "workspace", "missing"],
            "too workspace",
            "No such command 'missing'.",
        ),
        (["stat"], "too", "Did you mean 'start'?"),
    ],
)
def test_unknown_command_points_to_group_help(
    arguments, command, message, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    assert too_main(["--root", str(tmp_path), *arguments]) == 2
    captured = capsys.readouterr()
    output = strip_ansi(captured.err)
    assert not captured.out
    assert output.startswith("Error: No such command")
    assert message in output
    assert f"\n\nTry '{command} --help' for help.\n" in output
    assert "Usage:" not in output


def test_unknown_option_during_command_resolution_keeps_usage(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["too"])
    assert too_main(["--", "--missing"]) == 2
    output = strip_ansi(capsys.readouterr().err)
    assert output.startswith("Error: No such option: --missing")
    assert "\n\nUsage: too [OPTIONS] COMMAND [ARGS]\n" in output
    assert "Try '" not in output


@pytest.mark.parametrize(
    ("main", "arguments"),
    [
        (too_main, []),
        (too_main, ["prompt"]),
        (too_main, ["a", "prompt", "new"]),
        (too_main, ["start"]),
        (caps_main, []),
        (caps_main, ["a", "prompt", "new"]),
    ],
)
def test_short_help_alias_matches_long_help(main, arguments, tmp_path, capsys):
    outputs = []
    for flag in ("--help", "-h"):
        assert main(["--root", str(tmp_path), *arguments, flag]) == 0
        captured = capsys.readouterr()
        assert not captured.err
        outputs.append(strip_ansi(captured.out))
    assert outputs[0] == outputs[1]
    assert "-h, --help" in outputs[0]


@pytest.mark.parametrize("main", [too_main, caps_main])
def test_short_version_alias_matches_long_version(main, capsys):
    outputs = []
    for flag in ("--version", "-V"):
        assert main([flag]) == 0
        captured = capsys.readouterr()
        assert not captured.err
        outputs.append(captured.out)
    assert outputs[0] == outputs[1]
    assert main(["--help"]) == 0
    assert "-V, --version" in strip_ansi(capsys.readouterr().out)


def test_hidden_commands_keep_theme_and_root_invocation_hint(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["too"])
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert too_main(["hidden"]) == 0
    output = capsys.readouterr().out
    assert "\x1b[1;32m" in output
    plain = strip_ansi(output)
    assert "Hidden Commands:" in plain
    assert "Usage: too hidden [OPTIONS]" in plain.splitlines()
    assert "Run 'too COMMAND --help' for details." in plain
    assert "QUERY = MATCH" not in plain
    assert "serve Run an agent server" in " ".join(plain.split())


@pytest.mark.parametrize(
    ("arguments", "usage"),
    [
        (["a", "chat"], "too AGENT chat [OPTIONS]"),
        (["a", "prompt", "new"], "too [AGENT] prompt new [OPTIONS] NAME"),
        (["a", "workspace"], "too AGENT workspace [OPTIONS] COMMAND [ARGS]"),
        (["run"], "too run [OPTIONS] AGENT"),
    ],
)
def test_virtual_agent_usage_keeps_position_and_normal_weight(
    arguments, usage, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.delenv("NO_COLOR", raising=False)
    for flag, status in (("--help", 0), ("--unknown", 2)):
        assert too_main(["--root", str(tmp_path), *arguments, flag]) == status
        captured = capsys.readouterr()
        rendered = captured.err if status else captured.out
        assert rendered.endswith("\n")
        output = Text.from_ansi(rendered)
        assert f"Usage: {usage}" in output.plain.splitlines()
        start = output.plain.index("Usage:")
        usage_line = output.plain[start:].splitlines()[0]
        assert usage_line.count("AGENT") == 1
        for placeholder in ("AGENT", "[OPTIONS]"):
            offset = output.plain.index(placeholder, start)
            assert not output.get_style_at_offset(Console(), offset).bold
        assert output.get_style_at_offset(
            Console(), output.plain.index("too", start)
        ).bold


@pytest.mark.parametrize(
    ("command", "description", "argument_help"),
    [
        ("run", "Run an agent in the foreground", "Agent name, reference, or URL"),
        ("serve", "Run an agent server", "Agent name"),
    ],
)
@pytest.mark.parametrize("args", [[], ["--help"], ["-h"], ["--unknown"]])
def test_real_and_virtual_agent_arguments_share_usage(
    command, description, argument_help, args, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    status = 2 if "--unknown" in args else 0
    assert too_main(["--root", str(tmp_path), command, *args]) == status
    captured = capsys.readouterr()
    output = strip_ansi(captured.err if status else captured.out)
    assert f"Usage: too {command} [OPTIONS] AGENT" in output.splitlines()
    if status == 0:
        assert output.startswith(description + "\n")
        assert f"* AGENT TEXT {argument_help}" in [
            " ".join(line.split()) for line in output.splitlines()
        ]


@pytest.mark.parametrize("args", [["--help"], ["--thread", "--help"], ["-t", "--help"]])
def test_chat_help_uses_the_canonical_optional_thread_option(
    args, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    assert too_main(["--root", str(tmp_path), "a", "chat", *args]) == 0
    output = strip_ansi(capsys.readouterr().out)
    usage = output.partition("Usage:")[2].splitlines()[0].strip()
    assert usage == "too AGENT chat [OPTIONS]"
    row = next(line for line in output.splitlines() if "[THREAD]" in line)
    assert "--thread" in row and "-t" in row
    assert "-t, --thread [THREAD]" in row
    assert "most recently updated thread" in " ".join(output.split())


@pytest.mark.parametrize("main", [too_main, caps_main])
def test_prompt_help_uses_conventional_metavars(main, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["too"])
    assert main(["--root", str(tmp_path), "a", "prompt", "new", "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    assert "Usage: too [AGENT] prompt new [OPTIONS] NAME" in " ".join(output.split())
    assert "<str>" not in output
    for name in ("AGENT", "NAME"):
        row = next(
            line
            for line in output.splitlines()
            if line.startswith("  ") and name in line.split()
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
def test_error_usage_preserves_the_virtual_agent_position(
    main, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    assert main(["--root", str(tmp_path), "a", "prompt", "new", "--unknown"]) == 2
    output = strip_ansi(capsys.readouterr().err)
    assert output.startswith(
        "Error: No such option: --unknown\n\nUsage: too [AGENT] prompt new "
    )
    assert output.count("AGENT") == 1
    assert "TEXT" not in output


@pytest.mark.parametrize("group", ["workspace", "chore", "task"])
def test_required_group_agent_is_documented(group, tmp_path: Path, capsys):
    assert too_main(["--root", str(tmp_path), "a", group, "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    row = next(
        (
            line
            for line in output.splitlines()
            if line.startswith("  ") and "AGENT" in line.split()
        ),
        "",
    )
    assert "TEXT" in row and row.lstrip().startswith("* AGENT")
    assert "[required]" not in row


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
    assert usage == f"Usage: demo [OPTIONS] {'FILES...' if required else '[FILES...]'}"


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
    help_text = strip_ansi(loaded.get_help(Context(loaded, info_name=command)))
    assert capsys.readouterr().out == ""
    port_row = next(line for line in help_text.splitlines() if "--port" in line.split())
    assert "PORT" in port_row.split()

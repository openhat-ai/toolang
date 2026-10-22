"""Help output follows the same operand conventions across CLI entry points."""

from enum import Enum
from io import StringIO
from pathlib import Path
from typing import Annotated

import pytest
import typer
from rich.console import Console
from rich.cells import cell_len
from rich.text import Text
from typer._click import Context
from typer._click.utils import strip_ansi
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption
from typer.models import TyperPath
from typer.testing import CliRunner

from toolang.cli.common.help import CliCommand, CliGroup, HelpContext
from toolang.cli.common.lazy import LazyCommand, lazy_typer_command
from toolang.cli.common.parameters import PathType
from toolang.cli.toolang.main import app, main as too_main
from toolang.common.typer.ui import PLAIN, UV, _format_help, run


@pytest.mark.parametrize("theme", [PLAIN, UV])
@pytest.mark.parametrize("width", [44, 80, 120])
@pytest.mark.parametrize("color_system", [None, "standard"])
def test_script_root_and_runnable_share_field_sections(theme, width, color_system):
    from toolang.cli.toolang.commands.script import _program_command
    from toolang.lang.ast import Program

    class UnreadableStdin(StringIO):
        def read(self, *args, **kwargs):
            pytest.fail("help must not read stdin")

    program = Program.from_source("agic greet(name: Text, tone?: Text):\n  Greet.\n")
    root = _program_command(
        program,
        source_path=Path("demo.too"),
        source_label="demo.too",
        stdin=UnreadableStdin(),
    )
    parent = root.context_class(root, info_name="too demo.too", terminal_width=width)
    proxy = root.commands["greet"]
    assert isinstance(proxy, LazyCommand)
    command = proxy.load()
    child = command.context_class(command, parent=parent, info_name="greet")
    console = Console(width=width, force_terminal=True, color_system=color_system)
    for ctx in (parent, child):
        output = Text.from_ansi(_format_help(ctx, theme=theme, console=console))
        assert all(cell_len(line) <= width for line in output.plain.splitlines())
        for section in ("Allow fields:", "Limit fields:", "Model parameters:"):
            assert output.plain.count(section) == 1
            assert output.get_style_at_offset(console, output.plain.index(section)) == (
                output.get_style_at_offset(console, output.plain.index("Options:"))
            )
        assert "Default fields:" not in output.plain
        if ctx is child:
            assert "[ARGUMENTS]" in output.plain
            assert "* name=<NAME>" in output.plain
            assert "* tone=<TONE>" not in output.plain


@pytest.mark.parametrize("theme", [PLAIN, UV])
@pytest.mark.parametrize("width", [44, 80, 120])
@pytest.mark.parametrize("color_system", [None, "standard"])
def test_examples_share_section_styles_and_wrap_descriptions(
    theme, width, color_system, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("toolang.up.logging.configure_logging", lambda **kwargs: None)
    stdout = StringIO()
    console = Console(
        file=stdout, width=width, force_terminal=True, color_system=color_system
    )
    assert (
        run(
            app,
            args=["--root", str(tmp_path), "talk", "--help"],
            prog_name="too",
            theme=theme,
            console=console,
            terminal_width=width,
        )
        == 0
    )
    assert not capsys.readouterr().out
    output = Text.from_ansi(stdout.getvalue())
    assert all(cell_len(line) <= width for line in output.plain.splitlines())
    examples = output.plain.split("Examples:\n", 1)[1]
    assert "\n\n" not in examples.rstrip()
    for label in ("Arguments:", "Options:", "Examples:"):
        assert output.get_style_at_offset(console, output.plain.index(label)) == (
            output.get_style_at_offset(console, output.plain.index("Options:"))
        )
    first = next(line for line in examples.splitlines() if "List conversations" in line)
    description_column = first.index("List conversations")
    descriptions = " ".join(
        line[description_column:].strip() for line in examples.splitlines()
    )
    assert "Open a conversation with an agent" in descriptions


@pytest.mark.parametrize(
    ("command", "sections"),
    [
        (
            "serve",
            {"Allow fields", "Limit fields", "Default fields", "Model parameters"},
        ),
        (
            "chat",
            {"Allow fields", "Limit fields", "Default fields", "Model parameters"},
        ),
        ("rerun", {"Allow fields", "Limit fields", "Model parameters"}),
        ("caps", set()),
        ("models", set()),
    ],
)
def test_field_sections_match_supported_options(
    command, sections, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("toolang.up.logging.configure_logging", lambda **kwargs: None)
    assert too_main(["--root", str(tmp_path), command, "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    known = {"Allow fields", "Limit fields", "Default fields", "Model parameters"}
    assert {name for name in known if f"\n{name}:\n" in output} == sections
    for name in sections:
        assert output.count(f"\n{name}:\n") == 1
        assert output.index("Options:") < output.index(f"\n{name}:\n")


def test_field_tables_cover_parser_fields_and_repetition_rules(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("toolang.up.logging.configure_logging", lambda **kwargs: None)
    from toolang.cli.common.policy import _ALLOW_FIELDS, _DEFAULT_FIELDS, _LIMIT_FIELDS

    assert too_main(["--root", str(tmp_path), "serve", "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    for section, fields in (
        ("Allow fields", _ALLOW_FIELDS),
        ("Limit fields", _LIMIT_FIELDS),
        (
            "Default fields",
            tuple(name for name in _DEFAULT_FIELDS if name != "runnable"),
        ),
    ):
        rows = output.split(f"\n{section}:\n", 1)[1].split("\n\n", 1)[0]
        assert tuple(line.split()[0] for line in rows.splitlines()) == fields
    text = " ".join(output.split())
    assert "Repeat a resource to combine queries by union" in text
    assert "each field may appear once" in text
    assert "Maximum model calls per agic" in text
    assert "Total input and output tokens per run tree" in text
    assert "Total cost in USD per run tree" in text
    assert "Elapsed time in seconds per run tree" in text
    assert "--compact-model requires an exact model for a new runtime" in text


@pytest.mark.parametrize("custom_context", [False, True])
def test_callers_compose_sections_and_preserve_footer_and_hidden_options(
    custom_context,
):
    from toolang.common.typer.ui import HelpFormatter

    class AlternateFormatter(HelpFormatter):
        def write_epilog(self, ctx: Context) -> None:
            super().write_epilog(ctx)
            self.write_text("Custom footer.")

    class AlternateContext(HelpContext):
        formatter_class = AlternateFormatter

    class CustomCommand(CliCommand):
        context_class = AlternateContext if custom_context else HelpContext

        def write_help_sections(self, ctx: Context, formatter: HelpFormatter) -> None:
            super().write_help_sections(ctx, formatter)
            with formatter.section("Custom fields"):
                formatter.write_dl([("example", "A caller-owned description")])
                formatter.write_paragraph()
                formatter.write_text("A caller-owned paragraph.")
            with formatter.section("Examples"):
                formatter.write_dl([("demo", "Show an example")])

    demo = typer.Typer(add_completion=False)

    @demo.command(cls=CustomCommand, epilog="Ordinary footer.")
    def show(
        limit: Annotated[str | None, typer.Option("--limit")] = None,
        default: Annotated[str | None, typer.Option("--default", hidden=True)] = None,
    ) -> None:
        pytest.fail("help must not execute the command")

    stdout = StringIO()
    assert run(demo, args=["--help"], console=Console(file=stdout)) == 0
    output = stdout.getvalue()
    sections = [
        "Options:",
        "Limit fields:",
        "Custom fields:",
        "Examples:",
        "Ordinary footer.",
    ]
    positions = [output.index(section) for section in sections]
    assert positions == sorted(positions)
    assert "\n  A caller-owned paragraph.\n" in output
    assert "Default fields:" not in output and "--default" not in output
    if custom_context:
        assert output.rstrip().endswith("Custom footer.")


@pytest.mark.parametrize("theme", [PLAIN, UV])
@pytest.mark.parametrize("width", [44, 120])
def test_root_help_shows_the_source_version_in_dim_parentheses(
    theme, width, monkeypatch
):
    monkeypatch.setattr(
        "toolang.cli.toolang.main._version.toolang_version",
        lambda: "0.3.0-12-g12345678*",
    )
    stdout = StringIO()
    console = Console(
        file=stdout, force_terminal=True, color_system="standard", width=width
    )
    assert run(app, args=["--help"], prog_name="too", theme=theme, console=console) == 0
    output = Text.from_ansi(stdout.getvalue())
    description = "Toolang — a language and runtime for agents and humans."
    version = "(0.3.0-12-g12345678*)"
    assert " ".join(output.plain.split()).startswith(f"{description} {version}")
    start = output.plain.index(version)
    assert all(
        output.get_style_at_offset(console, index).dim
        for index in range(start, start + len(version))
    )
    assert not output.get_style_at_offset(console, 0).dim


@pytest.mark.parametrize("theme", [PLAIN, UV])
@pytest.mark.parametrize(
    ("arguments", "status"),
    [
        (["--help"], 0),
        (["prompt", "--help"], 0),
        (["prompt", "new", "--help"], 0),
        (["prompt", "new"], 0),
        (["serve", "--port", "1234"], 2),
        (["workspace", "list"], 2),
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


@pytest.mark.parametrize(
    ("command", "description"),
    [
        ("workspace", "Manage agent workspaces"),
        ("hub", "Manage the hub service"),
        ("talk", "Have conversations with agents"),
        ("chat", "Chat directly with an agent"),
        ("chore", "Manage one-off and scheduled tasks"),
        ("convo", "Manage conversations and rules"),
        ("start", "Start an agent in the background"),
        ("inspect", "Inspect agent runs"),
        ("top", "Monitor agent runs"),
    ],
)
def test_command_descriptions_match_root_and_command_help(
    command, description, tmp_path, capsys
):
    assert too_main(["--root", str(tmp_path), "--help"]) == 0
    rows = [
        " ".join(line.split())
        for line in strip_ansi(capsys.readouterr().out).splitlines()
    ]
    assert f"{command} {description}" in rows
    assert too_main(["--root", str(tmp_path), command, "--help"]) == 0
    assert strip_ansi(capsys.readouterr().out).startswith(f"{description}.\n")
    if command not in {"hub", "talk", "convo"}:
        assert too_main(["--root", str(tmp_path), "agent:sample", "--help"]) == 0
        rows = " ".join(strip_ansi(capsys.readouterr().out).split())
        assert f"{command} {description}" in rows


@pytest.mark.parametrize(
    ("group", "commands"),
    [
        ("chore", ("list", "new", "edit", "delete", "run", "cancel")),
        ("hub", ("start", "serve", "stop")),
    ],
)
def test_chore_and_hub_help_lists_supported_commands(group, commands, tmp_path, capsys):
    assert too_main(["--root", str(tmp_path), group, "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    section = output.split("Commands:\n", 1)[1].split("\n\n", 1)[0]
    assert (
        tuple(
            line.split()[0]
            for line in section.splitlines()
            if line.strip() and not line.startswith(" " * 10)
        )
        == commands
    )


@pytest.mark.parametrize("arguments", [["--help"], ["agent:sample", "--help"]])
def test_cd_summary_omits_root_fallback(arguments, tmp_path, capsys):
    assert too_main(["--root", str(tmp_path), *arguments]) == 0
    output = strip_ansi(capsys.readouterr().out)
    rows = [" ".join(line.split()) for line in output.splitlines()]

    assert "cd Enter an agent home" in rows
    assert "if no agent is specified" not in output


def test_declared_help_omits_final_period():
    pending = [(typer.main.get_command(app), None)]
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
    assert "\n\nUsage: too [OPTIONS] <COMMAND> [ARGUMENTS]\n" in output
    assert "Try '" not in output


@pytest.mark.parametrize("command", ["rerun", "inspect", "chat"])
def test_unknown_option_is_not_hidden_by_a_missing_agent(
    command, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    assert too_main(["--root", str(tmp_path), command, "--unknown"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert strip_ansi(output.err).startswith(
        f"Error: No such option: --unknown\n\nUsage: too <AGENT> {command} [OPTIONS]"
    )


@pytest.mark.parametrize(
    ("main", "arguments"),
    [
        (too_main, []),
        (too_main, ["prompt"]),
        (too_main, ["a", "prompt", "new"]),
        (too_main, ["start"]),
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


def test_short_version_alias_matches_long_version(capsys):
    outputs = []
    for flag in ("--version", "-V"):
        assert too_main([flag]) == 0
        captured = capsys.readouterr()
        assert not captured.err
        outputs.append(captured.out)
    assert outputs[0] == outputs[1]
    assert too_main(["--help"]) == 0
    assert "-V, --version" in strip_ansi(capsys.readouterr().out)


def test_additional_commands_keep_theme_and_root_invocation_hint(capsys, monkeypatch):
    root = typer.main.get_command(app)
    assert isinstance(root, TyperGroup)
    assert "more" in root.commands
    assert "hidden" not in root.commands
    assert root.commands["more"].hidden
    assert root.commands["_serve"].hidden
    more_panels = {
        "Run commands": {
            "steer": "Steer an active run",
            "cancel": "Cancel an active run",
            "rerun": "Rerun an earlier run as a new one",
        },
        "Thread commands": {
            "fork": "Fork a thread from an earlier run",
            "rewind": "Rewind a thread to an earlier run",
        },
        "Plugin commands": {
            "catalogs": "List installed model catalogs",
            "adapters": "List installed model adapters",
            "toolsets": "List installed toolsets",
            "sandboxes": "List installed sandboxes",
        },
        "Language commands": {
            "parse": "Parse .too source",
            "highlight": "Highlight .too source",
            "fmt": "Format .too source",
        },
    }
    for names in more_panels.values():
        for name, description in names.items():
            assert root.commands[name].hidden
            assert (root.commands[name].help or "").startswith(description)
    assert not root.commands["serve"].hidden
    assert not root.commands["tools"].hidden
    monkeypatch.setattr("sys.argv", ["too"])
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert too_main(["more"]) == 0
    output = capsys.readouterr().out
    assert "\x1b[1;32m" in output
    plain = strip_ansi(output)
    assert plain.splitlines()[0] == "Run commands:"
    assert "Usage:" not in plain
    assert "Run 'too COMMAND --help' for details." in plain
    assert "QUERY = MATCH" not in plain
    assert "_serve" not in plain
    assert "channel" not in plain
    assert "List available tools" not in plain
    rows = " ".join(plain.split())
    positions = []
    for panel, names in more_panels.items():
        assert f"{panel}:" in plain
        for name, description in names.items():
            row = f"{name} {description}"
            assert row in rows
            positions.append(rows.index(row))
    assert positions == sorted(positions)
    assert too_main(["--help"]) == 0
    main_help = strip_ansi(capsys.readouterr().out)
    assert main_help.rstrip().endswith("Run 'too more' to see additional commands.")
    assert "Run commands:" not in main_help
    for names in more_panels.values():
        for description in names.values():
            assert description not in main_help


@pytest.mark.parametrize("theme", [PLAIN, UV])
@pytest.mark.parametrize("args", [["more"], ["more", "-h"], ["more", "--help"]])
def test_additional_directory_uses_selected_help_output(theme, args, capsys):
    stdout = StringIO()
    console = Console(
        file=stdout,
        width=44,
        force_terminal=True,
        color_system="standard",
        _environ={},
    )
    assert run(app, args=args, prog_name="too", theme=theme, console=console) == 0
    captured = capsys.readouterr()
    assert not captured.out and not captured.err
    output = Text.from_ansi(stdout.getvalue())
    assert output.plain.splitlines()[0] == "Run commands:"
    assert "Usage:" not in output.plain
    assert all(len(line) <= 44 for line in output.plain.splitlines())
    style = output.get_style_at_offset(console, output.plain.index("Run commands:"))
    assert style.bold
    assert (style.color is not None) is (theme is UV)


def test_additional_directory_help_forms_are_identical():
    outputs = []
    for args in (["more"], ["more", "-h"], ["more", "--help"]):
        stdout = StringIO()
        assert (
            run(
                app,
                args=args,
                prog_name="too",
                theme=PLAIN,
                console=Console(file=stdout),
            )
            == 0
        )
        outputs.append(stdout.getvalue())
    assert outputs[0] == outputs[1] == outputs[2]


@pytest.mark.parametrize("tail", [["extra"], ["--unknown"]])
def test_additional_directory_rejects_unexpected_input(tail):
    stderr = StringIO()
    assert (
        run(
            app,
            args=["more", *tail],
            prog_name="too",
            theme=PLAIN,
            console=Console(file=StringIO()),
            error_console=Console(file=stderr),
        )
        == 2
    )
    assert stderr.getvalue().startswith("Error: ")


def test_root_help_hint_uses_the_invoked_executable():
    for prog_name in ("too", "toolang"):
        stdout = StringIO()
        console = Console(file=stdout, width=120)
        assert (
            run(app, args=["--help"], prog_name=prog_name, theme=PLAIN, console=console)
            == 0
        )
        assert (
            stdout.getvalue()
            .rstrip()
            .endswith(f"Run '{prog_name} more' to see additional commands.")
        )


def test_hidden_is_no_longer_a_command():
    stderr = StringIO()
    assert (
        run(
            app,
            args=["hidden"],
            prog_name="too",
            theme=PLAIN,
            console=Console(file=StringIO()),
            error_console=Console(file=stderr),
        )
        == 2
    )
    assert "No such command 'hidden'." in stderr.getvalue()


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ([], "No such command 'channel'."),
        (["list"], "Usage:"),
        (["--help"], "No such command 'channel'."),
    ],
)
def test_channel_command_is_removed(args, error, tmp_path, capsys):
    assert too_main(["--root", str(tmp_path), "channel", *args]) == 2
    assert error in strip_ansi(capsys.readouterr().err)


@pytest.mark.parametrize("target", ["alice", "agent:alice"])
def test_chore_clone_command_is_removed(target, tmp_path, capsys):
    assert (
        too_main(["--root", str(tmp_path), target, "chore", "clone", "cleanup", "copy"])
        == 2
    )
    assert "No such command 'clone'." in strip_ansi(capsys.readouterr().err)
    assert not list(tmp_path.rglob("*.md"))


@pytest.mark.parametrize(
    ("arguments", "usage"),
    [
        (["a", "chat"], "too <AGENT> chat [OPTIONS]"),
        (["a", "prompt", "new"], "too [AGENT] prompt new [OPTIONS] <NAME>"),
        (["a", "workspace"], "too <AGENT> workspace [OPTIONS] <COMMAND> [ARGUMENTS]"),
        (["serve"], "too serve [OPTIONS] <AGENT>"),
    ],
)
def test_agent_usage_keeps_recommended_position_and_normal_weight(
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
        (
            "serve",
            "Run an agent in the foreground",
            "Agent name, ref, URL",
        ),
        ("_serve", "Run an agent server", "Agent name"),
    ],
)
@pytest.mark.parametrize("args", [[], ["--help"], ["-h"], ["--unknown"]])
def test_after_agent_arguments_share_usage(
    command, description, argument_help, args, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    status = 2 if "--unknown" in args else 0
    assert too_main(["--root", str(tmp_path), command, *args]) == status
    captured = capsys.readouterr()
    output = strip_ansi(captured.err if status else captured.out)
    assert f"Usage: too {command} [OPTIONS] <AGENT>" in output.splitlines()
    if status == 0:
        assert output.startswith(description + ".\n")
        assert any(
            line.startswith(f"* AGENT {argument_help}")
            for line in [" ".join(line.split()) for line in output.splitlines()]
        )
        assert "Show this message and exit\n" in output
        assert output.index("Options:") < output.index("Allow fields:")


@pytest.mark.parametrize(
    "command",
    [
        "new",
        "remove",
        "start",
        "stop",
        "_serve",
        "serve",
        "info",
        "chat",
        "inspect",
        "steer",
        "cancel",
        "rerun",
        "rerun",
        "fork",
        "rewind",
        "workspace",
        "workspace list",
        "chore",
        "chore list",
    ],
)
def test_agent_help_matches_target_scope(command, tmp_path, capsys):
    words = command.split()
    assert (
        too_main(
            [
                "--root",
                str(tmp_path),
                *(["a"] if len(words) > 1 else []),
                *words,
                "--help",
            ]
        )
        == 0
    )
    output = " ".join(strip_ansi(capsys.readouterr().out).split())
    if len(words) > 1:
        assert "<AGENT>" in output
        assert "* AGENT" not in output
        return
    if command == "new":
        assert "new [OPTIONS] <NAME>" in output
        assert "* NAME New agent name" in output
        assert "AGENT" not in output
        return
    elif command in {"start", "stop", "serve"}:
        expected = "Agent name, ref, URL"
    elif command in {"remove", "_serve", "chore"}:
        expected = "Agent name"
    else:
        expected = "Agent name, path, ref, URL"
    assert f"* AGENT {expected}" in output
    if command != "new":
        assert "placements:" not in output


@pytest.mark.parametrize("kind", ["psyche", "skill", "service", "prompt"])
@pytest.mark.parametrize("command", ["new", "list"])
def test_cap_children_inherit_agent_usage_from_group(kind, command, tmp_path, capsys):
    assert too_main(["--root", str(tmp_path), kind, command, "--help"]) == 0
    output = " ".join(strip_ansi(capsys.readouterr().out).split())
    assert f"[AGENT] {kind} {command}" in output
    assert "AGENT Agent name" not in output
    assert too_main(["--root", str(tmp_path), kind, "--help"]) == 0
    group_help = " ".join(strip_ansi(capsys.readouterr().out).split())
    assert "AGENT Agent name, path, ref, URL" in group_help
    assert "placements:" not in group_help


@pytest.mark.parametrize("command", ["caps", "models", "providers", "tools"])
def test_inspection_help_describes_all_agent_targets(command, tmp_path, capsys):
    assert too_main(["--root", str(tmp_path), command, "--help"]) == 0
    output = " ".join(strip_ansi(capsys.readouterr().out).split())
    assert "AGENT Agent name, path, ref, URL" in output


@pytest.mark.parametrize("kind", ["psyche", "skill", "service", "prompt"])
def test_cap_commands_omit_and_reject_template(kind, tmp_path, capsys):
    for prefix in ([], ["a"]):
        assert too_main(["--root", str(tmp_path), *prefix, kind, "--help"]) == 0
        assert "template" not in capsys.readouterr().out
        assert too_main(["--root", str(tmp_path), *prefix, kind, "template"]) == 2
        assert "No such command" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--help"], ["--thread", "--help"], ["-t", "--help"]])
def test_chat_help_uses_the_canonical_optional_thread_option(
    args, tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    assert too_main(["--root", str(tmp_path), "a", "chat", *args]) == 0
    output = strip_ansi(capsys.readouterr().out)
    usage = output.partition("Usage:")[2].splitlines()[0].strip()
    assert usage == "too <AGENT> chat [OPTIONS]"
    row = next(line for line in output.splitlines() if "[THREAD]" in line)
    assert "--thread" in row and "-t" in row
    assert "-t, --thread [THREAD]" in row
    assert "Continue a thread instead of a new one" in " ".join(output.split())
    assert "[bare: latest thread]" in " ".join(output.split())
    assert "[bare: .]" in " ".join(output.split())
    assert "\0" not in output


def test_prompt_help_uses_conventional_metavars(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["too"])
    assert too_main(["--root", str(tmp_path), "a", "prompt", "new", "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    assert "Usage: too [AGENT] prompt new [OPTIONS] <NAME>" in " ".join(output.split())
    assert "<str>" not in output
    for name in ("NAME",):
        row = next(
            line
            for line in output.splitlines()
            if line.startswith("  ") and name in line.split()
        )
        assert "TEXT" not in row
    assert "--template" not in output


@pytest.mark.parametrize(
    ("arguments", "options"),
    [
        (
            ["serve", "a"],
            ("--sandbox", "--allow", "--limit", "--default", "--compact-model"),
        ),
        (
            ["start", "a"],
            ("--sandbox", "--allow", "--limit", "--default", "--compact-model"),
        ),
        (
            ["a", "chat"],
            ("--sandbox", "--allow", "--limit", "--default", "--compact-model"),
        ),
        (["a", "rerun"], ("--sandbox", "--allow", "--limit", "--model")),
        (
            ["_serve", "a"],
            ("--allow", "--limit", "--default", "--compact-model", "--log"),
        ),
    ],
)
def test_help_uses_semantic_configuration_metavars(
    arguments, options, tmp_path, capsys
):
    metavars = {
        "--sandbox": "<SANDBOX>",
        "--allow": "<RESOURCE>=<QUERY>",
        "--limit": "<FIELD>=<VALUE>",
        "--default": "<FIELD>=<VALUE>",
        "--compact-model": "<MODEL>",
        "--model": "<MODEL>",
        "--log": "<LOG_SPEC>",
    }
    assert too_main(["--root", str(tmp_path), *arguments, "--help"]) == 0
    output = strip_ansi(capsys.readouterr().out)
    for option in options:
        row = next(line for line in output.splitlines() if option in line.split())
        assert metavars[option] in row.split()
    if "--dev" in output:
        option_rows = [
            line
            for line in output.split("Options:\n", 1)[1]
            .split("\n\n", 1)[0]
            .splitlines()
            if line.lstrip().startswith("-")
        ]
        assert "--dev" in option_rows[-2].split()
        assert "--help" in option_rows[-1].split()


def test_error_usage_preserves_the_recommended_agent_position(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["too"])
    assert too_main(["--root", str(tmp_path), "a", "prompt", "new", "--unknown"]) == 2
    output = strip_ansi(capsys.readouterr().err)
    assert output.startswith(
        "Error: No such option: --unknown\n\nUsage: too [AGENT] prompt new "
    )
    assert output.count("AGENT") == 1
    assert "TEXT" not in output


@pytest.mark.parametrize("group", ["workspace", "chore"])
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
    assert "TEXT" not in row and row.lstrip().startswith("* AGENT")
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
    assert (
        usage == f"Usage: demo [OPTIONS] {'<FILES>...' if required else '[FILES...]'}"
    )


@pytest.mark.parametrize("command", ["serve", "start", "_serve"])
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
    if command in ("serve", "start"):
        assert "--sandbox" in options
    help_text = strip_ansi(loaded.get_help(Context(loaded, info_name=command)))
    assert capsys.readouterr().out == ""
    port_row = next(line for line in help_text.splitlines() if "--port" in line.split())
    assert "<PORT>" in port_row.split()


@pytest.mark.parametrize("command", ["chat", "serve"])
def test_non_script_execution_exposes_only_effective_workspace_options(command):
    root = typer.main.get_command(app)
    assert isinstance(root, TyperGroup)
    lazy = root.commands[command]
    assert isinstance(lazy, LazyCommand)
    loaded = lazy.load()
    options = {
        option for param in loaded.get_params(Context(loaded)) for option in param.opts
    }
    assert {"-w", "--workspace", "-d", "--workdir"} <= options
    assert "--no-auto-workspace" not in options


@pytest.mark.parametrize("command", ["tools", "models", "providers", "start"])
def test_commands_without_workspace_selection_hide_workspace_options(command):
    root = typer.main.get_command(app)
    assert isinstance(root, TyperGroup)
    lazy = root.commands[command]
    assert isinstance(lazy, LazyCommand)
    loaded = lazy.load()
    options = {
        option for param in loaded.get_params(Context(loaded)) for option in param.opts
    }
    assert not {"-w", "--workspace", "-d", "--workdir", "--no-auto-workspace"} & options


@pytest.mark.parametrize(
    "command",
    [["new"], *[[kind, "new"] for kind in ("psyche", "skill", "service", "prompt")]],
)
def test_creation_commands_omit_and_reject_template_options(command, tmp_path, capsys):
    assert too_main(["--root", str(tmp_path), *command, "--help"]) == 0
    assert "--template" not in capsys.readouterr().out
    for option in ("--template", "-t"):
        assert (
            too_main(["--root", str(tmp_path), *command, "demo", option, "default"])
            == 2
        )
        assert f"No such option: {option}" in capsys.readouterr().err
    assert not list(tmp_path.rglob("*.md"))
    assert not list(tmp_path.rglob("*.too"))

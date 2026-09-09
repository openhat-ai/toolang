"""Default width limits and complete, left-aligned description wrapping."""

import io
import os
import unittest
from unittest.mock import patch
from typing import Any, cast

import typer
from rich.cells import cell_len
from rich.console import Console
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption
from typer.main import get_command

from tests.support.typer_ui import invoke
from toolang.common.typer.ui import (
    PLAIN,
    HelpFormatter,
    _command_description,
    _format_help,
)

DESCRIPTION = (
    "BEGIN " + "one two three four five six seven eight nine ten " * 4 + "TAIL."
)


class OutputWidthTest(unittest.TestCase):
    def test_formatter_preserves_empty_usage_prefix_and_native_indentation(self):
        formatter = HelpFormatter(
            console=Console(width=80, theme=PLAIN, color_system=None)
        )
        formatter.write_usage("demo", "[OPTIONS]", prefix="")
        with formatter.section("Commands"):
            formatter.write_dl([("run", "Run a task."), ("inspect", "Inspect a task.")])
        self.assertEqual(
            formatter.getvalue(),
            "demo [OPTIONS]\n\nCommands:\n  run      Run a task.\n  inspect  Inspect a task.\n",
        )

    def test_default_width_caps_help_and_errors(self):
        app = typer.Typer(add_completion=False, help=DESCRIPTION, epilog=DESCRIPTION)

        @app.command(help=DESCRIPTION)
        def fail():
            raise RuntimeError(DESCRIPTION)

        @app.command(help=DESCRIPTION)
        def show():
            pass

        for terminal_width in (40, 80, 120, 200):
            with (
                self.subTest(width=terminal_width),
                patch.dict(
                    os.environ, {"COLUMNS": str(terminal_width), "NO_COLOR": "1"}
                ),
            ):
                ui = dict(theme=PLAIN)
                width = min(terminal_width, 120)
                help_result = invoke(app, ui=ui, args=["--help"])
                error_result = invoke(app, ui=ui, args=["fail"])
                self.assertEqual(help_result.exit_code, 0, help_result.exception)
                self.assertEqual(error_result.exit_code, 1, error_result.exception)
                for output in (
                    help_result.stdout,
                    error_result.stderr,
                ):
                    self.assertTrue(
                        all(cell_len(line) <= width for line in output.splitlines())
                    )
                    self.assertIn("TAIL.", output)
                    self.assertNotIn("…", output)
                self.assertIn(DESCRIPTION, " ".join(error_result.stderr.split()))

    def test_explicit_consoles_keep_their_widths(self):
        console = Console(width=180)
        error_console = Console(width=160, stderr=True)
        with patch.dict(os.environ, {"COLUMNS": "40"}):
            ui = dict(theme=PLAIN, console=console, error_console=error_console)
            app = typer.Typer(add_completion=False)

            @app.command()
            def fail():
                raise RuntimeError("Failed.")

            self.assertEqual(invoke(app, ["--help"], ui=ui).exit_code, 0)
            self.assertEqual(invoke(app, [], ui=ui).exit_code, 1)
        self.assertEqual(console.width, 180)
        self.assertEqual(error_console.width, 160)

    def test_command_descriptions_keep_sentences_paragraphs_and_explicit_summaries(
        self,
    ):
        full = DESCRIPTION + " Second sentence.\n\nAnother paragraph."
        summary = "An explicit summary that exceeds the old forty-five-character limit."
        app = typer.Typer(add_completion=False)

        @app.command(help=full + "\fInternal documentation.")
        def full_help():
            pass

        @app.command(
            help="Detailed child help.",
            short_help=summary,
            deprecated=cast(Any, "Use full-help."),
        )
        def explicit():
            pass

        @app.command(deprecated=True)
        def empty():
            pass

        command = get_command(app)
        assert isinstance(command, TyperGroup)
        with patch.object(
            TyperCommand,
            "get_short_help_str",
            side_effect=AssertionError("truncated summary"),
        ):
            descriptions = {
                name: _command_description(item)
                for name, item in command.commands.items()
            }
        self.assertEqual(descriptions["full-help"], full)
        self.assertEqual(
            descriptions["explicit"], summary + " (DEPRECATED: Use full-help.)"
        )
        self.assertEqual(descriptions["empty"], "(DEPRECATED)")

    def test_description_columns_wrap_completely_at_the_same_left_edge(self):
        command = TyperGroup(
            name="demo",
            help="ROOT " + DESCRIPTION,
            epilog="EPILOG " + DESCRIPTION,
            params=[
                TyperArgument(param_decls=["name"], required=True, help=DESCRIPTION),
                TyperOption(param_decls=["--name"], metavar="TEXT", help=DESCRIPTION),
            ],
            commands={"show": TyperCommand("show", help=DESCRIPTION)},
            add_help_option=False,
        )
        ctx = typer.Context(command, info_name="demo")
        for width in (40, 80, 120):
            with self.subTest(width=width):
                output = io.StringIO()
                rendered = _format_help(
                    ctx,
                    theme=PLAIN,
                    console=Console(file=output, width=width, color_system=None),
                )
                self.assertTrue(
                    all(cell_len(line) <= width for line in rendered.splitlines())
                )
                self.assertIn("ROOT " + DESCRIPTION, " ".join(rendered.split()))
                self.assertIn("EPILOG " + DESCRIPTION, " ".join(rendered.split()))
                for title in ("Arguments", "Commands", "Options"):
                    section = rendered.split(f"{title}:\n", 1)[1].split("\n\n", 1)[0]
                    lines = section.splitlines()
                    start = lines[0].index("BEGIN")
                    fragments = []
                    for line in lines:
                        fragment = line[start:].rstrip()
                        self.assertEqual(fragment, fragment.lstrip())
                        fragments.append(fragment)
                    self.assertEqual(" ".join(fragments), DESCRIPTION)

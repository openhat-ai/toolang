"""Integration contracts for attaching the standalone UI to ordinary Typer apps."""

import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Annotated, Any, cast

import typer
from rich.console import Console
from rich.text import Text
from rich.theme import Theme
from typer._click.exceptions import ClickException, MissingParameter
from typer.core import TyperCommand, TyperGroup
from typer.main import get_command
from typer.testing import CliRunner

from tests.support.typer_ui import THEMES
from tests.support.typer_ui import invoke
from toolang.common.typer.ui import PLAIN, UV, HelpFormatter, run


def sample_app():
    app = typer.Typer(add_completion=False, help="Process named jobs.")

    @app.command()
    def process(
        name: Annotated[str, typer.Argument(help="Job name.")],
        count: Annotated[int, typer.Option("--count", "-n")] = 1,
    ):
        """Process one named job."""
        typer.echo(f"{name}: {count}")

    @app.command()
    def runtime():
        """Demonstrate a runtime failure."""
        raise RuntimeError("Request timed out.")

    return app


class TyperUITest(unittest.TestCase):
    ui: dict[str, Any]

    def setUp(self):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.ui = dict(
            theme=PLAIN,
            console=Console(file=self.stdout, width=100, color_system=None),
            error_console=Console(file=self.stderr, width=100, color_system=None),
        )

    def run_app(self, app, args):
        with redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            return run(app, args=args, prog_name="demo", **self.ui)

    def reset_output(self):
        for output in (self.stdout, self.stderr):
            output.seek(0)
            output.truncate(0)

    def test_omitted_theme_matches_explicit_uv(self):
        self.ui["console"] = Console(
            file=self.stdout, force_terminal=True, color_system="standard", _environ={}
        )
        self.ui.pop("theme")
        self.assertEqual(self.run_app(sample_app(), ["--help"]), 0)
        default = self.stdout.getvalue()
        self.reset_output()
        self.ui["theme"] = UV
        self.assertEqual(self.run_app(sample_app(), ["--help"]), 0)
        self.assertEqual(self.stdout.getvalue(), default)
        self.assertIn("\x1b[1;32m", default)

    def test_native_usage_override_is_used_for_help_and_errors(self):
        class ScopedCommand(TyperCommand):
            def format_usage(self, ctx, formatter):
                formatter.write_usage("demo [SCOPE] show", "[OPTIONS]")

        app = typer.Typer(add_completion=False)

        @app.command(cls=ScopedCommand)
        def show():
            """Show a scoped resource."""

        for args, status in ((["--help"], 0), (["--unknown"], 2)):
            self.reset_output()
            self.assertEqual(self.run_app(app, args), status)
            output = self.stderr if status else self.stdout
            self.assertIn("Usage: demo [SCOPE] show [OPTIONS]", output.getvalue())

    def test_native_help_hook_and_formatter_subclass_are_preserved(self):
        class CustomFormatter(HelpFormatter):
            def write_description(self, ctx):
                self.write_text("Custom description.")
                self.write_paragraph()

        class CustomContext(typer.Context):
            formatter_class = CustomFormatter

        class CustomCommand(TyperCommand):
            context_class = CustomContext

            def format_help(self, ctx, formatter):
                formatter.write_help(ctx)
                formatter.write_paragraph()
                formatter.write_text("Custom footer.")

        app = typer.Typer(add_completion=False)
        returned = []

        @app.command(cls=CustomCommand)
        def show(ctx: typer.Context, name: str):
            returned.append(ctx.get_help())

        for args, status in ((["--help"], 0), ([], 2)):
            self.reset_output()
            self.assertEqual(self.run_app(app, args), status)
            output = (self.stderr if status else self.stdout).getvalue()
            self.assertTrue(output.startswith("Custom description.\n\nUsage:"))
            self.assertTrue(output.endswith("Custom footer.\n"))
        self.reset_output()
        self.assertEqual(self.run_app(app, ["example"]), 0)
        self.assertTrue(returned[0].startswith("Custom description."))
        self.assertTrue(returned[0].endswith("Custom footer."))
        self.assertEqual(self.stdout.getvalue(), "")

    def test_empty_group_and_explicit_help_are_identical(self):
        app = sample_app()
        self.assertEqual(self.run_app(app, []), 0)
        empty_help = self.stdout.getvalue()
        self.reset_output()
        self.assertEqual(self.run_app(app, ["--help"]), 0)
        self.assertEqual(self.stdout.getvalue(), empty_help)
        self.assertTrue(empty_help.startswith("Process named jobs.\n\nUsage:"))
        self.assertLess(empty_help.index("Commands:"), empty_help.index("Options:"))
        self.assertEqual(self.stderr.getvalue(), "")

    def test_help_ends_with_one_newline_without_output_outside_its_console(self):
        for theme in THEMES.values():
            for epilog in (
                None,
                " \n\n",
                "Examples:\n  demo\n\n",
                "See the examples for more details.",
            ):
                app = sample_app()
                app.info.epilog = epilog
                app.registered_commands[0].epilog = epilog
                ui = dict(
                    theme=theme,
                    console=Console(file=self.stdout, width=100, color_system=None),
                    error_console=Console(
                        file=self.stderr, width=100, color_system=None
                    ),
                )
                self.ui = ui
                for args in ([], ["--help"], ["process", "--help"], ["process"]):
                    with self.subTest(theme=theme, epilog=epilog, args=args):
                        self.reset_output()
                        result = invoke(app, args, ui=self.ui, prog_name="demo")
                        missing = args == ["process"]
                        self.assertEqual(result.exit_code, 2 if missing else 0)
                        output = (self.stderr if missing else self.stdout).getvalue()
                        other = (self.stdout if missing else self.stderr).getvalue()
                        self.assertIn("Usage:", output)
                        self.assertTrue(output.endswith("\n"))
                        self.assertTrue(output.splitlines()[-1].strip())
                        self.assertEqual(other, "")
                        self.assertEqual(result.output, "")

    def test_missing_required_argument_displays_help_on_stderr(self):
        self.assertEqual(self.run_app(sample_app(), ["process"]), 2)
        output = self.stderr.getvalue()
        self.assertTrue(output.startswith("Process one named job.\n\nUsage:"))
        self.assertIn("  * name <STR>", output)
        self.assertNotIn("[required]", output)
        self.assertNotIn("Error:", output)
        self.assertEqual(self.stdout.getvalue(), "")

    def test_get_help_returns_text_without_printing_or_changing_output_width(self):
        app = typer.Typer(add_completion=False)
        returned = []

        @app.command()
        def show(ctx: typer.Context):
            """Return help for embedding in another interface."""
            returned.append(ctx.get_help())

        for theme in THEMES.values():
            for color in (None, "standard", "256", "truecolor"):
                with self.subTest(theme=theme, color=color):
                    self.reset_output()
                    returned.clear()
                    console = Console(
                        file=self.stdout,
                        width=44,
                        force_terminal=bool(color),
                        color_system=color,
                        _environ={},
                    )
                    self.ui = dict(theme=theme, console=console)
                    self.assertEqual(self.run_app(app, []), 0)
                    self.assertEqual(self.stdout.getvalue(), "")
                    self.assertEqual(self.stderr.getvalue(), "")
                    decoded = Text.from_ansi(returned[0])
                    self.assertIn("Usage: demo", decoded.plain)
                    self.assertEqual("\x1b[" in returned[0], bool(color))
                    if color:
                        self.assertTrue(
                            decoded.get_style_at_offset(
                                console, decoded.plain.index("Options:")
                            ).bold
                        )
                    self.assertEqual(self.run_app(app, ["--help"]), 0)
                    self.assertEqual(
                        Text.from_ansi(returned[0]).plain,
                        Text.from_ansi(self.stdout.getvalue()).plain,
                    )

    def test_no_color_preserves_emphasis_without_coloring_descriptions(self):
        console = Console(
            file=self.stdout,
            width=100,
            force_terminal=True,
            color_system="standard",
            no_color=True,
            _environ={},
        )
        self.ui = dict(theme=THEMES["uv"], console=console)
        self.assertEqual(self.run_app(sample_app(), ["process", "--help"]), 0)
        text = Text.from_ansi(self.stdout.getvalue())
        for offset in range(len(text)):
            self.assertIsNone(text.get_style_at_offset(console, offset).color)
        self.assertTrue(
            text.get_style_at_offset(console, text.plain.index("Usage:")).bold
        )
        for word in ("Job name.", "[default:", "Show this message"):
            self.assertFalse(
                text.get_style_at_offset(console, text.plain.index(word)).bold
            )

    def test_help_option_keeps_native_aliases_disabling_and_resilient_parsing(self):
        app = sample_app()
        app.info.context_settings = {"help_option_names": ["-h", "--help"]}
        for flag in ("-h", "--help"):
            self.reset_output()
            result = invoke(app, [flag], ui=self.ui)
            self.assertEqual(result.exit_code, 0, result.exception)
            self.assertIn("-h, --help", self.stdout.getvalue())
            self.assertEqual(result.output, "")
        self.reset_output()
        probe = typer.Typer(add_completion=False)

        @probe.command()
        def noop():
            pass

        result = invoke(probe, ["--help"], ui=self.ui, resilient_parsing=True)
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertEqual(self.stdout.getvalue(), "")
        app.info.add_help_option = False
        result = invoke(app, ["--help"], ui=self.ui)
        self.assertEqual(result.exit_code, 2)
        self.assertIn("No such option: --help", self.stderr.getvalue())
        self.assertEqual(self.stdout.getvalue(), "")

    def test_input_errors_keep_the_correct_command_usage(self):
        for args, command in (
            (["--unknown"], "demo"),
            (["process", "job", "--unknown"], "demo process"),
            (["process", "job", "--count"], "demo process"),
            (["process", "job", "--count", "invalid"], "demo process"),
            (["process", "job", "extra"], "demo process"),
        ):
            with self.subTest(args=args):
                self.reset_output()
                self.assertEqual(self.run_app(sample_app(), args), 2)
                output = self.stderr.getvalue()
                self.assertTrue(output.startswith("Error:"))
                self.assertIn(f"\n\nUsage: {command} [OPTIONS]", output)
                self.assertEqual(self.stdout.getvalue(), "")

    def test_runtime_failure_has_no_usage_or_traceback(self):
        self.assertEqual(self.run_app(sample_app(), ["runtime"]), 1)
        self.assertEqual(self.stderr.getvalue(), "Error: Request timed out.\n")
        self.assertEqual(self.stdout.getvalue(), "")

    def test_debug_runtime_failure_includes_traceback(self):
        self.ui["debug"] = True
        self.assertEqual(self.run_app(sample_app(), ["runtime"]), 1)
        output = self.stderr.getvalue()
        self.assertTrue(output.startswith("Error: Request timed out."))
        self.assertIn("Traceback", output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("Usage:", output)

    def test_declared_runtime_error_does_not_require_a_context(self):
        app = typer.Typer(add_completion=False)

        @app.command()
        def runtime():
            raise ClickException("Service unavailable.")

        self.assertEqual(self.run_app(app, []), 1)
        self.assertEqual(self.stderr.getvalue(), "Error: Service unavailable.\n")

    def test_single_command_and_programmatic_return_values(self):
        app = typer.Typer(add_completion=False)

        @app.command()
        def value():
            return 7

        self.assertEqual(app(args=[], standalone_mode=False), 7)
        self.assertEqual(self.run_app(app, []), 0)

    def test_explicit_exit_and_cancellation(self):
        app = typer.Typer(add_completion=False)

        @app.command()
        def exit_code():
            raise typer.Exit(7)

        @app.command()
        def abort():
            raise typer.Abort()

        self.assertEqual(self.run_app(app, ["exit-code"]), 7)
        self.assertEqual(self.stderr.getvalue(), "")
        self.assertEqual(self.run_app(app, ["abort"]), 1)
        self.assertEqual(self.stderr.getvalue(), "Error: Aborted.\n")

    def test_custom_command_class_survives_repeated_runs(self):
        calls = []

        class CustomCommand(TyperCommand):
            def parse_args(self, ctx, args):
                calls.append("parse")
                return super().parse_args(ctx, args)

        app = typer.Typer(add_completion=False)

        @app.command(cls=CustomCommand)
        def value():
            typer.echo("ok")

        self.assertIsInstance(get_command(app), CustomCommand)
        self.assertEqual(self.run_app(app, []), 0)
        self.assertEqual(self.run_app(app, []), 0)
        self.assertIs(app.registered_commands[0].cls, CustomCommand)
        self.assertEqual(calls, ["parse", "parse"])
        self.assertEqual(self.stdout.getvalue(), "ok\nok\n")

    def test_nested_group_callback_and_mount_classes_are_preserved(self):
        for mounted in (False, True):
            with self.subTest(mounted=mounted):
                self.reset_output()
                calls = []

                class CustomGroup(TyperGroup):
                    def parse_args(self, ctx, args):
                        calls.append("group")
                        return super().parse_args(ctx, args)

                child = typer.Typer(help="Manage nested jobs.")

                @child.callback(cls=None if mounted else CustomGroup)
                def callback():
                    pass

                @child.command()
                def execute(name: str):
                    """Execute a nested job."""
                    typer.echo(name)

                root = typer.Typer(add_completion=False)
                if mounted:
                    root.add_typer(child, name="nested", cls=CustomGroup)
                else:
                    root.add_typer(child, name="nested")
                self.assertEqual(self.run_app(root, ["nested", "execute", "job"]), 0)
                self.assertEqual(calls, ["group"])
                self.assertEqual(self.stdout.getvalue(), "job\n")
                self.reset_output()
                self.assertEqual(self.run_app(root, ["nested", "execute"]), 2)
                self.assertTrue(
                    self.stderr.getvalue().startswith("Execute a nested job.")
                )
                self.assertIn("Usage: demo nested execute", self.stderr.getvalue())

    def test_late_commands_and_original_app_remain_independent(self):
        styled = sample_app()
        self.assertEqual(self.run_app(styled, ["--help"]), 0)
        self.reset_output()

        @styled.command()
        def added():
            """A command registered after the first run."""
            pass

        self.assertEqual(self.run_app(styled, ["added", "--help"]), 0)
        self.assertTrue(self.stdout.getvalue().startswith("A command registered after"))
        self.assertIs(type(get_command(styled)), TyperGroup)

    def test_group_callbacks_keep_execution_and_required_parameters(self):
        app = typer.Typer(add_completion=False)

        @app.callback(invoke_without_command=True)
        def callback():
            typer.echo("callback")

        self.assertEqual(self.run_app(app, []), 0)
        self.assertEqual(self.stdout.getvalue(), "callback\n")
        self.reset_output()
        required = typer.Typer(add_completion=False)

        @required.callback()
        def required_callback(name: Annotated[str, typer.Argument()]):
            """Select a named workspace."""
            pass

        self.assertEqual(self.run_app(required, []), 2)
        self.assertTrue(self.stderr.getvalue().startswith("Select a named workspace."))

    def test_required_environment_metadata_and_empty_short_column(self):
        app = typer.Typer(add_completion=False)

        @app.command()
        def show(
            source: Annotated[
                str,
                typer.Argument(envvar="UI_DEMO_SOURCE", help="Source name."),
            ],
        ):
            pass

        self.assertEqual(self.run_app(app, ["--help"]), 0)
        output = self.stdout.getvalue()
        self.assertIn("[env: UI_DEMO_SOURCE=]", output)
        self.assertNotIn("required]", output)
        self.assertIn("\n  --help  ", output)

    def test_ui_entrypoint_handles_help_and_errors(self):
        app = sample_app()
        self.assertIs(type(app), typer.Typer)
        for args, status, first_line in (
            ([], 0, "Process named jobs."),
            (["--help"], 0, "Process named jobs."),
            (["process"], 2, "Process one named job."),
            (["--unknown"], 2, "Error: No such option: --unknown"),
            (["runtime"], 1, "Error: Request timed out."),
        ):
            with self.subTest(args=args):
                self.reset_output()
                self.assertEqual(self.run_app(app, args), status)
                output = self.stderr.getvalue() if status else self.stdout.getvalue()
                self.assertEqual(output.splitlines()[0], first_line)

    def test_native_classes_defaults_help_and_errors_remain_unchanged(self):
        class CustomGroup(TyperGroup):
            pass

        class CustomCommand(TyperCommand):
            pass

        app = sample_app()
        child = typer.Typer(cls=CustomGroup)

        @child.callback(cls=CustomGroup)
        def callback():
            pass

        @child.command(cls=CustomCommand)
        def execute():
            pass

        app.add_typer(child, name="nested", cls=None)
        assert child.registered_callback is not None
        infos = [
            app.info,
            *app.registered_commands,
            *app.registered_groups,
            child.info,
            child.registered_callback,
            *child.registered_commands,
        ]
        original_classes = [info.cls for info in infos]
        runner = CliRunner()
        original_help = runner.invoke(app, ["--help"]).output
        original_error = runner.invoke(app, ["process"]).output
        for theme in ("plain", "uv"):
            self.ui = dict(theme=THEMES[theme], console=self.ui["console"])
            self.assertEqual(self.run_app(app, ["--help"]), 0)
        self.assertEqual(runner.invoke(app, ["--help"]).output, original_help)
        self.assertEqual(runner.invoke(app, ["process"]).output, original_error)
        for info, original in zip(infos, original_classes, strict=True):
            self.assertIs(info.cls, original)

    def test_shared_subapp_keeps_inherited_classes_across_themes(
        self,
    ):
        calls = []

        class CustomGroup(TyperGroup):
            def parse_args(self, ctx, args):
                calls.append(ctx.info_name)
                return super().parse_args(ctx, args)

        child = typer.Typer(cls=CustomGroup, add_completion=False)

        @child.command()
        def show():
            typer.echo("done")

        app = typer.Typer(add_completion=False)
        app.add_typer(child, name="first")
        app.add_typer(child, name="second")
        runner = CliRunner()
        native = runner.invoke(app, ["first", "--help"])
        registrations = [
            app.info,
            *app.registered_groups,
            child.info,
            *child.registered_commands,
        ]
        original = [info.cls for info in registrations]
        for theme in THEMES.values():
            self.ui = dict(theme=theme)
            for name in ("first", "second"):
                calls.clear()
                result = invoke(app, [name, "show"], ui=self.ui)
                self.assertEqual(result.exit_code, 0, result.exception)
                self.assertEqual(result.stdout, "done\n")
                self.assertEqual(calls, [name])
        self.assertEqual(runner.invoke(app, ["first", "--help"]).stdout, native.stdout)
        for info, cls in zip(registrations, original, strict=True):
            self.assertIs(info.cls, cls)

    def test_preset_and_rich_themes_can_be_changed_after_registration(self):
        app = sample_app()
        console = Console(
            file=self.stdout,
            width=100,
            force_terminal=True,
            color_system="standard",
            _environ={},
        )
        custom = Theme({**PLAIN.styles, "cli.heading": "bold magenta"})
        for theme, heading_style in (
            ("plain", "\x1b[1m"),
            ("uv", "\x1b[1;32m"),
            (custom, "\x1b[1;35m"),
        ):
            with self.subTest(theme=theme):
                self.reset_output()
                self.ui = dict(
                    theme=THEMES[theme] if isinstance(theme, str) else theme,
                    console=console,
                )
                self.assertEqual(self.run_app(app, ["--help"]), 0)
                self.assertIn(heading_style, self.stdout.getvalue())

    def test_invalid_theme_is_rejected(self):
        app = sample_app()
        self.ui = dict(theme=PLAIN)
        command_class = app.registered_commands[0].cls
        with self.assertRaises(TypeError):
            run(app, theme=cast(Any, "unknown"))
        self.assertIs(app.registered_commands[0].cls, command_class)

    def test_themes_keep_geometry_and_error_policy(self):
        console = Console(
            file=self.stdout,
            width=100,
            force_terminal=True,
            color_system="standard",
            _environ={},
        )
        plain_help = None
        for theme, ansi in (("plain", "\x1b[1m"), ("uv", "\x1b[1;32m")):
            with self.subTest(theme=theme):
                self.reset_output()
                app = sample_app()
                self.ui = dict(
                    theme=THEMES[theme],
                    console=console,
                    error_console=self.ui["error_console"],
                )
                self.assertEqual(self.run_app(app, ["--help"]), 0)
                output = self.stdout.getvalue()
                self.assertIn(ansi, output)
                self.assertNotIn("╭", output)
                unstyled = re.sub(r"\x1b\[[0-9;]*m", "", output)
                if plain_help is not None:
                    self.assertEqual(unstyled, plain_help)
                plain_help = unstyled
                for args, status, prefix in (
                    (["process"], 2, "Process one named job."),
                    (["--unknown"], 2, "Error: No such option:"),
                    (["runtime"], 1, "Error: Request timed out."),
                ):
                    self.reset_output()
                    self.assertEqual(self.run_app(app, args), status)
                    self.assertTrue(self.stderr.getvalue().startswith(prefix))
                    self.assertEqual("Usage:" in self.stderr.getvalue(), status == 2)

    def test_custom_rich_theme_styles_argument_names_independently(self):
        app = sample_app()
        console = Console(
            file=self.stdout,
            width=100,
            force_terminal=True,
            color_system="standard",
            _environ={},
        )
        custom = Theme({**PLAIN.styles, "cli.argument.name": "bold red"})
        original_styles = dict(custom.styles)
        self.ui = dict(theme=custom, console=console)
        self.assertEqual(self.run_app(app, ["process", "--help"]), 0)
        self.assertIn("\x1b[1;31mname", self.stdout.getvalue())
        self.assertIn("\x1b[1m--count", self.stdout.getvalue())
        self.assertEqual(custom.styles, original_styles)
        self.reset_output()
        self.assertEqual(self.run_app(app, ["--help"]), 0)
        self.assertIn("\x1b[1mprocess", self.stdout.getvalue())

    def test_command_names_are_bold_in_lists_and_help_or_error_usage(self):
        for theme in THEMES:
            with self.subTest(theme=theme):
                consoles = [
                    Console(
                        file=output,
                        width=100,
                        force_terminal=True,
                        color_system="standard",
                        _environ={},
                    )
                    for output in (self.stdout, self.stderr)
                ]
                app = sample_app()
                self.ui = dict(
                    theme=THEMES[theme], console=consoles[0], error_console=consoles[1]
                )
                for args in (
                    ["--help"],
                    ["process", "--help"],
                    ["process", "job", "--unknown"],
                ):
                    self.reset_output()
                    status = self.run_app(app, args)
                    self.assertEqual(status, 2 if "--unknown" in args else 0)
                    output = self.stderr if status else self.stdout
                    console = consoles[1] if status else consoles[0]
                    decoded = Text.from_ansi(output.getvalue())
                    start = decoded.plain.index("Usage:")
                    for name in ("demo", "process"):
                        offset = decoded.plain.index(name, start)
                        self.assertTrue(
                            decoded.get_style_at_offset(console, offset).bold
                        )
                    for placeholder in ("[OPTIONS]", "NAME"):
                        if placeholder in decoded.plain[start:]:
                            offset = decoded.plain.index(placeholder, start)
                            self.assertFalse(
                                decoded.get_style_at_offset(console, offset).bold
                            )

    def test_formatter_controls_spacing_without_affecting_theme(self):
        app = sample_app()
        group = get_command(app)
        assert isinstance(group, TyperGroup)
        command = group.commands["process"]
        ctx = typer.Context(command, info_name="process")
        with self.ui["console"].use_theme(PLAIN):
            formatter = HelpFormatter(
                console=self.ui["console"],
                indent_increment=4,
                column_gap=2,
                description_gap=3,
            )
            formatter.write_help(ctx)
        output = formatter.getvalue()
        self.assertIn("\n    *  name <STR>", output)
        self.assertIn("\n    -n,  --count", output)

    def test_later_user_class_changes_are_used_on_the_next_run(self):
        class Replacement(TyperCommand):
            pass

        app = sample_app()
        self.ui = dict(theme=PLAIN)
        app.registered_commands[0].cls = Replacement
        self.assertEqual(self.run_app(app, ["process", "job"]), 0)
        self.assertIs(app.registered_commands[0].cls, Replacement)
        group = get_command(app)
        assert isinstance(group, TyperGroup)
        self.assertIsInstance(group.commands["process"], Replacement)

    def test_programmatic_calls_keep_native_exceptions_and_return_values(self):
        app = sample_app()
        self.assertEqual(self.run_app(app, ["process"]), 2)
        with self.assertRaises(MissingParameter):
            app(args=["process"], standalone_mode=False)
        value_app = typer.Typer(add_completion=False)

        @value_app.command()
        def value():
            return 7

        self.assertEqual(self.run_app(value_app, []), 0)
        command = get_command(value_app)
        with self.assertRaises(SystemExit) as exited:
            command.main(args=[], prog_name="demo")
        self.assertEqual(exited.exception.code, 0)
        self.assertEqual(command.main(args=[], standalone_mode=False), 7)


if __name__ == "__main__":
    unittest.main()

"""Help derives from parameter metadata and never rewrites authored text."""

import unittest
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, cast
from unittest.mock import patch

import typer
from rich.console import Console
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption
from typer.main import get_command

from tests.support.typer_ui import invoke
from toolang.common.typer.ui import (
    PLAIN,
    _format_help,
    _parameter_help,
    _value_label,
)


class Mode(str, Enum):
    safe = "safe"


class HelpMetadataTest(unittest.TestCase):
    def test_command_help_keeps_full_description_and_deprecation_notice(self):
        for deprecated in (True, "Use replacement."):
            command = TyperCommand(
                "old",
                help="Full command description.",
                short_help="Short summary.",
                deprecated=cast(Any, deprecated),
            )
            ctx = typer.Context(command, info_name="old")
            output = _format_help(ctx, theme=PLAIN, console=Console(color_system=None))
            self.assertTrue(output.startswith("Full command description. (DEPRECATED"))
            self.assertNotIn("Short summary.", output)
            if isinstance(deprecated, str):
                self.assertIn(deprecated, output)

    def test_native_objects_preserve_registration_aliases_and_parameter_identity(self):
        argument = TyperArgument(param_decls=["files"], metavar="file", nargs=-1)
        option = TyperOption(param_decls=["--output", "-o"], metavar="PATH")
        child = TyperCommand("implementation", help="Run the task.")
        command = TyperGroup(
            name="demo", params=[argument, option], commands={"run": child}
        )
        ctx = typer.Context(command, info_name="demo")
        params = tuple(command.params)
        commands = dict(command.commands)
        console = Console(width=120, theme=PLAIN, color_system=None)
        output = _format_help(ctx, theme=PLAIN, console=console)
        self.assertIn("run", output)
        self.assertNotIn("implementation", output)
        self.assertEqual(argument.name, "files")
        self.assertEqual(argument.metavar, "file")
        self.assertEqual(child.name, "implementation")
        self.assertEqual(tuple(command.params), params)
        self.assertEqual(command.commands, commands)

    def test_generated_argument_and_option_types_use_uppercase_item_labels(self):
        app = typer.Typer(add_completion=False)

        @app.command()
        def show(
            source: Annotated[Path, typer.Argument()],
            label: Annotated[str, typer.Argument()],
            count: Annotated[int, typer.Argument()],
            score: Annotated[float, typer.Argument()],
            tags: Annotated[list[str] | None, typer.Argument(metavar="tag")] = None,
            output: Annotated[Path | None, typer.Option()] = None,
            model: Annotated[str | None, typer.Option()] = None,
            limit: Annotated[int, typer.Option()] = 1,
            threshold: Annotated[float, typer.Option()] = 0.5,
            pair: Annotated[tuple[int, str], typer.Option()] = (1, "value"),
        ):
            pass

        command = get_command(app)
        ctx = typer.Context(command, info_name="demo")
        self.assertEqual(
            [
                _value_label(param, ctx)
                for param in command.params
                if isinstance(param, TyperArgument)
            ],
            ["<PATH>", "<STR>", "<INT>", "<FLOAT>", "<STR>"],
        )
        options = {
            param.opts[0]: _value_label(param, ctx)
            for param in command.params
            if isinstance(param, TyperOption)
        }
        self.assertEqual(options["--output"], "<PATH>")
        self.assertEqual(options["--model"], "<STR>")
        self.assertEqual(options["--limit"], "<INT>")
        self.assertEqual(options["--threshold"], "<FLOAT>")
        self.assertEqual(options["--pair"], "<INT STR>...")
        ui = dict(theme=PLAIN, console=Console(width=120))
        result = invoke(app, ui=ui, args=["--help"])
        self.assertEqual(result.exit_code, 0, result.exception)
        for label in ("source <PATH>", "tag <STR>", "--model <STR>", "--limit <INT>"):
            self.assertIn(label, result.stdout)

    def test_literal_choices_datetime_formats_and_explicit_metavars_keep_case(self):
        date_format = "%Y-%m-%dT%H:%M"
        app = typer.Typer(add_completion=False)

        @app.command()
        def show(
            mode: Annotated[Mode, typer.Argument()],
            date: Annotated[datetime, typer.Argument(formats=[date_format])],
            choice: Annotated[Mode, typer.Option()] = Mode.safe,
            hidden_choices: Annotated[
                Mode, typer.Option(show_choices=False)
            ] = Mode.safe,
            when: Annotated[
                datetime | None, typer.Option(formats=[date_format])
            ] = None,
            custom: Annotated[str | None, typer.Option(metavar="key=VALUE")] = None,
        ):
            pass

        command = get_command(app)
        ctx = typer.Context(command, info_name="demo")
        assert isinstance(command.params[0], TyperArgument)
        assert isinstance(command.params[1], TyperArgument)
        self.assertEqual(_value_label(command.params[0], ctx), "<safe>")
        self.assertEqual(_value_label(command.params[1], ctx), f"<{date_format}>")
        options = {
            param.opts[0]: param
            for param in command.params
            if isinstance(param, TyperOption)
        }
        self.assertEqual(_value_label(options["--choice"], ctx), "<safe>")
        self.assertEqual(
            _parameter_help(options["--choice"], ctx).plain, "[default: safe]"
        )
        self.assertEqual(_value_label(options["--hidden-choices"], ctx), "<STR>")
        self.assertEqual(_value_label(options["--when"], ctx), f"<{date_format}>")
        self.assertEqual(_value_label(options["--custom"], ctx), "key=VALUE")

    def test_native_panel_metadata_includes_mounts_and_excludes_hidden_entries(self):
        app = typer.Typer(add_completion=False)

        @app.callback()
        def select(
            hidden_argument: Annotated[
                str, typer.Argument(hidden=True, rich_help_panel="Hidden arguments")
            ],
            source: Annotated[
                str | None, typer.Argument(rich_help_panel="Inputs")
            ] = None,
            destination: Annotated[
                str | None, typer.Argument(rich_help_panel="Outputs")
            ] = None,
            cache: Annotated[
                str | None, typer.Option(rich_help_panel="Cache options")
            ] = None,
            offline: Annotated[
                bool, typer.Option("--offline", rich_help_panel="Network options")
            ] = False,
            hidden_option: Annotated[
                bool,
                typer.Option(
                    "--hidden", "-x", hidden=True, rich_help_panel="Hidden options"
                ),
            ] = False,
        ):
            pass

        @app.command(rich_help_panel="Jobs")
        def execute():
            """Execute a job."""

        @app.command(hidden=True, rich_help_panel="Hidden commands")
        def hidden_command():
            pass

        child = typer.Typer()

        @child.command()
        def clean():
            """Clean a job."""

        app.add_typer(child, name="nested", rich_help_panel="Administration")
        ui = dict(theme=PLAIN, console=Console(width=80))
        result = invoke(app, ui=ui, args=["--help"])
        self.assertEqual(result.exit_code, 0, result.exception)
        for title in (
            "Inputs",
            "Outputs",
            "Cache options",
            "Network options",
            "Jobs",
            "Administration",
        ):
            self.assertIn(f"\n{title}:\n", result.stdout)
        self.assertNotIn("\nHidden", result.stdout)
        self.assertNotIn("--hidden", result.stdout)
        self.assertNotIn("hidden-command", result.stdout)
        self.assertIn("execute", result.stdout)
        self.assertIn("nested", result.stdout)
        self.assertIn("\n  source <STR>", result.stdout)
        self.assertIn("\n  destination <STR>", result.stdout)
        self.assertIn("\n  --help", result.stdout)
        self.assertIn("\n  --cache", result.stdout)
        self.assertIn("\n  --offline", result.stdout)

    def test_authored_labels_and_default_text_are_not_rewritten(self):
        app = typer.Typer(add_completion=False)

        @app.command()
        def show(
            source: Annotated[
                str,
                typer.Argument(
                    envvar=["PRIMARY_SOURCE", "BACKUP_SOURCE"],
                    help="Keep [env var: LITERAL] and [required].",
                ),
            ],
            label: Annotated[
                str, typer.Option(help="Keep [default: literal].")
            ] = "[env var: VALUE]",
        ):
            pass

        ui = dict(theme=PLAIN, console=Console(width=140))
        # Formatted help records are deliberately unusable. The renderer must
        # obtain authored descriptions and annotations from separate fields.
        with (
            patch.object(
                TyperArgument,
                "get_help_record",
                side_effect=AssertionError("formatted argument"),
            ),
            patch.object(
                TyperOption,
                "get_help_record",
                side_effect=AssertionError("formatted option"),
            ),
        ):
            result = invoke(app, ui=ui, args=["--help"], terminal_width=120)
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertIn("Keep [env var: LITERAL] and [required].", result.stdout)
        self.assertIn("[env: PRIMARY_SOURCE=, BACKUP_SOURCE=]", result.stdout)
        self.assertIn("Keep [default: literal].", result.stdout)
        self.assertIn("[default: [env var: VALUE]]", result.stdout)

    def test_environment_names_use_declarations_and_nested_context(self):
        app = typer.Typer(add_completion=False)

        @app.command()
        def show(
            source: Annotated[str, typer.Argument(envvar="SOURCE")],
            token: Annotated[str, typer.Option(envvar=["TOKEN", "LEGACY_TOKEN"])] = "",
            secret: Annotated[
                str, typer.Option(envvar="SECRET", show_envvar=False)
            ] = "",
            automatic: Annotated[str, typer.Option()] = "",
            disabled: Annotated[str, typer.Option()] = "",
            hidden: Annotated[str, typer.Option(hidden=True)] = "",
        ):
            pass

        command = get_command(app)
        for param in command.params:
            if isinstance(param, TyperOption) and param.name == "disabled":
                param.allow_from_autoenv = False
        parent = typer.Context(command, info_name="root", auto_envvar_prefix="DEMO")
        ctx = typer.Context(command, info_name="job-name", parent=parent)
        params = {
            param.name: param
            for param in command.params
            if isinstance(param, (TyperArgument, TyperOption))
        }
        self.assertEqual(_parameter_help(params["source"], ctx).plain, "[env: SOURCE=]")
        self.assertEqual(
            _parameter_help(params["token"], ctx).plain, "[env: TOKEN=, LEGACY_TOKEN=]"
        )
        self.assertEqual(
            _parameter_help(params["automatic"], ctx).plain,
            "[env: DEMO_JOB_NAME_AUTOMATIC=]",
        )
        self.assertEqual(_parameter_help(params["secret"], ctx).plain, "")
        self.assertEqual(_parameter_help(params["disabled"], ctx).plain, "")

    def test_defaults_and_ranges_keep_typer_semantics_without_calling_factories(self):
        called = []

        def factory():
            called.append(True)
            return "computed"

        app = typer.Typer(add_completion=False)

        @app.command()
        def show(
            dynamic: Annotated[str, typer.Option(default_factory=factory)],
            count: Annotated[int, typer.Option(min=1, max=5)] = 2,
            mode: Annotated[Mode, typer.Option()] = Mode.safe,
            tags: Annotated[list[str], typer.Option()] = ["a", "b"],
            enabled: Annotated[bool, typer.Option("--enabled/--no-enabled")] = True,
            named: Annotated[
                str, typer.Option(show_default="selected automatically")
            ] = "value",
            hidden_default: Annotated[str, typer.Option(show_default=False)] = "secret",
        ):
            pass

        command = get_command(app)
        ctx = typer.Context(command, info_name="demo", default_map={"count": 3})
        params = {
            param.name: param
            for param in command.params
            if isinstance(param, (TyperArgument, TyperOption))
        }
        expected = {
            "dynamic": "[default: (dynamic)]",
            "count": "[default: 3; 1<=x<=5]",
            "mode": "[default: safe]",
            "tags": "[default: a, b]",
            "enabled": "[default: enabled]",
            "named": "[default: (selected automatically)]",
            "hidden_default": "",
        }
        for name, text in expected.items():
            with self.subTest(name=name):
                self.assertEqual(_parameter_help(params[name], ctx).plain, text)
        self.assertEqual(called, [])

    def test_aliases_metavars_and_required_flags_are_structured(self):
        app = typer.Typer(add_completion=False)

        @app.command()
        def show(
            name: Annotated[str, typer.Argument(metavar="NAME")],
            target: Annotated[str, typer.Option("--target", "-t", metavar="PATH")],
            color: Annotated[bool, typer.Option("--color/--no-color", "-c/-C")] = True,
            tags: Annotated[list[str] | None, typer.Argument()] = None,
        ):
            pass

        ui = dict(theme=PLAIN, console=Console(width=140))
        result = invoke(app, ui=ui, args=["--help"], prog_name="demo")
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertIn("Usage: demo [OPTIONS] NAME [TAGS...]", result.stdout)
        self.assertIn("* NAME <STR>", result.stdout)
        self.assertIn("tags <STR>", result.stdout)
        self.assertIn("-t,", result.stdout)
        self.assertIn("--target PATH", result.stdout)
        self.assertIn("[required]", result.stdout)
        self.assertIn("-c / -C, --color / --no-color", result.stdout)

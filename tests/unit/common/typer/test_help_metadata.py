"""Help formats descriptions and metadata without changing declarations."""

import unittest
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, cast
from unittest.mock import patch

import typer
from rich.cells import cell_len
from rich.console import Console
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption
from typer.main import get_command
from typer._click.utils import strip_ansi

from tests.support.typer_ui import invoke
from toolang.common.typer.options import (
    BARE_VALUE,
    OptionalValue,
    OptionalValueCommand,
    OptionalValueGroup,
)
from toolang.common.typer.ui import (
    PLAIN,
    UV,
    _format_help,
    _parameter_help,
    _value_label,
)


class Mode(str, Enum):
    safe = "safe"


class HelpMetadataTest(unittest.TestCase):
    def test_top_description_gets_a_period_without_changing_command_summaries(self):
        for description, expected in (
            ("Run the task", "Run the task."),
            ("Run the task.", "Run the task."),
            ("More...", "More..."),
        ):
            with self.subTest(description=description):
                child = TyperCommand("run", help=description)
                parent = TyperGroup(name="demo", commands={"run": child})
                ctx = typer.Context(child, info_name="run")
                output = _format_help(ctx, theme=PLAIN)
                self.assertTrue(output.startswith(expected + "\n\nUsage:"))
                listing = _format_help(typer.Context(parent), theme=PLAIN)
                self.assertIn(
                    f"run {description}",
                    [" ".join(line.split()) for line in listing.splitlines()],
                )
                self.assertEqual(child.help, description)

    def test_only_help_option_loses_its_final_period_before_metadata(self):
        for names, expected in (
            (["--help", "-h"], "First sentence. Last sentence"),
            (["--value"], "First sentence. Last sentence."),
            (["--help-file"], "First sentence. Last sentence."),
        ):
            with self.subTest(names=names):
                description = "First sentence. Last sentence."
                option = TyperOption(
                    param_decls=names,
                    help=description,
                    default="value.",
                    show_default=True,
                )
                argument = TyperArgument(param_decls=["name"], help=description)
                ctx = typer.Context(TyperCommand("demo", params=[argument, option]))
                self.assertEqual(
                    _parameter_help(option, ctx).plain,
                    expected + "  [default: value.]",
                )
                self.assertEqual(_parameter_help(argument, ctx).plain, description)
                self.assertEqual(option.help, description)

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

    def test_arguments_show_only_metavars_and_options_use_uppercase_type_labels(self):
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
        arguments = result.stdout.split("Arguments:\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(
            [line.strip() for line in arguments.splitlines()],
            ["* source", "* label", "* count", "* score", "tag"],
        )
        for label in ("--model <STR>", "--limit <INT>"):
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
        output = _format_help(ctx, theme=PLAIN)
        arguments = output.split("Arguments:\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(
            [line.strip() for line in arguments.splitlines()], ["* mode", "* date"]
        )
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
        self.assertEqual(_value_label(options["--custom"], ctx), "<key=VALUE>")

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
        self.assertIn("\n  source\n", result.stdout)
        self.assertIn("\n  destination\n", result.stdout)
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
            "count": "[default: 3] [1<=x<=5]",
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
        arguments = result.stdout.split("Arguments:\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(
            [line.strip() for line in arguments.splitlines()], ["* NAME", "tags"]
        )
        self.assertIn("-t,", result.stdout)
        self.assertIn("--target <PATH>", result.stdout)
        self.assertIn("[required]", result.stdout)
        self.assertIn("-c / -C, --color / --no-color", result.stdout)

    def test_option_metavars_keep_value_brackets_independently_of_required_option(self):
        for required in (False, True):
            for metavar, expected in (
                (None, "<STR>"),
                ("MODEL_SPEC", "<MODEL_SPEC>"),
                ("<PATH>", "<PATH>"),
                ("[THREAD]", "[THREAD]"),
                ("key=VALUE", "<key=VALUE>"),
                ("<FIELD>=<VALUE>", "<FIELD>=<VALUE>"),
                ("key=<VALUE>", "key=<VALUE>"),
                ("ITEM...", "<ITEM>..."),
                ("<ITEM>...", "<ITEM>..."),
            ):
                with self.subTest(required=required, metavar=metavar):
                    option = TyperOption(
                        param_decls=["--value"], metavar=metavar, required=required
                    )
                    command = TyperCommand("demo", params=[option])
                    ctx = typer.Context(command, info_name="demo")
                    output = _format_help(ctx, theme=PLAIN)
                    self.assertIn(f"--value {expected}", output)
                    self.assertIn("Usage: demo [OPTIONS]\n", output)
                    self.assertEqual(option.metavar, metavar)

    def test_optional_value_metadata_is_separate_and_does_not_resolve_values(self):
        class BudgetCommand(OptionalValueCommand):
            optional_values = {"budget": OptionalValue(bare_value="8")}

        app = typer.Typer(add_completion=False)

        @app.command(cls=BudgetCommand)
        def show(
            budget: Annotated[
                int, typer.Option(envvar="ABC_DEF", min=1, max=10, help="Set budget")
            ] = 3,
        ):
            self.fail("help executed the command")

        command = get_command(app)
        ctx = typer.Context(command, info_name="demo", default_map={"budget": 4})
        option = next(param for param in command.params if param.name == "budget")
        assert isinstance(option, TyperOption)
        with (
            patch.object(
                option.type, "convert", side_effect=AssertionError("converted")
            ),
            patch.object(
                option, "value_from_envvar", side_effect=AssertionError("read env")
            ),
        ):
            self.assertEqual(
                _parameter_help(option, ctx).plain,
                "Set budget  [env: ABC_DEF=] [default: 4] [bare: 8] [1<=x<=10]",
            )
            for theme in (PLAIN, UV):
                for width in (44, 120):
                    with self.subTest(theme=theme, width=width):
                        ctx.terminal_width = width
                        output = strip_ansi(
                            _format_help(
                                ctx,
                                theme=theme,
                                console=Console(width=width, force_terminal=True),
                            )
                        )
                        self.assertIn(
                            "[env: ABC_DEF=] [default: 4] [bare: 8] [1<=x<=10]",
                            " ".join(output.split()),
                        )
                        self.assertFalse(
                            [
                                line
                                for line in output.splitlines()
                                if cell_len(line) > width
                            ]
                        )
            option.show_envvar = False
            option.show_default = False
            self.assertEqual(
                _parameter_help(option, ctx).plain, "Set budget  [bare: 8] [1<=x<=10]"
            )

    def test_bare_labels_are_literal_and_preserve_raw_selection(self):
        class SelectionCommand(OptionalValueCommand):
            optional_values = {
                "selected": OptionalValue(
                    bare_value=BARE_VALUE, show_bare="latest thread"
                ),
                "private": OptionalValue(bare_value=BARE_VALUE, show_bare=False),
                "empty": "",
                "literal": "[env: LITERAL=]",
                "path": ".",
            }

        captured = {}
        app = typer.Typer(add_completion=False)

        def default_factory():
            self.fail("help called the default factory")

        @app.command(cls=SelectionCommand)
        def show(
            dynamic: Annotated[str, typer.Option(default_factory=default_factory)],
            selected: str | None = None,
            private: str | None = None,
            empty: str | None = None,
            literal: str | None = None,
            path: Annotated[
                Path | None, typer.Option(exists=True, resolve_path=True)
            ] = None,
        ):
            captured.update(
                selected=selected,
                private=private,
                empty=empty,
                literal=literal,
                path=path,
            )

        command = get_command(app)
        ctx = typer.Context(command)
        expected = {
            "selected": "[bare: latest thread]",
            "private": "",
            "empty": '[bare: ""]',
            "literal": "[bare: [env: LITERAL=]]",
            "path": "[bare: .]",
            "dynamic": "[default: (dynamic)]",
        }
        for param in command.params:
            assert isinstance(param, TyperOption) and param.name is not None
            with (
                self.subTest(name=param.name),
                patch.object(
                    param.type, "convert", side_effect=AssertionError("converted")
                ),
            ):
                self.assertEqual(
                    _parameter_help(param, ctx).plain, expected[param.name]
                )
        result = invoke(
            app,
            args=[
                "--selected",
                "--private",
                "--empty",
                "--literal",
                "--path",
                "--dynamic=value",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertEqual(
            captured,
            {
                "selected": BARE_VALUE,
                "private": BARE_VALUE,
                "empty": "",
                "literal": "[env: LITERAL=]",
                "path": Path.cwd(),
            },
        )

    def test_bare_metadata_belongs_to_each_command_or_group(self):
        class Parent(OptionalValueGroup):
            optional_values = {"value": "parent"}

        class Child(OptionalValueCommand):
            optional_values = {"value": "child"}

        parent_option = TyperOption(param_decls=["--value"])
        child_option = TyperOption(param_decls=["--value"])
        parent = Parent(name="parent", params=[parent_option])
        child = Child(name="child", params=[child_option])
        parent.add_command(child)
        parent_ctx = typer.Context(parent)
        child_ctx = typer.Context(child, parent=parent_ctx)
        self.assertEqual(
            _parameter_help(parent_option, parent_ctx).plain, "[bare: parent]"
        )
        self.assertEqual(
            _parameter_help(child_option, child_ctx).plain, "[bare: child]"
        )

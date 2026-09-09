"""Argument item declarations drive Usage without changing parsing semantics."""

import unittest
from enum import Enum
from typing import Annotated
from unittest.mock import patch

import typer
from rich.console import Console
from typer.core import TyperArgument, TyperCommand, TyperGroup
from typer.main import get_command
from typer.testing import CliRunner

from tests.support.typer_ui import grouped_app as create_app
from tests.support.typer_ui import invoke
from toolang.common.typer.ui import PLAIN, _usage


class Mode(str, Enum):
    fast = "fast"
    safe = "safe"


def make_ui():
    return dict(
        theme=PLAIN,
        console=Console(width=140, color_system=None),
        error_console=Console(width=140, color_system=None, stderr=True),
    )


class ArgumentUsageTest(unittest.TestCase):
    def test_grouped_arguments_keep_declaration_order_in_usage_and_item_labels_in_help(
        self,
    ):
        app = create_app()
        ui = make_ui()
        result = invoke(app, ui=ui, args=["groups", "--help"], prog_name="demo")
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertIn(
            "Usage: demo groups [OPTIONS] <SOURCE> <DESTINATION> [LABEL] [TAG...]\n",
            result.stdout,
        )
        self.assertIn("\n    tag\n", result.stdout)
        self.assertNotIn("tags...", result.stdout)
        self.assertNotIn("tag...", result.stdout)

    def test_help_missing_arguments_and_input_errors_use_the_same_usage(self):
        app = create_app()
        ui = make_ui()
        _usage = "Usage: demo groups [OPTIONS] <SOURCE> <DESTINATION> [LABEL] [TAG...]"
        for args, code in (
            (["groups", "--help"], 0),
            (["groups"], 2),
            (["groups", "--unknown"], 2),
            (["groups", "in", "out", "--temperature", "invalid"], 2),
        ):
            with self.subTest(args=args):
                result = invoke(app, ui=ui, args=args, prog_name="demo")
                self.assertEqual(result.exit_code, code, result.exception)
                output = result.stderr if code else result.stdout
                self.assertIn(_usage, output)
                if "--unknown" in args or "invalid" in args:
                    self.assertTrue(output.startswith("Error:"))
                    self.assertIn("\n\n" + _usage, output)
                else:
                    self.assertTrue(
                        output.startswith(
                            "Inspect aligned argument and option groups.\n\n"
                        )
                    )

    def test_nested_usage_formats_parent_arguments_and_required_item_sequences(self):
        root = typer.Typer(add_completion=False)
        child = typer.Typer(add_completion=False)
        received = []

        @root.callback()
        def workspace(
            workspace_name: Annotated[str, typer.Argument(metavar="workspace")],
        ):
            received.append(workspace_name)

        @child.callback()
        def dataset(dataset_name: Annotated[str, typer.Argument(metavar="dataset")]):
            received.append(dataset_name)

        @child.command()
        def read(entries: Annotated[list[str], typer.Argument(metavar="entry")]):
            received.append(entries)

        root.add_typer(child, name="data")
        ui = make_ui()
        prefix = ["my-workspace", "data", "my-dataset", "read"]
        _usage = "Usage: demo <WORKSPACE> data <DATASET> read [OPTIONS] <ENTRY>..."
        for args in ([*prefix, "--help"], [*prefix, "--unknown"]):
            result = invoke(root, ui=ui, args=args, prog_name="demo")
            self.assertEqual(result.exit_code, 2 if "--unknown" in args else 0)
            self.assertIn(_usage, result.output)
        received.clear()
        result = invoke(root, ui=ui, args=[*prefix, "one", "two"], prog_name="demo")
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertEqual(received, ["my-workspace", "my-dataset", ["one", "two"]])

    def test_enum_and_fixed_arity_usage_describe_names_and_preserve_item_count(self):
        app = typer.Typer(add_completion=False)
        received = []

        @app.command()
        def move(
            status: Annotated[Mode, typer.Argument()],
            coordinates: Annotated[
                tuple[int, int], typer.Argument(metavar="coordinate")
            ],
            destination: Annotated[
                tuple[int, int], typer.Argument(metavar="destination")
            ] = (0, 0),
        ):
            received.append((status, coordinates, destination))

        ui = make_ui()
        result = invoke(app, ui=ui, args=["--help"], prog_name="demo")
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertIn(
            "Usage: demo [OPTIONS] <STATUS> <COORDINATE> <COORDINATE> [DESTINATION DESTINATION]\n",
            result.stdout,
        )
        result = invoke(app, ui=ui, args=["fast", "1", "2", "3", "4"])
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertEqual(received, [(Mode.fast, (1, 2), (3, 4))])

    def test_usage_renders_native_context_without_formatted_argument_strings(self):
        app = typer.Typer(add_completion=False, options_metavar="[GLOBAL OPTIONS]")

        @app.callback()
        def root():
            pass

        @app.command()
        def show(
            entries: Annotated[
                list[str] | None, typer.Argument(metavar="entry")
            ] = None,
        ):
            pass

        command = get_command(app)
        assert isinstance(command, TyperGroup)
        parent = typer.Context(command, info_name="demo")
        child = command.get_command(parent, "show")
        assert child is not None
        assert isinstance(child.params[0], TyperArgument)
        ctx = typer.Context(child, info_name="show", parent=parent)
        with (
            patch.object(
                typer.Context,
                "get_usage",
                side_effect=AssertionError("formatted usage"),
            ),
            patch.object(
                TyperArgument,
                "get_usage_pieces",
                side_effect=AssertionError("formatted argument"),
            ),
        ):
            self.assertEqual(
                _usage(ctx).plain, "Usage: demo show [GLOBAL OPTIONS] [ENTRY...]"
            )
            self.assertEqual(
                _usage(parent).plain, "Usage: demo [GLOBAL OPTIONS] COMMAND [ARGS]..."
            )
        self.assertEqual(child.params[0].metavar, "entry")
        self.assertFalse(child.params[0].required)
        self.assertEqual(child.params[0].nargs, -1)

    def test_usage_preserves_bracketed_literals_and_normalizes_item_repetition(self):
        for metavar, required, nargs, expected in (
            ("<ENTRY>", True, 1, "<ENTRY>"),
            ("[ENTRY]", False, 1, "[ENTRY]"),
            ("entry=<ENTRY>", True, 1, "entry=<ENTRY>"),
            ("entry=<ENTRY>", False, 1, "[entry=<ENTRY>]"),
            ("<NAME>.json", True, 1, "<NAME>.json"),
            ("<NAME[INDEX]>", True, 1, "<NAME[INDEX]>"),
            ("<NAME[INDEX]>", True, -1, "<NAME[INDEX]>..."),
            ("[NAME].json", True, 1, "[NAME].json"),
            ("Part[]", True, 1, "<PART[]>"),
            ("FILES...", True, -1, "<FILES>..."),
            ("<FILE>...", True, -1, "<FILE>..."),
            ("[FILE...]", False, -1, "[FILE...]"),
        ):
            with self.subTest(metavar=metavar, required=required, nargs=nargs):
                argument = TyperArgument(
                    param_decls=["entry"],
                    metavar=metavar,
                    required=required,
                    nargs=nargs,
                )
                command = TyperCommand("show", params=[argument])
                ctx = typer.Context(command, info_name="demo")
                self.assertEqual(_usage(ctx).plain, f"Usage: demo [OPTIONS] {expected}")
                self.assertEqual(argument.metavar, metavar)

    def test_ui_runs_preserve_declarations_and_native_usage(self):
        app = create_app()
        runner = CliRunner()
        native = runner.invoke(app, ["groups", "--help"], terminal_width=120)
        ui = make_ui()
        styled = invoke(app, ["groups", "--help"], ui=ui)
        self.assertIn("[TAG...]", styled.stdout)
        unchanged = runner.invoke(app, ["groups", "--help"], terminal_width=120)
        self.assertEqual(unchanged.stdout, native.stdout)
        result = runner.invoke(app, ["groups", "source", "dest", "batch", "a", "b"])
        self.assertEqual(result.exit_code, 0, result.exception)
        self.assertIn('"tags": ["a", "b"]', result.stdout)

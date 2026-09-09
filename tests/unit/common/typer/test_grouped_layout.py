"""Help panels share semantic columns without inheriting unrelated widths."""

import io
import unittest
from copy import copy

from rich.cells import cell_len
from rich.console import Console
from typer import Context
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption
from typer.models import TyperPath

from toolang.common.typer.ui import PLAIN, _format_help


def context(*, arguments=(), commands=(), options=()):
    group = TyperGroup(
        name="demo",
        params=[*arguments, *options],
        commands={cmd.name: cmd for cmd in commands},
        add_help_option=False,
    )
    return Context(group, info_name="demo")


def command(name, description, group):
    return TyperCommand(name, help=description, rich_help_panel=group)


def argument(name, description, group, *, required=False, nargs=1, type=None):
    return TyperArgument(
        param_decls=[name],
        type=type,
        help=description,
        rich_help_panel=group,
        required=required,
        nargs=nargs,
    )


def as_optional(argument):
    result = copy(argument)
    result.required = False
    return result


def option(name, description, group, *, short=(), metavar=None):
    return TyperOption(
        param_decls=[name, *short],
        metavar=metavar,
        is_flag=metavar is None,
        help=description,
        rich_help_panel=group,
        show_default=False,
    )


def column(output, token):
    line = next(line for line in output.splitlines() if token in line)
    return cell_len(line[: line.index(token)])


class GroupedLayoutTest(unittest.TestCase):
    def render(self, ctx, width=80):
        output = io.StringIO()
        console = Console(file=output, width=width, theme=PLAIN, color_system=None)
        rendered = _format_help(ctx, theme=PLAIN, console=console)
        self.assertTrue(all(cell_len(line) <= width for line in rendered.splitlines()))
        return rendered

    def test_commands_share_columns_across_groups_in_terminal_cells(self):
        wide_name = "\u6a21\u578b" * 4
        ctx = context(
            commands=(
                command("go", "CMD_A", "Primary"),
                command(wide_name, "CMD_B", "Secondary"),
                command("status", "CMD_C", "Secondary"),
            )
        )
        for width in (40, 80, 120):
            with self.subTest(width=width):
                output = self.render(ctx, width)
                names = [column(output, name) for name in ("go", wide_name, "status")]
                descriptions = [
                    column(output, token) for token in ("CMD_A", "CMD_B", "CMD_C")
                ]
                self.assertEqual(len(set(names)), 1)
                self.assertEqual(len(set(descriptions)), 1)
                self.assertEqual(descriptions[0] - names[0], cell_len(wide_name) + 2)

    def test_argument_marker_is_shared_and_removed_only_when_all_are_optional(self):
        arguments = (
            argument("source", "ARG_A", "Inputs", required=True, type=TyperPath()),
            argument("destination", "ARG_B", "Outputs", type=TyperPath()),
            argument("tag", "ARG_C", "Inputs", nargs=-1),
        )
        for width in (40, 80, 120):
            with self.subTest(width=width):
                base = 2
                required = self.render(context(arguments=arguments), width)
                optional = self.render(
                    context(arguments=tuple(as_optional(arg) for arg in arguments)),
                    width,
                )
                for output, offset in ((required, 2), (optional, 0)):
                    for name in ("source", "destination", "tag"):
                        self.assertEqual(column(output, name), base + offset)
                    self.assertEqual(
                        len(
                            {
                                column(output, token)
                                for token in ("ARG_A", "ARG_B", "ARG_C")
                            }
                        ),
                        1,
                    )
                    self.assertNotIn("[required]", output)
                self.assertNotIn("*", optional)
                self.assertEqual(column(required, "*"), base)

    def test_option_groups_share_alias_column_but_keep_local_description_widths(self):
        common = (
            option("--help", "OPT_A", None),
            option("--cache", "OPT_B", "Cache", short=("-c",), metavar="PATH"),
            option("--clear", "OPT_C", "Cache"),
            option("--model", "OPT_D", "Model", short=("-m",)),
            option("--quiet", "OPT_E", "Model"),
        )
        long_group = (
            option(
                "--client-authentication-certificate",
                "OPT_F",
                "Network",
                metavar="PATH",
            ),
            option("--offline", "OPT_G", "Network"),
        )
        for width in (40, 80, 120):
            with self.subTest(width=width):
                output = self.render(context(options=common + long_group), width)
                baseline = self.render(context(options=common), width)
                starts = {
                    column(output, token)
                    for token in (
                        "--help",
                        "--cache",
                        "--clear",
                        "--model",
                        "--quiet",
                        "--client",
                        "--offline",
                    )
                }
                self.assertEqual(len(starts), 1)
                self.assertEqual(column(output, "--cache") - column(output, "-c,"), 4)
                for tokens in (
                    ("OPT_B", "OPT_C"),
                    ("OPT_D", "OPT_E"),
                    ("OPT_F", "OPT_G"),
                ):
                    self.assertEqual(
                        column(output, tokens[0]), column(output, tokens[1])
                    )
                for token in ("OPT_A", "OPT_B", "OPT_C", "OPT_D", "OPT_E"):
                    self.assertEqual(column(output, token), column(baseline, token))
                self.assertLess(column(output, "OPT_B"), column(output, "OPT_F"))

                # Reassemble the folded label to ensure narrow panels lose no text.
                network = output.split("Network:\n", 1)[1]
                start = column(output, "--client")
                end = column(output, "OPT_F") - 2
                fragments = []
                for line in network.splitlines():
                    if "--offline" in line:
                        break
                    if "\u2500" not in line:
                        fragments.append(line[start:end].strip())
                self.assertEqual(
                    "".join(fragments).replace(" ", ""),
                    "--client-authentication-certificate<PATH>",
                )

    def test_no_short_options_removes_prefix_across_every_group(self):
        options = (
            option("--help", "OPT_A", None),
            option("--cache", "OPT_B", "Cache"),
            option("--offline", "OPT_C", "Network"),
        )
        output = self.render(context(options=options))
        base = 2
        for name in ("--help", "--cache", "--offline"):
            self.assertEqual(column(output, name), base)

    def test_multiple_aliases_determine_shared_option_prefix(self):
        options = (
            option("--verbose", "OPT_A", "Output", short=("-v", "-V")),
            option("--silent", "OPT_B", "Output"),
            option("--cache", "OPT_C", "Cache", short=("-c",)),
        )
        output = self.render(context(options=options))
        self.assertIn("-v, -V, --verbose", output)
        self.assertEqual(
            len(
                {column(output, name) for name in ("--verbose", "--silent", "--cache")}
            ),
            1,
        )

    def test_groups_preserve_first_appearance_and_row_order_including_default_panel(
        self,
    ):
        entries = (("z", "Tools"), ("a", "Other"), ("m", None), ("b", "Tools"))
        contexts = {
            "Commands": context(
                commands=tuple(
                    command(name, f"ENTRY_{name}", group) for name, group in entries
                )
            ),
            "Arguments": context(
                arguments=tuple(
                    argument(name, f"ENTRY_{name}", group) for name, group in entries
                )
            ),
            "Options": context(
                options=tuple(
                    option(f"--{name}", f"ENTRY_{name}", group)
                    for name, group in entries
                )
            ),
        }
        for title, ctx in contexts.items():
            with self.subTest(title=title):
                output = self.render(ctx)
                ordered = (
                    "Tools:",
                    "ENTRY_z",
                    "ENTRY_b",
                    "Other:",
                    "ENTRY_a",
                    f"{title}:",
                    "ENTRY_m",
                )
                positions = [output.index(token) for token in ordered]
                self.assertEqual(positions, sorted(positions))
                self.assertEqual(output.count("Tools:"), 1)
                for other in contexts.keys() - {title}:
                    self.assertNotIn(f"{other}:", output)

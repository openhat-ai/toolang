"""Offline references for shared CLI option syntax."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import typer
from rich.text import Text
from typer._click import Context, HelpFormatter as NativeHelpFormatter

from toolang.cli.common.help import CliCommand, CliGroup, show_help
from toolang.common.typer.ui import HelpFormatter


def _definitions(
    formatter: HelpFormatter,
    heading: str,
    rows: Sequence[tuple[str, str]],
    *notes: str,
    role: str = "cli.argument.name",
) -> None:
    with formatter.section(heading):
        formatter.write_dl(
            [(Text(label, style=role), description) for label, description in rows],
            col_max=formatter.width,
        )
        if notes:
            formatter.write_paragraph()
            for note in notes:
                formatter.write_text(note)


def _option(formatter: HelpFormatter, name: str, description: str) -> None:
    formatter.write_text(
        Text.assemble(
            (name, "cli.option.name"), (" <KEY>=<VALUE>", "cli.option.metavar")
        )
    )
    formatter.write_paragraph()
    formatter.write_text(description + ".")


def _examples(formatter: HelpFormatter, rows: Sequence[tuple[str, str]]) -> None:
    with formatter.section("Examples"):
        for index, (description, command) in enumerate(rows):
            if index:
                formatter.write_paragraph()
            formatter.write_text(description)
            with formatter.indentation():
                formatter.write_text(Text(command, style="cli.command.name"))


def _allow(formatter: HelpFormatter) -> None:
    _option(formatter, "--allow", "Select allowed resources")
    _definitions(
        formatter,
        "Keys",
        [
            ("models", "Model queries"),
            ("tools", "Tool queries"),
            ("psyches", "Psyche queries"),
            ("skills", "Skill queries"),
            ("services", "Service queries"),
            ("prompts", "Prompt queries"),
        ],
        "Repeat a key to combine TQ queries by union.",
    )
    _definitions(
        formatter,
        "Values",
        [
            ("QUERY", "Match resources with a TQ query"),
            ("all", "Select all resources"),
            ("none", "Select no resources"),
        ],
        "With all or none, that key may appear only once.",
        "Quote values containing spaces.",
        "Existing policy restrictions still apply.",
    )
    _examples(
        formatter,
        [
            ("Allow no tools", "too gus chat --allow tools=none"),
            ("Select OpenAI models", "too gus chat --allow 'models=openai/*'"),
        ],
    )


def _limit(formatter: HelpFormatter) -> None:
    _option(formatter, "--limit", "Set run limits")
    _definitions(
        formatter,
        "Keys",
        [
            ("agic_model_calls", "Maximum model calls per agic"),
            ("agic_tool_calls", "Maximum tool calls per agic"),
            ("tokens", "Total input and output tokens per run tree"),
            ("cost", "Total cost in USD per run tree"),
            ("time", "Elapsed time in seconds per run tree"),
        ],
        "Each key may appear once.",
    )
    _definitions(
        formatter,
        "Values",
        [
            ("INTEGER", "Non-negative integer"),
            ("DECIMAL", "Non-negative decimal; cost only"),
            ("none", "Unset the limit"),
        ],
    )
    _examples(
        formatter,
        [
            (
                "Limit tokens and cost",
                "too gus chat --limit tokens=100000 \\\n  --limit cost=1.5",
            ),
            ("Unset the time limit", "too gus chat --limit time=none"),
        ],
    )


def _default(formatter: HelpFormatter) -> None:
    _option(formatter, "--default", "Set runtime or Chat session defaults")
    _definitions(
        formatter,
        "Keys",
        [
            ("model", "Model value; see 'too help model'"),
            (
                "runnable",
                "Entry ref, or none for automatic entry selection; Chat only",
            ),
        ],
        "Each key may appear once. Quote values containing spaces.",
    )
    _definitions(
        formatter,
        "Applies to",
        [
            ("serve, start", "model; applies to the runtime instance"),
            ("chat", "model and runnable; apply to the current session"),
            ("Script, rerun", "Use --model for one run"),
        ],
        "Chat defaults do not reconfigure the runtime or other sessions.",
    )
    with formatter.section("Configuration"):
        formatter.write_text(
            "Configuration uses [model]; legacy [default] tables are ignored."
        )
        formatter.write_text("TOOLANG_DEFAULT_MODEL supplies runtime model defaults.")
        formatter.write_text(
            "TOOLANG_DEFAULT_RUNNABLE and runtime runnable defaults are unsupported; "
            "use driver handlers."
        )
    _examples(
        formatter,
        [
            (
                "Set the session model",
                "too gus chat --default \\\n  'model=openai/gpt-5 effort=high'",
            ),
            (
                "Select the session entry",
                "too gus chat \\\n  --default runnable=flow:review",
            ),
        ],
    )
    _definitions(
        formatter,
        "See also",
        [("too help model", "Model value syntax")],
        role="cli.command.name",
    )


def _model(formatter: HelpFormatter) -> None:
    formatter.write_text(Text("Model selection", style="cli.heading"))
    with formatter.section("Syntax"):
        formatter.write_dl(
            [
                (
                    Text.assemble(
                        (option, "cli.option.name"), (value, "cli.option.metavar")
                    ),
                    description,
                )
                for option, value, description in (
                    ("--model", " <MODEL>", "Script or rerun"),
                    ("--default", " model=<MODEL>", "Runtime or Chat defaults"),
                    (
                        "--compact-model",
                        " <MODEL>",
                        "Compaction model for a new runtime",
                    ),
                )
            ],
            col_max=formatter.width,
        )
    with formatter.section("Model body"):
        formatter.write_text(
            Text("[ref=]MODEL [effort=VALUE] [max_output=VALUE]", style="cli.usage")
        )
        for note in (
            "An exact model ref may be bare or use ref=. Put a bare ref first.",
            "Omit the ref to change parameters on an effective model.",
            "Quote multi-token values. Duplicate refs or parameters are rejected.",
            "--compact-model requires an exact ref; it rejects default, unset, "
            "and parameter-only values.",
        ):
            formatter.write_text(note)
    _definitions(
        formatter,
        "Parameters",
        [
            ("effort", "Reasoning level, non-negative token budget, or auto"),
            ("max_output", "Positive output token count, or auto"),
        ],
        "auto clears that parameter.",
        "Supported reasoning levels depend on the selected model.",
    )
    _definitions(
        formatter,
        "Selection values",
        [
            ("default", "Use the inherited model or session baseline"),
            ("unset", "Clear selection; startup entry fallback still applies"),
        ],
        "In Chat and per-run overrides, unset selects model-free execution.",
        "none is not a model selection value.",
    )
    _examples(
        formatter,
        [
            (
                "Set a model with reasoning",
                "too gus chat --default \\\n  'model=openai/gpt-5 effort=high'",
            ),
            (
                "Set the compaction model",
                "too gus start --compact-model \\\n  'openai/gpt-5 max_output=2048'",
            ),
        ],
    )
    _definitions(
        formatter,
        "See also",
        [
            ("too models", "List available models"),
            ("too help default", "Default setting lifetimes"),
        ],
        role="cli.command.name",
    )


_TOPICS: dict[str, tuple[str, Callable[[HelpFormatter], None]]] = {
    "allow": ("Select allowed resources", _allow),
    "limit": ("Set run limits", _limit),
    "default": ("Set runtime or Chat session defaults", _default),
    "model": ("Select models and set model parameters", _model),
}


class _TopicCommand(CliCommand):
    def format_help(self, ctx: Context, formatter: NativeHelpFormatter) -> None:
        output = formatter if isinstance(formatter, HelpFormatter) else HelpFormatter()
        assert self.name is not None
        _TOPICS[self.name][1](output)
        if output is not formatter:
            formatter.write(output.getvalue())


class _HelpGroup(CliGroup):
    def list_commands(self, ctx: Context) -> list[str]:
        return list(_TOPICS)


def _directory(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        show_help(ctx)


def help_app() -> typer.Typer:
    app = typer.Typer(
        cls=_HelpGroup,
        help="Read shared option syntax",
        subcommand_metavar="[TOPIC]",
        add_completion=False,
        invoke_without_command=True,
        no_args_is_help=False,
        epilog="Run 'too help TOPIC' for details.\n"
        "Use 'too COMMAND --help' for command-specific options.",
    )
    app.callback()(_directory)
    for name, (summary, _) in _TOPICS.items():
        app.command(name, cls=_TopicCommand, help=summary, rich_help_panel="Topics")(
            show_help
        )
    return app

"""Field references shared by CLI help callers."""

from typer._click import Context
from typer.core import TyperOption

from toolang.common.typer.ui import HelpFormatter


def write_field_sections(ctx: Context, formatter: HelpFormatter) -> None:
    """Describe only visible options supported by this command."""
    options = {
        option
        for param in ctx.command.get_params(ctx)
        if isinstance(param, TyperOption) and not param.hidden
        for option in param.opts
    }
    if "--allow" in options:
        with formatter.section("Allow fields"):
            formatter.write_dl(
                [
                    ("models", "Model queries"),
                    ("tools", "Tool queries"),
                    ("psyches", "Psyche queries"),
                    ("skills", "Skill queries"),
                    ("services", "Service queries"),
                    ("prompts", "Prompt queries"),
                ]
            )
            formatter.write_paragraph()
            formatter.write_text("QUERY accepts a TQ query, all, or none.")
            formatter.write_text("Repeat a resource to combine queries by union.")
            formatter.write_text("Do not combine queries with all or none.")
            formatter.write_text("Existing policy restrictions still apply.")
    if "--limit" in options:
        with formatter.section("Limit fields"):
            formatter.write_dl(
                [
                    ("agic_model_calls", "Maximum model calls per agic"),
                    ("agic_tool_calls", "Maximum tool calls per agic"),
                    ("tokens", "Total input and output tokens per run tree"),
                    ("cost", "Total cost in USD per run tree"),
                    ("time", "Elapsed time in seconds per run tree"),
                ]
            )
            formatter.write_paragraph()
            formatter.write_text(
                "VALUE accepts a non-negative integer; cost also accepts a decimal."
            )
            formatter.write_text("Use none to unset a limit.")
            formatter.write_text(
                "Repeat --limit for different fields; each field may appear once."
            )
    if "--default" in options:
        with formatter.section("Default fields"):
            formatter.write_dl(
                [
                    ("model", "Default model and parameters"),
                    ("runnable", "Default runnable ref"),
                ]
            )
            formatter.write_paragraph()
            formatter.write_text(
                "Repeat --default for different fields; each field may appear once."
            )
            formatter.write_text("Quote values containing spaces.")
    if options & {"--model", "--compact-model"}:
        with formatter.section("Model parameters"):
            formatter.write_dl(
                [
                    ("effort", "Reasoning level, token budget, or auto"),
                    ("max_output", "Maximum output tokens (positive integer), or auto"),
                ]
            )
            formatter.write_paragraph()
            formatter.write_text(
                "Append parameters as name=value; quote values containing spaces."
            )
            if "--compact-model" in options:
                formatter.write_text(
                    "--compact-model requires an exact model for a new runtime."
                )

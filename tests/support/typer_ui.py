"""Exercise the UI entry point inside Typer's native test isolation."""

import typer
from typer.main import get_command
from typer.testing import CliRunner

from toolang.common.typer.ui import PLAIN, UV, run

THEMES = {"plain": PLAIN, "uv": UV}


def invoke(app, args=(), *, ui=None, input=None, env=None, color=False, **settings):
    runner = CliRunner()
    if ui is None:
        return runner.invoke(app, args, input=input, env=env, color=color, **settings)

    settings.setdefault("prog_name", runner.get_default_prog_name(get_command(app)))
    entry = typer.Typer(add_completion=False)

    @entry.command()
    def main():
        raise typer.Exit(run(app, args=args, **ui, **settings))

    return runner.invoke(entry, [], input=input, env=env, color=color)


def grouped_app():
    """An ordinary Typer app with grouped arguments and a repeated item."""
    import json
    from pathlib import Path
    from typing import Annotated

    app = typer.Typer(add_completion=False)

    @app.callback()
    def root():
        pass

    @app.command()
    def groups(
        source: Annotated[Path, typer.Argument(rich_help_panel="Input arguments")],
        destination: Annotated[
            Path, typer.Argument(rich_help_panel="Output arguments")
        ],
        label: Annotated[
            str | None, typer.Argument(rich_help_panel="Input arguments")
        ] = None,
        tags: Annotated[
            list[str] | None,
            typer.Argument(metavar="tag", rich_help_panel="Output arguments"),
        ] = None,
        temperature: float = 0.5,
    ):
        """Inspect aligned argument and option groups."""
        typer.echo(json.dumps({"tags": tags}))

    return app

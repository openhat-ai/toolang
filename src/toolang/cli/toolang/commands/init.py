"""Initialize one local Script without preparing an agent."""

from pathlib import Path
import shlex
from typing import Annotated

import typer

from toolang.catalog.templates import load_template


def init_script(
    ctx: typer.Context,
    directory: Annotated[
        Path,
        typer.Argument(
            metavar="DIR",
            help="Directory for work.too. Use . for the current directory",
        ),
    ],
) -> None:
    """Create the bundled Script in the selected directory."""

    try:
        template = load_template("script").raw_text
        destination = directory.expanduser().resolve() / "work.too"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(template.rstrip("\n") + "\n")
    except OSError as exc:
        raise typer.BadParameter(f"could not initialize script: {exc}") from exc
    typer.echo(f"Created {destination}")
    executable = ctx.find_root().info_name or "too"
    typer.echo(f"Run with: {shlex.join([executable, 'run', str(destination)])}")

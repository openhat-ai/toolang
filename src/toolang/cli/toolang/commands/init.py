"""Initialize one local Script without preparing an agent."""

from pathlib import Path
import os
import shlex
from typing import Annotated

import typer

from toolang.catalog.templates import load_template


def init_script(
    ctx: typer.Context,
    directory: Annotated[
        Path,
        typer.Argument(metavar="DIR", help="Directory to set up for Toolang"),
    ],
) -> None:
    """Create the bundled Script in the selected directory."""

    try:
        template = load_template("script").raw_text
        destination = directory.expanduser().resolve() / "aide.too"
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(template.rstrip("\n") + "\n")
            stream.flush()
            os.fchmod(stream.fileno(), os.fstat(stream.fileno()).st_mode | 0o111)
    except OSError as exc:
        raise typer.BadParameter(f"could not initialize script: {exc}") from exc
    typer.echo(f"Created {destination}")
    executable = ctx.find_root().info_name or "too"
    script = shlex.join([executable, str(destination)])
    examples = (
        (f"{script} info", "show agent details"),
        (f"{script} --help", "show runnables and options"),
        (script, "execute the default runnable"),
        (f"{script} chat", "start an interactive chat"),
    )
    width = max(len(command) for command, _description in examples)
    typer.echo("\nTry:")
    for command, description in examples:
        typer.echo(f"  {command:<{width}}  # {description}")

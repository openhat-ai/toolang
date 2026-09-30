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
    """Create the bundled script and its portable project configuration."""

    try:
        template = load_template("script").raw_text
        destination = directory.expanduser().resolve() / "aide.too"
        config = destination.with_name("toolang.toml")
        for target in (config, destination):
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"destination already exists: {target}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with config.open("x", encoding="utf-8") as stream:
            stream.write(
                "# Project settings for Toolang scripts.\n"
                "# Relative paths are resolved from this file's directory.\n"
            )
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(template.rstrip("\n") + "\n")
            stream.flush()
            os.fchmod(stream.fileno(), os.fstat(stream.fileno()).st_mode | 0o111)
    except OSError as exc:
        raise typer.BadParameter(f"could not initialize script: {exc}") from exc
    typer.echo(f"Created {config}")
    typer.echo(f"Created {destination}")
    typer.echo("Add .toolang/ to your project's Git ignore rules.")
    executable = ctx.find_root().info_name or "too"
    script = shlex.join([executable, str(destination)])
    examples = (
        (f"{script} info", "show agent details"),
        (f"{script} --help", "show runnables and options"),
        (f"{script} whats_for", "explain the current project"),
        (f"{script} whats_new", "list updates from the past week"),
    )
    width = max(len(command) for command, _description in examples)
    typer.echo("\nTry:")
    for command, description in examples:
        typer.echo(f"  {command:<{width}}  # {description}")

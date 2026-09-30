"""Initialize one local Script without preparing an agent."""

from pathlib import Path
import os
import shlex
from typing import Annotated

import typer
from typer._click.exceptions import ClickException

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
        conflicts = [
            target.name
            for target in (destination, config)
            if target.exists() or target.is_symlink()
        ]
        if conflicts:
            raise ClickException(
                f"aborted to avoid overwriting: {', '.join(conflicts)}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        with config.open("x", encoding="utf-8") as stream:
            stream.write("# Toolang settings. Paths are relative to this file.\n")
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(template.rstrip("\n") + "\n")
            stream.flush()
            os.fchmod(stream.fileno(), os.fstat(stream.fileno()).st_mode | 0o111)
    except FileExistsError as exc:
        name = Path(exc.filename).name if exc.filename else directory.name
        if exc.filename and Path(exc.filename) in (destination, config):
            raise ClickException(f"aborted to avoid overwriting: {name}") from exc
        raise ClickException(f"{name} already exists") from exc
    except OSError as exc:
        raise ClickException(str(exc)) from exc
    typer.echo("Created aide.too and toolang.toml.")
    typer.echo("Add .toolang/ to .gitignore.")
    executable = ctx.find_root().info_name or "too"
    command = shlex.join(
        [executable, str(directory.expanduser() / "aide.too"), "--help"]
    )
    typer.echo(f"Try: {command}")

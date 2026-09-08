"""External editor integration for authored Markdown files."""

from __future__ import annotations

import click
from typer._click.exceptions import ClickException


def edit_markdown(text: str) -> str | None:
    """Edit a document and translate external Click errors into Typer errors."""

    try:
        return click.edit(text, extension=".md", require_save=True)
    except click.ClickException as exc:
        raise ClickException(exc.format_message()) from exc

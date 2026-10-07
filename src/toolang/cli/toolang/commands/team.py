"""List public and custom messaging conversations."""

import asyncio
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table
from typer._click.exceptions import ClickException
from valkey.exceptions import ValkeyError

from toolang.cli.common.context import context_root
from toolang.cli.common.messaging import settings
from toolang.messaging.client import MessagingClient
from toolang.messaging.errors import MessagingError
from toolang.messaging.schemas import stream_id
from .text.rendering import display_text


def team_command(ctx: typer.Context) -> None:
    try:
        config, _human = settings(context_root(ctx))

        async def listing():
            async with MessagingClient(config) as client:
                return await client.contacts(include_dms=False)

        groups = asyncio.run(listing())
        table = Table("ID", "Name", "Members", "Online", "Latest message", box=None)
        for group in groups:
            latest = (
                datetime.fromtimestamp(stream_id(group["latest"])[0] / 1000)
                .astimezone()
                .isoformat(timespec="seconds")
                if group["latest"]
                else "—"
            )
            table.add_row(
                *(
                    display_text(value)
                    for value in (
                        group["group"],
                        group["name"],
                        ", ".join(group["members"]),
                        ", ".join(group["online"]) or "—",
                        latest,
                    )
                )
            )
        Console(markup=False).print(table)
    except (MessagingError, ValkeyError, ValueError, OSError) as exc:
        raise ClickException(str(exc)) from exc

"""List existing conversations and the targets used to open them."""

import asyncio
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text
from typer._click.exceptions import ClickException
from valkey.exceptions import ValkeyError

from toolang.cli.common.context import context_root
from toolang.cli.common.messaging import settings
from toolang.messaging.client import MessagingClient
from toolang.messaging.errors import MessagingError
from toolang.messaging.schemas import conversation, stream_id
from .text.rendering import display_text


def _message_time(sid: str, now: datetime) -> str:
    timestamp = datetime.fromtimestamp(stream_id(sid)[0] / 1000, tz=now.tzinfo)
    if timestamp.date() == now.date():
        return timestamp.strftime("%H:%M")
    pattern = "%m-%d %H:%M" if timestamp.year == now.year else "%Y-%m-%d %H:%M"
    return timestamp.strftime(pattern)


def team_command(ctx: typer.Context) -> None:
    try:
        config, _human = settings(context_root(ctx))

        async def listing():
            async with MessagingClient(config) as client:
                return await client.contacts(
                    include_preview=True
                ), await client.agents()

        groups, agents = asyncio.run(listing())
        groups.sort(
            key=lambda group: (
                group["group"] != "all",
                *(-part for part in stream_id(group["latest"] or "0-0")),
                group["group"],
            )
        )
        table = Table(box=None, padding=(0, 1))
        table.add_column("Target", no_wrap=True)
        table.add_column("Participants")
        table.add_column("Latest message", no_wrap=True, overflow="ellipsis")
        now = datetime.now().astimezone()
        for group in groups:
            participants = Text()
            separator = (
                " ↔ " if conversation(group["group"]).kind in {"owner", "dm"} else " · "
            )
            for member in sorted(
                group["members"], key=lambda name: (name in agents, name)
            ):
                if participants:
                    participants.append(separator, style="dim")
                if member in agents:
                    online = member in group["online"]
                    participants.append(
                        "●" if online else "○", style="green" if online else "dim"
                    )
                participants.append(display_text(member))
            latest = Text("—", style="dim")
            if group["latest"]:
                latest = Text(_message_time(group["latest"], now), style="dim")
                if preview := group["preview"]:
                    sender = display_text(preview["sender"])
                    body = " ".join(display_text(preview["body"]).split())
                    latest.append(f" {sender}: {body}", style="not dim")
                else:
                    latest.append(" Message unavailable")
            table.add_row(Text(group["group"]), participants, latest)
        console = Console(markup=False)
        console.print(table)
        console.print(
            Text("● online  ○ offline · Open: too text <target>", style="dim")
        )
    except (MessagingError, ValkeyError, ValueError, OSError) as exc:
        raise ClickException(str(exc)) from exc

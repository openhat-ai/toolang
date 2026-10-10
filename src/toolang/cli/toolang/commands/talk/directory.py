"""List existing conversations and the targets used to open them."""

import asyncio
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text
from typer._click.exceptions import ClickException

from toolang.cli.common.context import context_root
from toolang.cli.common.messaging import settings
from toolang.teaming.client import HubClient
from toolang.teaming.errors import TeamingError
from toolang.teaming.schemas import stream_id, target
from .rendering import display_text


def _message_time(sid: str, now: datetime) -> str:
    timestamp = datetime.fromtimestamp(stream_id(sid)[0] / 1000, tz=now.tzinfo)
    if timestamp.date() == now.date():
        return timestamp.strftime("%H:%M")
    pattern = "%m-%d %H:%M" if timestamp.year == now.year else "%Y-%m-%d %H:%M"
    return timestamp.strftime(pattern)


def directory_command(ctx: typer.Context) -> None:
    try:
        config, _human = settings(context_root(ctx))

        async def listing():
            async with HubClient(config) as client:
                return await client.contacts(include_preview=True), await client.team()

        conversations, team = asyncio.run(listing())
        conversations.sort(
            key=lambda row: (
                *(-part for part in stream_id(row["latest"] or "0-0")),
                row["conversation"],
            )
        )
        console = Console(markup=False)
        console.print("DM: too talk alice · Observe agents: too talk alice,bob")
        console.print("Open a listed DM or GC: too talk <id>")
        console.print()
        agents = {
            target(row["member"]).name: row
            for row in team
            if target(row["member"]).kind == "agent"
        }
        directory = Table(title="Team", title_justify="left", box=None, padding=(0, 1))
        # Reserve the status column and four cells of table padding.
        directory.add_column(
            "Agent",
            max_width=max(1, console.width - 8 - 4),
            no_wrap=True,
            overflow="ellipsis",
        )
        directory.add_column("Presence", width=8, no_wrap=True)
        for name, row in sorted(agents.items()):
            presence = (
                "—"
                if row["online"] is None
                else "online"
                if row["online"]
                else "offline"
            )
            directory.add_row(Text(display_text(name)), presence)
        console.print(directory)
        if not agents:
            console.print("No agents.")
        console.print()
        now = datetime.now().astimezone()
        times = {
            row["latest"]: _message_time(row["latest"], now)
            for row in conversations
            if row["latest"]
        }
        time_width = max(map(len, times.values()), default=0)
        # Reserve the ID, column padding, and a timestamp with a readable preview.
        metadata_width = max(11, console.width - 11 - 8 - max(14, time_width + 12))
        name_width = min(24, metadata_width // 2, metadata_width - 7)
        table = Table(
            title="Conversations",
            title_justify="left",
            box=None,
            padding=(0, 1),
            expand=True,
        )
        table.add_column("ID", min_width=11, no_wrap=True)
        table.add_column(
            "Name",
            max_width=name_width,
            no_wrap=True,
            overflow="ellipsis",
        )
        table.add_column(
            "Members",
            max_width=min(32, metadata_width - name_width),
            no_wrap=True,
            overflow="ellipsis",
        )
        table.add_column("Latest message", ratio=1, no_wrap=True, overflow="ellipsis")
        for row in conversations:
            participants = Text()
            for member in sorted(row["participants"]):
                if participants:
                    participants.append(" · ", style="dim")
                participants.append(display_text(target(member).name))
            latest = Text("—", style="dim")
            if row["latest"]:
                latest = Text(times[row["latest"]].ljust(time_width), style="dim")
                if preview := row.get("preview"):
                    sender = display_text(target(preview["sender"]).name)
                    body = " ".join(display_text(preview["body"]).split())
                    latest.append(f" {sender}: {body}", style="not dim")
                else:
                    latest.append(" Message unavailable")
            table.add_row(
                Text(row["conversation"]),
                Text(" ".join(display_text(row["name"] or "—").split())),
                participants,
                latest,
            )
        console.print(table)
        if not conversations:
            console.print("No conversations.")
    except (TeamingError, ValueError, OSError) as exc:
        raise ClickException(str(exc)) from exc

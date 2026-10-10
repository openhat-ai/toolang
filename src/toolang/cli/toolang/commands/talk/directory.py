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
from toolang.teaming.schemas import stream_id
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
        members = {row["member"]: row for row in team}
        directory = Table(title="Team", box=None, padding=(0, 1))
        for heading in ("Member", "Name", "Owner", "Presence"):
            directory.add_column(heading)
        for member, row in sorted(members.items()):
            presence = (
                "—"
                if row["online"] is None
                else "online"
                if row["online"]
                else "offline"
            )
            directory.add_row(
                *(
                    Text(display_text(str(value)))
                    for value in (
                        member,
                        row["display_name"],
                        row["owner"] or "—",
                        presence,
                    )
                )
            )
        console.print(directory)
        if not members:
            console.print("No team members.")
        table = Table(title="Convos", box=None, padding=(0, 1))
        for heading in ("ID", "Name", "Kind", "Participants", "Latest message"):
            table.add_column(heading)
        now = datetime.now().astimezone()
        for row in conversations:
            participants = Text()
            for member in sorted(row["participants"]):
                if participants:
                    participants.append(" · ", style="dim")
                state = members.get(member, {}).get("online")
                if state is not None:
                    participants.append(
                        "●" if state else "○", style="green" if state else "dim"
                    )
                participants.append(display_text(member))
            latest = Text("—", style="dim")
            if row["latest"]:
                latest = Text(_message_time(row["latest"], now), style="dim")
                if preview := row.get("preview"):
                    sender = display_text(preview["sender"])
                    body = " ".join(display_text(preview["body"]).split())
                    latest.append(f" {sender}: {body}", style="not dim")
                else:
                    latest.append(" Message unavailable")
            table.add_row(
                Text(row["conversation"]),
                Text(display_text(row["name"] or "—")),
                Text(row["kind"]),
                participants,
                latest,
            )
        console.print(table)
        if not conversations:
            console.print("No conversations.")
        console.print("DM: too talk alice · Observe agents: too talk alice,bob")
        console.print("Open a listed DM or GC: too talk <id> · ● online  ○ offline")
    except (TeamingError, ValueError, OSError) as exc:
        raise ClickException(str(exc)) from exc

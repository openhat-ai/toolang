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
from toolang.teaming.schemas import ConversationSummary, TeamMember, stream_id, target
from .rendering import display_text


_NAME_MAX_WIDTH = 24
_PREVIEW_MIN_WIDTH = 12
_CELL_PADDING = 1


def _message_time(sid: str, now: datetime) -> str:
    timestamp = datetime.fromtimestamp(stream_id(sid)[0] / 1000, tz=now.tzinfo)
    if timestamp.date() == now.date():
        return timestamp.strftime("%H:%M")
    pattern = "%m-%d %H:%M" if timestamp.year == now.year else "%Y-%m-%d %H:%M"
    return timestamp.strftime(pattern)


def _print_directory(
    console: Console,
    conversations: list[ConversationSummary],
    team: list[TeamMember],
    *,
    now: datetime,
) -> None:
    """Render a snapshot using the caller's console and local clock."""
    console.print("DM: too talk alice · Observe agents: too talk alice,bob")
    console.print("Open a listed DM or GC: too talk <id>")
    console.print()
    agents = {}
    for row in team:
        member = target(row["member"])
        if member.kind == "agent":
            agents[member.name] = row["online"]
    directory = Table(
        title="Team", title_justify="left", box=None, padding=(0, _CELL_PADDING)
    )
    presence_width = len("Presence")
    directory.add_column(
        "Agent",
        max_width=max(1, console.width - presence_width - 2 * 2 * _CELL_PADDING),
        no_wrap=True,
        overflow="ellipsis",
    )
    directory.add_column("Presence", width=presence_width, no_wrap=True)
    for name, online in sorted(agents.items()):
        presence = "—" if online is None else "online" if online else "offline"
        directory.add_row(Text(display_text(name)), presence)
    console.print(directory)
    if not agents:
        console.print("No agents.")
    console.print()

    times = {
        row["latest"]: _message_time(row["latest"], now)
        for row in conversations
        if row["latest"]
    }
    time_width = max(map(len, times.values()), default=0)
    rows = []
    for row in sorted(
        conversations,
        key=lambda row: (
            *(-part for part in stream_id(row["latest"] or "0-0")),
            row["conversation"],
        ),
    ):
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
        rows.append(
            (
                Text(row["conversation"]),
                Text(" ".join(display_text(row["name"] or "—").split())),
                participants,
                latest,
            )
        )

    headers = ("ID", "Name", "Members", "Latest message")
    content_widths = [
        max(len(header), max((row[index].cell_len for row in rows), default=0))
        for index, header in enumerate(headers)
    ]
    id_width, name_width, members_width, latest_width = content_widths
    name_width = min(_NAME_MAX_WIDTH, name_width)
    # Protect timestamps and previews, then share the remaining space between
    # names and members. Short cells release their unused space to the other column.
    padding_width = 2 * len(headers) * _CELL_PADDING
    preview_width = min(
        latest_width, max(len("Latest message"), time_width + _PREVIEW_MIN_WIDTH)
    )
    metadata_width = max(
        len("Name") + len("Members"),
        console.width - id_width - padding_width - preview_width,
    )
    name_limit = min(name_width, max(len("Name"), metadata_width // 2))
    members_limit = min(members_width, max(len("Members"), metadata_width - name_limit))
    name_limit = min(name_width, metadata_width - members_limit)
    table = Table(
        title="Conversations",
        title_justify="left",
        box=None,
        padding=(0, _CELL_PADDING),
        expand=True,
    )
    table.add_column("ID", min_width=id_width, no_wrap=True)
    table.add_column("Name", max_width=name_limit, no_wrap=True, overflow="ellipsis")
    table.add_column(
        "Members", max_width=members_limit, no_wrap=True, overflow="ellipsis"
    )
    table.add_column("Latest message", ratio=1, no_wrap=True, overflow="ellipsis")
    for row in rows:
        table.add_row(*row)
    console.print(table)
    if not conversations:
        console.print("No conversations.")


def directory_command(ctx: typer.Context) -> None:
    try:
        config, _human = settings(context_root(ctx))

        async def listing():
            async with HubClient(config) as client:
                return await client.contacts(include_preview=True), await client.team()

        conversations, team = asyncio.run(listing())
        _print_directory(
            Console(markup=False), conversations, team, now=datetime.now().astimezone()
        )
    except (TeamingError, ValueError, OSError) as exc:
        raise ClickException(str(exc)) from exc

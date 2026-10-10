"""List existing conversations and their latest messages."""

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
from toolang.teaming.schemas import ConversationSummary, stream_id, target
from .rendering import display_text


_NAME_MAX_WIDTH = 24
_PREVIEW_MIN_WIDTH = 9


def _message_time(sid: str, now: datetime) -> str:
    timestamp = datetime.fromtimestamp(stream_id(sid)[0] / 1000, tz=now.tzinfo)
    if timestamp.date() == now.date():
        return timestamp.strftime("%H:%M")
    pattern = "%m-%d %H:%M" if timestamp.year == now.year else "%Y-%m-%d %H:%M"
    return timestamp.strftime(pattern)


def _print_directory(
    console: Console,
    conversations: list[ConversationSummary],
    *,
    now: datetime,
) -> None:
    """Render a snapshot using the caller's console and local clock."""
    if not conversations:
        console.print("0 conversations")
        return

    times = {
        row["latest"]: _message_time(row["latest"], now)
        for row in conversations
        if row["latest"]
    }
    rows = []
    member_suffixes = []
    for row in sorted(
        conversations,
        key=lambda row: (
            *(-part for part in stream_id(row["latest"] or "0-0")),
            row["conversation"],
        ),
    ):
        members = sorted(row["participants"])
        participants = Text(
            ",".join(display_text(target(member).name) for member in members[:2]) or "-"
        )
        suffix = f",… ({len(members)})" if len(members) >= 3 else ""
        member_suffixes.append(suffix)
        participants.append(suffix)
        updated = Text("-", style="dim")
        message = Text("-", style="dim")
        if row["latest"]:
            updated = Text(times[row["latest"]], style="dim")
            if preview := row.get("preview"):
                sender = display_text(target(preview["sender"]).name)
                body = " ".join(display_text(preview["body"]).split())
                message = Text(f"{sender}: {body}")
            else:
                message = Text("Message unavailable", style="dim")
        rows.append(
            (
                Text(row["conversation"]),
                Text(" ".join(display_text(row["name"] or "-").split())),
                participants,
                updated,
                message,
            )
        )

    headers = ("CONVERSATION", "NAME", "MEMBERS", "UPDATED", "MESSAGE")
    content_widths = [
        max(len(header), max((row[index].cell_len for row in rows), default=0))
        for index, header in enumerate(headers)
    ]
    id_width, name_width, members_width, time_width, message_width = content_widths
    name_width = min(_NAME_MAX_WIDTH, name_width)
    # Protect timestamps and previews, then share the remaining space between
    # names and members. Short cells release their unused space to the other column.
    padding_width = len(headers) - 1
    preview_width = min(message_width, _PREVIEW_MIN_WIDTH)
    metadata_width = max(
        len("NAME") + len("MEMBERS"),
        console.width - id_width - time_width - padding_width - preview_width,
    )
    members_limit = min(
        members_width, max(len("MEMBERS"), metadata_width - len("NAME"))
    )
    name_limit = min(name_width, metadata_width - members_limit)
    message_limit = max(
        len("MESSAGE"),
        console.width
        - id_width
        - name_limit
        - members_limit
        - time_width
        - padding_width,
    )
    table = Table(
        box=None,
        header_style="",
        show_lines=False,
        pad_edge=False,
        collapse_padding=True,
    )
    table.add_column("CONVERSATION", width=id_width, no_wrap=True)
    table.add_column("NAME", max_width=name_limit, no_wrap=True, overflow="ellipsis")
    table.add_column("MEMBERS", width=members_limit, no_wrap=True, overflow="ellipsis")
    table.add_column("UPDATED", width=time_width, justify="right", no_wrap=True)
    table.add_column(
        "MESSAGE", max_width=message_limit, no_wrap=True, overflow="ellipsis"
    )
    for row, suffix in zip(rows, member_suffixes):
        if suffix and row[2].cell_len > members_limit:
            names = Text(row[2].plain[: -len(suffix)])
            names.truncate(max(1, members_limit - len(suffix)), overflow="crop")
            row[2].plain = names.plain.rstrip(", ") + suffix
        table.add_row(*row)
    console.print(table)
    console.print()
    count = len(conversations)
    console.print(f"{count} {'conversation' if count == 1 else 'conversations'}")


def directory_command(ctx: typer.Context) -> None:
    try:
        config, _human = settings(context_root(ctx))

        async def listing():
            async with HubClient(config) as client:
                return await client.contacts(include_preview=True)

        conversations = asyncio.run(listing())
        _print_directory(
            Console(markup=False), conversations, now=datetime.now().astimezone()
        )
    except (TeamingError, ValueError, OSError) as exc:
        raise ClickException(str(exc)) from exc

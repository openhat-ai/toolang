"""Send plain text or enter a single conversation."""

import asyncio
import os
import sys
from typing import Annotated

import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.context import context_root
from toolang.cli.common.execution_progress.config import resolve_progress_max_width
from toolang.cli.common.input import resolve_inputbox_max_width
from toolang.cli.common.messaging import settings, talk_context
from toolang.cli.common.terminal_session import prepare_terminal
from toolang.teaming.client import HubClient
from toolang.teaming.errors import TeamingError, MessagingError
from toolang.common.version import toolang_version
from toolang.teaming.schemas import (
    PendingDM,
    Resolution,
    conversation_id,
    identifier,
)


async def _hub_version(client: HubClient) -> str:
    try:
        async with asyncio.timeout(2):
            return (await client.info()).version
    except (MessagingError, TimeoutError):
        return "unknown"


def talk_command(
    ctx: typer.Context,
    target: Annotated[
        str | None,
        typer.Argument(
            metavar="CONVO",
            help="Conversation ID or shorthand",
        ),
    ] = None,
    body: Annotated[
        list[str] | None,
        typer.Argument(
            metavar="message",
            help="Message to send; omit to open the conversation",
        ),
    ] = None,
) -> None:
    if target is None:
        typer.echo(ctx.get_help())
        return
    root = context_root(ctx)
    words = list(body or [])
    if words[:1] == ["--"]:
        words.pop(0)
    try:
        config, human = settings(root)

        if "," not in target and target.startswith(("dm_", "gc_")):
            conversation_id(target)
        else:
            for name in target.split(","):
                identifier(name)

        async def resolve_or_send() -> Resolution:
            async with HubClient(config) as client:
                resolved = (
                    await client.resolve(target, kind="system")
                    if target == "all"
                    else await client.resolve(target)
                )
                if words:
                    receipt = await client.send(
                        resolved.conversation,
                        body=" ".join(words),
                        participants=list(resolved.participants)
                        if resolved.conversation.startswith("dm_")
                        else None,
                    )
                    typer.echo(
                        f"Sent {receipt['message']['id']} to {resolved.conversation}"
                    )
                return resolved

        selection = asyncio.run(resolve_or_send())
        resolved = selection.conversation
        if words:
            return
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ClickException(
                "Interactive Talk requires a TTY; provide a message to send and exit"
            )
        resolved = conversation_id(resolved)
        from .tui import TalkTui
        from toolang.cli.common.terminal_surfaces import resolve_terminal_surfaces

        directory = root / "human" / "talk-input"
        history_path = directory / f"{resolved}.jsonl"
        max_width = resolve_progress_max_width(os.environ)
        inputbox_max_width = resolve_inputbox_max_width(os.environ, fallback=max_width)
        surfaces = resolve_terminal_surfaces()

        async def interactive() -> None:
            async with HubClient(config) as client:
                info = (
                    await client.conversation(resolved)
                    if selection.exists
                    else PendingDM(resolved, selection.participants)
                )
                await TalkTui(
                    client,
                    info,
                    human,
                    history_path,
                    surfaces,
                    read_only=not info.allows_sender(human),
                    client_version=toolang_version(),
                    hub_version=await _hub_version(client),
                    max_width=max_width,
                    inputbox_max_width=inputbox_max_width,
                    selection=None if selection.exists else target,
                    terminal=prepare_terminal(
                        "talk",
                        root=root,
                        convo=resolved,
                        context=talk_context(
                            root, config.identity, human, config.endpoint
                        ),
                    ),
                ).run()

        asyncio.run(interactive())
    except (
        TeamingError,
        ValueError,
        OSError,
    ) as exc:
        raise ClickException(str(exc)) from exc

"""Send plain text or enter a single conversation."""

import asyncio
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import sys
from typing import Annotated

import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.context import context_root
from toolang.cli.common.errors import TmuxPlacementError
from toolang.cli.common.execution_progress.config import resolve_progress_max_width
from toolang.cli.common.input import resolve_inputbox_max_width
from toolang.cli.common.messaging import settings
from toolang.cli.common.tmux import resolve_launcher
from toolang.teaming.client import HubClient
from toolang.teaming.errors import TeamingError
from toolang.teaming.schemas import (
    PendingDM,
    Resolution,
    conversation_id,
    identifier,
)


def talk_identity(root: Path, connection: str, human: str) -> str:
    return sha256(f"{root.resolve()}\0{connection}\0{human}".encode()).hexdigest()[:20]


def talk_command(
    ctx: typer.Context,
    target: Annotated[
        str | None,
        typer.Argument(
            help="Agent name, two comma-separated agent names, or dm_/gc_ ID"
        ),
    ] = None,
    body: Annotated[
        list[str] | None,
        typer.Argument(help="Literal message; omit to open interactive input"),
    ] = None,
) -> None:
    if target is None:
        from .directory import directory_command

        directory_command(ctx)
        return
    root = context_root(ctx)
    words = list(body or [])
    if words[:1] == ["--"]:
        words.pop(0)
    try:
        config, human = settings(root)

        try:
            conversation_id(target)
        except TeamingError:
            for name in target.split(","):
                identifier(name)

        async def resolve_or_send() -> Resolution:
            async with HubClient(config) as client:
                resolved = await client.resolve(target)
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
        identity = talk_identity(root, config.identity, human)
        # A running window pins its Hub endpoint; drafts belong to the dataset.
        window_context = sha256(f"{identity}\0{config.endpoint}".encode()).hexdigest()[
            :20
        ]
        launcher = resolve_launcher(agent=window_context)
        if launcher is not None:
            launcher = replace(
                launcher,
                session_mark="@toolang_talk",
                window_mark="@toolang_convo",
                pad_kind="talk",
                shared_session="talk",
            )
            argv = [
                sys.executable,
                "-m",
                "toolang.cli.toolang.main",
                "--root",
                str(root),
                "talk",
                resolved if selection.exists else target,
            ]
            if not launcher.place_chat(
                thread_id=resolved, argv=argv, directory=str(Path.cwd())
            ):
                return
        from .tui import TalkTui
        from toolang.cli.common.terminal_surfaces import resolve_terminal_surfaces

        # Persist drafts and input history per dataset, viewer, and conversation.
        state = (
            root
            / ".runtime"
            / "talk-v2"
            / identity
            / sha256(resolved.encode()).hexdigest()[:20]
        )
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
                    state,
                    surfaces,
                    read_only=not info.allows_sender(human),
                    max_width=max_width,
                    inputbox_max_width=inputbox_max_width,
                    selection=None if selection.exists else target,
                ).run()

        asyncio.run(interactive())
    except (
        TeamingError,
        TmuxPlacementError,
        ValueError,
        OSError,
    ) as exc:
        raise ClickException(str(exc)) from exc

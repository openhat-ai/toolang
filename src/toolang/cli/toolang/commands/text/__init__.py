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
from valkey.exceptions import ValkeyError

from toolang.cli.common.context import context_root
from toolang.cli.common.errors import TmuxPlacementError
from toolang.cli.common.execution_progress.config import resolve_progress_max_width
from toolang.cli.common.messaging import settings
from toolang.cli.common.tmux import resolve_launcher
from toolang.messaging.client import MessagingClient
from toolang.messaging.errors import MessagingError
from toolang.messaging.schemas import conversation


def text_identity(root: Path, connection: str, human: str) -> str:
    return sha256(f"{root.resolve()}\0{connection}\0{human}".encode()).hexdigest()[:20]


def text_command(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Argument(help="Agent name, group name, or canonical conversation ID")
    ],
    body: Annotated[
        list[str] | None,
        typer.Argument(help="Literal message; omit to open interactive input"),
    ] = None,
    dm: Annotated[
        bool, typer.Option("--dm", help="Resolve target as an agent name")
    ] = False,
    group: Annotated[
        bool, typer.Option("--group", help="Resolve target as a custom group name")
    ] = False,
) -> None:
    root = context_root(ctx)
    if dm and group:
        raise ClickException("Use only one of --dm or --group")
    words = list(body or [])
    if words[:1] == ["--"]:
        words.pop(0)
    try:
        config, human = settings(root)

        async def resolve_or_send() -> str:
            async with MessagingClient(config) as client:
                resolved = await client.resolve(
                    target, kind="dm" if dm else "group" if group else None
                )
                if words:
                    receipt = await client.send(
                        resolved, sender=human, body=" ".join(words)
                    )
                    typer.echo(f"Sent {receipt['message']['id']} to {resolved}")
                return resolved

        resolved = asyncio.run(resolve_or_send())
        if words:
            return
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ClickException(
                "Interactive text requires a TTY; provide a message to send and exit"
            )
        identity = text_identity(root, config.identity, human)
        launcher = resolve_launcher(agent=identity)
        if launcher is not None:
            launcher = replace(
                launcher,
                session_mark="@toolang_text",
                window_mark="@toolang_group",
                pad_kind="text",
                session_name=f"text-{human}",
            )
            argv = [
                sys.executable,
                "-m",
                "toolang.cli.toolang.main",
                "--root",
                str(root),
                "text",
                resolved,
            ]
            if not launcher.place_chat(
                thread_id=resolved, argv=argv, directory=str(Path.cwd())
            ):
                return
        from .tui import TextTui
        from toolang.cli.common.terminal_surfaces import resolve_terminal_surfaces

        state = (
            root
            / ".runtime"
            / "text"
            / identity
            / sha256(resolved.encode()).hexdigest()[:20]
        )
        max_width = resolve_progress_max_width(os.environ)
        surfaces = resolve_terminal_surfaces()

        async def interactive() -> None:
            async with MessagingClient(config) as client:
                agents = await client.agents()
                if human in agents:
                    raise MessagingError("Human name conflicts with a registered agent")
                await TextTui(
                    client,
                    resolved,
                    human,
                    state,
                    surfaces,
                    read_only=not conversation(resolved).allows_sender(human, agents),
                    max_width=max_width,
                ).run()

        asyncio.run(interactive())
    except (
        MessagingError,
        ValkeyError,
        TmuxPlacementError,
        ValueError,
        OSError,
    ) as exc:
        raise ClickException(str(exc)) from exc

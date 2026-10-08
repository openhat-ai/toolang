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
from toolang.cli.common.messaging import settings
from toolang.cli.common.tmux import resolve_launcher
from toolang.teaming.messaging import MessagingClient
from toolang.teaming.errors import TeamingError


def text_identity(root: Path, connection: str, human: str) -> str:
    return sha256(f"{root.resolve()}\0{connection}\0{human}".encode()).hexdigest()[:20]


def text_command(
    ctx: typer.Context,
    target: Annotated[
        str | None,
        typer.Argument(help="Agent name, group name, or canonical conversation ID"),
    ] = None,
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
    if target is None:
        if dm or group:
            raise ClickException("A target is required with --dm or --group")
        from .directory import directory_command

        directory_command(ctx)
        return
    root = context_root(ctx)
    if dm and group:
        raise ClickException("Use only one of --dm or --group")
    words = list(body or [])
    if words[:1] == ["--"]:
        words.pop(0)
    try:
        config, human = settings(root)

        async def resolve_or_send() -> str:
            async with MessagingClient(config, actor=human) as client:
                resolved = await client.resolve(
                    target, kind="dm" if dm else "group" if group else None
                )
                if words:
                    receipt = await client.send(resolved, body=" ".join(words))
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
            async with MessagingClient(config, actor=human) as client:
                info = await client.conversation(resolved)
                await TextTui(
                    client,
                    resolved,
                    human,
                    state,
                    surfaces,
                    read_only=not info.allows_sender(human),
                    label=info.label,
                    max_width=max_width,
                ).run()

        asyncio.run(interactive())
    except (
        TeamingError,
        TmuxPlacementError,
        ValueError,
        OSError,
    ) as exc:
        raise ClickException(str(exc)) from exc

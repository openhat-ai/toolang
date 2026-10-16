"""Human conversation membership and explicit rules file transfers."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.context import context_root
from toolang.cli.common.help import CliCommand, CliGroup
from toolang.cli.common.messaging import settings
from toolang.common.typer.options import OptionalValueCommand
from toolang.teaming.client import HubClient
from toolang.teaming.errors import TeamingError
from toolang.teaming.schemas import SetRulesRequest, conversation_id
from .talk.directory import directory_command


class _RulesCommand(OptionalValueCommand, CliCommand):
    optional_values = {"update": "-"}


def convo_app() -> typer.Typer:
    app = typer.Typer(
        cls=CliGroup,
        help="Manage conversations and shared MEMBERS.md",
        add_completion=False,
        no_args_is_help=False,
        invoke_without_command=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )

    @app.callback()
    def default(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is None:
            typer.echo(ctx.get_help())

    for name, command in (
        ("list", directory_command),
        ("create", create),
        ("join", join),
        ("leave", leave),
        ("rules", rules),
    ):
        app.command(name, cls=_RulesCommand if name == "rules" else CliCommand)(command)
    return app


def _membership(ctx: typer.Context, operation: str, conversation: str) -> None:
    try:
        conversation_id(conversation)
        config, _ = settings(context_root(ctx))

        async def change() -> None:
            async with HubClient(config) as client:
                if operation == "join":
                    await client.join_conversation(conversation)
                else:
                    await client.leave_conversation(conversation)

        asyncio.run(change())
        typer.echo(f"{'Joined' if operation == 'join' else 'Left'} {conversation}")
    except (TeamingError, ValueError, OSError) as exc:
        raise ClickException(str(exc)) from exc


def join(ctx: typer.Context, conversation: str) -> None:
    """Join an ordinary group conversation"""
    _membership(ctx, "join", conversation)


def leave(ctx: typer.Context, conversation: str) -> None:
    """Leave an ordinary group; preserve rules and history"""
    _membership(ctx, "leave", conversation)


def create(
    ctx: typer.Context, name: Annotated[str | None, typer.Argument()] = None
) -> None:
    """Create a group containing the configured human"""
    try:
        config, _ = settings(context_root(ctx))

        async def creating() -> str:
            async with HubClient(config) as client:
                return (await client.create_conversation(name)).id

        typer.echo(asyncio.run(creating()))
    except (TeamingError, ValueError, OSError) as exc:
        raise ClickException(str(exc)) from exc


def rules(
    ctx: typer.Context,
    conversation: str,
    output: Annotated[
        str | None,
        typer.Option(
            "--output", "-o", metavar="PATH", help="Download to PATH; - selects stdout"
        ),
    ] = None,
    update: Annotated[
        str | None,
        typer.Option(
            "--update",
            "-u",
            metavar="[PATH]",
            help="Upload PATH; omitted PATH or - reads stdin",
        ),
    ] = None,
    revision: Annotated[
        str | None,
        typer.Option("--revision", help="Expected rules revision from a prior read"),
    ] = None,
) -> None:
    """Read shared MEMBERS.md, or replace it with an explicit expected revision"""
    if output is not None and update is not None:
        raise typer.BadParameter("--output and --update are mutually exclusive")
    if (update is not None) != (revision is not None):
        raise typer.BadParameter(
            "--update requires --revision; --revision is only valid with --update"
        )
    try:
        conversation_id(conversation)
        config, _ = settings(context_root(ctx))
        request = None
        if update is not None:
            source = (
                typer.get_binary_stream("stdin")
                if update == "-"
                else Path(update).open("rb")
            )
            try:
                content = source.read(64 * 1024 + 1).decode("utf-8")
            finally:
                if update != "-":
                    source.close()
            assert revision is not None
            request = SetRulesRequest(content=content, revision=revision)

        async def transfer():
            async with HubClient(config) as client:
                return (
                    await client.get_rules(conversation)
                    if request is None
                    else await client.set_rules(
                        conversation, request.content, revision=request.revision
                    )
                )

        document = asyncio.run(transfer())
        if request is None:
            data = document.content.encode("utf-8")
            if output is None or output == "-":
                typer.get_binary_stream("stdout").write(data)
            else:
                Path(output).write_bytes(data)
            if not data:
                typer.echo("No rules set.", err=True)
        else:
            typer.echo("Rules updated.", err=True)
        typer.echo(f"Revision: {document.revision}", err=True)
    except (TeamingError, ValueError, OSError) as exc:
        raise ClickException(str(exc)) from exc

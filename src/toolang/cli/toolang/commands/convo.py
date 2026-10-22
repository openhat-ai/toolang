"""Human conversation membership and explicit rules file transfers."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from typer._click.exceptions import ClickException

from toolang.cli.common.context import context_root
from toolang.cli.common.help import CliCommand, CliGroup
from toolang.cli.common.messaging import settings
from toolang.common.typer.options import OptionalValue, OptionalValueCommand
from toolang.teaming.client import HubClient
from toolang.teaming.errors import TeamingError
from toolang.teaming.schemas import (
    MAX_RULES_BYTES,
    SetRulesRequest,
    conversation_id,
    rules_revision,
)
from .talk.directory import directory_command


class _RulesCommand(OptionalValueCommand, CliCommand):
    optional_values = {"update": OptionalValue(bare_value="-", show_bare=False)}


ConversationArgument = Annotated[
    str,
    typer.Argument(metavar="CONVO", help="Conversation ID starting with dm_ or gc_"),
]


def convo_app() -> typer.Typer:
    app = typer.Typer(
        cls=CliGroup,
        help="Manage conversations and rules",
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


def join(ctx: typer.Context, conversation: ConversationArgument) -> None:
    """Join a group conversation"""
    _membership(ctx, "join", conversation)


def leave(ctx: typer.Context, conversation: ConversationArgument) -> None:
    """Leave a group conversation"""
    _membership(ctx, "leave", conversation)


def create(
    ctx: typer.Context,
    name: Annotated[
        str | None,
        typer.Argument(metavar="NAME", help="Group name, 1–128 characters"),
    ] = None,
) -> None:
    """Create a group conversation"""
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
    conversation: ConversationArgument,
    output: Annotated[
        str | None,
        typer.Option(
            "--output",
            "-o",
            metavar="PATH",
            help="Retrieve rules to PATH; - selects stdout",
        ),
    ] = None,
    update: Annotated[
        str | None,
        typer.Option(
            "--update",
            "-u",
            metavar="[PATH]",
            help="Update rules from PATH; omitted PATH or - reads stdin",
        ),
    ] = None,
    revision: Annotated[
        str | None,
        typer.Option(
            "--revision",
            metavar="REVISION",
            help="Use the revision returned by rules; required with --update",
        ),
    ] = None,
) -> None:
    """Retrieve or update rules"""
    if output is not None and update is not None:
        raise typer.BadParameter("--output and --update are mutually exclusive")
    if (update is not None) != (revision is not None):
        raise typer.BadParameter(
            "--update requires --revision; --revision is only valid with --update"
        )
    if revision is not None:
        try:
            rules_revision(revision)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="--revision") from exc
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
                data = source.read(MAX_RULES_BYTES + 1)
            finally:
                if update != "-":
                    source.close()
            if len(data) > MAX_RULES_BYTES:
                raise ClickException("Conversation rules exceed 64 KiB")
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ClickException("Rules input must be UTF-8") from exc
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

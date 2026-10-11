"""Author and inspect the current chore definitions"""

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import quote

import typer
from typer._click.exceptions import ClickException

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.catalog import templates
from toolang.cli.common.editor import edit_markdown
from toolang.cli.common.context import context_layout, require_prefix_agent, user_call
from toolang.cli.common.client import runtime_post
from toolang.cli.common.execution import open_execution
from toolang.cli.common.output import echo_table
from toolang.cli.common.parameters import TextType
from toolang.cli.common.routing import PrefixAgentChoreGroup, RequiredPrefixAgentCommand
from toolang.state.source import read_home_program_source
from toolang.lang.ast import parse_chore_declarations
from toolang.work.authoring import allocate_authored_chore_name
from toolang.work.inspection import ChoreInspection, ChoreRun
from toolang.work.state import inline_chores

ChoreRef = Annotated[
    str,
    typer.Argument(
        click_type=TextType(), help="Chore name, ref, or ID", metavar="CHORE"
    ),
]
ChoreName = Annotated[
    str | None, typer.Argument(help="Filename without .md; generated when omitted")
]


def list_chores(ctx: typer.Context) -> None:
    """List the current definitions and their execution state"""
    require_prefix_agent(ctx)
    layout = context_layout(ctx)
    runs: Iterable[ChoreRun] = ()
    errors: dict[str, str] = {}
    with open_execution(ctx) as resources:
        if resources is not None:
            runs = tuple(
                cast(Iterable[ChoreRun], resources.store.list_runs(limit=None))
            )
            errors = {
                run.id: resources.store.resolve_error(run.error)
                for run in runs
                if run.error is not None
            }
    entries = user_call(
        ChoreInspection.load,
        layout=layout,
        runs=runs,
        inline=inline_chores(
            user_call(
                parse_chore_declarations,
                user_call(read_home_program_source, layout.root, layout.name)
                .load_program()
                .source_text,
            )
        ),
        read_only=True,
        error_messages=errors,
    ).list()
    if not entries:
        typer.echo("No chores found.")
        return
    echo_table(
        (
            "NAME",
            "TITLE",
            "STATUS",
            "LAST RUN",
            "NEXT RUN",
            "SCHEDULE",
            "ERROR",
            "LOCATION",
        ),
        [
            (
                item.name,
                item.title,
                item.status or "-",
                item.runtime.last_run.status if item.runtime.last_run else "-",
                item.runtime.next_run_at or "-",
                item.schedule or "once",
                item.runtime.error
                or (item.runtime.last_run.error if item.runtime.last_run else None)
                or "-",
                item.path,
            )
            for item in entries
        ],
    )


def new_chore(ctx: typer.Context, name: ChoreName = None) -> None:
    """Create a definition in chores/"""
    agent = require_prefix_agent(ctx)
    layout = context_layout(ctx)
    text = edit_markdown(
        templates.render_template("chore", "default", agent_name=agent)
    )
    if text is None:
        raise typer.Exit()
    definition = user_call(
        ChoreFile.parse, text, name=name or allocate_authored_chore_name(layout)
    )
    saved = user_call(AuthoredChores(layout.home).create, definition)
    typer.echo(f"chore {saved.name} created\t{_path(saved)}")


def clone_chore(ctx: typer.Context, chore: ChoreRef, name: ChoreName = None) -> None:
    """Copy a definition under a new name and execution identity"""
    layout = context_layout(ctx)
    catalog = AuthoredChores(layout.home)
    source = _require(catalog, chore)
    clone = source.with_meta(
        {key: value for key, value in source.meta.items() if key != "id"}
    )
    saved = user_call(
        catalog.create,
        replace(clone, name=name or allocate_authored_chore_name(layout), path=None),
    )
    typer.echo(f"chore {saved.name} cloned\t{_path(saved)}")


def edit_chore(ctx: typer.Context, chore: ChoreRef) -> None:
    """Edit a definition without changing its name or progress"""
    catalog = AuthoredChores(context_layout(ctx).home)
    existing = _require(catalog, chore)
    text = edit_markdown(existing.content)
    if text is None:
        raise typer.Exit()
    definition = user_call(ChoreFile.parse, text, name=existing.name)
    saved = user_call(catalog.update, definition)
    typer.echo(str(_path(saved)))


def delete_chore(ctx: typer.Context, chore: ChoreRef) -> None:
    """Remove a definition; keep completed work and execution history"""
    catalog = AuthoredChores(context_layout(ctx).home)
    removed = user_call(catalog.remove, chore)
    typer.echo(f"chore {removed.name} deleted")


def run_chore(ctx: typer.Context, chore: ChoreRef) -> None:
    """Request one manual occurrence, serialized with any active run"""
    runtime_post(
        ctx,
        f"/api/v1/chores/{quote(chore.removeprefix('chore/'), safe='')}/run",
        payload={},
    )
    typer.echo(f"chore {chore} manual run requested")


def cancel_chore(ctx: typer.Context, chore: ChoreRef) -> None:
    """Cancel the active or pending occurrence"""
    runtime_post(
        ctx,
        f"/api/v1/chores/{quote(chore.removeprefix('chore/'), safe='')}/cancel",
        payload={},
    )
    typer.echo(f"chore {chore} cancel requested")


def _require(catalog: AuthoredChores, reference: str) -> ChoreFile:
    value = user_call(catalog.get, reference)
    if value is None:
        raise ClickException(f"chore not found: {reference}")
    return value


def _path(chore: ChoreFile) -> Path:
    if chore.path is None:
        raise ValueError("authored chore path is required")
    return chore.path


def _create_app() -> typer.Typer:
    app = typer.Typer(
        cls=PrefixAgentChoreGroup,
        help="Manage agent chores",
        add_completion=False,
        no_args_is_help=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    for name, command in (
        ("list", list_chores),
        ("new", new_chore),
        ("clone", clone_chore),
        ("edit", edit_chore),
        ("delete", delete_chore),
        ("run", run_chore),
        ("cancel", cancel_chore),
    ):
        app.command(
            name,
            cls=RequiredPrefixAgentCommand,
            no_args_is_help=name not in {"list", "new"},
        )(command)
    return app


chore_app = _create_app()

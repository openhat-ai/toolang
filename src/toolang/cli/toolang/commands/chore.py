"""Author and inspect the current chore definitions"""

from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import quote

import typer

from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.catalog import templates
from toolang.cli.common.editor import edit_markdown
from toolang.cli.common.context import context_layout, require_prefix_agent, user_call
from toolang.cli.common.client import runtime_post
from toolang.cli.common.execution import open_execution
from toolang.cli.common.output import echo_table
from toolang.cli.common.parameters import TextType
from toolang.cli.common.routing import PrefixAgentChoreGroup, RequiredPrefixAgentCommand
from toolang.catalog.chore_authoring import (
    allocate_authored_chore_name,
    create_chore,
    editable_chore_path,
)
from toolang.driver.chore.inspection import ChoreInspection, ChoreRun

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
                (item.runtime.status or "-") if item.runtime.synced else "unsynced",
                item.runtime.last_run.status if item.runtime.last_run else "-",
                min(
                    (
                        value
                        for value in (
                            item.runtime.pending_at,
                            item.runtime.next_scheduled_at,
                        )
                        if value
                    ),
                    default="-",
                ),
                item.schedule or "once",
                item.source_error
                or item.runtime.scheduler_error
                or (item.runtime.last_run.error if item.runtime.last_run else None)
                or "-",
                f"{item.path}:{item.line}" if item.line is not None else item.path,
            )
            for item in entries
        ],
    )


def new_chore(ctx: typer.Context, name: ChoreName = None) -> None:
    """Create a definition in chores/"""
    require_prefix_agent(ctx)
    layout = context_layout(ctx)
    text = edit_markdown(templates.load_template("chore").raw_text)
    if text is None:
        raise typer.Exit()
    definition = user_call(
        ChoreFile.parse, text, name=name or allocate_authored_chore_name(layout)
    )
    saved = user_call(create_chore, layout, definition)
    typer.echo(f"chore {saved.name} created\t{_path(saved)}")


def edit_chore(ctx: typer.Context, chore: ChoreRef) -> None:
    """Edit a definition without changing its name or progress"""
    catalog = AuthoredChores(
        context_layout(ctx).home, lock_path=context_layout(ctx).home_edit_lock
    )
    path = user_call(editable_chore_path, context_layout(ctx), chore)
    text = edit_markdown(path.read_text(encoding="utf-8"))
    if text is None:
        raise typer.Exit()
    definition = user_call(ChoreFile.parse, text, name=path.stem)
    with catalog.write_lock():
        user_call(editable_chore_path, context_layout(ctx), chore)
        saved = user_call(catalog.update, definition)
    typer.echo(str(_path(saved)))


def delete_chore(ctx: typer.Context, chore: ChoreRef) -> None:
    """Remove a definition; keep completed work and execution history"""
    catalog = AuthoredChores(
        context_layout(ctx).home, lock_path=context_layout(ctx).home_edit_lock
    )
    with catalog.write_lock():
        user_call(editable_chore_path, context_layout(ctx), chore)
        path = user_call(catalog.remove_file, chore)
    typer.echo(f"chore {path.stem} deleted")


def run_chore(ctx: typer.Context, chore: ChoreRef) -> None:
    """Request one manual occurrence, serialized with any active run"""
    runtime_post(
        ctx,
        f"/api/v1/chores/{quote(chore.removeprefix('chore/'), safe='')}/run",
        payload={},
    )
    typer.echo(f"chore {chore} manual run requested")


def cancel_chore(ctx: typer.Context, chore: ChoreRef) -> None:
    """Cancel the active or pending occurrence; keep future scheduled work"""
    runtime_post(
        ctx,
        f"/api/v1/chores/{quote(chore.removeprefix('chore/'), safe='')}/cancel",
        payload={},
    )
    typer.echo(f"chore {chore} cancel requested")


def _path(chore: ChoreFile) -> Path:
    if chore.path is None:
        raise ValueError("authored chore path is required")
    return chore.path


def _create_app() -> typer.Typer:
    app = typer.Typer(
        cls=PrefixAgentChoreGroup,
        help="Manage one-off and scheduled work",
        add_completion=False,
        no_args_is_help=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    for name, command, short_help in (
        ("list", list_chores, "List chores and status"),
        ("new", new_chore, "Create a chore"),
        ("edit", edit_chore, "Edit a chore"),
        ("delete", delete_chore, "Delete a chore; keep history"),
        ("run", run_chore, "Run a chore once"),
        ("cancel", cancel_chore, "Cancel a chore occurrence"),
    ):
        app.command(
            name,
            short_help=short_help,
            cls=RequiredPrefixAgentCommand,
            no_args_is_help=name not in {"list", "new"},
        )(command)
    return app


chore_app = _create_app()

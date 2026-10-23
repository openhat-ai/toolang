"""Author and inspect the current chore definitions"""

from collections.abc import Iterable, Sequence
from pathlib import Path
from shutil import get_terminal_size
from typing import Annotated, cast
from urllib.parse import quote

import typer
from wcwidth import wcswidth

from toolang.api.version import AGENT_API_VERSION
from toolang.catalog.chore import AuthoredChores, ChoreFile
from toolang.catalog import templates
from toolang.cli.common.editor import edit_markdown
from toolang.cli.common.context import context_layout, require_prefix_agent, user_call
from toolang.cli.common.client import runtime_post
from toolang.cli.common.execution import open_execution
from toolang.cli.common.output import (
    MIN_TABLE_WIDTH,
    echo_collection_summary,
    echo_table,
    format_local_timestamp,
)
from toolang.cli.common.records import _clip_location
from toolang.cli.common.terminal_title import clip_title, label_text, safe_text
from toolang.cli.common.parameters import TextType
from toolang.cli.common.agent_argument import AgentArgument
from toolang.cli.common.help import CliCommand, CliGroup
from toolang.catalog.chore_authoring import (
    allocate_authored_chore_name,
    create_chore,
    editable_chore_path,
)
from toolang.driver.chore.inspection import ChoreInspection, ChoreRun
from toolang.driver.chore.schemas import ChoreInfo

ChoreRef = Annotated[
    str,
    typer.Argument(
        click_type=TextType(), help="Chore name, ref, or ID", metavar="CHORE"
    ),
]
ChoreName = Annotated[
    str | None,
    typer.Argument(
        metavar="NAME", help="Chore filename without .md; generated when omitted"
    ),
]


def list_chores(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("home", placements="resident")],
) -> None:
    """List chores and status"""
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
        echo_collection_summary(0, "chore")
        return
    _echo_chores(
        entries,
        width=max(MIN_TABLE_WIDTH, get_terminal_size(fallback=(120, 24)).columns),
    )
    echo_collection_summary(len(entries), "chore")


def _echo_chores(entries: Sequence[ChoreInfo], *, width: int) -> None:
    """Shrink prose before omitting title and last-run status in that order."""
    if not entries:
        return
    headers = (
        "CHORE",
        "TITLE",
        "STATUS",
        "LAST RUN",
        "NEXT RUN",
        "SCHEDULE",
        "ERROR",
        "LOCATION",
    )
    rows = [
        (
            item.runtime.thread_id or item.name,
            item.title,
            (item.runtime.status or "-") if item.runtime.synced else "unsynced",
            item.runtime.last_run.status if item.runtime.last_run else "-",
            format_local_timestamp(
                min(
                    (
                        value
                        for value in (
                            item.runtime.pending_at,
                            item.runtime.next_scheduled_at,
                        )
                        if value
                    ),
                    default=None,
                ),
            ),
            item.schedule or "once",
            item.source_error
            or item.runtime.scheduler_error
            or (item.runtime.last_run.error if item.runtime.last_run else None)
            or "-",
            f"{item.path}:{item.line}" if item.line is not None else item.path,
        )
        for item in entries
    ]
    rows = [
        tuple(
            (
                label_text(value)
                if headers[index] in {"TITLE", "ERROR"}
                else safe_text(
                    value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
                )
            )
            or "-"
            for index, value in enumerate(row)
        )
        for row in rows
    ]
    natural_widths = [
        max(len(header), *(wcswidth(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    minimums = [len(header) for header in headers]
    minimums[-1] = max(
        (
            minimums[-1],
            *(len(f":{entry.line}") + 1 for entry in entries if entry.line is not None),
        )
    )
    readable_minimums = [
        min(
            natural_widths[index],
            max(
                minimums[index],
                {"TITLE": 12, "ERROR": 16, "LOCATION": 12}.get(header, 0),
            ),
        )
        for index, header in enumerate(headers)
    ]
    indices = list(range(len(headers)))
    for omitted in (None, "TITLE", "LAST RUN"):
        if omitted is not None:
            indices.remove(headers.index(omitted))
        widths = natural_widths.copy()
        table_width = sum(widths[index] for index in indices) + 2 * (len(indices) - 1)
        for column in ("TITLE", "ERROR", "LOCATION"):
            index = headers.index(column)
            if index not in indices:
                continue
            reduction = min(
                max(0, table_width - width), widths[index] - readable_minimums[index]
            )
            widths[index] -= reduction
            table_width -= reduction
        if table_width <= width:
            break

    # Only squeeze diagnostics below readable widths after optional columns yield.
    for column in ("ERROR", "LOCATION"):
        index = headers.index(column)
        reduction = min(max(0, table_width - width), widths[index] - minimums[index])
        widths[index] -= reduction
        table_width -= reduction

    def clip(value: str, index: int, entry: ChoreInfo) -> str:
        if headers[index] != "LOCATION":
            return clip_title(value, width=widths[index])
        suffix = f":{entry.line}" if entry.line is not None else ""
        return (
            _clip_location(value.removesuffix(suffix), widths[index] - len(suffix))
            + suffix
        )

    echo_table(
        [headers[index] for index in indices],
        [
            tuple(clip(row[index], index, entry) for index in indices)
            for entry, row in zip(entries, rows)
        ],
        width=max(width, table_width),
    )


def new_chore(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("source", placements="resident")],
    name: ChoreName = None,
) -> None:
    """Create a chore"""
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


def edit_chore(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("source", placements="resident")],
    chore: ChoreRef,
) -> None:
    """Edit a chore"""
    catalog = AuthoredChores(
        context_layout(ctx).home,
        lock_path=context_layout(ctx).home_edit_lock,
        agent_name=context_layout(ctx).name,
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


def delete_chore(
    ctx: typer.Context,
    agent: Annotated[str, AgentArgument("source", placements="resident")],
    chore: ChoreRef,
) -> None:
    """Delete a chore; keep history"""
    catalog = AuthoredChores(
        context_layout(ctx).home,
        lock_path=context_layout(ctx).home_edit_lock,
        agent_name=context_layout(ctx).name,
    )
    with catalog.write_lock():
        user_call(editable_chore_path, context_layout(ctx), chore)
        path = user_call(catalog.remove_file, chore)
    typer.echo(f"chore {path.stem} deleted")


def run_chore(
    ctx: typer.Context,
    agent: Annotated[
        str,
        AgentArgument(
            "home", placements="resident", requires_live=True, required_driver="chore"
        ),
    ],
    chore: ChoreRef,
) -> None:
    """Queue one manual chore run"""
    runtime_post(
        ctx,
        f"/api/v{AGENT_API_VERSION}/chores/{quote(chore.removeprefix('chore/'), safe='')}/run",
        payload={},
    )
    typer.echo(f"chore {chore} manual run requested")


def cancel_chore(
    ctx: typer.Context,
    agent: Annotated[
        str,
        AgentArgument(
            "home", placements="resident", requires_live=True, required_driver="chore"
        ),
    ],
    chore: ChoreRef,
) -> None:
    """Cancel the current chore occurrence; keep future scheduled work"""
    runtime_post(
        ctx,
        f"/api/v{AGENT_API_VERSION}/chores/{quote(chore.removeprefix('chore/'), safe='')}/cancel",
        payload={},
    )
    typer.echo(f"chore {chore} cancel requested")


def _path(chore: ChoreFile) -> Path:
    if chore.path is None:
        raise ValueError("authored chore path is required")
    return chore.path


def _create_app() -> typer.Typer:
    app = typer.Typer(
        cls=CliGroup,
        help="Manage one-off and scheduled tasks",
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
        ("run", run_chore, "Queue one manual chore run"),
        (
            "cancel",
            cancel_chore,
            "Cancel the current chore occurrence; keep future scheduled work",
        ),
    ):
        app.command(
            name,
            short_help=short_help,
            cls=CliCommand,
            no_args_is_help=name not in {"list", "new"},
        )(command)
    return app


chore_app = _create_app()

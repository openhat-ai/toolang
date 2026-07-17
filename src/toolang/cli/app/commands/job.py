"""Task and chore commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

import click
import typer
from typer.core import TyperCommand

from .... import templates
from toolang.agent import local as agents
from ....execution.records import UpdateKind
from toolang.state.durable import scan_durable_state
from toolang.catalog.job import JobCatalog
import toolang.work.definitions as job_definitions
from toolang.work.state import AgentJobs
from toolang.work.store import open_job_store
from ...common.client import append_agent_update, runtime_post
from ...common.context import context_root, require_prefix_agent, user_call
from ...common.output import echo_table
from ...common.routing import PrefixAgentJobGroup, RequiredPrefixAgentCommand

JobKind = Literal["task", "chore"]


@dataclass(frozen=True, slots=True)
class _JobCommand:
    name: str
    help: Callable[[JobKind], str]
    factory: Callable[[JobKind, str], Callable[..., None]]
    cls: type[TyperCommand] | None = None
    no_args_is_help: bool = False


def _create_app(kind: JobKind, title: str) -> typer.Typer:
    commands = (
        _JobCommand(
            "list", lambda kind: f"List {kind}s.", _list, RequiredPrefixAgentCommand
        ),
        _JobCommand(
            "new", lambda kind: f"Create a {kind}.", _new, RequiredPrefixAgentCommand
        ),
        _JobCommand(
            "clone",
            lambda kind: f"Clone a {kind}.",
            _clone,
            RequiredPrefixAgentCommand,
            True,
        ),
        _JobCommand(
            "edit",
            lambda kind: f"Edit a {kind}.",
            _edit,
            RequiredPrefixAgentCommand,
            True,
        ),
        _JobCommand(
            "delete",
            lambda kind: f"Delete a {kind}.",
            _delete,
            RequiredPrefixAgentCommand,
            True,
        ),
        _JobCommand(
            "draft",
            lambda kind: f"Move a {kind} to drafts.",
            _draft,
            RequiredPrefixAgentCommand,
            True,
        ),
        _JobCommand(
            "ready",
            lambda kind: f"Move a {kind} to ready.",
            _ready,
            RequiredPrefixAgentCommand,
            True,
        ),
        _JobCommand(
            "archive",
            lambda kind: f"Move a {kind} to archive.",
            _archive,
            RequiredPrefixAgentCommand,
            True,
        ),
        _JobCommand(
            "cancel",
            lambda kind: f"Cancel a {kind}.",
            _cancel,
            RequiredPrefixAgentCommand,
            True,
        ),
        _JobCommand(
            "reopen",
            lambda kind: f"Reopen a {kind}.",
            _reopen,
            RequiredPrefixAgentCommand,
            True,
        ),
        _JobCommand(
            "run",
            lambda kind: (
                "Trigger a chore run now." if kind == "chore" else f"Run a {kind}."
            ),
            _run,
            RequiredPrefixAgentCommand,
            True,
        ),
    )
    app = typer.Typer(
        cls=PrefixAgentJobGroup,
        help=f"Manage agent {kind}s.",
        add_completion=False,
        no_args_is_help=True,
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
    )
    for command in commands:
        if (kind, command.name) in {("task", "run"), ("chore", "reopen")}:
            continue
        app.command(
            command.name,
            help=command.help(kind),
            cls=command.cls,
            no_args_is_help=command.no_args_is_help,
        )(command.factory(kind, title))
    return app


def _list(kind: JobKind, title: str) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        drafts: Annotated[
            bool, typer.Option("--drafts", help="List draft items.")
        ] = False,
        archived: Annotated[
            bool, typer.Option("--archived", help="List archived items.")
        ] = False,
        all_items: Annotated[
            bool, typer.Option("--all", help="List ready, draft, and archived items.")
        ] = False,
    ) -> None:
        agent = require_prefix_agent(ctx)
        root = context_root(ctx)
        catalog = JobCatalog(root, agent)
        lifecycles: tuple[job_definitions.JobLifecycle, ...] = (
            ("ready", "draft", "archived")
            if all_items
            else ("draft",)
            if drafts
            else ("archived",)
            if archived
            else ("ready",)
        )
        if kind == "task":
            entries = tuple(
                entry
                for lifecycle in lifecycles
                for entry in catalog.list(kind="task", lifecycle=lifecycle)
            )
            if not entries:
                typer.echo("No tasks found.")
                return
            echo_table(
                ("ID", title.upper(), "LIFECYCLE", "LOCATION"),
                [
                    (
                        entry.document.task_id(),
                        entry.document.display_title(
                            fallback_name=entry.document.task_id()
                        ),
                        entry.lifecycle,
                        _location(root, agent, entry.path),
                    )
                    for entry in entries
                ],
            )
            return
        entries = tuple(
            entry
            for lifecycle in lifecycles
            for entry in catalog.list(kind="chore", lifecycle=lifecycle)
        )
        if not entries:
            typer.echo("No chores found.")
            return
        echo_table(
            ("ID", title.upper(), "LIFECYCLE", "SCHEDULE", "LOCATION"),
            [
                (
                    entry.document.chore_id(),
                    entry.document.display_title(
                        fallback_name=entry.document.chore_id()
                    ),
                    entry.lifecycle,
                    entry.document.schedule,
                    _location(root, agent, entry.path),
                )
                for entry in entries
            ],
        )

    return command


def _new(kind: JobKind, _title: str) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        draft: Annotated[
            bool, typer.Option("--draft", help="Create the item in drafts.")
        ] = False,
    ) -> None:
        agent = require_prefix_agent(ctx)
        text = click.edit(
            templates.render_template(kind, "default", agent_name=agent),
            extension=".md",
            require_save=True,
        )
        if text is None:
            raise typer.Exit()
        root = context_root(ctx)
        path = user_call(
            JobCatalog(root, agent).create,
            kind,
            text,
            lifecycle="draft" if draft else "ready",
        )
        if not draft:
            _reconcile(root, agent, kind)
        _notify(root, agent, kind, path.stem, path)
        typer.echo(f"{kind} {path.stem} created\t{path}")

    return command


def _clone(kind: JobKind, title: str) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        root, agent = context_root(ctx), require_prefix_agent(ctx)
        path = user_call(
            JobCatalog(root, agent).clone,
            kind,
            id,
        )
        _reconcile(root, agent, kind)
        _notify(root, agent, kind, path.stem, path)
        typer.echo(f"{kind} {path.stem} cloned\t{path}")

    return command


def _edit(kind: JobKind, title: str) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        root, agent = context_root(ctx), require_prefix_agent(ctx)
        catalog = JobCatalog(root, agent)
        text = user_call(catalog.read, kind, id)
        updated = click.edit(text, extension=".md", require_save=True)
        if updated is None:
            raise typer.Exit()
        path = user_call(catalog.update, kind, id, updated)
        _reconcile(root, agent, kind)
        _notify(root, agent, kind, id, path)
        typer.echo(str(path))

    return command


def _move(kind: JobKind, title: str, lifecycle: str) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        root, agent = context_root(ctx), require_prefix_agent(ctx)
        catalog = JobCatalog(root, agent)
        operation = {
            "draft": catalog.draft,
            "ready": catalog.ready,
            "archived": catalog.archive,
        }[lifecycle]
        path = user_call(operation, kind, id)
        if path is None:
            raise click.ClickException(f"{kind} not found: {id}")
        _reconcile(root, agent, kind)
        _notify(root, agent, kind, id, path)
        verb = {"draft": "drafted", "ready": "ready", "archived": "archived"}[lifecycle]
        typer.echo(f"{kind} {id} {verb}\t{path}")

    return command


def _draft(kind: JobKind, title: str) -> Callable[..., None]:
    return _move(kind, title, "draft")


def _ready(kind: JobKind, title: str) -> Callable[..., None]:
    return _move(kind, title, "ready")


def _archive(kind: JobKind, title: str) -> Callable[..., None]:
    return _move(kind, title, "archived")


def _reopen(kind: JobKind, title: str) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        if kind != "task":
            raise click.ClickException("reopen is only supported for tasks")
        root, agent = context_root(ctx), require_prefix_agent(ctx)
        store = open_job_store(root, agent)
        try:
            record = user_call(
                store.reopen_task,
                jobs=_agent_jobs(root, agent),
                task_id=id,
            )
        finally:
            store.close()
        typer.echo(f"task {record.job_id} reopened\t{record.status}")

    return command


def _run(kind: JobKind, title: str) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        if kind != "chore":
            raise click.ClickException("run is only supported for chores")
        runtime_post(ctx, f"/api/v1/chores/{id}/run", payload={})
        typer.echo(f"chore {id} manual run requested")

    return command


def _cancel(kind: JobKind, title: str) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        root, agent = context_root(ctx), require_prefix_agent(ctx)
        store = open_job_store(root, agent)
        try:
            store.reconcile(jobs=_agent_jobs(root, agent), kind=kind)
            record = store.get(job_id=id, kind=kind)
            if record is None:
                raise click.ClickException(f"{kind} not found: {id}")
            if record.status == "running" and record.last_run_id is not None:
                runtime_post(
                    ctx, f"/api/v1/runs/{record.last_run_id}/cancel", payload={}
                )
                typer.echo(f"{kind} {id} cancel requested\t{record.last_run_id}")
                return
            if kind == "task" and record.status == "todo":
                updated = store.cancel_pending_task(task_id=id)
                typer.echo(f"task {id} canceled\t{updated.status}")
                return
            raise click.ClickException(
                f"{kind} cannot be canceled from status: {record.status}"
            )
        finally:
            store.close()

    return command


def _delete(kind: JobKind, title: str) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        root, agent = context_root(ctx), require_prefix_agent(ctx)
        catalog = JobCatalog(root, agent)
        active = catalog.get(kind, id, lifecycle=None)
        if active is not None and active.lifecycle != "archived":
            raise click.ClickException(
                f"{kind} is not archived: {id}; archive it before deleting"
            )
        entry = catalog.get(kind, id, lifecycle="archived")
        if entry is None:
            raise click.ClickException(f"archived {kind} not found: {id}")
        removed = user_call(catalog.remove, kind, id)
        if not removed:
            raise click.ClickException(f"archived {kind} not found: {id}")
        _reconcile(root, agent, kind)
        _notify(root, agent, kind, id, entry.path)
        typer.echo(f"{kind} {id} deleted")

    return command


def _notify(root: Path, agent: str, kind: JobKind, id: str, path: Path) -> None:
    append_agent_update(
        root,
        agent,
        cast(UpdateKind, f"{kind}_changed"),
        {"id": id, "path": str(path)},
    )


def _reconcile(root: Path, agent: str, kind: JobKind) -> None:
    store = open_job_store(root, agent)
    try:
        store.reconcile(jobs=_agent_jobs(root, agent), kind=kind)
    finally:
        store.close()


def _agent_jobs(root: Path, agent: str) -> AgentJobs:
    program = scan_durable_state(root, agent).load_program().parse()
    return AgentJobs.load(root, agent, program)


def _location(root: Path, agent: str, path: Path) -> str:
    try:
        return str(path.relative_to(agents.agent_home(root, agent)))
    except ValueError:
        return str(path)


chore_app = _create_app("chore", "Chore")
task_app = _create_app("task", "Task")

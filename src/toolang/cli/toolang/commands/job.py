"""Task and chore commands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Literal

import click
import typer
from typer.core import TyperCommand

from ....catalog import templates
from ....catalog.types import JobStage
from toolang.up.process import agent_home
from toolang.state.source import read_authored_source
from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.errors import CatalogError
from toolang.work.authoring import (
    allocate_authored_job_id,
    assign_missing_authored_job_ids,
)
from toolang.work.state import AgentJobs, job_display_title
from toolang.work.store import open_job_store
from ...common.client import runtime_post
from ...common.updates import append_agent_update
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
        catalog = _jobs(root, agent)
        stages: tuple[JobStage, ...] = (
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
                for stage in stages
                for entry in catalog.list(kind="task", stage=stage)
            )
            if not entries:
                typer.echo("No tasks found.")
                return
            echo_table(
                ("ID", title.upper(), "STAGE", "LOCATION"),
                [
                    (
                        entry.id,
                        job_display_title(entry, fallback=entry.id),
                        entry.stage,
                        _location(root, agent, _job_path(entry)),
                    )
                    for entry in entries
                ],
            )
            return
        entries = tuple(
            entry
            for stage in stages
            for entry in catalog.list(kind="chore", stage=stage)
        )
        if not entries:
            typer.echo("No chores found.")
            return
        echo_table(
            ("ID", title.upper(), "STAGE", "SCHEDULE", "LOCATION"),
            [
                (
                    entry.id,
                    job_display_title(entry, fallback=entry.id),
                    entry.stage,
                    entry.schedule,
                    _location(root, agent, _job_path(entry)),
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
        job = user_call(
            JobFile.parse,
            text,
            kind=kind,
            stage="draft" if draft else "ready",
            job_id=allocate_authored_job_id(root, agent),
        )
        saved = user_call(_jobs(root, agent).create, job.with_meta(job.meta))
        path = _job_path(saved)
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
        source = _jobs(root, agent).get(kind, id, stage=None)
        if source is None:
            raise click.ClickException(f"{kind} not found: {id}")
        clone_id = allocate_authored_job_id(root, agent)
        clone = source.with_meta({**source.meta, "id": clone_id, "name": clone_id})
        saved = user_call(
            _jobs(root, agent).create,
            replace(clone, path=None, stage="ready"),
        )
        path = _job_path(saved)
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
        catalog = _jobs(root, agent)
        existing = catalog.get(kind, id, stage=None)
        if existing is None:
            raise click.ClickException(f"{kind} not found: {id}")
        text = existing.content
        updated = click.edit(text, extension=".md", require_save=True)
        if updated is None:
            raise typer.Exit()
        document = user_call(
            JobFile.parse,
            updated,
            kind=kind,
            stage=existing.stage,
            job_id=id,
        )
        saved = user_call(catalog.update, document.with_meta(document.meta))
        path = _job_path(saved)
        _reconcile(root, agent, kind)
        _notify(root, agent, kind, id, path)
        typer.echo(str(path))

    return command


def _move(kind: JobKind, title: str, stage: JobStage) -> Callable[..., None]:
    def command(
        ctx: typer.Context,
        id: str = typer.Argument(..., help=f"{title} id", metavar="ID"),
    ) -> None:
        root, agent = context_root(ctx), require_prefix_agent(ctx)
        moved = user_call(_jobs(root, agent).move, kind, id, stage)
        path = _job_path(moved)
        _reconcile(root, agent, kind)
        _notify(root, agent, kind, id, path)
        verb = {"draft": "drafted", "ready": "ready", "archived": "archived"}[stage]
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
        catalog = _jobs(root, agent)
        active = catalog.get(kind, id, stage=None)
        if active is not None and active.stage != "archived":
            raise click.ClickException(
                f"{kind} is not archived: {id}; archive it before deleting"
            )
        entry = catalog.get(kind, id, stage="archived")
        if entry is None:
            raise click.ClickException(f"archived {kind} not found: {id}")
        removed = user_call(catalog.remove, kind, id)
        _reconcile(root, agent, kind)
        _notify(root, agent, kind, id, _job_path(removed))
        typer.echo(f"{kind} {id} deleted")

    return command


def _notify(root: Path, agent: str, kind: JobKind, id: str, path: Path) -> None:
    append_agent_update(
        root,
        agent,
        f"{kind}_changed",
        {"id": id, "path": str(path)},
    )


def _reconcile(root: Path, agent: str, kind: JobKind) -> None:
    store = open_job_store(root, agent)
    try:
        store.reconcile(jobs=_agent_jobs(root, agent), kind=kind)
    finally:
        store.close()


def _agent_jobs(root: Path, agent: str) -> AgentJobs:
    program = read_authored_source(root, agent).load_program().parse()
    return AgentJobs.load(root, agent, program)


def _jobs(root: Path, agent: str) -> AuthoredJobs:
    catalog = AuthoredJobs(agent_home(root, agent))
    try:
        assign_missing_authored_job_ids(root, agent, catalog=catalog)
    except (CatalogError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    return catalog


def _job_path(job: JobFile) -> Path:
    if job.path is None:
        raise ValueError("authored job path is required")
    return job.path


def _location(root: Path, agent: str, path: Path) -> str:
    try:
        return str(path.relative_to(agent_home(root, agent)))
    except ValueError:
        return str(path)


chore_app = _create_app("chore", "Chore")
task_app = _create_app("task", "Task")

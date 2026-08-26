"""Task and chore commands."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Literal, cast

import click
import typer
from typer.core import TyperCommand

from ....catalog import templates
from ....catalog.types import JobStage
from toolang.common.layout import AgentLayout
from toolang.catalog.job import AuthoredJobs, JobFile
from toolang.catalog.errors import CatalogError
from toolang.work.errors import JobStoreSchemaError
from toolang.work.inspection import JobInspection, JobRun
from toolang.work.schemas import JobInfo
from toolang.work.authoring import (
    allocate_authored_job_id,
    assign_missing_authored_job_ids,
)
from ...common.client import runtime_post
from ...common.context import (
    context_layout,
    context_root,
    require_prefix_agent,
    user_call,
)
from ...common.execution import open_execution
from ...common.output import echo_table
from ...common.routing import PrefixAgentJobGroup, RequiredPrefixAgentCommand
from toolang.common.version import toolang_version

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
        require_prefix_agent(ctx)
        layout = context_layout(ctx)
        stages: tuple[JobStage, ...] = (
            ("ready", "draft", "archived")
            if all_items
            else ("draft",)
            if drafts
            else ("archived",)
            if archived
            else ("ready",)
        )
        runs: Iterable[JobRun] = ()
        error_messages: dict[str, str] = {}
        try:
            with open_execution(ctx) as resources:
                if resources is not None:
                    runs = tuple(
                        cast(
                            Iterable[JobRun],
                            resources.store.list_runs(limit=None),
                        )
                    )
                    error_messages = {
                        run.id: resources.store.resolve_error(run.error)
                        for run in runs
                        if run.error is not None
                    }
        except click.ClickException as exc:
            typer.echo(f"warning: {exc.message}", err=True)
        try:
            inspection = JobInspection.load(
                layout=layout,
                runs=runs,
                read_only=True,
                error_messages=error_messages,
            )
        except JobStoreSchemaError as exc:
            raise click.ClickException(
                _job_store_schema_error(exc, path=layout.job_store)
            ) from exc
        entries = tuple(
            entry
            for stage in stages
            for entry in inspection.list(kind=kind, stage=stage)
        )
        if kind == "task":
            if not entries:
                typer.echo("No tasks found.")
                return
            echo_table(
                (
                    "ID",
                    title.upper(),
                    "STAGE",
                    "STATUS",
                    "LAST RUN",
                    "ERROR",
                    "LOCATION",
                ),
                [
                    (
                        entry.id,
                        entry.title,
                        entry.stage,
                        entry.status or "-",
                        _last_run_status(entry),
                        _runtime_error(entry),
                        entry.path,
                    )
                    for entry in entries
                ],
            )
            return
        if not entries:
            typer.echo("No chores found.")
            return
        echo_table(
            (
                "ID",
                title.upper(),
                "STAGE",
                "STATUS",
                "LAST RUN",
                "NEXT RUN",
                "SCHEDULE",
                "ERROR",
                "LOCATION",
            ),
            [
                (
                    entry.id,
                    entry.title,
                    entry.stage,
                    entry.status or "-",
                    _last_run_status(entry),
                    entry.runtime.next_run_at or "-",
                    entry.schedule or "-",
                    _runtime_error(entry),
                    entry.path,
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
            job_id=allocate_authored_job_id(_layout(root, agent)),
        )
        saved = user_call(_jobs(root, agent).create, job.with_meta(job.meta))
        path = _job_path(saved)
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
        clone_id = allocate_authored_job_id(_layout(root, agent))
        clone = source.with_meta({**source.meta, "id": clone_id})
        saved = user_call(
            _jobs(root, agent).create,
            replace(clone, path=None, stage="ready"),
        )
        path = _job_path(saved)
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
        runtime_post(ctx, f"/api/v1/tasks/{id}/reopen", payload={})
        typer.echo(f"task {id} reopened")

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
        runtime_post(ctx, f"/api/v1/{kind}s/{id}/cancel", payload={})
        typer.echo(f"{kind} {id} cancel requested")

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
        user_call(catalog.remove, kind, id)
        typer.echo(f"{kind} {id} deleted")

    return command


def _jobs(root: Path, agent: str) -> AuthoredJobs:
    catalog = AuthoredJobs(_layout(root, agent).home)
    try:
        assign_missing_authored_job_ids(
            _layout(root, agent),
            catalog=catalog,
        )
    except (CatalogError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    return catalog


def _job_path(job: JobFile) -> Path:
    if job.path is None:
        raise ValueError("authored job path is required")
    return job.path


def _last_run_status(job: JobInfo) -> str:
    run = job.runtime.last_run
    if run is None:
        return "-"
    return run.status


def _runtime_error(job: JobInfo) -> str:
    run = job.runtime.last_run
    error = job.runtime.error or (run.error if run is not None else None) or ""
    compact = " ".join(error.split())
    return compact if len(compact) <= 56 else f"{compact[:53].rstrip()}..."


def _job_store_schema_error(error: JobStoreSchemaError, *, path: Path) -> str:
    if error.version > error.current:
        advice = "Upgrade this CLI before inspecting scheduler state."
    else:
        advice = (
            "Start this agent once with the current Toolang runtime to upgrade "
            "scheduler state, then retry."
        )
    return (
        f"scheduler state is incompatible with toolang {toolang_version()}: "
        f"{path} uses schema {error.version}, while this build requires schema "
        f"{error.current}. {advice} The database was not changed."
    )


def _layout(root: Path, agent: str) -> AgentLayout:
    return AgentLayout.resident(root, agent)


chore_app = _create_app("chore", "Chore")
task_app = _create_app("task", "Task")

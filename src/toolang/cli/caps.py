"""Cap subcommands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

import click
import typer

from .. import caps as cap_store
from .. import templates
from ..execution.records import UpdateKind
from ..loops import prepare as prepare_loop
from ..state.durable import scan_durable_state
from ..state.prepared import EntryKind, PreparedEntry, PreparedVisibility
from .utils import (
    _OptionalPrefixAgentCommand,
    _OptionalPrefixAgentTemplateCommand,
    _append_agent_update,
    _context_agent,
    _context_root,
    _echo_block,
    _echo_table,
    _wrap_user_error,
)

CapKind = Literal["skill", "psyche", "prompt", "service"]
CapVisibilityFilter = Literal["private", "shared"]


def register_cap_commands(app: typer.Typer, *, rich_help_panel: str | None = None) -> None:
    cap_titles: dict[CapKind, str] = {
        "psyche": "Psyche",
        "skill": "Skill",
        "service": "Service",
        "prompt": "Prompt",
    }
    cap_group_help: dict[CapKind, str] = {
        "psyche": "Manage psyches.",
        "skill": "Manage skills.",
        "service": "Manage services.",
        "prompt": "Manage prompts.",
    }
    cap_list_help: dict[CapKind, str] = {
        "psyche": "List available psyches.",
        "skill": "List available skills.",
        "service": "List available services.",
        "prompt": "List available prompts.",
    }

    @dataclass(frozen=True, slots=True)
    class CapCommandSpec:
        name: str
        help: Callable[[CapKind], str]
        factory: Callable[[CapKind, str], Callable[..., None]]
        no_args_is_help: bool = False

    command_specs: tuple[CapCommandSpec, ...] = (
        CapCommandSpec(
            name="list",
            help=lambda kind: cap_list_help[kind],
            factory=_make_cap_list_command,
        ),
        CapCommandSpec(
            name="new",
            help=lambda kind: f"Create a local {kind}.",
            factory=_make_new_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="edit",
            help=lambda kind: f"Edit a local {kind}.",
            factory=_make_edit_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="add",
            help=lambda kind: f"Add a remote {kind}.",
            factory=_make_add_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="remove",
            help=lambda kind: f"Remove a remote {kind}.",
            factory=_make_remove_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="delete",
            help=lambda kind: f"Delete a local {kind}.",
            factory=_make_delete_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="template",
            help=lambda kind: f"List or show {kind} templates.",
            factory=_make_template_command,
        ),
    )

    for kind in cap_titles:
        title = cap_titles[kind]
        cap_app = typer.Typer(
            help=cap_group_help[kind],
            add_completion=False,
            no_args_is_help=True,
            pretty_exceptions_enable=False,
            pretty_exceptions_show_locals=False,
        )
        for spec in command_specs:
            cap_app.command(
                spec.name,
                help=spec.help(kind),
                no_args_is_help=spec.no_args_is_help,
                cls=(
                    _OptionalPrefixAgentTemplateCommand
                    if spec.name == "template"
                    else _OptionalPrefixAgentCommand
                ),
            )(spec.factory(kind, title))
        app.add_typer(
            cap_app,
            name=kind,
            no_args_is_help=True,
            rich_help_panel=rich_help_panel,
        )


def _make_cap_list_command(kind: CapKind, title: str) -> Callable[..., None]:
    def list_caps(
        ctx: typer.Context,
        visibility: Annotated[
            CapVisibilityFilter | None,
            typer.Option("--visibility", help="Filter by visibility: private or shared."),
        ] = None,
    ) -> None:
        selected_agent = _context_agent(ctx)
        agent_name = selected_agent or "default"
        effective_visibility = _cap_list_visibility(ctx, visibility)
        entries = cap_store.list_entries(
            _context_root(ctx),
            agent_name,
            visibility=None if effective_visibility == "all" else effective_visibility,
            kinds={cast(EntryKind, kind)},
        )
        if not entries:
            typer.echo(f"No {kind}s found.")
            return
        rows = [
            (
                entry.name,
                cap_store.entry_visibility(entry, agent_name=agent_name),
                cap_store.entry_origin(entry),
                cap_store.entry_inclusion(entry),
                cap_store.entry_ref(entry, agent_name=agent_name),
            )
            for entry in entries
        ]
        rows.sort(key=lambda item: (0 if item[1] == "shared" else 1, item[0], item[2], item[3], item[4]))
        _echo_table((title.upper(), "VISIBILITY", "ORIGIN", "INCLUSION", "REF"), rows)

    return list_caps


def _make_new_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def new_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
        template: Annotated[
            str,
            typer.Option("--template", "-t", help="Template name."),
        ] = "default",
    ) -> None:
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        text = click.edit(
            templates.render_template(kind, template, name=name, agent_name=agent_name),
            extension=".md",
            require_save=True,
        )
        if text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            cap_store.put_local_entry_text,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
            text=text,
        )
        if selected_agent:
            _refresh_and_append_cap_update(
                _context_root(ctx),
                selected_agent,
                kind=kind,
                name=name,
                visibility=visibility,
            )
        typer.echo(str(path))

    return new_cap


def _make_edit_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def edit_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        text = _wrap_user_error(
            cap_store.load_local_entry_text,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
        )
        updated_text = click.edit(
            text,
            extension=".md",
            require_save=True,
        )
        if updated_text is None:
            raise typer.Exit()
        path = _wrap_user_error(
            cap_store.put_local_entry_text,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
            text=updated_text,
        )
        if selected_agent:
            _refresh_and_append_cap_update(
                _context_root(ctx),
                selected_agent,
                kind=kind,
                name=name,
                visibility=visibility,
            )
        typer.echo(str(path))

    return edit_cap


def _make_add_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def add_cap(
        ctx: typer.Context,
        ref: str = typer.Argument(..., help=f"{title} ref"),
    ) -> None:
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        path = _wrap_user_error(
            cap_store.add_remote_entry,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            ref=ref,
        )
        if selected_agent:
            _refresh_and_append_cap_update(
                _context_root(ctx),
                selected_agent,
                kind=kind,
                name=cap_store.remote_entry_name(cast(EntryKind, kind), ref),
                visibility=visibility,
            )
        typer.echo(str(path))

    return add_cap


def _make_remove_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def remove_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        entry = _named_entry(
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
            source_origin="remote",
            source_inclusion="configured",
        )
        removed = _wrap_user_error(
            cap_store.remove_remote_entry,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if not removed:
            raise click.ClickException(f"remote {kind} not found: {name}")
        if selected_agent:
            _refresh_and_append_cap_update(
                _context_root(ctx),
                selected_agent,
                kind=kind,
                name=name,
                visibility=visibility,
            )
        typer.echo(f"Removed remote {kind} {name} from {entry.ref}")

    return remove_cap


def _make_delete_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def delete_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        visibility, agent_name = _target_visibility(ctx)
        selected_agent = _context_agent(ctx)
        entry = _named_entry(
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
            source_origin="local",
        )
        deleted_path = _context_root(ctx) / entry.path
        if entry.shape == "dir":
            deleted_path = deleted_path.parent
        removed = _wrap_user_error(
            cap_store.remove_local_entry,
            _context_root(ctx),
            agent_name,
            visibility=visibility,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if not removed:
            raise click.ClickException(f"local {kind} not found: {name}")
        if selected_agent:
            _refresh_and_append_cap_update(
                _context_root(ctx),
                selected_agent,
                kind=kind,
                name=name,
                visibility=visibility,
            )
        typer.echo(f"Deleted local {kind} {name} from {deleted_path}")

    return delete_cap


def _make_template_command(kind: CapKind, title: str) -> Callable[..., None]:
    del title

    def template(
        template_name: Annotated[str | None, typer.Argument(help="Template name", hidden=True)] = None,
    ) -> None:
        if template_name is not None:
            _echo_block(templates.load_template(kind, template_name).raw_text.rstrip("\n"))
            return
        specs = templates.list_templates(kind)
        if not specs:
            typer.echo(f"No {kind} templates found.")
            return
        rows = [(item.name, item.description or "-") for item in specs]
        _echo_table(("TEMPLATE", "DESCRIPTION"), rows)

    return template


def _target_visibility(ctx: typer.Context) -> tuple[PreparedVisibility, str]:
    agent_name = _context_agent(ctx)
    if agent_name:
        return "private", agent_name
    return "shared", "default"


def _cap_list_visibility(
    ctx: typer.Context,
    visibility: CapVisibilityFilter | None,
) -> PreparedVisibility | Literal["all"]:
    selected_agent = _context_agent(ctx)
    if visibility is None:
        return "all" if selected_agent else "shared"
    if visibility == "private" and not selected_agent:
        raise click.ClickException("an agent prefix is required when --visibility is private")
    return visibility


def _named_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    visibility: PreparedVisibility,
    kind: EntryKind,
    name: str,
    source_origin: Literal["local", "remote"] | None = None,
    source_inclusion: cap_store.EntryInclusion | None = None,
) -> PreparedEntry:
    entries = cap_store.list_entries(
        toolang_root,
        agent_name,
        visibility=visibility,
        kinds={kind},
    )
    for entry in entries:
        if entry.name != name:
            continue
        if source_origin is not None and entry.source.origin != source_origin:
            continue
        if source_inclusion is not None and entry.source.inclusion != source_inclusion:
            continue
        return entry
    qualifier = f"{source_origin} " if source_origin is not None else ""
    raise click.ClickException(f"{qualifier}{kind} not found: {name}")


def _append_cap_update(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: CapKind,
    name: str,
    visibility: PreparedVisibility,
) -> None:
    update_kind = cast(UpdateKind, f"{kind}_changed")
    _append_agent_update(
        toolang_root,
        agent_name,
        update_kind,
        {
            "name": name,
            "visibility": visibility,
        },
    )


def _refresh_and_append_cap_update(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: CapKind,
    name: str,
    visibility: PreparedVisibility,
) -> None:
    durable = _wrap_user_error(scan_durable_state, toolang_root, agent_name)
    _wrap_user_error(prepare_loop.build_prepared_state, durable)
    _append_cap_update(
        toolang_root,
        agent_name,
        kind=kind,
        name=name,
        visibility=visibility,
    )

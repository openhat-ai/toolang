"""Cap subcommands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast
from urllib.parse import urlparse

import click
import typer

from .. import caps as cap_store
from .. import templates
from ..execution.records import UpdateKind
from ..state.prepared import EntryKind, PreparedEntry, PreparedScope
from .utils import (
    _OptionalPrefixAgentCommand,
    _OptionalPrefixAgentTemplateCommand,
    _append_agent_update,
    _context_agent,
    _context_root,
    _echo_table,
    _make_template_list_command,
    _make_template_show_command,
    _wrap_user_error,
)

CapKind = Literal["skill", "psyche", "prompt", "service"]
CapListFilter = Literal["global", "agent"]


def register_cap_commands(app: typer.Typer) -> None:
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
            help=lambda kind: f"Remove a {kind}.",
            factory=_make_remove_cap_command,
            no_args_is_help=True,
        ),
        CapCommandSpec(
            name="templates",
            help=lambda kind: f"List {kind} templates.",
            factory=lambda kind, title: _make_template_list_command(kind, title=title),
        ),
        CapCommandSpec(
            name="template",
            help=lambda kind: f"Show a {kind} template.",
            factory=lambda kind, title: _make_template_show_command(kind, title=title),
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
        app.add_typer(cap_app, name=kind, no_args_is_help=True)


def _make_cap_list_command(kind: CapKind, title: str) -> Callable[..., None]:
    def list_caps(
        ctx: typer.Context,
        filter_scope: Annotated[
            CapListFilter | None,
            typer.Option("--filter", help="Filter by scope: global or agent."),
        ] = None,
    ) -> None:
        selected_agent = _context_agent(ctx)
        agent_name = selected_agent or "default"
        effective_scope = _cap_list_scope(ctx, filter_scope)
        entries = cap_store.list_entries(
            _context_root(ctx),
            agent_name,
            scope=None if effective_scope == "all" else effective_scope,
            kinds={cast(EntryKind, kind)},
        )
        if not entries:
            typer.echo(f"No {kind}s found.")
            return
        rows = [
            (
                entry.name,
                _entry_ref(entry),
                _entry_scope(entry, agent_name=agent_name),
                _entry_location(_context_root(ctx), entry),
            )
            for entry in entries
        ]
        rows.sort(key=lambda item: (0 if item[2] == "global" else 1, item[0], item[1], item[3]))
        _echo_table((title.upper(), "REF", "SCOPE", "LOCATION"), rows)

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
        scope, agent_name = _target_scope(ctx)
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
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
            text=text,
        )
        if selected_agent:
            _append_cap_update(_context_root(ctx), selected_agent, kind=kind, name=name, scope=scope)
        typer.echo(str(path))

    return new_cap


def _make_edit_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def edit_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = _context_agent(ctx)
        text = _wrap_user_error(
            cap_store.load_local_entry_text,
            _context_root(ctx),
            agent_name,
            scope=scope,
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
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
            text=updated_text,
        )
        if selected_agent:
            _append_cap_update(_context_root(ctx), selected_agent, kind=kind, name=name, scope=scope)
        typer.echo(str(path))

    return edit_cap


def _make_add_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def add_cap(
        ctx: typer.Context,
        ref: str = typer.Argument(..., help=f"{title} ref"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = _context_agent(ctx)
        path = _wrap_user_error(
            cap_store.add_remote_entry,
            _context_root(ctx),
            agent_name,
            scope=scope,
            kind=cast(EntryKind, kind),
            ref=ref,
        )
        if selected_agent:
            _append_cap_update(
                _context_root(ctx),
                selected_agent,
                kind=kind,
                name=cap_store.remote_entry_name(cast(EntryKind, kind), ref),
                scope=scope,
            )
        typer.echo(str(path))

    return add_cap


def _make_remove_cap_command(kind: CapKind, title: str) -> Callable[..., None]:
    def remove_cap(
        ctx: typer.Context,
        name: str = typer.Argument(..., help=f"{title} name"),
    ) -> None:
        scope, agent_name = _target_scope(ctx)
        selected_agent = _context_agent(ctx)
        entry = _named_entry(
            _context_root(ctx),
            agent_name,
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if entry.source.form == "remote":
            removed = _wrap_user_error(
                cap_store.remove_remote_entry,
                _context_root(ctx),
                agent_name,
                scope=scope,
                kind=cast(EntryKind, kind),
                name=name,
            )
            if not removed:
                raise click.ClickException(f"{kind} not found: {name}")
            if selected_agent:
                _append_cap_update(_context_root(ctx), selected_agent, kind=kind, name=name, scope=scope)
            typer.echo(f"Removed {kind} {name} from {entry.ref}")
            return

        deleted_path = _context_root(ctx) / entry.path
        if entry.shape == "dir":
            deleted_path = deleted_path.parent
        removed = _wrap_user_error(
            cap_store.remove_local_entry,
            _context_root(ctx),
            agent_name,
            scope=scope,
            kind=cast(EntryKind, kind),
            name=name,
        )
        if not removed:
            raise click.ClickException(f"{kind} not found: {name}")
        if selected_agent:
            _append_cap_update(_context_root(ctx), selected_agent, kind=kind, name=name, scope=scope)
        typer.echo(f"Removed {kind} {name} from {deleted_path}")

    return remove_cap


def _target_scope(ctx: typer.Context) -> tuple[PreparedScope, str]:
    agent_name = _context_agent(ctx)
    if agent_name:
        return "agent", agent_name
    return "global", "default"


def _cap_list_scope(
    ctx: typer.Context,
    filter_scope: CapListFilter | None,
) -> PreparedScope | Literal["all"]:
    selected_agent = _context_agent(ctx)
    if filter_scope is None:
        return "all" if selected_agent else "global"
    if filter_scope == "agent" and not selected_agent:
        raise click.ClickException("an agent prefix is required when --filter is agent")
    return filter_scope


def _entry_scope(entry: PreparedEntry, *, agent_name: str) -> PreparedScope:
    prefix = f"agents/{agent_name}/"
    if entry.path.startswith(prefix) or entry.source.path.startswith(prefix):
        return "agent"
    return "global"


def _entry_ref(entry: PreparedEntry) -> str:
    if entry.source.form == "local":
        return entry.name
    return _remote_ref_shorthand(entry.kind, entry.ref)


def _remote_ref_shorthand(kind: EntryKind, ref: str) -> str:
    parsed = urlparse(ref)
    if parsed.scheme != "github":
        return ref
    path = parsed.path.strip("/")
    owner = parsed.netloc.strip()
    if not owner or not path:
        return ref
    parts = path.split("/")
    if kind == "skill" and len(parts) >= 3 and parts[-2] == "skills":
        return f"{owner}/{parts[-1]}"
    if kind == "service" and len(parts) >= 3 and parts[-2] == "services":
        return f"{owner}/{Path(parts[-1]).stem}"
    if kind == "prompt" and len(parts) >= 3 and parts[-2] == "prompts":
        return f"{owner}/{Path(parts[-1]).stem}"
    if kind == "psyche" and len(parts) >= 3 and parts[-2] == "psyches":
        return f"{owner}/{Path(parts[-1]).stem}"
    return ref


def _entry_location(toolang_root: Path, entry: PreparedEntry) -> str:
    if entry.source.form == "remote":
        return entry.ref
    location = toolang_root / entry.path
    if entry.shape == "dir":
        location = location.parent
    return str(location)


def _named_entry(
    toolang_root: Path,
    agent_name: str,
    *,
    scope: PreparedScope,
    kind: EntryKind,
    name: str,
    source_form: Literal["local", "remote"] | None = None,
) -> PreparedEntry:
    entries = cap_store.list_entries(
        toolang_root,
        agent_name,
        scope=scope,
        kinds={kind},
    )
    for entry in entries:
        if entry.name != name:
            continue
        if source_form is not None and entry.source.form != source_form:
            continue
        return entry
    qualifier = f"{source_form} " if source_form is not None else ""
    raise click.ClickException(f"{qualifier}{kind} not found: {name}")


def _append_cap_update(
    toolang_root: Path,
    agent_name: str,
    *,
    kind: CapKind,
    name: str,
    scope: PreparedScope,
) -> None:
    update_kind = cast(UpdateKind, f"{kind}_changed")
    _append_agent_update(
        toolang_root,
        agent_name,
        update_kind,
        {
            "name": name,
            "scope": scope,
        },
    )
